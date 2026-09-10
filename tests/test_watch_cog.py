import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import watch_cog as module
from watch_cog import WatchCog


class WatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.file_patch = patch.object(module, 'DATA_FILE', Path(self.temp.name) / 'nested/data.json')
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)
        self.cog = WatchCog(SimpleNamespace(user=SimpleNamespace(id=99)))
        self.cfg = {'image_keyword': 'cat', 'enabled': True, 'last_media_url': None}
        self.cog.data = {'1': {'2': self.cfg}}
        self.message = SimpleNamespace(
            guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False, mention="<@2>"),
            channel=SimpleNamespace(send=AsyncMock()))

    def test_single_previous_candidate_is_not_repeated(self):
        self.assertIsNone(self.cog._pick_excluding(['a', 'a'], 'a'))
        self.assertEqual(self.cog._pick_excluding(['a', 'b'], 'a'), 'b')
        self.assertEqual(self.cog._pick_excluding(['a'], None), 'a')
        self.assertIsNone(self.cog._pick_excluding([], None))

    async def test_burst_updates_history_before_next_selection(self):
        async def fetch(cfg, exclude_url=None):
            await asyncio.sleep(0)
            return self.cog._pick_excluding(['a', 'b'], exclude_url)
        self.cog._fetch_media = fetch
        await asyncio.gather(*(self.cog.on_message(self.message) for _ in range(8)))
        urls = [call.args[0] for call in self.message.channel.send.call_args_list]
        self.assertEqual(len(urls), 8)
        self.assertTrue(all(a != b for a, b in zip(urls, urls[1:])))
        self.assertEqual(module._load_data()['1']['2']['last_media_url'], urls[-1])

    async def test_stop_remove_or_replace_during_search_cancels_send(self):
        for action in ('stop', 'remove', 'replace', 'stop_and_resume'):
            with self.subTest(action=action):
                self.cfg = {'image_keyword': 'cat', 'enabled': True}
                self.cog.data = {'1': {'2': self.cfg}}
                async def fetch(*args, **kwargs):
                    if action == 'stop':
                        self.cfg['enabled'] = False
                    elif action == 'remove':
                        del self.cog.data['1']['2']
                    elif action == 'replace':
                        self.cog.data['1']['2'] = dict(self.cfg)
                    else:
                        interaction = SimpleNamespace(guild_id=1, response=SimpleNamespace(send_message=AsyncMock()))
                        await WatchCog.watch_off.callback(self.cog, interaction, self.message.author)
                        await WatchCog.watch_on.callback(self.cog, interaction, self.message.author)
                    return 'a'
                self.cog._fetch_media = fetch
                await self.cog.on_message(self.message)
                self.message.channel.send.assert_not_awaited()

    async def test_failed_send_does_not_advance_history(self):
        self.cog._fetch_media = AsyncMock(return_value='a')
        self.message.channel.send.side_effect = RuntimeError('test send failure')
        with self.assertLogs(module.logger, level='ERROR'):
            await self.cog.on_message(self.message)
        self.assertIsNone(self.cfg['last_media_url'])

    async def test_exhausted_gif_falls_back_to_image(self):
        self.cog._fetch_gif = AsyncMock(return_value=None)
        self.cog._fetch_image = AsyncMock(return_value='b')
        with patch.object(module.random, 'shuffle'):
            result = await self.cog._fetch_media({'gif_keyword': 'cat', 'image_keyword': 'cat'}, 'a')
        self.assertEqual(result, 'b')
        self.cog._fetch_gif.assert_awaited_once_with('cat', exclude_url='a')
        self.cog._fetch_image.assert_awaited_once_with('cat', exclude_url='a')

    def test_atomic_save_preserves_existing_file_if_replace_fails(self):
        module._save_data({'old': True})
        with patch.object(module.os, 'replace', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                module._save_data({'new': True})
        self.assertEqual(module._load_data(), {'old': True})
        self.assertEqual(list(module.DATA_FILE.parent.iterdir()), [module.DATA_FILE])

    async def test_empty_keywords_are_rejected(self):
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))
        await WatchCog.watch_add.callback(self.cog, interaction, self.message.author, '  ', '\t')
        self.assertIn('どちらか', interaction.response.send_message.call_args.args[0])

    async def test_long_list_is_split(self):
        self.cog.data['1'] = {str(i): {'image_keyword': 'x' * 100} for i in range(40)}
        interaction = SimpleNamespace(guild_id=1, guild=SimpleNamespace(get_member=lambda _: None),
            response=SimpleNamespace(send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
        await WatchCog.watch_list.callback(self.cog, interaction)
        chunks = [interaction.response.send_message.call_args.args[0]]
        chunks += [c.args[0] for c in interaction.followup.send.call_args_list]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 2000 for c in chunks))
        self.assertEqual(''.join(chunks).count('画像:'), 40)

    async def test_cog_registers_and_uses_bounded_timeout(self):
        import discord
        from discord.ext import commands
        async with commands.Bot(command_prefix='!', intents=discord.Intents.default()) as bot:
            cog = WatchCog(bot)
            await bot.add_cog(cog)
            self.assertEqual(cog.session.timeout.total, 15)
            self.assertTrue(bot.tree.get_command('watch').guild_only)
            await bot.remove_cog('WatchCog')
            self.assertTrue(cog.session.closed)


if __name__ == '__main__':
    unittest.main()
