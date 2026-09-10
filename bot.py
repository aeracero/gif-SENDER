"""
bot.py
Discordボットのエントリーポイント。
cogs/ 以下の拡張機能(watch_cogなど)を読み込んで起動します。

ローカル実行:
    python -m venv .venv && source .venv/bin/activate  (Windowsは .venv\\Scripts\\activate)
    pip install -r requirements.txt
    .env に DISCORD_TOKEN と TENOR_API_KEY を書いて
    python bot.py

Railwayでの実行:
    Procfile の `worker: python bot.py` がそのままエントリーポイントになります。
    環境変数(DISCORD_TOKEN, TENOR_API_KEY)はRailwayのVariablesタブで設定してください。
"""

import asyncio
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

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

INITIAL_EXTENSIONS = [
    "cogs.watch_cog",
]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash command sync failed: {e}")


async def main():
    async with bot:
        for ext in INITIAL_EXTENSIONS:
            await bot.load_extension(ext)
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
