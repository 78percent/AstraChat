"""Pure neutral fixtures. Optional SDK contracts still use MockTransport only."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway import provider_gate as pg


def fixture():
    return pg.PromptParts(
        runtime_boundary="neutral runtime boundary", persona="neutral fixture persona",
        before_worldbook=("before fixture",), after_worldbook=("after fixture",),
        summary="committed fixture summary",
        examples=(pg.Message("user", "example user"), pg.Message("assistant", "example assistant")),
        recent=(pg.Message("user", "recent user"), pg.Message("assistant", "recent assistant")),
        current=(pg.SpeakerInput("100", "fixture A", "1", "neutral question A"),
                 pg.SpeakerInput("101", "fixture B", "2", "neutral question B")),
    )


def response(text="", *, chunk=True):
    return SimpleNamespace(is_chunk=chunk, completion_text=text, id="fixture-response",
                           usage=SimpleNamespace(input_tokens=5, output_tokens=2, total_tokens=7))


class FakeStream:
    def __init__(self, items=None, *, block=False, block_close=False, fail_close=False):
        self.items = iter(items if items is not None else [response("A"), response("B"), response("AB", chunk=False)])
        self.block, self.block_close, self.fail_close = block, block_close, fail_close
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.close_entered, self.close_release = asyncio.Event(), asyncio.Event()
        self.close_calls = 0
        self.close_completed = False
        self.iteration_cancelled = False
        self.close_cancelled = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.entered.set()
        if self.block:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.iteration_cancelled = True
                raise
        try:
            return next(self.items)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        self.close_calls += 1
        self.close_entered.set()
        try:
            if self.block_close:
                await self.close_release.wait()
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        finally:
            self.release.set()
        if self.fail_close:
            raise RuntimeError("fixture cleanup detail")
        self.close_completed = True


class Backend:
    def __init__(self, stream=None, *, error=None, block_create=False):
        self.stream = stream or FakeStream()
        self.error, self.block_create = error, block_create
        self.create_entered, self.create_release = asyncio.Event(), asyncio.Event()
        self.calls = []
        self.timeouts = []
        self.timeout_before_headers = False

    async def create(self, client, kwargs):
        self.calls.append((client.api_key, client.max_retries, copy.deepcopy(kwargs)))
        self.timeouts.append(client.timeout)
        self.create_entered.set()
        if self.timeout_before_headers:
            await asyncio.sleep(client.timeout)
            raise TimeoutError("offline response header timeout")
        if self.block_create:
            await self.create_release.wait()
        if self.error:
            raise self.error
        return self.stream


class FakeClient:
    def __init__(self, backend, *, api_key="fixture-key", max_retries=0,
                 base_url="https://fixture.invalid/v1", timeout=600.0):
        self.backend, self.api_key, self.max_retries = backend, api_key, max_retries
        self.base_url = base_url
        self._custom_headers, self._custom_query = {}, {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.closed = False
        self.timeout = timeout

    async def create(self, **kwargs):
        return await self.backend.create(self, kwargs)

    def with_options(self, *, api_key, max_retries, set_default_headers, base_url, timeout=None):
        assert set_default_headers == {"User-Agent": f"astrbot/{pg.ASTRBOT_VERSION}"}
        return FakeClient(self.backend, api_key=api_key, max_retries=max_retries, base_url=base_url,
                          timeout=self.timeout if timeout is None else timeout)


class FakeProvider:
    def __init__(self, backend):
        self.client = FakeClient(backend)
        self.api_keys = ["fixture-key"]
        self.chosen_api_key = "fixture-key"
        self.provider_config = {"type": pg.PROVIDER_TYPE, "provider": "deepseek", "id": "fixture",
                                "key": ["fixture-key"], "custom_extra_body": {"thinking": {"type": "disabled"}}}
        self.request_headers, self.custom_headers = {}, {}
        self.observed = {"queries": 0, "closed": 0}
        self.prepare_hook = None

    def get_model(self):
        return "fixture-model"

    async def _prepare_chat_payload(self, prompt, image_urls=None, audio_urls=None, contexts=None,
                                    system_prompt=None, tool_calls_result=None, model=None,
                                    extra_user_content_parts=None, **kwargs):
        assert prompt is None and system_prompt is None and tool_calls_result is None
        assert image_urls is None and audio_urls is None and extra_user_content_parts is None
        assert kwargs == {}
        rows = copy.deepcopy(contexts)
        if self.prepare_hook:
            self.prepare_hook(rows)
        self.observed["prepared"] = rows
        return {"messages": rows, "model": model}, rows

    async def _query_stream(self, payloads, tools, *, request_max_retries=None, conversation_id=None):
        assert tools is None and conversation_id is None
        self.observed["queries"] += 1
        self.observed["retry_limit"] = request_max_retries
        try:
            stream = await self.client.chat.completions.create(
                messages=payloads["messages"], model=payloads["model"], stream=True,
                extra_body=self.provider_config.get("custom_extra_body", {}),
                extra_headers={}, stream_options={"include_usage": True},
            )
            async for item in stream:
                yield item
        finally:
            self.observed["closed"] += 1

    async def text_chat_stream(self, **kwargs):
        raise AssertionError("public recovery loop must not run")


class ProviderGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.contract = patch.object(pg, "_runtime_contract", return_value=(
            pg.ASTRBOT_VERSION, pg.OPENAI_VERSION, FakeProvider, FakeClient))
        self.contract.start()
        self.backend = Backend()
        self.provider = FakeProvider(self.backend)
        self.output = []
        self.gates = []

    async def asyncTearDown(self):
        self.backend.create_release.set()
        self.backend.stream.release.set()
        self.backend.stream.close_release.set()
        for gate in self.gates:
            if gate._tasks:
                done, pending = await asyncio.wait(tuple(gate._tasks), timeout=2)
                self.assertFalse(pending, "fixture left owned tasks pending")
        self.contract.stop()

    def gate(self, **kwargs):
        gate = pg.FailFastOpenAI(self.provider, **kwargs)
        self.gates.append(gate)
        return gate

    async def collect(self, text):
        self.output.append(text)

    async def test_order_payload_no_native_state_no_final_text_duplication(self):
        original_client = self.provider.client
        original_config = copy.deepcopy(self.provider.provider_config)
        gate = self.gate()
        result = await gate.consume(fixture(), self.collect)
        self.assertEqual(self.output, ["A", "B"])
        self.assertEqual((result.chunk_count, result.character_count, result.final_seen), (2, 2, True))
        self.assertEqual(dict(result.usage)["total_tokens"], 7)
        payload = self.backend.calls[0][2]
        rows = payload["messages"]
        self.assertEqual([m["content"] for m in rows[:-1]], [
            "neutral runtime boundary", "before fixture", "neutral fixture persona", "after fixture",
            "committed fixture summary", "example user", "example assistant", "recent user", "recent assistant"])
        batch = json.loads(rows[-1]["content"])["current_speaker_batch"]
        self.assertEqual([item["sender_id"] for item in batch], ["100", "101"])
        self.assertEqual(payload["extra_body"], {"thinking": {"type": "disabled"}, "max_tokens": 1200})
        self.assertEqual(self.provider.observed["retry_limit"], 1)
        self.assertEqual(self.provider.observed["queries"], 1)
        self.assertEqual(self.backend.stream.close_calls, 1)
        self.assertIs(self.provider.client, original_client)
        self.assertEqual(self.provider.provider_config, original_config)
        self.assertFalse(original_client.closed)

    async def test_json_mode_is_per_attempt_and_does_not_mutate_shared_provider(self):
        original = copy.deepcopy(self.provider.provider_config)
        gate = self.gate()
        await gate.consume(fixture(), self.collect, json_output=True)
        self.assertEqual(self.backend.calls[0][2]["extra_body"]["response_format"], {"type": "json_object"})
        self.assertEqual(self.provider.provider_config, original)
        self.backend.stream = FakeStream()
        await gate.consume(fixture(), self.collect)
        self.assertNotIn("response_format", self.backend.calls[1][2]["extra_body"])

    async def test_prepare_cannot_remove_or_reorder_history(self):
        self.provider.prepare_hook = lambda rows: rows.reverse()
        with self.assertRaisesRegex(pg.AdapterUnavailable, "payload_content_changed"):
            await self.gate().consume(fixture(), self.collect)
        self.assertEqual(self.backend.calls, [])

    async def test_one_create_on_error_no_public_recovery(self):
        self.backend.error = RuntimeError("private fixture error body")
        with self.assertRaisesRegex(pg.ProviderAttemptError, "provider_request_failed") as caught:
            await self.gate().consume(fixture(), self.collect)
        self.assertNotIn("private fixture", str(caught.exception))
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.provider.observed["queries"], 1)
        self.assertEqual(self.backend.stream.close_calls, 0)

    async def test_missing_final_metadata_is_partial_and_final_only_is_not_delta(self):
        self.backend.stream = FakeStream([response("A")])
        result = await self.gate().consume(fixture(), self.collect)
        self.assertTrue(result.partial)
        self.assertEqual(result.usage, ())
        self.backend.stream = FakeStream([response("unusable cumulative answer", chunk=False)])
        with self.assertRaisesRegex(pg.ProviderAttemptError, "no_incremental_text"):
            await self.gate().consume(fixture(), self.collect)
        self.assertEqual(self.output, ["A"])

    async def test_reasoning_only_delta_is_not_emitted(self):
        thought = response("")
        thought.reasoning_content = "fixture separate reasoning"
        self.backend.stream = FakeStream([thought, response("answer"), response("answer", chunk=False)])
        await self.gate().consume(fixture(), self.collect)
        self.assertEqual(self.output, ["answer"])

    async def test_timeout_closes_stream_and_iteration_is_not_cancelled(self):
        self.backend.stream = FakeStream(block=True)
        with self.assertRaisesRegex(pg.ProviderAttemptError, "attempt_timeout"):
            await self.gate().consume(fixture(), self.collect, timeout_seconds=0.03)
        self.assertEqual(self.backend.stream.close_calls, 1)
        self.assertTrue(self.backend.stream.close_completed)
        self.assertFalse(self.backend.stream.iteration_cancelled)
        self.assertEqual(self.output, [])

    async def test_caller_cancel_closes_once_without_late_delta(self):
        self.backend.stream = FakeStream(block=True)
        task = asyncio.create_task(self.gate().consume(fixture(), self.collect))
        await asyncio.wait_for(self.backend.stream.entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.backend.stream.close_calls, 1)
        self.assertTrue(self.backend.stream.close_completed)
        self.assertEqual(self.output, [])

    async def test_second_cancel_cannot_cancel_cleanup(self):
        self.backend.stream = FakeStream(block=True, block_close=True)
        gate = self.gate()
        task = asyncio.create_task(gate.consume(fixture(), self.collect))
        await asyncio.wait_for(self.backend.stream.entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(self.backend.stream.close_entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.backend.stream.close_cancelled)
        self.backend.stream.close_release.set()
        _, pending = await asyncio.wait(tuple(gate._tasks), timeout=2)
        self.assertFalse(pending)
        self.assertTrue(self.backend.stream.close_completed)
        self.assertEqual(self.backend.stream.close_calls, 1)
        self.assertTrue(gate._blocked)

    async def test_late_create_is_closed_and_adapter_remains_blocked(self):
        self.backend.block_create = True
        gate = self.gate(cleanup_timeout_seconds=0.03)
        with self.assertRaisesRegex(pg.ProviderAttemptError, "stream_cleanup_unsettled"):
            await gate.consume(fixture(), self.collect, timeout_seconds=0.03)
        self.assertTrue(gate._blocked)
        self.backend.create_release.set()
        _, pending = await asyncio.wait(tuple(gate._tasks), timeout=2)
        self.assertFalse(pending)
        self.assertEqual(self.backend.stream.close_calls, 1)
        self.assertEqual(self.output, [])
        with self.assertRaisesRegex(pg.AdapterUnavailable, "cleanup_unsettled"):
            await gate.consume(fixture(), self.collect)

    async def test_cleanup_failure_blocks_new_request(self):
        self.backend.stream = FakeStream(fail_close=True)
        gate = self.gate()
        with self.assertRaisesRegex(pg.ProviderAttemptError, "stream_cleanup_failed"):
            await gate.consume(fixture(), self.collect)
        self.assertTrue(gate._blocked)

    async def test_successful_generation_with_pending_close_is_not_success(self):
        self.backend.stream = FakeStream(block_close=True)
        gate = self.gate(cleanup_timeout_seconds=0.03)
        with self.assertRaisesRegex(pg.ProviderAttemptError, "stream_cleanup_unsettled"):
            await gate.consume(fixture(), self.collect)
        self.assertTrue(gate._blocked)
        self.assertFalse(self.backend.stream.close_cancelled)
        self.backend.stream.close_release.set()

    async def test_request_key_snapshot_does_not_follow_shared_client_mutation(self):
        self.provider.prepare_hook = lambda rows: setattr(self.provider.client, "api_key", "externally-changed")
        await self.gate().consume(fixture(), self.collect)
        self.assertEqual(self.backend.calls[0][0], "fixture-key")
        self.assertEqual(self.provider.client.api_key, "externally-changed")

    async def test_attempt_transport_timeout_is_bounded_without_mutating_shared_client(self):
        original = self.provider.client
        gate = self.gate()
        for budget, expected in ((45.0, 35.0), (2.0, 2.0)):
            self.backend.stream = FakeStream()
            await gate.consume(fixture(), self.collect, timeout_seconds=budget)
            self.assertEqual(self.backend.timeouts[-1], expected)
            self.assertTrue(gate.reusable)
        _, short_view, _, _ = gate._snapshot(request_timeout=.05)
        self.assertEqual(short_view.timeout, .05)
        self.assertEqual(len(self.backend.calls), 2)
        self.assertTrue(all(call[1] == 0 for call in self.backend.calls))
        self.assertIs(self.provider.client, original)
        self.assertEqual(original.timeout, 600.0)
        self.assertFalse(original.closed)

    async def test_header_timeout_settles_shielded_create_and_allows_next_attempt(self):
        self.backend.timeout_before_headers = True
        gate = self.gate(cleanup_timeout_seconds=.3)
        with self.assertRaises(pg.ProviderAttemptError):
            await gate.consume(fixture(), self.collect, timeout_seconds=.05)
        self.assertEqual(self.output, [])
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.backend.timeouts, [.05])
        self.assertEqual(gate.pending_tasks, 0)
        self.assertTrue(gate.reusable)
        self.assertEqual(self.backend.stream.close_calls, 0)
        self.backend.timeout_before_headers = False
        await gate.consume(fixture(), self.collect, timeout_seconds=2)
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(self.output, ["A", "B"])
        self.assertTrue(gate.reusable)

    async def test_compatibility_fail_closed_before_create(self):
        mutations = [
            lambda p: p.provider_config.update(type="unknown"),
            lambda p: p.api_keys.append("second-key"),
            lambda p: setattr(p.client, "max_retries", 2),
            lambda p: p.request_headers.update(Authorization="override"),
            lambda p: p.provider_config.update(custom_extra_body={"messages": []}),
            lambda p: p.provider_config.update(custom_extra_body={"tools": []}),
            lambda p: p.provider_config.update(custom_extra_body={"max_tokens": 1201}),
            lambda p: setattr(p.client, "base_url", "http://fixture.invalid/v1"),
        ]
        for mutate in mutations:
            candidate = FakeProvider(self.backend)
            mutate(candidate)
            with self.assertRaises(pg.AdapterUnavailable):
                pg.FailFastOpenAI(candidate)
        for versions in [("4.29.2", pg.OPENAI_VERSION), (pg.ASTRBOT_VERSION, "3.34.0")]:
            with patch.object(pg, "_runtime_contract", return_value=(*versions, FakeProvider, FakeClient)):
                with self.assertRaisesRegex(pg.AdapterUnavailable, "unsupported_runtime_version"):
                    pg.FailFastOpenAI(self.provider)
        with patch.object(FakeProvider, "_query_stream", lambda self, payload: None):
            with self.assertRaisesRegex(pg.AdapterUnavailable, "unsupported_private_interface"):
                pg.FailFastOpenAI(self.provider)
        self.assertEqual(self.backend.calls, [])

    async def test_invalid_prompt_roles_and_collections_fail_before_request(self):
        for parts in [replace(fixture(), examples=(pg.Message("tool", "bad"),)),
                      replace(fixture(), recent=(pg.Message("assistant", ""),)),
                      replace(fixture(), before_worldbook="not-a-tuple"),
                      replace(fixture(), current=())]:
            with self.assertRaises(pg.AdapterUnavailable):
                await self.gate().consume(parts, self.collect)
        self.assertEqual(self.backend.calls, [])


@unittest.skipUnless(os.environ.get("ASTRBOT_PROVIDER_GATE_SDK_TEST") == "1",
                     "actual SDK contract runs only in the isolated fixed image")
class ActualSDKContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from openai import AsyncOpenAI
        import httpx2
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        self.httpx = httpx2
        self.calls, self.output, self.request_timeouts = [], [], []
        self.provider = ProviderOpenAIOfficial({
            "id": "offline-fixture", "type": pg.PROVIDER_TYPE, "provider": "deepseek",
            "key": ["offline-fixture-key"], "api_base": "https://fixture.invalid/v1",
            "model": "fixture-model", "custom_extra_body": {"thinking": {"type": "disabled"}},
        }, {})
        await self.provider.client.close()
        self.mode = "success"
        self.blocked_stream = None

        async def handler(request):
            self.calls.append(json.loads(request.content))
            self.request_timeouts.append(dict(request.extensions["timeout"]))
            if self.mode == "error":
                raise self.httpx.ReadTimeout("offline fixture", request=request)
            if self.mode == "header_timeout":
                # MockTransport does not run HTTPcore timers. Simulate its read
                # timeout at the exact per-request value delivered by the SDK.
                await asyncio.sleep(request.extensions["timeout"]["read"])
                raise self.httpx.ReadTimeout("offline header timeout", request=request)
            if self.mode == "block":
                return self.httpx.Response(200, headers={"content-type": "text/event-stream"},
                                           stream=self.blocked_stream)
            events = []
            for text, finish in [("A", None), ("B", None), (None, "stop")]:
                events.append({"id": "fixture-response", "object": "chat.completion.chunk",
                               "created": 1, "model": "fixture-model", "choices": [{"index": 0,
                               "delta": {"content": text} if text else {}, "finish_reason": finish}]})
            events.append({"id": "fixture-response", "object": "chat.completion.chunk", "created": 1,
                           "model": "fixture-model", "choices": [],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
            content = "".join("data: " + json.dumps(item) + "\n\n" for item in events) + "data: [DONE]\n\n"
            return self.httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

        self.http_client = self.httpx.AsyncClient(transport=self.httpx.MockTransport(handler))
        self.provider.client = AsyncOpenAI(api_key="offline-fixture-key", max_retries=0,
                                           base_url="https://fixture.invalid/v1", http_client=self.http_client)

    async def asyncTearDown(self):
        await self.provider.client.close()

    async def collect(self, text):
        self.output.append(text)

    async def test_actual_prepare_stream_sdk_mock_contract(self):
        original = self.provider.client
        result = await pg.FailFastOpenAI(self.provider).consume(fixture(), self.collect)
        self.assertEqual(self.output, ["A", "B"])
        self.assertTrue(result.final_seen)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["max_tokens"], 1200)
        self.assertEqual(self.calls[0]["thinking"], {"type": "disabled"})
        self.assertEqual([row["content"] for row in self.calls[0]["messages"]],
                         [row.content for row in pg.assemble_messages(fixture())])
        self.assertIs(self.provider.client, original)
        self.assertFalse(original.is_closed())

    async def test_actual_sdk_passes_native_json_mode_without_mutating_shared_provider(self):
        original = copy.deepcopy(self.provider.provider_config)
        await pg.FailFastOpenAI(self.provider).consume(fixture(), self.collect, json_output=True)
        self.assertEqual(self.calls[0]["response_format"], {"type": "json_object"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.provider.provider_config, original)

    async def test_actual_sdk_and_provider_retry_both_make_one_attempt(self):
        self.mode = "error"
        with self.assertRaisesRegex(pg.ProviderAttemptError, "provider_request_failed"):
            await pg.FailFastOpenAI(self.provider).consume(fixture(), self.collect)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.output, [])

    async def test_actual_sdk_propagates_per_attempt_phase_timeouts_and_preserves_original(self):
        original = self.provider.client
        original_timeout = copy.deepcopy(original.timeout)
        transport_timeout = copy.deepcopy(self.http_client.timeout)
        gate = pg.FailFastOpenAI(self.provider)
        for budget, expected in ((45.0, 35.0), (2.0, 2.0)):
            await gate.consume(fixture(), self.collect, timeout_seconds=budget)
            self.assertEqual(self.request_timeouts[-1], dict.fromkeys(("connect", "read", "write", "pool"), expected))
        self.assertEqual(len(self.calls), 2)
        self.assertIs(self.provider.client, original)
        self.assertEqual(original.timeout, original_timeout)
        self.assertEqual(self.http_client.timeout, transport_timeout)
        self.assertFalse(original.is_closed())
        self.assertTrue(gate.reusable)

    async def test_actual_sdk_timeout_before_headers_has_one_request_and_settles(self):
        self.mode = "header_timeout"
        original = self.provider.client
        original_timeout = copy.deepcopy(original.timeout)
        gate = pg.FailFastOpenAI(self.provider, cleanup_timeout_seconds=.5)
        with self.assertRaises(pg.ProviderAttemptError):
            await gate.consume(fixture(), self.collect, timeout_seconds=.05)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.request_timeouts[0], dict.fromkeys(("connect", "read", "write", "pool"), .05))
        self.assertEqual(self.output, [])
        self.assertEqual(gate.pending_tasks, 0)
        self.assertTrue(gate.reusable)
        self.assertFalse(original.is_closed())
        self.assertEqual(original.timeout, original_timeout)
        self.mode = "success"
        await gate.consume(fixture(), self.collect, timeout_seconds=2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.output, ["A", "B"])
        self.assertTrue(gate.reusable)

    async def test_actual_sdk_timeout_closes_only_response(self):
        class BlockingStream(self.httpx.AsyncByteStream):
            def __init__(self):
                self.release = asyncio.Event()
                self.close_calls = 0

            async def __aiter__(self):
                await self.release.wait()
                if False:
                    yield b""

            async def aclose(self):
                self.close_calls += 1
                self.release.set()

        self.mode = "block"
        self.blocked_stream = BlockingStream()
        with self.assertRaisesRegex(pg.ProviderAttemptError, "attempt_timeout"):
            await pg.FailFastOpenAI(self.provider).consume(fixture(), self.collect, timeout_seconds=0.05)
        self.assertEqual(self.blocked_stream.close_calls, 1)
        self.assertFalse(self.provider.client.is_closed())
        self.assertEqual(self.output, [])

    async def test_actual_sdk_policy_limit_closes_response_without_inventing_final(self):
        class EarlyStream(self.httpx.AsyncByteStream):
            def __init__(self):
                self.release = asyncio.Event()
                self.close_calls = 0

            async def __aiter__(self):
                event = {"id": "fixture-response", "object": "chat.completion.chunk",
                         "created": 1, "model": "fixture-model", "choices": [{"index": 0,
                         "delta": {"content": "A"}, "finish_reason": None}]}
                yield ("data: " + json.dumps(event) + "\n\n").encode()
                await self.release.wait()

            async def aclose(self):
                self.close_calls += 1
                self.release.set()

        self.mode = "block"
        self.blocked_stream = EarlyStream()
        original = self.provider.client
        gate = pg.FailFastOpenAI(self.provider)

        async def stop_after_one(text):
            self.output.append(text)
            raise pg.PolicyLimitReached()

        result = await gate.consume(fixture(), stop_after_one)
        self.assertEqual(self.output, ["A"])
        self.assertFalse(result.final_seen)
        self.assertEqual(result.stop_reason, "policy_limit")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.blocked_stream.close_calls, 1)
        self.assertEqual(gate.pending_tasks, 0)
        self.assertTrue(gate.reusable)
        self.assertIs(self.provider.client, original)
        self.assertFalse(original.is_closed())


if __name__ == "__main__":
    unittest.main()
