"""Restricted adapter for AstrBot 4.29.1's existing DeepSeek search tool.

The tool receives a synthetic read-only context, not a chat event or its history.
No API credential is copied into persistent settings. Network execution stays in
AstrBot's native tool, whose aiohttp contexts settle on caller cancellation.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit


class NativeSearchUnavailable(RuntimeError):
    """Static reason only; native response/error bodies must not escape."""


def _load_native_search(context):
    try:
        import astrbot
        from astrbot.core.tools.web_search_tools import DeepSeekWebSearchTool
        if astrbot.__version__ != "4.29.1":
            raise ValueError
        manager = context.get_llm_tool_manager()
        tool = manager.get_builtin_tool(DeepSeekWebSearchTool)
        if type(tool) is not DeepSeekWebSearchTool or tool.name != "web_search_deepseek":
            raise ValueError
        return tool
    except Exception:
        raise NativeSearchUnavailable("native_search_contract_unavailable") from None


def _credential(provider) -> str:
    try:
        url = urlsplit(str(provider.client.base_url))
        key = provider.client.api_key
        config = provider.provider_config
        valid = (config.get("provider") == "deepseek"
                 and config.get("type") == "openai_chat_completion"
                 and url.scheme == "https" and url.hostname == "api.deepseek.com"
                 and url.port in (None, 443) and url.path in ("", "/", "/v1", "/v1/")
                 and not url.username and not url.password and not url.query and not url.fragment
                 and isinstance(key, str) and 16 <= len(key) <= 512
                 and not key.startswith("$") and all(c.isascii() and c.isprintable() and not c.isspace() for c in key))
    except Exception:
        valid = False
    if not valid:
        raise NativeSearchUnavailable("native_search_provider_unavailable")
    return key


class _ConfigView:
    __slots__ = ("_key",)

    def __init__(self, key):
        self._key = key

    def get_config(self, *, umo):
        if umo != "roleplay:lookup:isolated":
            raise NativeSearchUnavailable("native_search_context_invalid")
        # Return only the settings read by the reviewed native search tool.
        return {"provider_settings": {"web_search": True, "websearch_provider": "deepseek",
                                      "websearch_deepseek_key": [self._key]}}


class NativeDeepSeekSearch:
    def __init__(self, context, provider, *, max_sources=3):
        if type(max_sources) is not int or not 1 <= max_sources <= 3:
            raise ValueError("invalid_search_source_count")
        _credential(provider)
        self._provider = provider
        self._tool = _load_native_search(context)
        self.max_sources = max_sources

    async def __call__(self, query: str) -> list[dict]:
        if (not isinstance(query, str) or not query.strip() or not 1 <= len(query) <= 128
                or "\0" in query):
            raise NativeSearchUnavailable("native_search_query_invalid")
        key = _credential(self._provider)
        tool_context = SimpleNamespace(context=SimpleNamespace(
            event=SimpleNamespace(unified_msg_origin="roleplay:lookup:isolated"),
            context=_ConfigView(key),
        ))
        try:
            # Exactly one upstream call; the caller owns the overall deadline.
            encoded = await self._tool.call(tool_context, query=query,
                                            count=self.max_sources, need_content=False)
            if not isinstance(encoded, str) or len(encoded) > 262144:
                raise ValueError
            data = json.loads(encoded)
            if not isinstance(data, dict) or set(data) != {"results"} or not isinstance(data["results"], list):
                raise ValueError
            if len(data["results"]) > self.max_sources:
                raise ValueError
            results = []
            for item in data["results"]:
                if not isinstance(item, dict):
                    raise ValueError
                row = {field: item.get(field, "") for field in ("title", "url", "snippet", "index")}
                if any(not isinstance(value, str) for value in row.values()):
                    raise ValueError
                if len(row["url"]) > 4096 or len(row["index"]) > 128:
                    raise ValueError
                row["title"] = row["title"][:300]
                row["snippet"] = row["snippet"][:6000]
                results.append(row)
            return results
        except asyncio.CancelledError:
            raise
        except Exception:
            raise NativeSearchUnavailable("native_search_failed") from None


def create_lookup_service(context, provider, raw_settings):
    # Keep AstrBot tool imports and credential access off the disabled path.
    from .online_lookup import LookupSettings, OnlineLookupService
    settings = LookupSettings.from_mapping(raw_settings)
    if not settings.enabled:
        return None
    return OnlineLookupService(settings, search=NativeDeepSeekSearch(
        context, provider, max_sources=settings.max_sources))
