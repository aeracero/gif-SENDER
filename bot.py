"""
bot.py
Discordボットのエントリーポイント。
同じディレクトリにある watch_cog.py を拡張機能として読み込んで起動します。
（bot.py と watch_cog.py は必ず同じフォルダに置くこと。サブフォルダに分けると
 Railway上で `ModuleNotFoundError` の原因になりやすいため、あえてフラットな
 構成にしています。）

ローカル実行:
    python -m venv .venv && source .venv/bin/activate  (Windowsは .venv\\Scripts\\activate)
    pip install -r requirements.txt
    .env に DISCORD_TOKEN と GIF_API_KEY を書いて
    python bot.py

Railwayでの実行:
    Procfile の `worker: python bot.py` がそのままエントリーポイントになります。
    環境変数(DISCORD_TOKEN, GIF_API_KEY)はRailwayのVariablesタブで設定してください。

スラッシュコマンドの反映について:
    グローバル同期(sync())だけだと、Discord側の反映に最大1時間ほどかかることがあります。
    このbotは起動時に「参加している全サーバー」へギルド単位でも同期するため、
    どのサーバーでも起動直後から即座にコマンドが使えます。
    新しいサーバーに招待されたときも on_guild_join で自動的に即時反映します。
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ログをRailwayのログ画面（標準出力）に出す設定。
# watch_cog.py側のlogger.info/warning/exceptionもこれで表示されるようになる。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ローカル実行時のみ .env を読み込む。Railwayでは環境変数が直接注入されるため
# .env が無くてもエラーにはならない(load_dotenvは静かに無視する)。
load_dotenv()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError(
        "環境変数 DISCORD_TOKEN が設定されていません。.env またはRailwayのVariablesで設定してください。"
    )

intents = discord.Intents.default()
intents.members = True  # /watch add で @メンションからメンバーを解決するために必要

bot = commands.Bot(command_prefix="!", intents=intents)
logger = logging.getLogger(__name__)

INITIAL_EXTENSIONS = [
    "watch_cog",
    "gif_cog",
]


async def _sync_to_guild(guild: discord.abc.Snowflake) -> int:
    """指定ギルドにグローバルコマンドをコピーして即時同期し、件数を返す。"""
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    return len(synced)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        # まずグローバルコマンドとして登録（新規参加サーバー用のベースになる）
        await bot.tree.sync()

        # 現在参加している全サーバーに即時反映
        for guild in bot.guilds:
            count = await _sync_to_guild(guild)
            logger.info(f"Synced {count} slash command(s) instantly to {guild.name} ({guild.id})")
    except Exception:
        logger.exception("Slash command sync failed")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """新しく招待されたサーバーにも即座にコマンドを反映する。"""
    try:
        count = await _sync_to_guild(guild)
        logger.info(f"Synced {count} slash command(s) instantly to newly joined guild: {guild.name} ({guild.id})")
    except Exception:
        logger.exception(f"Slash command sync failed for guild {guild.id}")


async def main():
    async with bot:
        for ext in INITIAL_EXTENSIONS:
            await bot.load_extension(ext)
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
