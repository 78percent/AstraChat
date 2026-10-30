"""Native search adapters use synthetic secrets and fake tools, never the network."""
import asyncio
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.astrbot_plugin_roleplay_gateway import online_tools as tools


KEY = "synthetic-deepseek-key-not-real"


def provider():
    return SimpleNamespace(client=SimpleNamespace(base_url="https://api.deepseek.com/v1/", api_key=KEY),
                           provider_config={"provider": "deepseek", "type": "openai_chat_completion"})


class FakeTool:
    name = "web_search_deepseek"

    def __init__(self):
        self.calls = []
        self.reply = json.dumps({"results": [{"title": "Official docs", "url": "https://docs.python.org/",
                                             "snippet": "Public example", "index": "a.1"}]})
        self.failure = None

    async def call(self, context, **kwargs):
        self.calls.append((context, kwargs))
        if self.failure:
            raise self.failure
        return self.reply


class NativeSearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tool, self.provider = FakeTool(), provider()
        with patch.object(tools, "_load_native_search", return_value=self.tool):
            self.search = tools.NativeDeepSeekSearch(object(), self.provider)

    async def test_only_public_query_and_isolated_configuration_reach_native_tool(self):
        rows = await self.search("TaskGroup site:docs.python.org")
        self.assertEqual(len(self.tool.calls), 1)
        context, params = self.tool.calls[0]
        self.assertEqual(params, {"query": "TaskGroup site:docs.python.org", "count": 3, "need_content": False})
        self.assertEqual(vars(context.context.event), {"unified_msg_origin": "roleplay:lookup:isolated"})
        cfg = context.context.context.get_config(umo="roleplay:lookup:isolated")
        self.assertEqual(cfg, {"provider_settings": {"web_search": True,
            "websearch_provider": "deepseek", "websearch_deepseek_key": [KEY]}})
        self.assertNotIn(KEY, repr(rows))
        self.assertFalse(hasattr(context.context.event, "send"))
        cfg["provider_settings"]["websearch_deepseek_key"][0] = "changed"
        self.assertEqual(context.context.context.get_config(umo="roleplay:lookup:isolated")
                         ["provider_settings"]["websearch_deepseek_key"], [KEY])

    async def test_query_limits_reject_before_tool_call(self):
        for query in (None, "", "   ", "x" * 129, "text\0tail"):
            with self.subTest(query=repr(query)):
                with self.assertRaisesRegex(tools.NativeSearchUnavailable, "query_invalid"):
                    await self.search(query)
        self.assertEqual(self.tool.calls, [])

    async def test_native_error_is_static_and_not_retried(self):
        self.tool.failure = RuntimeError("echoed request " + KEY + " private text")
        with self.assertRaises(tools.NativeSearchUnavailable) as raised:
            await self.search("public query")
        self.assertEqual(str(raised.exception), "native_search_failed")
        self.assertEqual(len(self.tool.calls), 1)
        self.assertNotIn(KEY, str(raised.exception))

    async def test_invalid_or_oversized_native_outputs_do_not_escape(self):
        for value in ("Error: " + KEY, None, "x" * 262145,
                      json.dumps({"results": "bad"}),
                      json.dumps({"results": [{"url": "x", "snippet": 1}]}),
                      json.dumps({"results": [{}] * 4}),
                      json.dumps({"results": [], "extra": "bad"})):
            self.tool.reply = value
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(tools.NativeSearchUnavailable, "native_search_failed"):
                    await self.search("public query")

    async def test_excerpt_and_title_are_bounded(self):
        self.tool.reply = json.dumps({"results": [{"title": "t" * 1000, "url": "https://example.com",
                                                  "snippet": "s" * 20000, "index": "1"}]})
        rows = await self.search("public query")
        self.assertEqual(len(rows[0]["title"]), 300)
        self.assertEqual(len(rows[0]["snippet"]), 6000)

    async def test_cancellation_reaches_native_resource_finally(self):
        entered, settled = asyncio.Event(), asyncio.Event()
        async def wait(context, **kwargs):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                settled.set()
        self.tool.call = wait
        pending = asyncio.create_task(self.search("public query"))
        try:
            await asyncio.wait_for(entered.wait(), .5)
        except TimeoutError:
            if pending.done():
                pending.result()
            raise
        finally:
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
        self.assertTrue(settled.is_set())

    async def test_route_and_key_revalidated_per_call(self):
        self.provider.client.base_url = "https://relay.example/v1/"
        with self.assertRaisesRegex(tools.NativeSearchUnavailable, "provider_unavailable"):
            await self.search("public query")
        self.assertFalse(self.tool.calls)
        self.provider.client.base_url = "https://api.deepseek.com/"
        self.provider.client.api_key = "$ROLEPLAY_DEEPSEEK_API_KEY"
        with self.assertRaisesRegex(tools.NativeSearchUnavailable, "provider_unavailable"):
            await self.search("public query")
        self.provider.client.api_key = KEY + "-rotated"
        await self.search("public query")
        context, _ = self.tool.calls[0]
        self.assertEqual(context.context.context.get_config(umo="roleplay:lookup:isolated")
                         ["provider_settings"]["websearch_deepseek_key"], [KEY + "-rotated"])

    async def test_foreign_context_origin_is_rejected(self):
        await self.search("public query")
        context, _ = self.tool.calls[0]
        with self.assertRaisesRegex(tools.NativeSearchUnavailable, "context_invalid"):
            context.context.context.get_config(umo="private:person:actual-id")


class FactoryTests(unittest.TestCase):
    def test_disabled_has_no_native_import_or_credential_lookup(self):
        with patch.object(tools, "_load_native_search", side_effect=AssertionError("not called")):
            self.assertIsNone(tools.create_lookup_service(object(), object(), {}))

    def test_enabled_factory_uses_existing_tool_and_validated_settings(self):
        from plugins.astrbot_plugin_roleplay_gateway.online_lookup import OnlineLookupService
        with patch.object(tools, "_load_native_search", return_value=FakeTool()) as loaded:
            service = tools.create_lookup_service(object(), provider(), {"enabled": True, "max_sources": 2})
        self.assertIsInstance(service, OnlineLookupService)
        self.assertTrue(service.settings.enabled)
        self.assertEqual(service.settings.max_sources, 2)
        loaded.assert_called_once()

    def test_contract_rejects_wrong_version_or_replaced_builtin(self):
        modules = {name: ModuleType(name) for name in (
            "astrbot", "astrbot.core", "astrbot.core.tools", "astrbot.core.tools.web_search_tools")}
        modules["astrbot"].__version__ = "4.29.1"
        modules["astrbot.core.tools.web_search_tools"].DeepSeekWebSearchTool = FakeTool
        manager = SimpleNamespace(get_builtin_tool=lambda cls: cls())
        context = SimpleNamespace(get_llm_tool_manager=lambda: manager)
        with patch.dict(sys.modules, modules):
            self.assertIsInstance(tools._load_native_search(context), FakeTool)
            modules["astrbot"].__version__ = "future-version"
            with self.assertRaisesRegex(tools.NativeSearchUnavailable, "contract_unavailable"):
                tools._load_native_search(context)
            modules["astrbot"].__version__ = "4.29.1"
            manager.get_builtin_tool = lambda cls: object()
            with self.assertRaisesRegex(tools.NativeSearchUnavailable, "contract_unavailable"):
                tools._load_native_search(context)

    def test_config_changes_require_reload_in_each_enabled_path(self):
        import test_gateway_adapter as fixture
        config = {"enabled": True, "mode": "neutral_receipt", "platform_id": "test-qq",
                  "bot_self_id": "900", "admin_private_ids": ["100"],
                  "allowed_group_ids": ["200"], "wake_names": ["三叶"], "wake_regex": []}
        plugin = fixture.adapter.RoleplayGateway(object(), config)
        self.assertTrue(plugin._enabled())
        config["online_lookup"] = {"enabled": True}
        self.assertFalse(plugin._enabled())
        config["online_lookup"] = {"enabled": False}
        self.assertTrue(plugin._enabled())
        config["online_lookup"] = {"enabled": "false"}
        self.assertFalse(plugin._enabled())


if __name__ == "__main__":
    unittest.main()
