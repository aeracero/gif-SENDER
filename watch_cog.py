"""
watch_cog.py
（bot.py と同じフォルダに置くこと。サブフォルダに分けるとRailway上で
 ModuleNotFoundErrorの原因になりやすいため、フラットな構成にしています）
指定したユーザーが発言するたびに、設定したキーワードに合う画像(GIF/静止画)を
自動で送信するCogです。

画像ソース:
- GIF: KLIPY API (Tenor互換エンドポイント。要APIキー / 環境変数 GIF_API_KEY)
  ※ Google が2026年6月30日にTenor APIを完全終了したため、Tenor互換のKLIPYに切り替えています
- 静止画: Openverse API (APIキー不要・CCライセンス画像)

対象ユーザーごとに「GIF用キーワード」と「画像用キーワード」を別々に設定できます。
両方設定した場合は発言のたびにランダムでどちらかを送信します
（片方が取得失敗したらもう片方にフォールバック）。

直前に送った画像/GIFのURLを対象ごとに記録し(設定ファイルに永続化)、
次回はそれを避けて選ぶことで連続で同じものが出るのを防いでいます。

使い方 (スラッシュコマンド):
/watch add target:@ユーザー [gif_keyword:単語] [image_keyword:単語] [cooldown:秒数]
    ※ gif_keyword / image_keyword は少なくとも片方を指定してください
/watch off target:@ユーザー   … 設定を残したまま一時停止（即時反映）
/watch on target:@ユーザー    … 一時停止を解除（即時反映）
/watch remove target:@ユーザー … 設定そのものを削除
/watch list
"""

import json
import os
import random
import time
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

GIF_API_KEY = os.environ.get("GIF_API_KEY", "")
# KLIPYが提供するTenor互換エンドポイント。パラメータもレスポンス形式もTenorと同一。
GIF_SEARCH_URL = "https://api.klipy.com/v2/search"
OPENVERSE_SEARCH_URL = "https://api.openverse.org/v1/images/"

# 候補プールを増やすほど「連続で同じ画像」が起きにくくなる。
SEARCH_POOL_SIZE = 50

# データ保存先。Railwayではプロジェクトのルートで実行される想定なので、
# カレントディレクトリ基準にしてbot.pyと同階層に置く。
# 永続化したい場合はRailwayのVolumeをこのパスにマウントすること。
DATA_FILE = Path(os.environ.get("WATCH_DATA_PATH", "watch_data.json"))

DEFAULT_COOLDOWN = 10  # 秒。個別に上書き可能


def _load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class WatchCog(commands.Cog):
    """指定ユーザーの発言に反応して画像を送るCog"""

    watch_group = app_commands.Group(
        name="watch", description="指定した相手が発言したときに画像を送る機能"
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 構造:
        # { "guild_id": { "user_id": {
        #       "gif_keyword": str | None,
        #       "image_keyword": str | None,
        #       "cooldown": int,
        #       "enabled": bool,
        #       "last_media_url": str | None,  # 再起動をまたいでも重複防止できるよう永続化
        # } } }
        self.data: dict = _load_data()
        # クールダウン管理: { (guild_id, user_id): last_sent_timestamp } — こちらはメモリのみでOK
        self._last_sent: dict = {}
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self.session:
            await self.session.close()

    # ---------- スラッシュコマンド ----------

    @watch_group.command(
        name="add",
        description="このユーザーが発言したら画像/GIFを送るように設定します",
    )
    @app_commands.describe(
        target="対象にするユーザー",
        gif_keyword="GIF検索に使うキーワード（省略可）",
        image_keyword="静止画検索に使うキーワード（省略可）",
        cooldown="連投を防ぐクールダウン秒数（省略時は10秒）",
    )
    async def watch_add(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        gif_keyword: Optional[str] = None,
        image_keyword: Optional[str] = None,
        cooldown: Optional[app_commands.Range[int, 1, 3600]] = DEFAULT_COOLDOWN,
    ):
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
            "cooldown": cooldown,
            "enabled": True,
            "last_media_url": None,
        }
        _save_data(self.data)

        parts = []
        if gif_keyword:
            parts.append(f"GIF:「{gif_keyword}」")
        if image_keyword:
            parts.append(f"画像:「{image_keyword}」")
        await interaction.response.send_message(
            f"✅ {target.mention} さんの発言時に {' / '.join(parts)} を送るように設定しました"
            f"（クールダウン{cooldown}秒）。",
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
        cfg["enabled"] = False
        _save_data(self.data)
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
        await interaction.response.send_message(
            f"▶️ {target.mention} さんへの送信を再開しました。", ephemeral=True
        )

    @watch_group.command(name="remove", description="設定そのものを削除します")
    @app_commands.describe(target="解除するユーザー")
    async def watch_remove(self, interaction: discord.Interaction, target: discord.Member):
        guild_id = str(interaction.guild_id)
        removed = self.data.get(guild_id, {}).pop(str(target.id), None)
        _save_data(self.data)
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
            cooldown = cfg.get("cooldown", DEFAULT_COOLDOWN)
            status = "▶️稼働中" if cfg.get("enabled", True) else "⏸️停止中"
            lines.append(f"- {name} → {' / '.join(parts)}（{cooldown}秒 / {status}）")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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

        cooldown = cfg.get("cooldown", DEFAULT_COOLDOWN)
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now - self._last_sent.get(key, 0) < cooldown:
            return
        self._last_sent[key] = now

        media_url = await self._fetch_media(cfg, exclude_url=cfg.get("last_media_url"))
        if media_url:
            await message.channel.send(media_url)
            cfg["last_media_url"] = media_url
            _save_data(self.data)

    async def _fetch_media(self, cfg: dict, exclude_url: Optional[str] = None) -> Optional[str]:
        """設定されているgif_keyword / image_keywordからランダムに1件取得。
        両方設定されている場合はどちらを先に試すかもランダムにし、
        失敗したらもう一方にフォールバックする。exclude_urlが指定されている場合、
        候補が複数あればそのURLを避けて選ぶ（＝前回と必ず違う画像になる）。
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
        """candidatesからランダムに1件選ぶ。exclude_urlと異なるものが他にあれば
        必ずそちらを選ぶ（候補がexclude_urlしか無い場合のみ同じものを返す）。"""
        if not candidates:
            return None
        filtered = [c for c in candidates if c != exclude_url]
        pool = filtered if filtered else candidates
        return random.choice(pool)

    async def _fetch_gif(self, keyword: str, exclude_url: Optional[str] = None) -> Optional[str]:
        if not GIF_API_KEY or self.session is None:
            return None
        params = {
            "q": keyword,
            "key": GIF_API_KEY,
            "client_key": "watch_cog",
            "limit": SEARCH_POOL_SIZE,
            "random": "true",
            "media_filter": "gif",
        }
        try:
            async with self.session.get(GIF_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
                results = payload.get("results", [])
                candidates = [
                    r["media_formats"]["gif"]["url"]
                    for r in results
                    if r.get("media_formats", {}).get("gif", {}).get("url")
                ]
                return self._pick_excluding(candidates, exclude_url)
        except Exception:
            return None

    async def _fetch_image(self, keyword: str, exclude_url: Optional[str] = None) -> Optional[str]:
        """Openverse (CCライセンス画像検索、APIキー不要)から静止画を1件取得。"""
        if self.session is None:
            return None
        params = {
            "q": keyword,
            "page_size": SEARCH_POOL_SIZE,
            "license_type": "commercial,modification",
        }
        try:
            async with self.session.get(OPENVERSE_SEARCH_URL, params=params) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
                results = payload.get("results", [])
                candidates = [r.get("url") or r.get("thumbnail") for r in results]
                candidates = [c for c in candidates if c]
                return self._pick_excluding(candidates, exclude_url)
        except Exception:
            return None


async def setup(bot: commands.Bot):
    await bot.add_cog(WatchCog(bot))
