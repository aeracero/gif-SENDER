"""Persistent, guild-scoped GIF collections. All DB calls run on the event loop.

Validation may run in a worker; SQLite and file mutations are short synchronous
operations with no awaits, so one bot process cannot interleave a write.
"""
import hashlib
import io
import os
from pathlib import Path
import random
import re
import sqlite3
import tempfile
import unicodedata
import warnings

from PIL import Image

MAX_GIF_BYTES = 8 * 1024 * 1024
MAX_COLLECTION = 100


def keyword_key(value: str) -> str:
    value = ' '.join(unicodedata.normalize('NFKC', value).casefold().split())
    if not value or len(value) > 100:
        raise ValueError('キーワードは1〜100文字で指定してください。')
    return value


def validate_gif(data: bytes):
    if not data or len(data) > MAX_GIF_BYTES:
        raise ValueError('GIFは1件8MiB以下にしてください。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != 'GIF':
                    raise ValueError('GIF形式ではありません。')
                pixels = image.width * image.height
                if pixels > 4_000_000:
                    raise ValueError('GIFの解像度が大きすぎます。')
                frames = 0
                while True:
                    frames += 1
                    if frames > 300 or pixels * frames > 40_000_000:
                        raise ValueError('GIFのフレーム数・展開サイズが大きすぎます。')
                    image.load()
                    try:
                        image.seek(frames)
                    except EOFError:
                        break
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('壊れたGIF、または展開サイズが大きすぎるGIFです。') from exc


class GifStore:
    def __init__(self, root: Path, max_bytes: int = 1024 * 1024 * 1024):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = self.root / 'files'
        self.files.mkdir(exist_ok=True)
        self.max_bytes = max_bytes
        self.db = sqlite3.connect(self.root / 'index.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS collections (
                guild TEXT NOT NULL, keyword TEXT NOT NULL, local_enabled INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(guild, keyword));
            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY, guild TEXT NOT NULL, keyword TEXT NOT NULL,
                digest TEXT NOT NULL, size INTEGER NOT NULL, title TEXT NOT NULL,
                source TEXT NOT NULL, credit TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                UNIQUE(guild, keyword, digest),
                FOREIGN KEY(guild, keyword) REFERENCES collections(guild, keyword));
            CREATE TABLE IF NOT EXISTS history (
                guild TEXT NOT NULL, target TEXT NOT NULL, media_id INTEGER NOT NULL,
                uses INTEGER NOT NULL DEFAULT 0, sequence INTEGER NOT NULL,
                PRIMARY KEY(guild, target, media_id),
                FOREIGN KEY(media_id) REFERENCES media(id) ON DELETE CASCADE);
        ''')
        # Crashes before DB commit may leave an unindexed file. Remove only
        # our own content-addressed GIFs; never traverse arbitrary paths.
        referenced = {r[0] for r in self.db.execute('SELECT DISTINCT digest FROM media')}
        for path in self.files.glob('*.gif'):
            if re.fullmatch(r'[0-9a-f]{64}', path.stem) and path.stem not in referenced:
                path.unlink()

    def close(self):
        self.db.close()

    def ensure_collection(self, guild, keyword):
        key = keyword_key(keyword)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO collections (guild,keyword) VALUES (?, ?)', (str(guild), key))
        return key

    def has_collection(self, guild, keyword):
        try:
            key = keyword_key(keyword)
        except ValueError:
            return False
        return bool(self.db.execute('SELECT 1 FROM collections WHERE guild=? AND keyword=? AND local_enabled=1',
                                   (str(guild), key)).fetchone())

    def set_local(self, guild, keyword, enabled):
        key = self.ensure_collection(guild, keyword)
        with self.db:
            self.db.execute('UPDATE collections SET local_enabled=? WHERE guild=? AND keyword=?',
                            (int(enabled), str(guild), key))

    def path(self, item):
        return self.files / (item['digest'] + '.gif')

    def add(self, guild, keyword, data, title='', source='', credit=''):
        """Call validate_gif before add. Returns (id, newly_added)."""
        if not data or len(data) > MAX_GIF_BYTES:
            raise ValueError('GIFは1件8MiB以下にしてください。')
        if len(credit) > 1400 or len(source) > 350:
            raise ValueError('出典情報が長すぎます。')
        key = self.ensure_collection(guild, keyword)
        digest = hashlib.sha256(data).hexdigest()
        existing = self.db.execute('SELECT id FROM media WHERE guild=? AND keyword=? AND digest=?',
                                   (str(guild), key, digest)).fetchone()
        if existing:
            return existing['id'], False
        count = self.db.execute('SELECT COUNT(*) FROM media WHERE guild=? AND keyword=?',
                                (str(guild), key)).fetchone()[0]
        if count >= MAX_COLLECTION:
            raise ValueError('このキーワードは100件に達しています。不要なGIFを削除してください。')
        path = self.files / (digest + '.gif')
        if not path.exists():
            used = sum(p.stat().st_size for p in self.files.glob('*.gif'))
            if used + len(data) > self.max_bytes:
                raise ValueError('GIF保存容量の上限に達しました。不要なGIFを削除してください。')
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.files, delete=False) as f:
                    temporary = Path(f.name)
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        with self.db:
            cur = self.db.execute('''INSERT INTO media
                (guild, keyword, digest, size, title, source, credit) VALUES (?,?,?,?,?,?,?)''',
                (str(guild), key, digest, len(data), title[:300], source, credit))
        return cur.lastrowid, True

    def entries(self, guild, keyword):
        return self.db.execute('SELECT * FROM media WHERE guild=? AND keyword=? ORDER BY id',
                               (str(guild), keyword_key(keyword))).fetchall()

    def get(self, guild, media_id):
        return self.db.execute('SELECT * FROM media WHERE guild=? AND id=?',
                               (str(guild), media_id)).fetchone()

    def set_state(self, guild, media_id, state):
        if state not in ('pending', 'approved', 'excluded'):
            raise ValueError('Invalid media state')
        with self.db:
            return self.db.execute('UPDATE media SET state=? WHERE guild=? AND id=?',
                                   (state, str(guild), media_id)).rowcount > 0

    def approve_pending(self, guild, keyword):
        with self.db:
            return self.db.execute("UPDATE media SET state='approved' WHERE guild=? AND keyword=? AND state='pending'",
                                   (str(guild), keyword_key(keyword))).rowcount

    def remove(self, guild, media_id):
        item = self.get(guild, media_id)
        if not item:
            return False
        with self.db:
            self.db.execute('DELETE FROM media WHERE id=?', (media_id,))
        if not self.db.execute('SELECT 1 FROM media WHERE digest=?', (item['digest'],)).fetchone():
            self.path(item).unlink(missing_ok=True)
        return True

    def pick(self, guild, target, keyword, max_size=MAX_GIF_BYTES):
        key = keyword_key(keyword)
        items = self.db.execute('''SELECT m.*, COALESCE(h.uses, 0) AS uses,
            COALESCE(h.sequence, 0) AS sequence FROM media m LEFT JOIN history h
            ON h.media_id=m.id AND h.guild=m.guild AND h.target=?
            WHERE m.guild=? AND m.keyword=? AND m.state='approved' AND m.size<=?''',
            (str(target), str(guild), key, max_size)).fetchall()
        last = self.db.execute('''SELECT h.media_id FROM history h JOIN media m ON m.id=h.media_id
            WHERE h.guild=? AND h.target=? AND m.keyword=? ORDER BY h.sequence DESC LIMIT 1''',
            (str(guild), str(target), key)).fetchone()
        items = [r for r in items if (not last or r['id'] != last[0]) and self.path(r).is_file()]
        if not items:
            return None
        uses = min(r['uses'] for r in items)
        return random.choice([r for r in items if r['uses'] == uses])

    def mark_sent(self, guild, target, media_id):
        if not self.get(guild, media_id):
            return  # An administrator removed it while the Discord send awaited.
        with self.db:
            sequence = self.db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM history').fetchone()[0]
            self.db.execute('''INSERT INTO history VALUES (?,?,?,?,?)
                ON CONFLICT(guild,target,media_id) DO UPDATE SET uses=uses+1, sequence=excluded.sequence''',
                (str(guild), str(target), media_id, 1, sequence))
