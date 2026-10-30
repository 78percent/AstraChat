"""Integration of the third service with the existing plugin's lifecycle."""
from unittest.mock import patch
import unittest
import test_gateway_adapter as fixture
from plugins.astrbot_plugin_roleplay_gateway import native_bridge


class FakeServer:
    fail = False

    def __init__(self, settings, token, *, accept, emit, voice_enabled=False):
        self.settings, self.accept = settings, accept
        self.voice_enabled = voice_enabled
        self.started = self.closed = False

    async def start(self, *, host, port):
        if self.fail:
            raise OSError("fixture_bind_failed")
        self.started = host == "0.0.0.0" and port == 6198

    async def close(self):
        self.closed = True


class NativePluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.RoleplayAdapterTests.asyncSetUp(self)
        self.config.update(native_enabled=True, native_self_id="wxid_bot",
                           native_allowed_group_ids=["123@chatroom"], native_admin_ids=[],
                           native_bridge_token="a"*64, native_reply_probability=1.0)
        self.plugin = fixture.adapter.RoleplayGateway(self.context, self.config)
        self.plugin._emit = self.logs.append
        FakeServer.fail = False
        self.server_patch = patch.object(native_bridge, "NativeBridgeServer", FakeServer)
        self.server_patch.start()

    async def asyncTearDown(self):
        await fixture.RoleplayAdapterTests.asyncTearDown(self)
        self.server_patch.stop()
        FakeServer.fail = False

    async def test_separate_memory_and_shared_parallel_limit(self):
        await self.plugin.initialize()
        self.assertTrue(self.plugin.native_server.started)
        self.assertEqual(self.plugin.native_service.settings.reply_pacing.probability, 1.0)
        self.assertEqual(self.plugin.service.settings.reply_pacing.probability, .6)
        self.assertEqual(self.plugin.native_service.memory.path.parent, self.plugin_data / "native")
        self.assertIs(self.plugin.native_service._parallel, self.plugin.service._parallel)
        self.assertTrue(self.plugin._native_enabled())
        native = self.plugin.native_server
        await self.plugin.terminate()
        self.assertTrue(native.closed)
        self.assertIsNone(self.plugin.native_service)
        self.assertIsNone(self.plugin.native_server)

    async def test_native_failure_keeps_qq_alive(self):
        FakeServer.fail = True
        await self.plugin.initialize()
        self.assertIsNotNone(self.plugin.service)
        self.assertIsNone(self.plugin.native_service)
        self.assertIsNone(self.plugin.native_server)
        self.assertTrue(any(row.get("code") == "native_initialization_failed" for row in self.logs))

    async def test_binding_or_probability_change_disables_until_reload(self):
        await self.plugin.initialize()
        for key, value in (("native_self_id", "wxid_other"), ("native_reply_probability", .5),
                           ("native_bridge_token", "b"*64), ("native_enabled", False)):
            old = self.config[key]
            self.config[key] = value
            self.assertFalse(self.plugin._native_enabled())
            self.config[key] = old
        self.assertTrue(self.plugin._native_enabled())

    async def test_cold_start_defers_native_socket_until_provider_ready(self):
        self.context.get_provider_by_id = lambda name: None
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.native_server)
        self.context.get_provider_by_id = lambda name: self.provider
        await self.plugin.on_astrbot_loaded()
        self.assertTrue(self.plugin.native_server.started)
        original = self.plugin.native_server
        await self.plugin.on_astrbot_loaded()
        self.assertIs(self.plugin.native_server, original)

    async def test_voice_addressee_flag_is_native_only_and_reload_bound(self):
        self.config.update(native_voice_enabled=True, native_group_voice_ai_wake=True)
        await self.plugin.initialize()
        self.assertTrue(self.plugin.native_service.group_voice_ai_wake)
        self.assertFalse(self.plugin.service.group_voice_ai_wake)
        self.assertTrue(self.plugin._native_enabled())
        self.config['native_group_voice_ai_wake'] = False
        self.assertFalse(self.plugin._native_enabled())

    async def test_voice_addressee_requires_voice_and_strict_boolean(self):
        self.config.update(native_voice_enabled=False, native_group_voice_ai_wake=True)
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.native_service)
        self.assertIsNotNone(self.plugin.service)
        self.config.update(native_voice_enabled=True, native_group_voice_ai_wake='true')
        await self.plugin._initialize_native_service(allow_missing=False)
        self.assertIsNone(self.plugin.native_service)
