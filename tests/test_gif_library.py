import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zipfile

import discord
from discord.ext import commands
from PIL import Image

from gif_cog import GifCog, ReviewView
from gif_sources import archive_items, collect_commons, commons_credit, commons_matches_keyword, download_commons
from gif_store import GifStore, keyword_key, validate_gif
import watch_cog
from watch_cog import WatchCog


def gif(color):
    out = io.BytesIO()
    Image.new('RGB', (4, 4), color).save(out, format='GIF')
    return out.getvalue()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = GifStore(self.root)
        self.addCleanup(lambda: self.store.close())

    def add(self, color, guild=1, keyword='cat'):
        data = gif(color)
        validate_gif(data)
        return self.store.add(guild, keyword, data, color)[0]

    def test_full_cycles_survive_restart_and_do_not_repeat_boundary(self):
        ids = {self.add(c) for c in ('red', 'green', 'blue')}
        for i in ids:
            self.store.set_state(1, i, 'approved')
        first = self.store.pick(1, 2, 'cat')
        self.store.mark_sent(1, 2, first['id'])
        self.store.close()
        self.store = GifStore(self.root)
        seen = [first['id']]
        for _ in range(8):
            item = self.store.pick(1, 2, 'cat')
            self.assertNotEqual(seen[-1], item['id'])
            seen.append(item['id'])
            self.store.mark_sent(1, 2, item['id'])
        for i in range(0, 9, 3):
            self.assertEqual(set(seen[i:i+3]), ids)

    def test_pending_excluded_missing_and_oversize_are_not_sent(self):
        i = self.add('red')
        self.assertIsNone(self.store.pick(1, 2, 'cat'))
        self.store.set_state(1, i, 'approved')
        self.assertIsNone(self.store.pick(1, 2, 'cat', max_size=1))
        self.store.set_state(1, i, 'excluded')
        self.assertEqual(self.store.approve_pending(1, 'cat'), 0)
        self.assertIsNone(self.store.pick(1, 2, 'cat'))
        self.store.set_state(1, i, 'approved')
        self.store.path(self.store.get(1, i)).unlink()
        self.assertIsNone(self.store.pick(1, 2, 'cat'))

    def test_duplicate_content_and_guild_isolation(self):
        i = self.add('red')
        self.assertEqual(self.store.add(1, 'ＣＡＴ', gif('red')), (i, False))
        j = self.add('red', guild=3)
        self.assertNotEqual(i, j)
        self.assertIsNone(self.store.get(3, i))
        self.assertFalse(self.store.remove(3, i))
        self.store.remove(1, i)
        self.assertTrue(self.store.path(self.store.get(3, j)).exists())
        self.store.remove(3, j)
        self.assertEqual(list(self.store.files.glob('*.gif')), [])

    def test_single_candidate_and_failed_send_leave_selection_available(self):
        i = self.add('red')
        self.store.set_state(1, i, 'approved')
        self.assertEqual(self.store.pick(1, 2, 'cat')['id'], i)
        self.assertEqual(self.store.pick(1, 2, 'cat')['id'], i)
        self.store.mark_sent(1, 2, i)
        self.assertIsNone(self.store.pick(1, 2, 'cat'))
        self.assertEqual(self.store.pick(1, 3, 'cat')['id'], i)

    def test_disk_quota_and_collection_cap(self):
        self.store.max_bytes = 1
        with self.assertRaises(ValueError):
            self.add('red')
        self.store.max_bytes = 100000
        with patch('gif_store.MAX_COLLECTION', 1):
            self.add('red')
            with self.assertRaises(ValueError):
                self.add('blue')

    def test_mode_and_normalization(self):
        self.store.set_local(1, ' ＣＡＴ  ', True)
        self.assertTrue(self.store.has_collection(1, 'cat'))
        self.store.set_local(1, 'cat', False)
        self.assertFalse(self.store.has_collection(1, 'cat'))
        self.assertFalse(self.store.has_collection(1, 'x' * 101))
        self.assertEqual(keyword_key(' A   B '), 'a b')

    def test_unapprove_all_only_changes_approved_in_requested_collection(self):
        approved = self.add('red')
        pending = self.add('blue')
        other_keyword = self.add('green', keyword='dog')
        other_guild = self.add('yellow', guild=9)
        for guild, media_id in [(1, approved), (1, other_keyword), (9, other_guild)]:
            self.store.set_state(guild, media_id, 'approved')
        self.assertEqual(self.store.unapprove_all(1, 'cat'), 1)
        self.assertEqual(self.store.get(1, approved)['state'], 'excluded')
        self.assertEqual(self.store.get(1, pending)['state'], 'pending')
        self.assertEqual(self.store.get(1, other_keyword)['state'], 'approved')
        self.assertEqual(self.store.get(9, other_guild)['state'], 'approved')
        self.assertTrue(self.store.path(self.store.get(1, approved)).exists())
        self.assertEqual(self.store.unapprove_all(1, 'cat'), 0)

    def test_exclude_all_preserves_files_and_history(self):
        approved = self.add('red')
        pending = self.add('blue')
        self.store.set_state(1, approved, 'approved')
        self.store.mark_sent(1, 2, approved)
        self.assertEqual(self.store.exclude_all(1, 'cat'), 2)
        self.assertIsNone(self.store.pick(1, 2, 'cat'))
        self.assertTrue(self.store.path(self.store.get(1, pending)).exists())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM history').fetchone()[0], 1)

    def test_screenshot_regression_rejects_mandelbrot_for_cyrene(self):
        page = {'title': 'File:Inversion of lambda Mandelbrot set with different translations.gif',
                'imageinfo': [{'extmetadata': {'Artist': {'value': 'キュレネ'},
                    'ImageDescription': {'value': 'A mathematical fractal animation'}}}]}
        self.assertFalse(commons_matches_keyword(page, 'キュレネ'))
        page['title'] = 'File:キュレネのアニメーション.gif'
        self.assertTrue(commons_matches_keyword(page, 'キュレネ'))
        self.assertFalse(commons_matches_keyword({'title': 'File:Cathedral.gif'}, 'cat'))
        self.assertTrue(commons_matches_keyword({'title': 'File:ＣＡＴ.gif'}, 'cat'))
        self.assertFalse(commons_matches_keyword({'title': 'File:cat.gif'}, 'red cat'))

    def test_zip_cannot_escape_storage_and_rejects_non_gifs(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as z:
            z.writestr('../../outside.gif', gif('red'))
            z.writestr('not-a-gif.txt', 'hello')
        entries = list(archive_items(out.getvalue(), 'batch.zip'))
        self.assertEqual(len(entries), 1)
        validate_gif(entries[0][1])
        self.store.add(1, '../cat', entries[0][1], entries[0][0])
        self.assertFalse((self.root.parent / 'outside.gif').exists())
        with self.assertRaises(ValueError):
            validate_gif(b'GIF89a truncated')
        with self.assertRaises(ValueError):
            list(archive_items(b'bad zip', 'batch.zip'))


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p = patch.object(watch_cog, 'DATA_FILE', Path(self.temp.name) / 'watch.json')
        self.p.start()
        self.addCleanup(self.p.stop)
        self.bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        self.watch = WatchCog(self.bot)
        await self.bot.add_cog(self.watch)
        self.manager = GifCog(self.bot)
        await self.bot.add_cog(self.manager)
        self.watch.data = {'1': {'2': {'enabled': True, 'gif_keyword': 'cat'}}}
        self.message = SimpleNamespace(author=SimpleNamespace(id=2, bot=False),
            guild=SimpleNamespace(id=1, filesize_limit=10_000_000), channel=SimpleNamespace(send=AsyncMock()))

    async def asyncTearDown(self):
        await self.bot.close()

    def interaction(self):
        return SimpleNamespace(guild_id=1, guild=SimpleNamespace(filesize_limit=10_000_000),
            user=SimpleNamespace(id=5, guild_permissions=SimpleNamespace(manage_guild=True)),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def test_commands_register_and_restrict_management(self):
        group = self.bot.tree.get_command('gif')
        self.assertTrue(group.guild_only)
        self.assertTrue(group.default_permissions.manage_guild)
        i = self.interaction()
        i.user.guild_permissions.manage_guild = False
        command = group.get_command('delete')
        self.assertFalse(await command._check_can_run(i))

    async def test_local_burst_uses_attachments_without_search(self):
        ids = set()
        for c in ('red', 'blue', 'green'):
            ids.add(self.watch.gif_store.add(1, 'cat', gif(c), c, 'https://example.org/source', 'Artist / CC0')[0])
        self.watch.gif_store.approve_pending(1, 'cat')
        self.watch._fetch_media = AsyncMock()
        sent = []
        async def send(caption, *, file, allowed_mentions):
            self.assertTrue(file.fp.read().startswith(b'GIF'))
            self.assertIn('Artist / CC0', caption)
            self.assertFalse(allowed_mentions.everyone)
            sent.append(int(file.filename[4:-4]))
            await asyncio.sleep(0)
        self.message.channel.send.side_effect = send
        await asyncio.gather(*(self.watch.on_message(self.message) for _ in range(6)))
        self.assertEqual(set(sent[:3]), ids)
        self.assertEqual(set(sent[3:]), ids)
        self.assertTrue(all(a != b for a,b in zip(sent, sent[1:])))
        self.watch._fetch_media.assert_not_awaited()

    async def test_pending_local_pool_never_falls_back_to_unrelated_online(self):
        self.watch.gif_store.add(1, 'cat', gif('red'))
        self.watch._fetch_media = AsyncMock()
        await self.watch.on_message(self.message)
        self.message.channel.send.assert_not_awaited()
        self.watch._fetch_media.assert_not_awaited()

    async def test_import_review_and_exclude(self):
        i = self.interaction()
        attachment = SimpleNamespace(size=len(gif('red')), filename='red.gif', read=AsyncMock(return_value=gif('red')))
        await GifCog.import_files.callback(self.manager, i, 'cat', attachment)
        item = self.watch.gif_store.entries(1, 'cat')[0]
        self.assertEqual(item['state'], 'pending')
        await GifCog.review.callback(self.manager, i, 'cat')
        self.assertIsInstance(i.followup.send.call_args.kwargs['view'], ReviewView)
        await GifCog.approve.callback(self.manager, i, item['id'])
        await GifCog.exclude.callback(self.manager, i, item['id'])
        self.assertIsNone(self.watch.gif_store.pick(1, 2, 'cat'))

    async def test_preview_omits_none_view_and_review_buttons_advance(self):
        first, _ = self.watch.gif_store.add(1, 'cat', gif('red'))
        second, _ = self.watch.gif_store.add(1, 'cat', gif('blue'))
        i = self.interaction()
        await GifCog.preview.callback(self.manager, i, first)
        self.assertNotIn('view', i.followup.send.call_args.kwargs)
        self.assertTrue(i.followup.send.call_args.kwargs['file'].fp.closed)
        view = ReviewView(self.manager, 1, 5, 'cat', first)
        i.response.edit_message = AsyncMock()
        await view.advance(i, 'approved')
        self.assertEqual(view.media_id, second)
        self.assertEqual(self.watch.gif_store.get(1, first)['state'], 'approved')
        await view.advance(i, 'excluded')
        self.assertIsNone(i.response.edit_message.call_args.kwargs['view'])
        self.assertEqual(self.watch.gif_store.get(1, second)['state'], 'excluded')

    async def test_failed_local_send_does_not_advance_history(self):
        media_id, _ = self.watch.gif_store.add(1, 'cat', gif('red'))
        self.watch.gif_store.approve_pending(1, 'cat')
        self.message.channel.send.side_effect = RuntimeError('send failed')
        with self.assertLogs(watch_cog.logger, level='ERROR'):
            await self.watch.on_message(self.message)
        self.assertEqual(self.watch.gif_store.pick(1, 2, 'cat')['id'], media_id)

    async def test_unknown_keyword_still_uses_existing_search(self):
        self.watch._fetch_media = AsyncMock(return_value='https://example.com/a.gif')
        await self.watch.on_message(self.message)
        self.message.channel.send.assert_awaited_once_with('https://example.com/a.gif')

    async def test_collector_persists_only_eligible_gifs_as_pending(self):
        metadata = {'Artist': {'value': '<b>Artist</b>'}, 'LicenseShortName': {'value': 'CC0'}}
        payload = {'query': {'pages': [
            {'pageid': 1, 'title': 'File:Cat.gif', 'imageinfo': [{'mime': 'image/gif', 'size': 10,
                'url': 'https://upload.wikimedia.org/a.gif', 'extmetadata': metadata}]},
            {'pageid': 2, 'title': 'File:Unknown.gif', 'imageinfo': [{'mime': 'image/gif', 'size': 10}]},
        ]}}
        response = SimpleNamespace(status=200, json=AsyncMock(return_value=payload))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        session = SimpleNamespace(get=MagicMock(return_value=context))
        with patch('gif_sources.download_commons', AsyncMock(return_value=gif('red'))):
            result = await collect_commons(session, self.watch.gif_store, 1, 'cat')
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['unrelated'], 1)
        item = self.watch.gif_store.entries(1, 'cat')[0]
        self.assertEqual(item['state'], 'pending')
        self.assertEqual(item['credit'], 'Artist / CC0')
        self.assertIn('curid=1', item['source'])
        self.assertIsNone(commons_credit({'extmetadata': {'LicenseShortName': {'value': 'All rights reserved'}}}))

    async def test_unrelated_commons_gif_is_never_downloaded(self):
        payload = {'query': {'pages': [{'pageid': 111556851,
            'title': 'File:Inversion of lambda Mandelbrot set with different translations.gif',
            'imageinfo': [{'mime': 'image/gif', 'size': 10, 'url': 'https://upload.wikimedia.org/fractal.gif',
                'extmetadata': {'Artist': {'value': 'Adam'}, 'LicenseShortName': {'value': 'CC0'}}}]}]}}
        response = SimpleNamespace(status=200, json=AsyncMock(return_value=payload))
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        session = SimpleNamespace(get=MagicMock(return_value=context))
        with patch('gif_sources.download_commons', AsyncMock()) as download:
            result = await collect_commons(session, self.watch.gif_store, 1, 'キュレネ')
        download.assert_not_awaited()
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['unrelated'], 1)
        self.assertEqual(self.watch.gif_store.entries(1, 'キュレネ'), [])

    async def test_downloader_rejects_internal_hosts_without_request(self):
        session = SimpleNamespace(get=MagicMock())
        with self.assertRaises(ValueError):
            await download_commons(session, 'http://127.0.0.1/secrets')
        session.get.assert_not_called()
