"""Readiness uses temporary service stores, never logs or platform accounts."""
import json
import os
import unittest
from unittest.mock import patch

import test_gateway_adapter as fixture
from test_native_plugin_lifecycle import FakeServer
from test_weixin_adapter import native_contract
from plugins.astrbot_plugin_roleplay_gateway import native_bridge, online_tools, weixin_adapter
from plugins.astrbot_plugin_roleplay_gateway.gate import GateService
from plugins.astrbot_plugin_roleplay_gateway.online_lookup import LookupSettings, OnlineLookupService


class StatusFakeServer(FakeServer):
    def __init__(self, settings, token, *, accept, emit, research_enabled=False):
        super().__init__(settings, token, accept=accept, emit=emit)


class RuntimeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.RoleplayAdapterTests.asyncSetUp(self)
        self.config.update(
            wechat_enabled=True, wechat_platform_id="test-wechat", wechat_admin_ids=["wx-user"],
            wechat_allowed_private_ids=["wx-user"],
            native_enabled=True, native_self_id="wxid_bot", native_allowed_group_ids=["123@chatroom"],
            native_admin_ids=[], native_bridge_token="a" * 64, native_reply_probability=1.0,
        )
        self.plugin = fixture.adapter.RoleplayGateway(self.context, self.config)
        self.plugin._emit = self.logs.append
        self.patches = [patch.object(native_bridge, "NativeBridgeServer", StatusFakeServer),
                        patch.object(weixin_adapter, "_runtime_contract", native_contract)]
        FakeServer.fail = False
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        await fixture.RoleplayAdapterTests.asyncTearDown(self)
        for item in reversed(self.patches):
            item.stop()
        FakeServer.fail = False

    def status(self):
        return json.loads((self.plugin_data / "runtime-status.json").read_text(encoding="utf-8"))

    async def test_all_three_ready_without_info_logs_and_no_sensitive_fields(self):
        self.plugin._emit = fixture.adapter.RoleplayGateway._emit
        with patch.object(fixture.adapter.logger, "info", return_value=None):
            await self.plugin.initialize()
        status = self.status()
        self.assertEqual(status["schema_version"], 1)
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["phase"], "initialized")
        self.assertFalse(status["terminated"])
        self.assertFalse(status["lookup_enabled"])
        self.assertEqual(set(status["services"]), {"qq", "wechat", "native"})
        for state in status["services"].values():
            self.assertEqual(state, {"ready": True, "enabled": True, "lookup_enabled": False})
        serialized = json.dumps(status)
        for private in ("wxid_bot", "123@chatroom", "test-wechat", "library-guide", "a" * 64):
            self.assertNotIn(private, serialized)
        self.assertEqual(self.backend.calls, [])

    async def test_lookup_flag_describes_each_constructed_service(self):
        self.config["online_lookup"] = {"enabled": True}
        self.plugin = fixture.adapter.RoleplayGateway(self.context, self.config)
        self.plugin._emit = self.logs.append

        async def search(_query):
            raise AssertionError("startup must not search")

        def factory(*_args):
            return OnlineLookupService(LookupSettings(enabled=True), search=search)

        with patch.object(online_tools, "create_lookup_service", side_effect=factory):
            await self.plugin.initialize()
        status = self.status()
        self.assertTrue(status["lookup_enabled"])
        self.assertTrue(all(row["lookup_enabled"] for row in status["services"].values()))

    async def test_provider_deferred_then_loaded_refreshes_same_instance(self):
        self.context.get_provider_by_id = lambda _: None
        await self.plugin.initialize()
        first = self.status()
        self.assertFalse(any(row["ready"] for row in first["services"].values()))
        self.context.get_provider_by_id = lambda _: self.provider
        await self.plugin.on_astrbot_loaded()
        loaded = self.status()
        self.assertEqual(loaded["instance_id"], first["instance_id"])
        self.assertEqual(loaded["started_at_unix"], first["started_at_unix"])
        self.assertGreaterEqual(loaded["updated_at_unix"], first["updated_at_unix"])
        self.assertEqual(loaded["phase"], "loaded")
        self.assertTrue(all(row["ready"] for row in loaded["services"].values()))

    async def test_native_failure_does_not_claim_ready_or_hide_other_routes(self):
        FakeServer.fail = True
        await self.plugin.initialize()
        states = self.status()["services"]
        self.assertTrue(states["qq"]["ready"])
        self.assertTrue(states["wechat"]["ready"])
        self.assertFalse(states["native"]["ready"])
        self.assertFalse(states["native"]["lookup_enabled"])

    async def test_qq_initialization_failure_still_writes_not_ready(self):
        self.persona_path.write_text("changed neutral fixture", encoding="utf-8")
        with self.assertRaises(ValueError):
            await self.plugin.initialize()
        self.assertFalse(any(row["ready"] for row in self.status()["services"].values()))

    async def test_termination_is_not_ready_even_when_a_close_raises(self):
        await self.plugin.initialize()
        original = self.plugin.service.close

        async def close_then_fail():
            await original()
            raise RuntimeError("fixture")

        with patch.object(self.plugin.service, "close", side_effect=close_then_fail):
            with self.assertRaises(RuntimeError):
                await self.plugin.terminate()
        status = self.status()
        self.assertTrue(status["terminated"])
        self.assertEqual(status["phase"], "terminated")
        self.assertFalse(any(any(row.values()) for row in status["services"].values()))

    async def test_real_fixed_receipt_fallback_without_lookup_or_model(self):
        self.config.update(mode="neutral_receipt", wechat_enabled=False, native_enabled=False)
        self.plugin = fixture.adapter.RoleplayGateway(self.context, self.config)
        self.plugin._emit = self.logs.append
        await self.plugin.initialize()
        self.assertIs(type(self.plugin.service), GateService)
        status = self.status()
        self.assertEqual(status["services"]["qq"], {"ready": True, "enabled": True, "lookup_enabled": False})
        self.assertFalse(status["services"]["wechat"]["ready"])
        self.assertFalse(status["services"]["native"]["ready"])
        self.assertEqual(self.backend.calls, [])

    async def test_old_instance_close_cannot_replace_newer_snapshot(self):
        await self.plugin.initialize()
        newer = fixture.adapter.RoleplayGateway(self.context, self.config)
        newer._write_runtime_status("initializing")
        expected = self.status()
        await self.plugin.terminate()
        self.assertEqual(self.status(), expected)

    async def test_status_io_failure_does_not_break_service_startup(self):
        with patch.object(fixture.adapter.os, "replace", side_effect=PermissionError):
            await self.plugin.initialize()
        self.assertIsNotNone(self.plugin.service)
        self.assertIsNotNone(self.plugin.wechat_service)
        self.assertIsNotNone(self.plugin.native_service)
        self.assertTrue(any(row.get("code") == "runtime_status_write_failed" for row in self.logs))
        self.assertFalse(list(self.plugin_data.glob(".runtime-status-*.tmp")))


if __name__ == "__main__":
    unittest.main()
