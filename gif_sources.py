"""Bounded GIF import and a Commons collector; no GIPHY/KLIPY caching."""
import asyncio
from html.parser import HTMLParser
import io
import re
import time
from urllib.parse import urlsplit
import zipfile

import aiohttp

from gif_store import MAX_GIF_BYTES, validate_gif

COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
USER_AGENT = 'gif-SENDER/1.0 (https://github.com/aeracero/gif-SENDER)'
MAX_IMPORT_BYTES = 25 * 1024 * 1024


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value):
    parser = PlainText()
    parser.feed(value or '')
    return ' '.join(' '.join(parser.parts).split())


def archive_items(data: bytes, filename: str):
    """Yield bounded in-memory files; never extract archive paths to disk."""
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError('取り込みファイルは25MiB以下にしてください。ZIPを分割できます。')
    if filename.lower().endswith('.gif'):
        yield filename, data
        return
    if not filename.lower().endswith('.zip'):
        raise ValueError('GIFまたはGIFをまとめたZIPを添付してください。')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [f for f in archive.infolist() if not f.is_dir() and f.filename.lower().endswith('.gif')]
            if len(files) > 100 or len(archive.infolist()) > 1000:
                raise ValueError('1回のZIPにはGIFを100件まで入れてください。')
            for info in files:
                if info.file_size > MAX_GIF_BYTES or info.flag_bits & 1:
                    yield info.filename, None
                    continue
                with archive.open(info) as f:
                    content = f.read(MAX_GIF_BYTES + 1)
                yield info.filename, content
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ValueError('ZIPが壊れているか、対応していない形式です。') from exc


async def download_commons(session, url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'upload.wikimedia.org' or parsed.port not in (None, 443) or parsed.username:
        raise ValueError('Commonsの配信URLではありません。')
    async with session.get(url, allow_redirects=False, headers={'User-Agent': USER_AGENT}) as response:
        if response.status != 200:
            raise ValueError(f'GIF取得失敗 HTTP {response.status}')
        if response.content_length and response.content_length > MAX_GIF_BYTES:
            raise ValueError('GIFが8MiBを超えています。')
        data = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > MAX_GIF_BYTES:
                raise ValueError('GIFが8MiBを超えています。')
        return bytes(data)


def commons_credit(info):
    meta = info.get('extmetadata', {})
    def value(key):
        return plain(meta.get(key, {}).get('value', ''))
    license_name = value('LicenseShortName')
    license_url = value('LicenseUrl')
    artist = value('Artist')
    if not (re.fullmatch(r'CC BY(?:-SA)? [1-4]\.0', license_name) or license_name in ('CC0', 'Public domain')):
        return None
    if not artist or len(artist) > 900:
        return None
    if license_name.startswith('CC BY'):
        parsed = urlsplit(license_url)
        if parsed.scheme not in ('http', 'https') or parsed.hostname not in ('creativecommons.org', 'www.creativecommons.org'):
            return None
    return f'{artist} / {license_name}' + (f'\n{license_url}' if license_url else '')


async def collect_commons(session, store, guild, keyword, count=100):
    """Collect up to count NEW candidates (collection itself is capped at 100).

    Search is deliberately bounded to 300 results / ten minutes, with incremental
    commits. A timeout or interruption keeps already-downloaded candidates.
    """
    store.ensure_collection(guild, keyword)
    result = {'added': 0, 'duplicate': 0, 'skipped': 0, 'reason': ''}
    continuation = {}
    deadline = time.monotonic() + 600
    for _ in range(30):
        if time.monotonic() >= deadline:
            result['reason'] = '収集時間の上限に達しました。途中まで保存しています。'
            break
        params = dict(action='query', format='json', formatversion=2,
                      generator='search', gsrsearch=f'{keyword} filemime:image/gif',
                      gsrnamespace=6, gsrlimit=10, prop='imageinfo',
                      iiprop='url|mime|size|extmetadata', iiextmetadatalanguage='en')
        params.update(continuation)
        try:
            async with session.get(COMMONS_API, params=params, headers={'User-Agent': USER_AGENT}) as response:
                if response.status != 200:
                    result['reason'] = f'検索がHTTP {response.status}で停止しました。'
                    break
                payload = await response.json()
            if payload.get('error'):
                result['reason'] = '検索APIがエラーを返しました。'
                break
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            result['reason'] = '検索APIへの接続に失敗しました。'
            break
        pages = sorted(payload.get('query', {}).get('pages', []), key=lambda p: p.get('index', 0))
        for page in pages:
            if time.monotonic() >= deadline:
                result['reason'] = '収集時間の上限に達しました。途中まで保存しています。'
                return result
            info = (page.get('imageinfo') or [{}])[0]
            credit = commons_credit(info)
            if info.get('mime') != 'image/gif' or info.get('size', MAX_GIF_BYTES + 1) > MAX_GIF_BYTES or not credit:
                result['skipped'] += 1
                continue
            try:
                data = await download_commons(session, info.get('url', ''))
                await asyncio.to_thread(validate_gif, data)
            except (ValueError, aiohttp.ClientError, asyncio.TimeoutError):
                result['skipped'] += 1
                continue
            try:
                _, added = store.add(guild, keyword, data, page.get('title', 'GIF'),
                                     f'https://commons.wikimedia.org/?curid={int(page["pageid"])}', credit)
            except ValueError as exc:
                result['reason'] = str(exc)
                return result
            result['added' if added else 'duplicate'] += 1
            if result['added'] >= count:
                return result
        continuation = payload.get('continue')
        if not continuation or not pages:
            break
    return result
