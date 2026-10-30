"""Adapter contracts using public API stubs; never imports or starts AstrBot."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.gate import NEUTRAL_REPLY
# Preload ordinary plugin dependencies outside patch.dict(sys.modules). Otherwise
# restoring the AstrBot stubs can remove newly imported child modules while their
# package attributes still point at the old copies, splitting later SDK patches.
from plugins.astrbot_plugin_roleplay_gateway import memory_commands  # noqa: F401
from plugins.astrbot_plugin_roleplay_gateway import weixin_adapter  # noqa: F401


class ActionFailed(Exception):
    pass


class FakeStar:
    def __init__(self, context):
        self.context = context


def load_adapter():
    modules = {name: types.ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
        "aiocqhttp", "aiocqhttp.exceptions",
    )}
    api = modules["astrbot.api"]
    api.AstrBotConfig = dict
    api.logger = types.SimpleNamespace(info=lambda text: None)

    def tagged(kind):
        def factory(value=None, **kwargs):
            def decorate(fn):
                fn.contract_filters = getattr(fn, "contract_filters", []) + [(kind, value, kwargs)]
                return fn
            return decorate
        return factory

    filter_api = types.SimpleNamespace(
        PlatformAdapterType=types.SimpleNamespace(AIOCQHTTP="aiocqhttp"),
        EventMessageType=types.SimpleNamespace(ALL="all"),
        platform_adapter_type=tagged("platform"), event_message_type=tagged("event"),
        on_astrbot_loaded=tagged("loaded"),
    )
    modules["astrbot.api.event"].filter = filter_api
    modules["astrbot.api.event"].AstrMessageEvent = object
    star = modules["astrbot.api.star"]
    star.Context = object
    star.Star = FakeStar
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: None)
    star.register = lambda *args: lambda cls: cls
    modules["aiocqhttp.exceptions"].ActionFailed = ActionFailed
    spec = importlib.util.spec_from_file_location(
        "plugins.astrbot_plugin_roleplay_gateway._adapter_contract",
        ROOT / "plugins/astrbot_plugin_roleplay_gateway/main.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


adapter = load_adapter()


class FakeBot:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.failure:
            raise self.failure
        return {"message_id": 42}


class FakeEvent:
    def __init__(self, *, kind="group", segments=None, platform="test-qq",
                 sender="100", incoming="1", group="200", bot_id="900"):
        self.platform, self.sender, self.group, self.bot_id = platform, sender, group, bot_id
        self.message_str = "你好"  # Simulate upstream wake-prefix removal.
        self.message_obj = types.SimpleNamespace(raw_message={
            "post_type": "message", "message_type": kind, "message_id": incoming,
            "message": segments if segments is not None else [
                {"type": "text", "data": {"text": "三叶你好"}},
            ],
        })
        self.bot = FakeBot()
        self.stopped = False

    def get_platform_id(self):
        return self.platform

    def get_self_id(self):
        return self.bot_id

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return self.group

    def stop_event(self):
        self.stopped = True


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-adapter-", dir=ROOT / "tests")
        self.data = Path(self.temp.name)
        adapter.StarTools.get_data_dir = lambda name: self.data
        self.config = {
            "enabled": True, "mode": "neutral_receipt", "platform_id": "test-qq",
            "bot_self_id": "900", "admin_private_ids": ["100"],
            "allowed_group_ids": ["200"], "wake_names": ["三叶"], "wake_regex": [],
        }
        self.logs = []
        self.plugin = adapter.RoleplayGateway(object(), self.config)
        self.plugin._emit = self.logs.append
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.temp.cleanup()

    async def dispatch(self, event):
        await asyncio.wait_for(self.plugin.on_message(event), 5)
        await asyncio.wait_for(self.plugin.service.wait_idle(), 5)

    async def test_original_text_and_exact_group_action_contract(self):
        event = FakeEvent(incoming="0001")
        snapshot = adapter.snapshot_event(event)
        self.assertEqual(snapshot.text, "三叶你好")
        self.assertEqual(snapshot.incoming_message_id, "1")
        await self.dispatch(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.bot.calls, [("send_group_msg", {
            "self_id": 900, "group_id": 200,
            "message": [{"type": "text", "data": {"text": NEUTRAL_REPLY}}],
        })])
        row = await self.plugin.service.store.get("test-qq", "1")
        self.assertEqual((row["status"], row["outgoing_message_id"]), ("SENT", "42"))

    async def test_private_action_and_native_processing_stopped(self):
        event = FakeEvent(kind="private", segments=[{"type": "text", "data": {"text": "hello"}}])
        await self.dispatch(event)
        self.assertTrue(event.stopped)
        action, params = event.bot.calls[0]
        self.assertEqual(action, "send_private_msg")
        self.assertEqual(params["user_id"], 100)
        self.assertNotIn("group_id", params)

    async def test_owned_denied_or_disabled_still_stop_other_instance_does_not(self):
        for event in [FakeEvent(group="999"), FakeEvent(kind="private", sender="101")]:
            await self.dispatch(event)
            self.assertTrue(event.stopped)
            self.assertEqual(event.bot.calls, [])
        self.config["enabled"] = False
        disabled = FakeEvent()
        await self.dispatch(disabled)
        self.assertTrue(disabled.stopped)
        self.assertEqual(disabled.bot.calls, [])
        for event in [FakeEvent(platform="other"), FakeEvent(bot_id="901")]:
            await self.dispatch(event)
            self.assertFalse(event.stopped)
            self.assertEqual(event.bot.calls, [])

    async def test_only_reply_and_at_all_are_not_wakes(self):
        event = FakeEvent(segments=[{"type": "reply", "data": {"id": "900"}},
                                   {"type": "at", "data": {"qq": "all"}}])
        snapshot = adapter.snapshot_event(event)
        self.assertEqual(snapshot.text, "")
        self.assertEqual(snapshot.mentions, ("all",))
        await self.dispatch(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.bot.calls, [])

    async def test_action_failed_is_definite_failure_without_exception_text(self):
        event = FakeEvent()
        event.bot.failure = ActionFailed("do-not-log-this-body")
        await self.dispatch(event)
        row = await self.plugin.service.store.get("test-qq", "1")
        self.assertEqual(row["status"], "FAILED")
        self.assertNotIn("do-not-log-this-body", repr(self.logs))

    async def test_bad_raw_event_still_stops(self):
        event = FakeEvent()
        event.message_obj.raw_message = {"message": "not-an-array"}
        await self.dispatch(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.bot.calls, [])
        self.assertEqual(self.logs[-1]["code"], "invalid_onebot_event")

    async def test_declared_filters_priority_and_single_argument_logging(self):
        self.assertEqual(adapter.RoleplayGateway.on_message.contract_filters,
                         [("event", "all", {"priority": 10000}),
                          ("platform", "aiocqhttp", {})])
        messages = []
        with patch.object(adapter.logger, "info", messages.append):
            adapter.RoleplayGateway._emit({"code": "contract-test"})
        self.assertEqual(messages, ['roleplay_gateway {"code": "contract-test"}'])


class StreamAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_provider_gate import Backend, FakeProvider, FakeClient, FakeStream, response
        from plugins.astrbot_plugin_roleplay_gateway import provider_gate as pg
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="stream-adapter-", dir=ROOT / "tests")
        adapter.StarTools.get_data_dir = lambda name: Path(self.temp.name)
        self.backend = Backend(FakeStream([response("中性模型测试成功。"), response("中性模型测试成功。", chunk=False)]))
        self.provider = FakeProvider(self.backend)
        self.provider.provider_config["id"] = "deepseek-flash-official"
        self.provider.client.base_url = "https://api.deepseek.com/v1"
        self.provider.get_model = lambda: "deepseek-flash"
        self.context = types.SimpleNamespace(get_provider_by_id=lambda name: self.provider)
        self.contract = patch.object(pg, "_runtime_contract", return_value=(
            pg.ASTRBOT_VERSION, pg.OPENAI_VERSION, FakeProvider, FakeClient))
        self.contract.start()
        self.config = {"enabled": True, "mode": "neutral_stream", "platform_id": "test-qq",
                       "bot_self_id": "900", "admin_private_ids": ["100"],
                       "allowed_group_ids": ["200"], "wake_names": ["三叶"], "wake_regex": []}
        self.plugin = adapter.RoleplayGateway(self.context, self.config)
        self.logs = []
        self.plugin._emit = self.logs.append

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.contract.stop()
        self.temp.cleanup()

    async def test_stream_entrypoint_calls_model_once_and_preserves_send_receipt(self):
        await self.plugin.initialize()
        self.assertEqual(self.backend.calls, [])
        event = FakeEvent(kind="private", segments=[{"type":"text", "data":{"text":"中性测试"}}])
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        self.assertTrue(event.stopped)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(event.bot.calls[0][0], "send_private_msg")
        self.assertEqual(event.bot.calls[0][1]["message"][0]["data"]["text"], "中性模型测试成功。")
        row = await self.plugin.service.store.get("test-qq", "1")
        self.assertEqual((row["status"],row["outgoing_message_id"]), ("SENT","42"))
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertNotIn("中性模型测试成功", repr(self.logs))

    async def test_stream_provider_wrong_endpoint_rejected_before_any_request(self):
        self.provider.client.base_url = "https://unapproved.invalid/v1"
        with self.assertRaises(ValueError):
            await self.plugin.initialize()
        self.assertIsNone(self.plugin.service)
        self.assertEqual(self.backend.calls, [])

    async def test_memory_command_never_falls_through_to_neutral_model(self):
        await self.plugin.initialize()
        event = FakeEvent(kind="private", segments=[{"type": "text", "data": {"text": "/记忆 查看"}}])
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        self.assertTrue(event.stopped)
        self.assertEqual(event.bot.calls, [])
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.logs[-1]["code"], "memory_command_requires_roleplay")

    async def test_qq_bubble_boundary_lines_removed_for_private_and_group(self):
        for kind, incoming in (("private", "101"), ("group", "102")):
            if self.plugin.service is not None:
                await self.plugin.terminate()
                self.plugin = adapter.RoleplayGateway(self.context, self.config)
                self.plugin._emit = self.logs.append
            await self.plugin.initialize()
            service = self.plugin.service
            body = "  正文保留缩进"

            async def accept(event, send):
                await send(event, "\n\n" + body + "\n\n")

            with patch.object(service, "accept", accept):
                event = FakeEvent(kind=kind, incoming=incoming)
                await self.plugin.on_message(event)
            self.assertEqual(event.bot.calls[0][1]["message"][0]["data"]["text"], body)
        self.assertEqual(self.backend.calls, [])

    async def test_live_mode_change_stops_stream_calls_until_reload(self):
        await self.plugin.initialize()
        self.config["mode"] = "neutral_receipt"
        event = FakeEvent()
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        self.assertTrue(event.stopped)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(event.bot.calls, [])

    async def test_blocked_guard_also_prevents_receipt_mode_reload(self):
        from plugins.astrbot_plugin_roleplay_gateway.neutral_stream import StreamSafetyGuard
        guard = StreamSafetyGuard()
        guard.block(object())
        self.context._roleplay_stream_safety_guard = guard
        self.config["mode"] = "neutral_receipt"
        self.plugin = adapter.RoleplayGateway(self.context, self.config)
        with self.assertRaisesRegex(RuntimeError, "restart_required"):
            await self.plugin.initialize()
        self.assertIsNone(self.plugin.service)
        self.assertEqual(self.backend.calls, [])

    async def test_pooled_adapter_rechecks_endpoint_before_second_input(self):
        await self.plugin.initialize()
        first = FakeEvent(kind="private", incoming="11", segments=[{"type":"text", "data":{"text":"test"}}])
        await self.plugin.on_message(first)
        await self.plugin.service.wait_idle()
        self.assertEqual(len(self.backend.calls), 1)
        self.provider.client.base_url = "https://unapproved.invalid/v1"
        second = FakeEvent(kind="private", incoming="12", segments=[{"type":"text", "data":{"text":"test"}}])
        await self.plugin.on_message(second)
        await self.plugin.service.wait_idle()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(second.bot.calls, [])

    async def test_async_provider_lookup_and_context_guard_survive_reload(self):
        async def getter(name):
            return self.provider
        self.context.get_provider_by_id = getter
        await self.plugin.initialize()
        guard = self.context._roleplay_stream_safety_guard
        await self.plugin.terminate()
        self.plugin = adapter.RoleplayGateway(self.context, self.config)
        await self.plugin.initialize()
        self.assertIs(self.context._roleplay_stream_safety_guard, guard)
        self.assertEqual(self.backend.calls, [])

    async def test_cold_start_defers_until_loaded_hook_and_owns_early_events(self):
        self.context.get_provider_by_id = lambda name: None
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.service)
        self.assertEqual(self.logs[-1]["code"], "waiting_for_providers")
        event = FakeEvent()
        await self.plugin.on_message(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.bot.calls, [])
        self.assertEqual(self.backend.calls, [])
        self.context.get_provider_by_id = lambda name: self.provider
        await self.plugin.on_astrbot_loaded()
        self.assertIsNotNone(self.plugin.service)
        self.assertEqual(self.logs[-1]["code"], "initialized")
        self.assertEqual(self.backend.calls, [])

    async def test_provider_still_missing_after_loaded_fails_closed(self):
        self.context.get_provider_by_id = lambda name: None
        await self.plugin.initialize()
        with self.assertRaisesRegex(ValueError, "neutral_stream_provider_missing"):
            await self.plugin.on_astrbot_loaded()
        self.assertIsNone(self.plugin.service)
        self.assertEqual(self.backend.calls, [])

    async def test_loaded_hook_does_not_reopen_store_or_resurrect_terminated_plugin(self):
        await self.plugin.initialize()
        service = self.plugin.service
        await asyncio.gather(self.plugin.on_astrbot_loaded(), self.plugin.initialize())
        self.assertIs(self.plugin.service, service)
        self.assertEqual(sum(row.get("code") == "initialized" for row in self.logs), 1)
        await self.plugin.terminate()
        await self.plugin.on_astrbot_loaded()
        self.assertIsNone(self.plugin.service)
        self.assertEqual(self.backend.calls, [])

    async def test_deferred_load_rechecks_provider_boundary(self):
        self.context.get_provider_by_id = lambda name: None
        await self.plugin.initialize()
        self.provider.client.base_url = "https://unapproved.invalid/v1"
        self.context.get_provider_by_id = lambda name: self.provider
        with self.assertRaisesRegex(ValueError, "neutral_stream_provider_boundary_changed"):
            await self.plugin.on_astrbot_loaded()
        self.assertIsNone(self.plugin.service)
        self.assertEqual(self.backend.calls, [])


class RoleplayAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await StreamAdapterTests.asyncSetUp(self)
        from test_provider_gate import FakeStream, response
        self.backend.stream = FakeStream([response('{"speech":["中性模型测试成功。"]}'), response("", chunk=False)])
        self.data_root = Path(self.temp.name) / "data"
        self.plugin_data = self.data_root / "plugin_data" / adapter.PLUGIN_NAME
        adapter.StarTools.get_data_dir = lambda name: self.plugin_data
        self.persona_path = self.data_root / "roleplay/personas/test-character.txt"
        self.persona_path.parent.mkdir(parents=True)
        self.persona_path.write_text("你是热爱天文的图书馆向导，认真解释问题。", encoding="utf-8")
        self.config.update(mode="roleplay_stream", persona_id="library-guide",
                           persona_name="图书馆向导", persona_filename=self.persona_path.name,
                           persona_sha256=hashlib.sha256(self.persona_path.read_bytes()).hexdigest())
        self.plugin = adapter.RoleplayGateway(self.context, self.config)
        self.plugin._emit = self.logs.append

    async def asyncTearDown(self):
        await StreamAdapterTests.asyncTearDown(self)

    async def test_cached_persona_and_durable_confirmed_reply(self):
        await self.plugin.initialize()
        self.assertEqual(self.backend.calls, [])
        event = FakeEvent(kind="private")
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        snapshot = await self.plugin.service.memory.snapshot(self.plugin.service.namespace(adapter.snapshot_event(event)))
        self.assertEqual(len(snapshot.turns), 1)
        self.assertEqual(snapshot.turns[0].replies[0].message_id, "42")
        self.assertEqual(self.backend.calls[0][2]["extra_body"]["response_format"], {"type": "json_object"})
        self.assertIn("图书馆向导", repr(self.backend.calls))
        self.assertNotIn("图书馆向导", repr(self.logs))

    async def test_changed_runtime_persona_hash_fails_before_model_or_memory_open(self):
        self.persona_path.write_text("修改后的中性卡", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "persona_revision_changed"):
            await self.plugin.initialize()
        self.assertIsNone(self.plugin.service)
        self.assertEqual(self.backend.calls, [])
        self.assertFalse((self.plugin_data / "memory.sqlite3").exists())

    async def test_cold_start_resolves_roleplay_after_providers_load(self):
        self.context.get_provider_by_id = lambda name: None
        await self.plugin.initialize()
        self.assertIsNone(self.plugin.service)
        self.context.get_provider_by_id = lambda name: self.provider
        await self.plugin.on_astrbot_loaded()
        self.assertIsNotNone(self.plugin.service)
        self.assertEqual(self.logs[-1]["mode"], "roleplay_stream")
        self.assertEqual(self.backend.calls, [])

    async def test_live_persona_selection_change_requires_reload(self):
        await self.plugin.initialize()
        self.config["persona_id"] = "another-guide"
        event = FakeEvent(kind="private")
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        self.assertTrue(event.stopped)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(event.bot.calls, [])

    async def test_live_memory_permission_change_requires_reload(self):
        await self.plugin.initialize()
        for value in (["100"], None, "100", [["100"]]):
            with self.subTest(value=value):
                self.config["memory_admin_ids"] = value
                event = FakeEvent(kind="private")
                await self.plugin.on_message(event)
                await self.plugin.service.wait_idle()
                self.assertTrue(event.stopped)
                self.assertEqual(event.bot.calls, [])
                self.assertEqual(self.backend.calls, [])

    async def test_private_memory_command_uses_store_and_onebot_not_model(self):
        await self.plugin.initialize()
        event = FakeEvent(kind="private", incoming="501", segments=[
            {"type": "text", "data": {"text": "/记忆 编辑 中性标记：青石。"}},
        ])
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        namespace = self.plugin.service.namespace(adapter.snapshot_event(event))
        snapshot = await self.plugin.service.memory.snapshot(namespace)
        self.assertEqual(snapshot.summary, "中性标记：青石。")
        self.assertEqual(snapshot.turns, ())
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(event.bot.calls), 1)
        self.assertEqual(event.bot.calls[0][0], "send_private_msg")
        self.assertEqual(event.bot.calls[0][1]["user_id"], 100)
        self.assertNotIn("青石", repr(self.logs))

    async def test_private_allowlist_does_not_grant_group_memory_permission(self):
        await self.plugin.initialize()
        event = FakeEvent(kind="group", incoming="502", segments=[
            {"type": "at", "data": {"qq": "900"}},
            {"type": "text", "data": {"text": "/记忆 清空全部"}},
        ])
        namespace = self.plugin.service.namespace(adapter.snapshot_event(event))
        await self.plugin.service.memory.edit_summary(namespace, "保留的群摘要")
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        snapshot = await self.plugin.service.memory.snapshot(namespace)
        self.assertEqual(snapshot.summary, "保留的群摘要")
        self.assertEqual(snapshot.epoch, 0)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(event.bot.calls), 1)
        self.assertEqual(event.bot.calls[0][0], "send_group_msg")
        self.assertNotIn("保留的群摘要", repr(event.bot.calls))

    async def test_roleplay_configuration_rejects_missing_or_traversing_persona(self):
        for field, value in (("persona_filename", "../outside.txt"), ("persona_id", ""),
                             ("persona_sha256", "not-a-hash"), ("persona_name", "bad\nname")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                adapter.RoleplayGateway(self.context, {**self.config, field: value})


class WechatGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_weixin_adapter import FakeWeixinEvent, native_contract
        await RoleplayAdapterTests.asyncSetUp(self)
        self.native = patch.object(weixin_adapter, "_runtime_contract", native_contract)
        self.native.start()
        self.event_class = FakeWeixinEvent
        self.config.update(wechat_enabled=True, wechat_platform_id="test-wechat",
                           wechat_allowed_private_ids=["user-abc@im.wechat"],
                           wechat_admin_ids=["user-abc@im.wechat"])
        self.plugin = adapter.RoleplayGateway(self.context, self.config)
        self.plugin._emit = self.logs.append

    async def asyncTearDown(self):
        await RoleplayAdapterTests.asyncTearDown(self)
        self.native.stop()

    async def test_both_services_share_parallel_limit_but_not_store_or_guard(self):
        await self.plugin.initialize()
        qq, wx = self.plugin.service, self.plugin.wechat_service
        self.assertIsNotNone(wx)
        self.assertIs(qq._parallel, wx._parallel)
        self.assertEqual(qq._parallel._value, 3)
        self.assertIsNot(qq.guard, wx.guard)
        self.assertEqual(qq.store.path.parent, self.plugin_data)
        self.assertEqual(wx.store.path.parent, self.plugin_data / "weixin")
        self.assertNotEqual(qq.memory.path, wx.memory.path)

    async def test_native_api_success_stores_no_fake_message_id_and_deduplicates(self):
        await self.plugin.initialize()
        event = self.event_class()
        await self.plugin.on_wechat_message(event)
        await self.plugin.wechat_service.wait_idle()
        self.assertTrue(event.stopped)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(event.platform.calls), 1)
        service = self.plugin.wechat_service
        row = await service.store.get("test-wechat", "wx:message-123")
        self.assertEqual(row["status"], "API_ACCEPTED")
        self.assertIsNone(row["outgoing_message_id"])
        self.assertTrue(row["delivery_ref"])
        envelope = weixin_adapter.snapshot_weixin_event(event, platform_id="test-wechat")
        snapshot = await service.memory.snapshot(service.namespace(envelope))
        self.assertEqual(len(snapshot.turns), 1)
        self.assertIsNone(snapshot.turns[0].replies[0].message_id)
        self.assertEqual(snapshot.turns[0].replies[0].delivery_status, "API_ACCEPTED")
        await self.plugin.on_wechat_message(event)
        await service.wait_idle()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(event.platform.calls), 1)

    async def test_disabled_default_owns_wechat_without_creating_store_or_calling_model(self):
        self.config["wechat_enabled"] = False
        await self.plugin.initialize()
        event = self.event_class()
        await self.plugin.on_wechat_message(event)
        self.assertTrue(event.stopped)
        self.assertIsNone(self.plugin.wechat_service)
        self.assertFalse((self.plugin_data / "weixin").exists())
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(event.platform.calls, [])

    async def test_other_instance_untouched_and_unauthorized_private_denied(self):
        await self.plugin.initialize()
        unrelated = self.event_class(platform_id="unmanaged")
        await self.plugin.on_wechat_message(unrelated)
        self.assertFalse(unrelated.stopped)
        denied = self.event_class(sender="outsider@im.wechat")
        await self.plugin.on_wechat_message(denied)
        await self.plugin.wechat_service.wait_idle()
        self.assertTrue(denied.stopped)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(denied.platform.calls, [])

    async def test_invalid_raw_id_and_group_never_reach_model(self):
        await self.plugin.initialize()
        missing = self.event_class()
        missing.message_obj.raw_message.pop("message_id")
        group = self.event_class()
        group.message_obj.group_id = "wx-group"
        for event in (missing, group):
            await self.plugin.on_wechat_message(event)
            self.assertTrue(event.stopped)
            self.assertEqual(event.platform.calls, [])
        self.assertEqual(self.backend.calls, [])

    async def test_current_wechat_memory_command_cannot_change_qq_summary(self):
        await self.plugin.initialize()
        qq_event = FakeEvent(kind="private", incoming="1001")
        qq_namespace = self.plugin.service.namespace(adapter.snapshot_event(qq_event))
        await self.plugin.service.memory.edit_summary(qq_namespace, "QQ独有青柚灯塔")
        event = self.event_class(text="/记忆 编辑 微信独有蓝桥")
        wx = self.plugin.wechat_service
        envelope = weixin_adapter.snapshot_weixin_event(event, platform_id="test-wechat")
        await self.plugin.on_wechat_message(event)
        await wx.wait_idle()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(event.platform.calls), 1)
        self.assertNotIn("青柚灯塔", repr(event.platform.calls))
        self.assertEqual((await wx.memory.snapshot(wx.namespace(envelope))).summary, "微信独有蓝桥")
        self.assertEqual((await self.plugin.service.memory.snapshot(qq_namespace)).summary, "QQ独有青柚灯塔")

    async def test_qq_history_absent_from_wechat_model_context(self):
        await self.plugin.initialize()
        qq_event = FakeEvent(kind="private", incoming="1001")
        qq_namespace = self.plugin.service.namespace(adapter.snapshot_event(qq_event))
        await self.plugin.service.memory.edit_summary(qq_namespace, "QQ独有青柚灯塔")
        event = self.event_class(text="你记得什么？")
        await self.plugin.on_wechat_message(event)
        await self.plugin.wechat_service.wait_idle()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertNotIn("青柚灯塔", repr(self.backend.calls))

    async def test_wechat_close_does_not_close_qq(self):
        await self.plugin.initialize()
        await self.plugin.wechat_service.close()
        self.plugin.wechat_service = None
        event = FakeEvent(kind="private", incoming="1001")
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        self.assertEqual(len(event.bot.calls), 1)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_wechat_contract_failure_keeps_qq_working(self):
        with patch.object(weixin_adapter, "check_runtime_contract", side_effect=ValueError("contract")):
            await self.plugin.initialize()
        self.assertIsNotNone(self.plugin.service)
        self.assertIsNone(self.plugin.wechat_service)
        self.assertTrue(any(row["code"] == "wechat_initialization_failed" for row in self.logs))
        event = FakeEvent(kind="private")
        await self.plugin.on_message(event)
        await self.plugin.service.wait_idle()
        self.assertEqual(len(event.bot.calls), 1)

    async def test_live_wechat_access_change_blocks_until_reload_and_qq_continues(self):
        await self.plugin.initialize()
        self.config["wechat_allowed_private_ids"] = ["other@im.wechat"]
        wx_event = self.event_class()
        await self.plugin.on_wechat_message(wx_event)
        self.assertTrue(wx_event.stopped)
        self.assertEqual(wx_event.platform.calls, [])
        qq_event = FakeEvent(kind="private")
        await self.plugin.on_message(qq_event)
        await self.plugin.service.wait_idle()
        self.assertEqual(len(qq_event.bot.calls), 1)

    async def test_repeated_startup_does_not_reopen_wechat_store(self):
        await self.plugin.initialize()
        wx = self.plugin.wechat_service
        await asyncio.gather(self.plugin.initialize(), self.plugin.on_astrbot_loaded())
        self.assertIs(self.plugin.wechat_service, wx)
        await self.plugin.terminate()
        await self.plugin.on_astrbot_loaded()
        self.assertIsNone(self.plugin.service)
        self.assertIsNone(self.plugin.wechat_service)

    async def test_context_parallel_budget_survives_plugin_reload(self):
        await self.plugin.initialize()
        shared = self.plugin._shared_parallel
        await self.plugin.terminate()
        self.plugin = adapter.RoleplayGateway(self.context, self.config)
        await self.plugin.initialize()
        self.assertIs(self.plugin._shared_parallel, shared)
        self.assertIs(self.plugin.service._parallel, shared)
        self.assertIs(self.plugin.wechat_service._parallel, shared)

    async def test_live_invalid_wechat_admin_configuration_fails_closed(self):
        await self.plugin.initialize()
        self.config["wechat_admin_ids"] = ["not-allowed"]
        event = self.event_class()
        await self.plugin.on_wechat_message(event)
        self.assertTrue(event.stopped)
        self.assertEqual(event.platform.calls, [])
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
