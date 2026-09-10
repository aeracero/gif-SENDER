"""Discord administration for persistent GIF collections."""
import asyncio
from contextlib import closing
import logging
from pathlib import PurePosixPath

import discord
from discord import app_commands
from discord.ext import commands

from gif_sources import MAX_IMPORT_BYTES, archive_items, collect_commons
from gif_store import keyword_key, validate_gif
from watch_cog import local_gif_caption

logger = logging.getLogger(__name__)


class ReviewView(discord.ui.View):
    def __init__(self, cog, guild, user, keyword, media_id):
        super().__init__(timeout=300)
        self.cog, self.guild, self.user = cog, guild, user
        self.keyword, self.media_id = keyword, media_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message('この確認操作は実行した管理者だけが使えます。', ephemeral=True)
            return False
        return True

    async def advance(self, interaction, state):
        self.cog.store.set_state(self.guild, self.media_id, state)
        pending = [r for r in self.cog.store.entries(self.guild, self.keyword) if r['state'] == 'pending']
        # Missing files are omitted from previews and send selection.
        pending = [r for r in pending if self.cog.store.path(r).is_file() and r['size'] <= interaction.guild.filesize_limit]
        if not pending:
            await interaction.response.edit_message(content='このキーワードの候補確認が終わりました。',
                                                     attachments=[], view=None)
            self.stop()
            return
        item = pending[0]
        self.media_id = item['id']
        with closing(discord.File(self.cog.store.path(item), filename=f"gif-{item['id']}.gif")) as f:
            await interaction.response.edit_message(content=self.cog.preview_text(item), attachments=[f], view=self,
                                                    allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label='採用して次へ', style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        await self.advance(interaction, 'approved')

    @discord.ui.button(label='除外して次へ', style=discord.ButtonStyle.danger)
    async def exclude(self, interaction, button):
        await self.advance(interaction, 'excluded')


class GifCog(commands.Cog):
    gif_group = app_commands.Group(name='gif', description='VolumeのGIFコレクションを管理',
                                   guild_only=True, default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, bot):
        self.bot = bot
        self.import_lock = asyncio.Lock()

    @property
    def watch(self):
        return self.bot.get_cog('WatchCog')

    @property
    def store(self):
        return self.watch.gif_store

    async def interaction_check(self, interaction):
        if interaction.guild is None or not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message('サーバー管理権限が必要です。', ephemeral=True)
            return False
        return True

    async def cog_app_command_error(self, interaction, error):
        cause = getattr(error, 'original', error)
        if isinstance(cause, ValueError):
            text = str(cause)
        else:
            logger.error('GIF command failed', exc_info=(type(cause), cause, cause.__traceback__))
            text = '操作に失敗しました。途中まで取り込んだ候補は保存されています。ログを確認してください。'
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @gif_group.command(name='collect', description='Commonsから最大100件のGIF候補を収集（採用前に確認）')
    async def collect(self, interaction: discord.Interaction, keyword: str,
                      count: app_commands.Range[int, 1, 100] = 100):
        keyword = keyword_key(keyword)
        if self.import_lock.locked():
            await interaction.response.send_message('別の取り込みが実行中です。完了後に試してください。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.import_lock:
            result = await collect_commons(self.watch.session, self.store, interaction.guild_id, keyword, count)
        await interaction.followup.send(
            f"収集完了: 新規{result['added']}件 / 重複{result['duplicate']}件 / 対象外{result['skipped']}件。\n"
            f"検索語不一致で除外: {result['unrelated']}件。\n"
            f"{result['reason']}\n`/gif review keyword:{keyword}` で確認・採用してください。\n"
            'CommonsにはキャラクターGIFが揃わないことがあります。0件の場合も無関係な画像では補いません。', ephemeral=True)

    @gif_group.command(name='import', description='自分で用意したGIFまたはZIPを一括取り込み（最大100件）')
    async def import_files(self, interaction: discord.Interaction, keyword: str,
                           attachment: discord.Attachment, credit: str = ''):
        keyword = keyword_key(keyword)
        if len(credit) > 1000:
            raise ValueError('クレジットは1000文字以内にしてください。')
        if attachment.size > MAX_IMPORT_BYTES:
            raise ValueError('取り込みファイルは25MiB以下にしてください。ZIPを分割できます。')
        if self.import_lock.locked():
            await interaction.response.send_message('別の取り込みが実行中です。完了後に試してください。', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        added = duplicate = skipped = 0
        async with self.import_lock:
            data = await attachment.read()
            # ZIP expansion and GIF decoding run off the Discord event loop.
            iterator = archive_items(data, attachment.filename)
            def next_item():
                return next(iterator, None)
            while True:
                entry = await asyncio.to_thread(next_item)
                if entry is None:
                    break
                filename, content = entry
                if content is None:
                    skipped += 1
                    continue
                try:
                    await asyncio.to_thread(validate_gif, content)
                except ValueError:
                    skipped += 1
                    continue
                _, fresh = self.store.add(interaction.guild_id, keyword, content,
                                           PurePosixPath(filename).name, credit=credit)
                added += int(fresh)
                duplicate += int(not fresh)
        await interaction.followup.send(
            f'取り込み完了: 新規{added}件 / 重複{duplicate}件 / 対象外{skipped}件。\n'
            f'`/gif review keyword:{keyword}` で確認・採用してください。', ephemeral=True)

    @gif_group.command(name='list', description='キーワードのGIF一覧とID・採用状況を表示')
    async def list_gifs(self, interaction: discord.Interaction, keyword: str,
                        page: app_commands.Range[int, 1, 10] = 1):
        entries = self.store.entries(interaction.guild_id, keyword)
        labels = {'pending': '未確認', 'approved': '採用', 'excluded': '除外'}
        items = entries[(page - 1) * 10:page * 10]
        text = f'{keyword_key(keyword)}: 合計{len(entries)}件 / {page}ページ\n'
        text += '\n'.join(f"#{r['id']} [{labels[r['state']]}] {r['title'][:60]} ({r['size'] // 1024}KiB)" for r in items)
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def preview_text(item):
        return f"#{item['id']} / {item['keyword']} / {item['state']}\n{item['title']}\n{local_gif_caption(item)}"[:2000]

    @gif_group.command(name='review', description='未確認GIFを1件ずつ見て採用・除外する')
    async def review(self, interaction: discord.Interaction, keyword: str):
        items = [r for r in self.store.entries(interaction.guild_id, keyword)
                 if r['state'] == 'pending' and self.store.path(r).is_file()
                 and r['size'] <= interaction.guild.filesize_limit]
        if not items:
            await interaction.response.send_message('確認待ちのGIFはありません。', ephemeral=True)
            return
        await self.send_preview(interaction, items[0], review=True)

    async def send_preview(self, interaction, item, review=False):
        if item['size'] > interaction.guild.filesize_limit:
            raise ValueError('このGIFは現在のサーバーの添付上限を超えています。IDを指定して除外・削除できます。')
        view = ReviewView(self, interaction.guild_id, interaction.user.id, item['keyword'], item['id']) if review else None
        await interaction.response.defer(ephemeral=True)
        with closing(discord.File(self.store.path(item), filename=f"gif-{item['id']}.gif")) as f:
            options = {'view': view} if view is not None else {}
            await interaction.followup.send(self.preview_text(item), file=f, **options,
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @gif_group.command(name='preview', description='IDを指定して保存したGIFを表示')
    async def preview(self, interaction: discord.Interaction, gif_id: int):
        item = self.store.get(interaction.guild_id, gif_id)
        if not item or not self.store.path(item).is_file():
            raise ValueError('GIFが見つかりません。')
        await self.send_preview(interaction, item)

    @gif_group.command(name='approve_all', description='このキーワードの未確認GIFをすべて採用（除外済みは維持）')
    async def approve_all(self, interaction: discord.Interaction, keyword: str):
        count = self.store.approve_pending(interaction.guild_id, keyword)
        await interaction.response.send_message(f'{count}件を採用しました。', ephemeral=True)

    @gif_group.command(name='approve', description='IDを指定してGIFを採用・復帰')
    async def approve(self, interaction: discord.Interaction, gif_id: int):
        if not self.store.set_state(interaction.guild_id, gif_id, 'approved'):
            raise ValueError('GIFが見つかりません。')
        await interaction.response.send_message(f'#{gif_id}を採用しました。', ephemeral=True)

    @gif_group.command(name='exclude', description='指定したGIFを今後の送信候補から除外')
    async def exclude(self, interaction: discord.Interaction, gif_id: int):
        if not self.store.set_state(interaction.guild_id, gif_id, 'excluded'):
            raise ValueError('GIFが見つかりません。')
        await interaction.response.send_message(f'#{gif_id}を除外しました。', ephemeral=True)

    @gif_group.command(name='exclude_all', description='このキーワードのGIFをすべて送信対象から外す（削除しません）')
    async def exclude_all(self, interaction: discord.Interaction, keyword: str):
        count = self.store.exclude_all(interaction.guild_id, keyword)
        await interaction.response.send_message(
            f'{count}件を除外しました。ファイルは保持しています。必要なGIFだけ /gif approve で復帰できます。',
            ephemeral=True)

    @gif_group.command(name='unapprove_all', description='採用済みGIFの採用を一括解除（未確認の候補・ファイルは保持）')
    async def unapprove_all(self, interaction: discord.Interaction, keyword: str):
        count = self.store.unapprove_all(interaction.guild_id, keyword)
        await interaction.response.send_message(
            f'{count}件の採用を解除しました。未確認の候補・ファイルは保持しています。', ephemeral=True)

    @gif_group.command(name='delete', description='指定したGIFの登録を削除し、未使用の実ファイルも削除')
    async def delete(self, interaction: discord.Interaction, gif_id: int):
        if not self.store.remove(interaction.guild_id, gif_id):
            raise ValueError('GIFが見つかりません。')
        await interaction.response.send_message(f'#{gif_id}を削除しました。', ephemeral=True)

    @gif_group.command(name='mode', description='キーワードのローカル優先送信をON/OFF（ファイルは保持）')
    async def mode(self, interaction: discord.Interaction, keyword: str, local: bool):
        self.store.set_local(interaction.guild_id, keyword, local)
        await interaction.response.send_message('ローカル送信を有効にしました。採用済みGIFがなければスキップします。'
                                                if local else '既存のオンライン検索に戻しました。', ephemeral=True)


async def setup(bot):
    await bot.add_cog(GifCog(bot))
