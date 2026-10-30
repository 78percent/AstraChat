"""Version-bound request gate for AstrBot 4.29.1 / openai 3.33.0 only.

The explicit neutral_stream mode uses this module; neutral_receipt does not. It performs no
file reads, persona binding, history persistence, native compression or route
fallback. Callers supply an explicit text snapshot; neutral fixtures are used
by the tests. Token budgeting and speaker-to-answer attribution are not here.

The version-bound private _prepare_chat_payload/_query_stream methods are
reused on a shallow provider copy with a deep-copied configuration. Public
text_chat_stream is deliberately bypassed: its outer recovery loop can make
ten attempts despite request_max_retries=1. The private method's parameter is
tools, not func_tool. SDK retries are also required to be zero.

Every attempt has a local client facade and an SDK with_options view with an
independent key/header snapshot. Neither the shared provider nor its client
is modified or closed. The facade captures the SDK stream; closing only the
private AstrBot generator would not close that stream in this version.

SDK iteration and close run in tracked, shielded tasks: httpx2 marks a response
closed BEFORE awaiting transport cleanup, so cancelling that cleanup and
calling close again is not sufficient. Unsettled cleanup blocks this adapter
from further requests. It is never force-cancelled or treated as safe fallback.
Only is_chunk text reaches the callback; final cumulative text is ignored.

Compatibility failure rollback: leave this adapter disabled, retain the
neutral gate, and revalidate against a fixed image. Do not silently fall back
to public text_chat_stream, fork upstream, or monkeypatch global SDK methods.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import copy
from dataclasses import dataclass
import inspect
import json
import math
import sys
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

ASTRBOT_VERSION = "4.29.1"
OPENAI_VERSION = "3.33.0"
PROVIDER_TYPE = "openai_chat_completion"


class AdapterUnavailable(RuntimeError):
    """A stable diagnostic code only; never includes credentials or content."""


class ProviderAttemptError(RuntimeError):
    """A single attempt failed; the caller must not infer that retry is safe."""


class PolicyLimitReached(Exception):
    """The caller accepted enough whole speech records; close transport cleanly."""


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class SpeakerInput:
    sender_id: str
    display_name: str
    message_id: str
    text: str


@dataclass(frozen=True)
class PromptParts:
    runtime_boundary: str
    persona: str
    current: tuple[SpeakerInput, ...]
    before_worldbook: tuple[str, ...] = ()
    after_worldbook: tuple[str, ...] = ()
    summary: str = ""
    examples: tuple[Message, ...] = ()
    recent: tuple[Message, ...] = ()
    reference_data: str = ""


def assemble_messages(parts: PromptParts) -> tuple[Message, ...]:
    """Preserve the declared order; do not expand macros or read native state."""
    if not isinstance(parts, PromptParts) or not parts.current:
        raise AdapterUnavailable("invalid_prompt_parts")
    if any(not isinstance(value, tuple) for value in (
        parts.current, parts.before_worldbook, parts.after_worldbook, parts.examples, parts.recent
    )):
        raise AdapterUnavailable("invalid_prompt_collection")
    result: list[Message] = []

    def system(text: str, *, required: bool = False) -> None:
        if not isinstance(text, str) or (required and not text.strip()):
            raise AdapterUnavailable("invalid_system_text")
        if text:
            result.append(Message("system", text))

    system(parts.runtime_boundary, required=True)
    for text in parts.before_worldbook:
        system(text)
    system(parts.persona)
    for text in parts.after_worldbook:
        system(text)
    system(parts.summary)
    for section in (parts.examples, parts.recent):
        for item in section:
            if (not isinstance(item, Message) or item.role not in {"user", "assistant"}
                    or not isinstance(item.content, str) or not item.content.strip()):
                raise AdapterUnavailable("invalid_dialogue_message")
            result.append(item)
    if parts.reference_data:
        if not isinstance(parts.reference_data, str):
            raise AdapterUnavailable("invalid_reference_data")
        # External text has data-level user authority, never system authority.
        result.append(Message("user", json.dumps(
            {"untrusted_reference_data": parts.reference_data}, ensure_ascii=False)))
    batch = []
    for item in parts.current:
        if not isinstance(item, SpeakerInput) or any(
            not isinstance(value, str) or not value.strip()
            for value in (item.sender_id, item.display_name, item.message_id, item.text)
        ):
            raise AdapterUnavailable("invalid_current_speaker")
        batch.append({"sender_id": item.sender_id, "display_name": item.display_name,
                      "message_id": item.message_id, "text": item.text})
    # Speaker metadata remains JSON data inside one user message, never a role.
    result.append(Message("user", json.dumps({"current_speaker_batch": batch}, ensure_ascii=False)))
    return tuple(result)


@dataclass(frozen=True)
class AttemptResult:
    chunk_count: int
    character_count: int
    final_seen: bool
    response_id: str | None
    usage: tuple[tuple[str, int], ...]
    stop_reason: str = ""

    @property
    def partial(self) -> bool:
        return not self.final_seen


def _runtime_contract() -> tuple[str, str, type, type]:
    try:
        import astrbot
        import openai
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        return astrbot.__version__, openai.__version__, ProviderOpenAIOfficial, openai.AsyncOpenAI
    except (ImportError, AttributeError):
        raise AdapterUnavailable("runtime_unavailable") from None


def _extra_body(value: Any) -> dict:
    if not isinstance(value, dict):
        raise AdapterUnavailable("invalid_extra_body")
    allowed = {"thinking", "temperature", "top_p", "max_tokens", "max_completion_tokens"}
    if set(value) - allowed:
        raise AdapterUnavailable("unsupported_extra_body")
    for key, item in value.items():
        if key == "thinking":
            if item != {"type": "disabled"}:
                raise AdapterUnavailable("unsupported_thinking_mode")
        elif key in {"max_tokens", "max_completion_tokens"}:
            if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                raise AdapterUnavailable("invalid_output_limit")
        elif (isinstance(item, bool) or not isinstance(item, (float, int))
              or not math.isfinite(item) or not 0 <= item <= (1 if key == "top_p" else 2)):
            raise AdapterUnavailable("invalid_sampling_parameter")
    if "max_tokens" in value and "max_completion_tokens" in value:
        raise AdapterUnavailable("conflicting_output_limits")
    result = copy.deepcopy(value)
    if not {"max_tokens", "max_completion_tokens"} & set(result):
        result["max_tokens"] = 1200
    if any(result.get(key, 0) > 1200 for key in ("max_tokens", "max_completion_tokens")):
        raise AdapterUnavailable("output_limit_exceeds_gate")
    return result


def _check_messages(actual: Any, expected: tuple[Message, ...]) -> None:
    if not isinstance(actual, list) or len(actual) != len(expected):
        raise AdapterUnavailable("payload_order_or_length_changed")
    for row, wanted in zip(actual, expected):
        if (not isinstance(row, dict) or set(row) - {"role", "content", "reasoning_content"}
                or row.get("role") != wanted.role or row.get("content") != wanted.content
                or row.get("reasoning_content", "") != ""):
            raise AdapterUnavailable("payload_content_changed")


class _StreamOwner:
    def __init__(self, track: Callable[[asyncio.Task], None]):
        self.track = track
        self.active = True
        self.close_requested = False
        self.raw: Any = None
        self.close_task: asyncio.Task | None = None

    def task(self, awaitable: Awaitable) -> asyncio.Task:
        task = asyncio.create_task(awaitable)
        self.track(task)
        return task

    async def attach(self, raw: Any) -> "_ManagedStream":
        self.raw = raw
        if not callable(getattr(raw, "close", None)) or not hasattr(raw, "__aiter__"):
            raise AdapterUnavailable("unsupported_sdk_stream")
        if self.close_requested:
            await self.close()
            raise ProviderAttemptError("attempt_inactive")
        return _ManagedStream(self)

    async def close(self) -> None:
        self.active = False
        self.close_requested = True
        if self.raw is None:
            return
        if self.close_task is None:
            self.close_task = self.task(self.raw.close())
        # Never cancel this task, including when a caller is cancelled twice.
        await asyncio.shield(self.close_task)


class _ManagedStream:
    def __init__(self, owner: _StreamOwner):
        self.owner = owner
        self.iterator = owner.raw.__aiter__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.owner.active:
            raise StopAsyncIteration
        # SDK's own finally/response.aclose cannot inherit worker cancellation.
        item = await asyncio.shield(self.owner.task(anext(self.iterator)))
        if not self.owner.active:
            raise StopAsyncIteration
        return item


class _ClientFacade:
    def __init__(self, view: Any, owner: _StreamOwner, messages: tuple[Message, ...],
                 model: str, extra: dict):
        self.view, self.owner = view, owner
        self.messages, self.model, self.extra = messages, model, extra
        self.calls = 0
        self.base_url = view.base_url
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls != 1 or not self.owner.active:
            raise AdapterUnavailable("multiple_or_inactive_create")
        permitted = {"messages", "model", "stream", "extra_body", "extra_headers", "stream_options"}
        if (set(kwargs) != permitted or kwargs["model"] != self.model
                or kwargs["stream"] is not True
                or kwargs["stream_options"] != {"include_usage": True}
                or kwargs["extra_headers"] not in (None, {})
                or kwargs["extra_body"] != self.extra):
            raise AdapterUnavailable("unexpected_sdk_request")
        _check_messages(kwargs["messages"], self.messages)
        async def create_owned():
            raw = await self.view.chat.completions.create(**kwargs)
            return await self.owner.attach(raw)
        # Capture and close even a stream returned after caller cancellation.
        return await asyncio.shield(self.owner.task(create_owned()))


class FailFastOpenAI:
    """One explicit request, with no retry, key rotation, or native history.

    timeout_seconds covers the entire attempt, including all deltas. Its
    default four seconds is NOT a first-chunk budget; a future coordinator
    must choose the total generation deadline separately from first output.
    """

    def __init__(self, provider: Any, *, cleanup_timeout_seconds: float = 2.0):
        self.provider = provider
        if (isinstance(cleanup_timeout_seconds, bool)
                or not isinstance(cleanup_timeout_seconds, (float, int))
                or not math.isfinite(cleanup_timeout_seconds) or not 0 < cleanup_timeout_seconds <= 10):
            raise AdapterUnavailable("invalid_cleanup_timeout")
        self.cleanup_timeout = cleanup_timeout_seconds
        self._tasks: set[asyncio.Task] = set()
        self._blocked = False
        self._busy = False
        self._snapshot()  # Fail before any request if the runtime is unsupported.

    @property
    def pending_tasks(self) -> int:
        """Tracked transport/iteration/cleanup work, excluding completed tasks."""
        return sum(not task.done() for task in self._tasks)

    @property
    def reusable(self) -> bool:
        """False after unsettled/failed cleanup, even if a late task later ends."""
        return not self._blocked and not self._busy and self.pending_tasks == 0

    def _track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)

        def done(item: asyncio.Task) -> None:
            self._tasks.discard(item)
            if not item.cancelled():
                item.exception()  # Retrieve late exceptions without logging content.
        task.add_done_callback(done)

    def _snapshot(self, *, request_timeout: float | None = None):
        try:
            return self._snapshot_checked(request_timeout=request_timeout)
        except AdapterUnavailable:
            raise
        except Exception:
            raise AdapterUnavailable("unsupported_provider_capability") from None

    def _snapshot_checked(self, *, request_timeout: float | None = None):
        version, sdk_version, provider_class, client_class = _runtime_contract()
        p = self.provider
        if version != ASTRBOT_VERSION or sdk_version != OPENAI_VERSION:
            raise AdapterUnavailable("unsupported_runtime_version")
        if type(p) is not provider_class or type(p.client) is not client_class:
            raise AdapterUnavailable("unsupported_provider_type")
        expected = {
            "_prepare_chat_payload": ("self", "prompt", "image_urls", "audio_urls", "contexts",
                                      "system_prompt", "tool_calls_result", "model", "extra_user_content_parts", "kwargs"),
            "_query_stream": ("self", "payloads", "tools", "request_max_retries", "conversation_id"),
        }
        for name, parameters in expected.items():
            method = getattr(provider_class, name, None)
            bound = getattr(p, name, None)
            if (not callable(method) or tuple(inspect.signature(method).parameters) != parameters
                    or getattr(bound, "__func__", None) is not method):
                raise AdapterUnavailable("unsupported_private_interface")
        if not inspect.iscoroutinefunction(provider_class._prepare_chat_payload) or not inspect.isasyncgenfunction(provider_class._query_stream):
            raise AdapterUnavailable("unsupported_private_interface")
        config = copy.deepcopy(p.provider_config)
        keys = list(p.api_keys)
        if (config.get("type") != PROVIDER_TYPE or "api_version" in config
                or config.get("provider") not in (None, "", "openai", "deepseek", "custom", "gemini")):
            raise AdapterUnavailable("unsupported_provider_type")
        if (len(keys) != 1 or not isinstance(keys[0], str) or not keys[0].strip()
                or config.get("key") != keys or p.client.api_key != keys[0]
                or p.chosen_api_key != keys[0]):
            raise AdapterUnavailable("single_consistent_key_required")
        if type(p.client.max_retries) is not int or p.client.max_retries != 0:
            raise AdapterUnavailable("sdk_retry_must_be_disabled")
        # AstrBot always injects this exact default User-Agent. Other header
        # overrides can replace authorization or SDK response semantics.
        default_headers = {"User-Agent": f"astrbot/{ASTRBOT_VERSION}"}
        if (config.get("custom_headers")
                or any(value not in ({}, default_headers) for value in (
                    p.request_headers, p.custom_headers, getattr(p.client, "_custom_headers", {})
                ))
                or getattr(p.client, "_custom_query", {})):
            raise AdapterUnavailable("custom_headers_or_query_unsupported")
        model = p.get_model()
        if not isinstance(model, str) or not model.strip() or model == "unknown":
            raise AdapterUnavailable("model_required")
        extra = _extra_body(config.get("custom_extra_body", {}))
        config["custom_extra_body"] = copy.deepcopy(extra)
        base_url = str(p.client.base_url)
        parsed_url = urlsplit(base_url)
        if (parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username
                or parsed_url.password or parsed_url.query or parsed_url.fragment):
            raise AdapterUnavailable("https_endpoint_required")
        if not callable(getattr(p.client, "with_options", None)):
            raise AdapterUnavailable("sdk_copy_capability_missing")
        clone = copy.copy(p)
        clone.provider_config = config
        clone.api_keys = keys.copy()
        # with_options shares the transport; never close this client view.
        # A scalar SDK timeout bounds connect/read/write/pool individually. It
        # also applies before response headers, when owner.raw cannot yet close
        # a stalled request. Kouri can take over 5 seconds between bytes; the
        # coordinator still owns the shorter end-to-end deadline.
        options = dict(api_key=keys[0], max_retries=0,
                       set_default_headers=default_headers, base_url=base_url)
        if request_timeout is not None:
            options["timeout"] = min(35.0, float(request_timeout))
        view = p.client.with_options(**options)
        if type(view) is not client_class or view is p.client or view.api_key != keys[0] or view.max_retries != 0:
            raise AdapterUnavailable("sdk_copy_contract_failed")
        return clone, view, model, extra

    async def consume(self, parts: PromptParts, on_delta: Callable[[str], Awaitable[None]],
                      *, timeout_seconds: float = 4.0, json_output: bool = False) -> AttemptResult:
        if self._blocked or self._busy:
            raise AdapterUnavailable("adapter_busy_or_cleanup_unsettled")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise AdapterUnavailable("invalid_timeout")
        messages = assemble_messages(parts)
        if type(json_output) is not bool:
            raise AdapterUnavailable("invalid_json_output_mode")
        clone, view, model, extra = self._snapshot(request_timeout=timeout_seconds)
        if json_output:
            # Per-attempt transport constraint, never mutate the shared provider.
            # Streaming array elements are decoded by the roleplay service.
            extra["response_format"] = {"type": "json_object"}
            clone.provider_config["custom_extra_body"] = copy.deepcopy(extra)
        owner = _StreamOwner(self._track)
        clone.client = _ClientFacade(view, owner, messages, model, extra)
        self._busy = True
        worker = asyncio.create_task(self._drive(clone, owner, messages, model, on_delta))
        self._track(worker)
        try:
            done, _ = await asyncio.wait({worker}, timeout=timeout_seconds)
            if not done:
                raise ProviderAttemptError("attempt_timeout")
            try:
                return worker.result()
            except (AdapterUnavailable, ProviderAttemptError):
                raise
            except asyncio.CancelledError:
                raise ProviderAttemptError("provider_cancelled") from None
            except Exception as exc:
                # Keep diagnostics useful without forwarding response bodies or keys.
                status = getattr(exc, 'status_code', None)
                code = {400: 'provider_parameters_rejected', 401: 'provider_auth_failed',
                        403: 'provider_access_denied', 404: 'provider_model_unavailable',
                        429: 'provider_rate_limited'}.get(status, 'provider_request_failed')
                if isinstance(exc, TimeoutError) or type(exc).__name__ in ('APITimeoutError', 'ReadTimeout', 'ConnectTimeout'):
                    code = 'provider_timeout'
                raise ProviderAttemptError(code) from None
        finally:
            was_cancelled = isinstance(sys.exc_info()[1], asyncio.CancelledError)
            owner.active = False
            owner.close_requested = True
            # This worker never owns/cancels SDK iteration/close tasks.
            if not worker.done():
                worker.cancel()
            cleanup = asyncio.create_task(owner.close())
            self._track(cleanup)
            try:
                _, pending = await asyncio.wait(tuple(self._tasks), timeout=self.cleanup_timeout)
                if pending:
                    self._blocked = True
                    if not was_cancelled:
                        raise ProviderAttemptError("stream_cleanup_unsettled")
                elif cleanup.exception() is not None:
                    self._blocked = True
                    raise ProviderAttemptError("stream_cleanup_failed") from None
            except asyncio.CancelledError:
                self._blocked = True
                raise
            finally:
                self._busy = False

    async def _drive(self, clone, owner, messages, model, on_delta) -> AttemptResult:
        payload, _ = await clone._prepare_chat_payload(
            prompt=None, contexts=[{"role": m.role, "content": m.content} for m in messages],
            image_urls=None, audio_urls=None, system_prompt=None, tool_calls_result=None,
            model=model, extra_user_content_parts=None,
        )
        if set(payload) != {"messages", "model"} or payload["model"] != model:
            raise AdapterUnavailable("unexpected_prepared_payload")
        _check_messages(payload["messages"], messages)
        query = clone._query_stream(payload, tools=None, request_max_retries=1, conversation_id=None)
        chunks = characters = 0
        final_seen = False
        response_id = None
        usage: tuple[tuple[str, int], ...] = ()
        try:
            async for response in query:
                if not owner.active:
                    raise ProviderAttemptError("attempt_inactive")
                is_chunk = getattr(response, "is_chunk", None)
                if is_chunk is True:
                    if final_seen:
                        raise ProviderAttemptError("chunk_after_final")
                    text = getattr(response, "completion_text", "")
                    if not isinstance(text, str):
                        raise ProviderAttemptError("invalid_chunk_text")
                    if text:
                        # Copy now: upstream reuses one mutable LLMResponse.
                        chunks += 1
                        characters += len(text)
                        await on_delta(text)
                elif is_chunk is False:
                    if final_seen:
                        raise ProviderAttemptError("duplicate_final_metadata")
                    final_seen = True
                    identifier = getattr(response, "id", None)
                    response_id = identifier if isinstance(identifier, str) else None
                    tokens = getattr(response, "usage", None)
                    pairs = []
                    for field in ("input_tokens", "output_tokens", "total_tokens"):
                        value = getattr(tokens, field, None)
                        if type(value) is int and value >= 0:
                            pairs.append((field, value))
                    usage = tuple(pairs)
                else:
                    raise ProviderAttemptError("unsupported_response_shape")
        except PolicyLimitReached:
            return AttemptResult(chunks, characters, False, response_id, usage, "policy_limit")
        finally:
            await query.aclose()
        if not chunks:
            raise ProviderAttemptError("no_incremental_text")
        return AttemptResult(chunks, characters, final_seen, response_id, usage)
