"""
watch_cog.py
（bot.py と同じフォルダに置くこと。サブフォルダに分けるとRailway上で
 ModuleNotFoundErrorの原因になりやすいため、フラットな構成にしています）
指定したユーザーが発言するたびに、設定したキーワードに合う画像(GIF/静止画)を
自動送信するCogです（クールダウンなし・新しい候補がない場合はスキップ）。

画像ソース:
- GIF: KLIPY API (Tenor互換エンドポイント。要APIキー / 環境変数 GIF_API_KEY)
  ※ Google が2026年6月30日にTenor APIを完全終了したため、Tenor互換のKLIPYに切り替えています
- 静止画: Openverse API (APIキー不要・CCライセンス画像。ライセンス種別の絞り込みは
  候補を増やすためにあえて外しています)

対象ユーザーごとに「GIF用キーワード」と「画像用キーワード」を別々に設定できます。
両方設定した場合は発言のたびにランダムでどちらかを送信します
（片方が取得失敗したらもう片方にフォールバック）。

直前に送った画像/GIFのURLを対象ごとに記録し(設定ファイルに永続化)、
次回はそれを避けて選ぶことで連続で同じものが出るのを防いでいます。
検索は最大2ページ分取得して候補プールを増やしており、候補が少ないキーワードは
ログに警告が出るようにしています。

ログ:
    logging モジュールでINFO/WARNINGを出力します。Railwayのログ画面で
    「候補数=1」のような警告が出ていたら、そのキーワードのヒット数自体が
    少なく、同じ画像が繰り返されやすい状態だと分かります。

使い方 (スラッシュコマンド):
/watch add target:@ユーザー [gif_keyword:単語] [image_keyword:単語]
    ※ gif_keyword / image_keyword は少なくとも片方を指定してください
/watch off target:@ユーザー   … 設定を残したまま一時停止（即時反映）
/watch on target:@ユーザー    … 一時停止を解除（即時反映）
/watch remove target:@ユーザー … 設定そのものを削除
/watch list
/erase_all … このチャンネルでBotが送ったメッセージを全て削除
"""

import asyncio
import json
import logging
import os
import random
import tempfile
import weakref
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

GIF_API_KEY = os.environ.get("GIF_API_KEY", "")
# KLIPYが提供するTenor互換エンドポイント。パラメータもレスポンス形式もTenorと同一。
GIF_SEARCH_URL = "https://api.klipy.com/v2/search"
OPENVERSE_SEARCH_URL = "https://api.openverse.org/v1/images/"

# 1ページあたりの取得件数。候補プールを増やすほど「連続で同じ画像」が起きにくくなる。
SEARCH_POOL_SIZE = 50
# 追加で何ページ分取得するか（1ページ目 + これで合計ページ数）。ヒット数が
# 少ないニッチなキーワードだと、増やしてもあまり変わらないこともある。
MAX_EXTRA_PAGES = 1

# データ保存先。Railwayではプロジェクトのルートで実行される想定なので、
# カレントディレクトリ基準にしてbot.pyと同階層に置く。
# 永続化したい場合はRailwayのVolumeをこのパスにマウントすること。
DATA_FILE = Path(os.environ.get("WATCH_DATA_PATH", "watch_data.json"))


def _load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=DATA_FILE.parent, delete=False
        ) as f:
            temporary = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, DATA_FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class WatchCog(commands.Cog):
    """指定ユーザーの発言に反応して画像を送るCog"""

    watch_group = app_commands.Group(
        name="watch", description="指定した相手が発言したときに画像を送る機能",
        guild_only=True
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 構造:
        # { "guild_id": { "user_id": {
        #       "gif_keyword": str | None,
        #       "image_keyword": str | None,
        #       "enabled": bool,
        #       "last_media_url": str | None,  # 再起動をまたいでも重複防止できるよう永続化
        # } } }
        self.data: dict = _load_data()
        self.session: Optional[aiohttp.ClientSession] = None
        self._target_locks = weakref.WeakValueDictionary()

    async def cog_load(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ---------- スラッシュコマンド ----------

    @watch_group.command(
        name="add",
        description="このユーザーが発言したら画像/GIFを即座に送るように設定します",
    )
    @app_commands.describe(
        target="対象にするユーザー",
        gif_keyword="GIF検索に使うキーワード（省略可）",
        image_keyword="静止画検索に使うキーワード（省略可）",
    )
    async def watch_add(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        gif_keyword: Optional[str] = None,
        image_keyword: Optional[str] = None,
    ):
        gif_keyword = (gif_keyword or "").strip() or None
        image_keyword = (image_keyword or "").strip() or None
        if not gif_keyword and not image_keyword:
            await interaction.response.send_message(
                "⚠️ gif_keyword か image_keyword のどちらかは指定してください。",
                ephemeral=True,
            )
            return

        guild_id = str(interaction.guild_id)
        self.data.setdefault(guild_id, {})[str(target.id)] = {
            "gif_keyword": gif_keyword,
            "image_keyword": image_keyword,
            "enabled": True,
            "last_media_url": None,
        }
        _save_data(self.data)
        logger.info(
            f"/watch add: guild={guild_id} target={target} gif={gif_keyword!r} image={image_keyword!r}"
        )

        parts = []
        if gif_keyword:
            parts.append(f"GIF:「{gif_keyword}」")
        if image_keyword:
            parts.append(f"画像:「{image_keyword}」")
        await interaction.response.send_message(
            f"✅ {target.mention} さんが発言するたびに {' / '.join(parts)} を即座に送るように設定しました。",
            ephemeral=True,
        )

    @watch_group.command(name="off", description="設定を残したまま一時的に送信を停止します")
    @app_commands.describe(target="停止するユーザー")
    async def watch_off(self, interaction: discord.Interaction, target: discord.Member):
        guild_id = str(interaction.guild_id)
        cfg = self.data.get(guild_id, {}).get(str(target.id))
        if not cfg:
            await interaction.response.send_message(
                f"{target.mention} さんは監視対象になっていません。", ephemeral=True
            )
            return
        cfg["revision"] = cfg.get("revision", 0) + 1
        cfg["enabled"] = False
        _save_data(self.data)
        logger.info(f"/watch off: guild={guild_id} target={target}")
        await interaction.response.send_message(
            f"⏸️ {target.mention} さんへの送信を一時停止しました（設定は保持されています）。",
            ephemeral=True,
        )

    @watch_group.command(name="on", description="一時停止していた送信を再開します")
    @app_commands.describe(target="再開するユーザー")
    async def watch_on(self, interaction: discord.Interaction, target: discord.Member):
        guild_id = str(interaction.guild_id)
        cfg = self.data.get(guild_id, {}).get(str(target.id))
        if not cfg:
            await interaction.response.send_message(
                f"{target.mention} さんの設定がありません。先に /watch add で設定してください。",
                ephemeral=True,
            )
            return
        cfg["enabled"] = True
        _save_data(self.data)
        logger.info(f"/watch on: guild={guild_id} target={target}")
        await interaction.response.send_message(
            f"▶️ {target.mention} さんへの送信を再開しました。", ephemeral=True
        )

    @watch_group.command(name="remove", description="設定そのものを削除します")
    @app_commands.describe(target="解除するユーザー")
    async def watch_remove(self, interaction: discord.Interaction, target: discord.Member):
        guild_id = str(interaction.guild_id)
        removed = self.data.get(guild_id, {}).pop(str(target.id), None)
        _save_data(self.data)
        logger.info(f"/watch remove: guild={guild_id} target={target} removed={bool(removed)}")
        if removed:
            await interaction.response.send_message(
                f"🛑 {target.mention} さんの設定を削除しました。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{target.mention} さんは監視対象になっていません。", ephemeral=True
            )

    @watch_group.command(name="list", description="現在の監視設定を一覧表示します")
    async def watch_list(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        entries = self.data.get(guild_id, {})
        if not entries:
            await interaction.response.send_message("現在、監視設定はありません。", ephemeral=True)
            return
        lines = []
        for user_id, cfg in entries.items():
            member = interaction.guild.get_member(int(user_id))
            name = member.mention if member else f"<不明なユーザー:{user_id}>"
            parts = []
            if cfg.get("gif_keyword"):
                parts.append(f"GIF:「{cfg['gif_keyword']}」")
            if cfg.get("image_keyword"):
                parts.append(f"画像:「{cfg['image_keyword']}」")
            status = "▶️稼働中" if cfg.get("enabled", True) else "⏸️停止中"
            lines.append(f"- {name} → {' / '.join(parts)}（{status}）")
        # Discord limits message content to 2,000 characters.
        content = "\n".join(lines)
        await interaction.response.send_message(content[:2000], ephemeral=True)
        for start in range(2000, len(content), 2000):
            await interaction.followup.send(content[start:start + 2000], ephemeral=True)

    @app_commands.command(
        name="erase_all", description="このチャンネルでBotが送信したメッセージを全て削除します"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def erase_all(self, interaction: discord.Interaction):
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "このコマンドはテキストチャンネル/スレッドでのみ使えます。", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🧹 このチャンネルのBot投稿を削除しています…", ephemeral=True
        )

        def is_bot_message(m: discord.Message) -> bool:
            return m.author.id == self.bot.user.id

        try:
            deleted = await interaction.channel.purge(limit=None, check=is_bot_message)
            logger.info(
                f"/erase_all: channel={interaction.channel} ({interaction.channel.id}) "
                f"deleted={len(deleted)}"
            )
            await interaction.followup.send(
                f"🗑️ {len(deleted)}件のメッセージを削除しました。", ephemeral=True
            )
        except discord.Forbidden:
            logger.warning(f"/erase_all: 権限不足 channel={interaction.channel.id}")
            await interaction.followup.send(
                "⚠️ 削除する権限がありません。Botに「メッセージの管理」権限が必要です。",
                ephemeral=True,
            )
        except Exception:
            logger.exception(f"/erase_all failed in channel={interaction.channel.id}")
            await interaction.followup.send("⚠️ 削除中にエラーが発生しました。", ephemeral=True)

    # ---------- メッセージ監視 ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        guild_id = str(message.guild.id)
        user_id = str(message.author.id)
        cfg = self.data.get(guild_id, {}).get(user_id)
        if not cfg or not cfg.get("enabled", True):
            return

        # Serialize selection, send and history update for the same target.
        # Weak references let idle target locks disappear automatically.
        key = (guild_id, user_id)
        lock = self._target_locks.setdefault(key, asyncio.Lock())
        revision = cfg.get("revision", 0)
        async with lock:
            def still_active():
                return (
                    self.data.get(guild_id, {}).get(user_id) is cfg
                    and cfg.get("enabled", True)
                    and cfg.get("revision", 0) == revision
                )

            if not still_active():
                return
            media_url = await self._fetch_media(cfg, exclude_url=cfg.get("last_media_url"))
            # Commands can change/remove the target while the HTTP request awaits.
            if not still_active():
                return
            if not media_url:
                logger.warning("新しい送信候補がありません: user=%s", message.author)
                return

            try:
                await message.channel.send(media_url)
            except Exception:
                logger.exception("メッセージ送信に失敗しました: user=%s", message.author)
                return

            cfg["last_media_url"] = media_url
            _save_data(self.data)
            logger.info("送信成功: user=%s url=%s", message.author, media_url)

    async def _fetch_media(self, cfg: dict, exclude_url: Optional[str] = None) -> Optional[str]:
        """設定されているgif_keyword / image_keywordからランダムに1件取得。
        両方設定されている場合はどちらを先に試すかもランダムにし、
        失敗したらもう一方にフォールバックする。exclude_urlが指定されている場合、
        そのURLを避けて選ぶ。新しい候補がなければ別のソースを試す。
        """
        options = []
        if cfg.get("gif_keyword"):
            options.append(("gif", cfg["gif_keyword"]))
        if cfg.get("image_keyword"):
            options.append(("image", cfg["image_keyword"]))
        if not options:
            return None

        random.shuffle(options)
        for media_type, keyword in options:
            if media_type == "gif":
                url = await self._fetch_gif(keyword, exclude_url=exclude_url)
            else:
                url = await self._fetch_image(keyword, exclude_url=exclude_url)
            if url:
                return url
        return None

    @staticmethod
    def _pick_excluding(candidates: list, exclude_url: Optional[str]) -> Optional[str]:
        """前回のURLを除外する。代替候補がなければ再送せずNoneを返す。"""
        pool = list(dict.fromkeys(c for c in candidates if c and c != exclude_url))
        return random.choice(pool) if pool else None

    async def _fetch_gif(self, keyword: str, exclude_url: Optional[str] = None) -> Optional[str]:
        if not GIF_API_KEY or self.session is None:
            return None

        candidates: list = []
        pos = None
        for _ in range(1 + MAX_EXTRA_PAGES):
            params = {
                "q": keyword,
                "key": GIF_API_KEY,
                "client_key": "watch_cog",
                "limit": SEARCH_POOL_SIZE,
                "random": "true",
                "media_filter": "gif",
            }
            if pos:
                params["pos"] = pos
            try:
                async with self.session.get(GIF_SEARCH_URL, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"GIF検索失敗 status={resp.status} keyword={keyword!r}")
                        break
                    payload = await resp.json()
            except Exception:
                logger.exception(f"GIF検索で例外 keyword={keyword!r}")
                break

            results = payload.get("results", [])
            page_candidates = [
                r["media_formats"]["gif"]["url"]
                for r in results
                if r.get("media_formats", {}).get("gif", {}).get("url")
            ]
            candidates.extend(page_candidates)
            pos = payload.get("next")
            if not pos or not page_candidates:
                break

        unique_candidates = list(dict.fromkeys(candidates))
        logger.info(f"GIF keyword={keyword!r} 候補数={len(unique_candidates)}")
        if len(unique_candidates) <= 1:
            logger.warning(
                f"GIF keyword={keyword!r} はヒット数が{len(unique_candidates)}件しかなく、"
                "前回と同じURLは再送しません。候補を増やすには一般的なキーワードを試してください。"
            )
        return self._pick_excluding(unique_candidates, exclude_url)

    async def _fetch_image(self, keyword: str, exclude_url: Optional[str] = None) -> Optional[str]:
        """Openverse (CCライセンス画像検索、APIキー不要)から静止画を取得。
        候補を増やすためライセンス種別の絞り込みはあえて行っていない。
        """
        if self.session is None:
            return None

        candidates: list = []
        for page in range(1, 2 + MAX_EXTRA_PAGES):
            params = {
                "q": keyword,
                "page_size": SEARCH_POOL_SIZE,
                "page": page,
            }
            try:
                async with self.session.get(OPENVERSE_SEARCH_URL, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"画像検索失敗 status={resp.status} keyword={keyword!r}")
                        break
                    payload = await resp.json()
            except Exception:
                logger.exception(f"画像検索で例外 keyword={keyword!r}")
                break

            results = payload.get("results", [])
            page_candidates = [r.get("url") or r.get("thumbnail") for r in results]
            page_candidates = [c for c in page_candidates if c]
            candidates.extend(page_candidates)
            if not results:
                break

        unique_candidates = list(dict.fromkeys(candidates))
        logger.info(f"画像 keyword={keyword!r} 候補数={len(unique_candidates)}")
        if len(unique_candidates) <= 1:
            logger.warning(
                f"画像 keyword={keyword!r} はヒット数が{len(unique_candidates)}件しかなく、"
                "前回と同じURLは再送しません。候補を増やすには一般的なキーワードを試してください。"
            )
        return self._pick_excluding(unique_candidates, exclude_url)


async def setup(bot: commands.Bot):
    await bot.add_cog(WatchCog(bot))
