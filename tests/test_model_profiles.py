"""Route isolation, credentials, errors and transactional switching; no live sends."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from plugins.astrbot_plugin_roleplay_gateway import model_profiles as profiles
from plugins.astrbot_plugin_roleplay_gateway.provider_gate import ProviderAttemptError
from plugins.astrbot_plugin_roleplay_gateway.console_admin import config_version


def provider(profile='kouri', model='gemini-4-argon'):
    cfg = {'id': profiles.KOURI_ID if profile == 'kouri' else profiles.DEEPSEEK_ID,
           'type': 'openai_chat_completion', 'provider': 'custom' if profile == 'kouri' else 'deepseek',
           'custom_extra_body': {} if profile == 'kouri' else {'thinking': {'type': 'disabled'}},
           'key': ['test-key-not-a-real-secret'], 'api_base': profiles.KOURI_BASE if profile == 'kouri' else 'https://api.deepseek.com'}
    p = SimpleNamespace(provider_config=cfg, api_keys=cfg['key'], model=model,
        client=SimpleNamespace(base_url=cfg['api_base'], close=AsyncMock(), _client=SimpleNamespace(follow_redirects=False)))
    p.get_model = lambda: p.model
    p.set_model = lambda value: setattr(p, 'model', value)
    return p


class ProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ds = provider('deepseek', 'deepseek-flash')
        self.plugin = SimpleNamespace(config={}, context=SimpleNamespace(get_provider_by_id=lambda _: self.ds))
        self.router = profiles.ModelProfiles(self.plugin, self.root)

    async def asyncTearDown(self):
        await self.router.close()
        self.temp.cleanup()

    async def save(self):
        return await self.router.save_key({'action': 'save', 'keyRevision': 'initial', 'apiKey': 'synthetic-key-123456789'})

    def test_old_configuration_and_independent_models(self):
        old = {'console_model': 'deepseek-custom'}
        self.assertEqual(profiles.selected(old), 'deepseek')
        self.assertEqual(profiles.profile_models(old), {'deepseek': 'deepseek-custom', 'kouri': 'gemini-4-argon'})
        config = {'console_profile': 'kouri', 'console_model': 'gemini-4-argon',
                  'console_profiles': {'deepseek': {'model': 'deepseek-custom'}, 'kouri': {'model': 'gemini-4-argon'}}}
        self.assertEqual(profiles.profile_models(json.loads(json.dumps(config)))['deepseek'], 'deepseek-custom')
        self.assertNotEqual(config_version(old), config_version(config))

    def test_exact_routes_reject_subdomains_userinfo_and_redirects(self):
        p = provider()
        profiles.check_provider(p, 'kouri', p.model)
        for url in ['http://api.kouri.chat/v1', 'https://api.kouri.chat.evil.test/v1',
                    'https://api.kouri.chat@evil.test/v1', 'https://api.kouri.chat/v1?key=bad',
                    'https://api.kourichat.com/v1', 'https://api.deepseek.com/v1']:
            p.client.base_url = url
            with self.assertRaisesRegex(ValueError, 'boundary_changed'):
                profiles.check_provider(p, 'kouri', p.model)
        p.client.base_url = profiles.KOURI_BASE
        p.client._client.follow_redirects = True
        with self.assertRaises(ValueError):
            profiles.check_provider(p, 'kouri', p.model)

    async def test_key_private_round_trip_and_conflict(self):
        safe = await self.save()
        self.assertTrue(safe['kouri']['keyConfigured'])
        self.assertNotIn('synthetic-key', json.dumps(safe))
        self.assertEqual(self.plugin.config, {})
        restored = profiles.public_profiles({}, self.root)
        self.assertEqual(restored, safe)
        with self.assertRaisesRegex(ValueError, 'revision_conflict'):
            await self.save()
        deleted = await self.router.save_key({'action': 'delete', 'keyRevision': safe['kouri']['keyRevision']})
        self.assertFalse(deleted['kouri']['keyConfigured'])

    async def test_active_key_cannot_be_changed_or_deleted(self):
        safe = await self.save()
        self.plugin.config['console_profile'] = 'kouri'
        with self.assertRaisesRegex(ValueError, 'model_key_in_use'):
            await self.router.save_key({'action': 'delete', 'keyRevision': safe['kouri']['keyRevision']})
        self.assertTrue(profiles.public_profiles(self.plugin.config, self.root)['kouri']['keyConfigured'])

    async def test_invalid_or_empty_key_does_not_replace_saved_key(self):
        safe = await self.save()
        for key in ['', 'bad key', 'x\nAuthorization:bad']:
            with self.assertRaisesRegex(ValueError, 'invalid_model_key'):
                await self.router.save_key({'action': 'save', 'keyRevision': safe['kouri']['keyRevision'], 'apiKey': key})
        self.assertEqual(profiles.public_profiles({}, self.root), safe)

    async def test_missing_key_does_not_construct_client(self):
        with patch.object(profiles, 'new_provider') as build:
            with self.assertRaisesRegex(ValueError, 'model_key_required'):
                await self.router.probe('kouri', 'gemini-4-argon')
            build.assert_not_called()

    async def test_kouri_route_does_not_mutate_deepseek_or_leak_its_key(self):
        await self.save()
        self.plugin.config.update(console_profile='kouri')
        with patch.object(profiles, 'new_provider', return_value=provider()) as build:
            kouri = self.router.resolve()
            self.assertEqual(kouri.get_model(), 'gemini-4-argon')
            cfg = build.call_args.args[0]
            self.assertEqual(cfg['key'], ['synthetic-key-123456789'])
            self.assertEqual(cfg['custom_extra_body'], {})
        self.assertEqual(self.ds.get_model(), 'deepseek-flash')
        self.assertIs(self.router.deepseek(), self.ds)

    async def test_probe_uses_stream_parser_and_does_not_change_active_state(self):
        await self.save()
        candidate = provider()
        async def consume(parts, callback, **kwargs):
            self.assertTrue(kwargs['json_output'])
            self.assertFalse(parts.recent)
            await callback('{"speech":["连接')
            await callback('成功"]}')
            return SimpleNamespace(final_seen=True)
        with patch.object(profiles, 'new_provider', return_value=candidate), patch(
                'plugins.astrbot_plugin_roleplay_gateway.provider_gate.FailFastOpenAI',
                return_value=SimpleNamespace(consume=consume)):
            result = await self.router.probe('kouri', 'gemini-4-argon')
        self.assertFalse(result['sent'])
        self.assertEqual(result['reply'], '连接成功')
        self.assertEqual(profiles.selected(self.plugin.config), 'deepseek')
        candidate.client.close.assert_awaited_once()

    async def test_probe_safe_error_codes(self):
        await self.save()
        for upstream, expected in [('provider_auth_failed', 'model_auth_failed'),
                                   ('provider_model_unavailable', 'model_not_available'),
                                   ('provider_rate_limited', 'model_rate_limited'), ('attempt_timeout', 'model_test_timeout')]:
            candidate = provider()
            with patch.object(profiles, 'new_provider', return_value=candidate), patch(
                    'plugins.astrbot_plugin_roleplay_gateway.provider_gate.FailFastOpenAI',
                    return_value=SimpleNamespace(consume=AsyncMock(side_effect=ProviderAttemptError(upstream)))):
                with self.assertRaisesRegex(ValueError, expected):
                    await self.router.probe('kouri', 'gemini-4-argon')
            candidate.client.close.assert_awaited_once()
        self.assertIsNone(self.router.last_test)

    async def test_invalid_stream_format_is_not_success(self):
        await self.save()
        async def consume(parts, callback, **kwargs):
            await callback('not json')
            return SimpleNamespace(final_seen=True)
        with patch.object(profiles, 'new_provider', return_value=provider()), patch(
                'plugins.astrbot_plugin_roleplay_gateway.provider_gate.FailFastOpenAI',
                return_value=SimpleNamespace(consume=consume)):
            with self.assertRaisesRegex(ValueError, 'model_response_invalid'):
                await self.router.probe('kouri', 'gemini-4-argon')


class SwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.test_console_management import ConfigurationApplicationTests
        self.fixture = ConfigurationApplicationTests()
        await self.fixture.asyncSetUp()
        f = self.fixture
        self.kouri = provider()
        f.service._available = [object()]
        def resolve(config=None):
            config = f.plugin.config if config is None else config
            profile = profiles.selected(config)
            target = self.kouri if profile == 'kouri' else f.provider
            target.set_model(profiles.profile_models(config)[profile])
            return target
        self.router = SimpleNamespace(resolve=resolve, probe=AsyncMock(return_value={'sent': False}))
        f.plugin._profiles = lambda: self.router
        f.plugin._stream_provider = AsyncMock(side_effect=lambda **kw: resolve())

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    def request(self, profile):
        return {**self.fixture.settings_request(), 'profile': profile,
                'model': 'gemini-4-argon' if profile == 'kouri' else 'deepseek-flash'}

    async def test_round_trip_preserves_deepseek_and_discards_pooled_adapters(self):
        f = self.fixture
        result = await f.admin.execute(self.request('kouri'))
        self.assertEqual(result['modelProfiles']['active'], 'kouri')
        self.assertEqual(f.provider.get_model(), 'deepseek-flash')
        self.assertEqual(f.service._available, [])
        on_disk = json.loads(f.path.read_text(encoding='utf-8'))
        self.assertEqual(profiles.selected(on_disk), 'kouri')
        result = await f.admin.execute(self.request('deepseek'))
        self.assertEqual(result['model'], 'deepseek-flash')
        self.assertEqual(profiles.profile_models(f.config)['kouri'], 'gemini-4-argon')
        self.assertEqual(f.service.persona.persona_id, 'test-role')

    async def test_failed_probe_leaves_disk_active_model_and_pools_untouched(self):
        f = self.fixture
        before = f.path.read_bytes()
        self.router.probe.side_effect = ValueError('model_auth_failed')
        with self.assertRaisesRegex(ValueError, 'model_auth_failed'):
            await f.admin.execute(self.request('kouri'))
        self.assertEqual(f.path.read_bytes(), before)
        self.assertEqual(profiles.selected(f.config), 'deepseek')
        self.assertEqual(len(f.service._available), 1)
        self.assertFalse(f.plugin._console_busy)

    async def test_failed_write_restores_active_profile_and_model(self):
        from plugins.astrbot_plugin_roleplay_gateway import console_admin
        f = self.fixture
        before = json.loads(f.path.read_text(encoding='utf-8'))
        writer = console_admin.atomic_json
        def write(path, value):
            if path.name == 'console-names.json':
                raise OSError('synthetic failure')
            return writer(path, value)
        with patch.object(console_admin, 'atomic_json', side_effect=write):
            with self.assertRaises(OSError):
                await f.admin.execute(self.request('kouri'))
        self.assertEqual(json.loads(f.path.read_text(encoding='utf-8')), before)
        self.assertEqual(profiles.selected(f.config), 'deepseek')
        self.assertEqual(f.provider.get_model(), 'deepseek-flash')


if __name__ == '__main__':
    unittest.main()
