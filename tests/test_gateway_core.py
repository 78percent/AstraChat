"""Offline contract checks; no AstrBot installation, model or QQ connection."""

from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.astrbot_plugin_roleplay_gateway.gate import (
    ConfigurationError, DefiniteSendFailure, Envelope, GateService,
    GateSettings, NEUTRAL_REPLY, ReceiptStore, WakeMatcher, message_id,
    ApiAccepted, incoming_message_id, opaque_id, send_evidence,
)


def settings(**changes):
    baseline = GateSettings(
        enabled=True, platform_id="test-qq", bot_self_id="900",
        admin_private_ids=frozenset({"100"}),
        allowed_group_ids=frozenset({"200", "201", "202", "203"}),
        wake_names=("三叶",), send_timeout_seconds=2,
    )
    return replace(baseline, **changes)


def event(incoming="1", **changes):
    return replace(Envelope("test-qq", "900", "100", incoming, "group",
                            group_id="200", text="三叶你好"), **changes)


class ValidationTests(unittest.TestCase):
    def test_weixin_identity_policy_is_explicit_and_does_not_widen_qq(self):
        raw = {"enabled": True, "platform_id": "fixture-wechat", "bot_self_id": "fixture-wechat",
               "admin_private_ids": ["wx_user@im.wechat"], "id_kind": "weixin"}
        with self.assertRaises(ConfigurationError):
            GateSettings.from_mapping(raw)
        configured = GateSettings.from_mapping(raw, id_kind="weixin")
        self.assertEqual(configured.id_kind, "weixin")
        self.assertEqual(configured.admin_private_ids, frozenset({"wx_user@im.wechat"}))
        for value in (None, "", True, "user:group", "bad id", "bad\nid", "bad\x7fid", "a" * 129):
            self.assertIsNone(opaque_id(value))
        self.assertEqual(incoming_message_id("msg:0001", "weixin"), "msg:0001")
        self.assertEqual(incoming_message_id("0001", "weixin"), "0001")
        self.assertEqual(incoming_message_id("0001"), "1")
        self.assertIsNone(incoming_message_id("msg:0001"))
        for changes in ({"allowed_group_ids": ["group"]}, {"bot_self_id": "other"},
                        {"admin_private_ids": ["user:group"]}):
            with self.assertRaises(ConfigurationError):
                GateSettings.from_mapping({**raw, **changes}, id_kind="weixin")

    def test_send_evidence_cannot_cross_protocols_or_fabricate_ids(self):
        self.assertEqual(send_evidence(ApiAccepted(), "weixin"), ("API_ACCEPTED", None))
        self.assertEqual(send_evidence({"message_id": -3}), ("SENT", "-3"))
        self.assertEqual(send_evidence(ApiAccepted()), ("UNKNOWN", None))
        for result in (True, False, None, {"status": "API_ACCEPTED"}, {"message_id": 123}):
            self.assertEqual(send_evidence(result, "weixin"), ("UNKNOWN", None))

    def test_message_ids_canonical_and_malformed(self):
        for source, expected in [(1, "1"), (" 001 ", "1"), ("-001", "-1")]:
            self.assertEqual(message_id(source), expected)
        for source in [None, True, 0, "0", "-0", "--1", "²", "１", "", {}, 1.0]:
            self.assertIsNone(message_id(source), repr(source))

    def test_configuration_is_explicit_and_deny_by_default(self):
        self.assertFalse(GateSettings.from_mapping({}).enabled)
        self.assertFalse(GateSettings.from_mapping({}).allowed_group_ids)
        for raw in [{"enabled": "true"}, {"enabled": True},
                    {"allowed_group_ids": [123]}, {"mode": "roleplay"},
                    {"send_timeout_seconds": float("nan")}]:
            with self.assertRaises(ConfigurationError):
                GateSettings.from_mapping(raw)

    def test_stream_mode_is_explicit_and_provider_is_fixed(self):
        settings = GateSettings.from_mapping({"mode": "neutral_stream"})
        self.assertEqual(settings.mode, "neutral_stream")
        self.assertEqual(settings.provider_id, "deepseek-flash-official")
        self.assertFalse(settings.enabled)
        for mode in ("neutral_receipt", "neutral_stream"):
            with self.assertRaises(ConfigurationError):
                GateSettings.from_mapping({"mode": mode, "provider_id": "unapproved-relay"})

    def test_memory_administrators_are_explicit_not_private_allowlist(self):
        role = {"mode": "roleplay_stream", "persona_id": "test", "persona_name": "向导",
                "persona_filename": "test.txt", "persona_sha256": "a" * 64,
                "admin_private_ids": ["100", "101"]}
        configured = GateSettings.from_mapping(role)
        self.assertEqual(configured.memory_admin_ids, frozenset())
        configured = GateSettings.from_mapping({**role, "memory_admin_ids": ["100"]})
        self.assertEqual(configured.memory_admin_ids, frozenset({"100"}))
        for invalid in ("100", [100], [""], ["-1"], ["0"]):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigurationError):
                GateSettings.from_mapping({**role, "memory_admin_ids": invalid})
            self.assertEqual(GateSettings.from_mapping({"memory_admin_ids": invalid}).memory_admin_ids,
                             frozenset())

    def test_regex_invalid_and_real_execution_timeout(self):
        with self.assertRaises(ConfigurationError):
            WakeMatcher((), ("[",), lambda code: None)
        reports = []
        matcher = WakeMatcher((), (r"(a+)+$",), reports.append)
        started = time.perf_counter()
        self.assertFalse(matcher.matches("a" * 4000 + "!"))
        self.assertLess(time.perf_counter() - started, 0.25)
        self.assertEqual(matcher.disabled, {0})
        self.assertIn("regex_timeout_0", reports)
        self.assertFalse(matcher.matches("a" * 4000 + "!"))
        self.assertEqual(reports.count("regex_timeout_0"), 1)

    def test_total_regex_budget_and_input_length(self):
        reports = []

        class Pattern:
            def search(self, text, timeout):
                time.sleep(timeout)
                raise TimeoutError()

        class Backend:
            @staticmethod
            def compile(pattern):
                return Pattern()

        matcher = WakeMatcher((), tuple(str(i) for i in range(32)), reports.append, Backend)
        self.assertFalse(matcher.matches("text"))
        self.assertLessEqual(len(matcher.disabled), 5)
        self.assertIn("regex_total_budget", reports)
        self.assertFalse(matcher.matches("a" * 4097))
        self.assertIn("wake_text_too_long", reports)


class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gateway-", dir=ROOT / "tests")
        self.path = Path(self.temp.name) / "receipts.sqlite3"
        self.store = ReceiptStore(self.path)
        await self.store.open()
        self.logs = []
        self.service = GateService(settings(), self.store, emit=self.logs.append)
        self.sent = []

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def send(self, item, text):
        self.sent.append((item.incoming_message_id, text))
        return {"message_id": "-00041"}

    async def idle(self):
        await asyncio.wait_for(self.service.wait_idle(), 5)

    def saved(self, incoming="1"):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM receipts WHERE incoming_message_id=?",
                               (incoming,)).fetchone()
            return dict(row) if row else None

    async def test_ignored_admission_has_static_reason_without_widening_trigger(self):
        cases = (
            (event(text="ordinary synthetic input"), "wake_not_matched"),
            (event(group_id="999", mentions=("900",)), "group_not_allowed"),
            (event(sender_id="900", mentions=("900",)), "self_message"),
            (event(platform_id="other", mentions=("900",)), "wrong_identity"),
            (event(post_type="notice"), "not_message"),
        )
        for item, reason in cases:
            result = await self.service.accept(item, self.send)
            self.assertEqual(result, "IGNORED")
            self.assertEqual(result.reason, reason)
            self.assertFalse(self.service.eligible(item))
        self.assertIsNone(self.saved())
        self.assertFalse(self.sent)
        self.service.enabled = lambda: False
        self.assertEqual((await self.service.accept(event(), self.send)).reason, "disabled")
        self.service._closing = True
        self.assertEqual((await self.service.accept(event(), self.send)).reason, "closing")

    async def test_weixin_private_stable_id_and_acceptance_survive_restart(self):
        self.service.settings = settings(id_kind="weixin", platform_id="fixture-wechat",
                                         bot_self_id="fixture-wechat", admin_private_ids=frozenset({"wx-user"}),
                                         allowed_group_ids=frozenset())
        item = Envelope("fixture-wechat", "fixture-wechat", "wx-user", "raw:0001", "private", text="中性测试")
        async def accepted(event, text):
            self.sent.append((event.incoming_message_id, text))
            return ApiAccepted()
        self.assertEqual(await self.service.accept(replace(item, incoming_message_id=None), accepted), "INVALID")
        self.assertEqual(await self.service.accept(replace(item, kind="group", group_id="wx-group"), accepted), "IGNORED")
        self.assertEqual(await self.service.accept(replace(item, sender_id="other"), accepted), "IGNORED")
        self.assertEqual(await self.service.accept(item, accepted), "ACCEPTED")
        self.assertEqual(await self.service.accept(item, accepted), "DUPLICATE")
        await self.idle()
        row = self.saved("raw:0001")
        self.assertEqual(row["status"], "API_ACCEPTED")
        self.assertIsNone(row["outgoing_message_id"])
        self.assertTrue(row["delivery_ref"])
        configured = self.service.settings
        await self.service.close()
        self.store = ReceiptStore(self.path)
        await self.store.open()
        self.service = GateService(configured, self.store)
        self.assertEqual(await self.service.accept(item, accepted), "DUPLICATE")
        self.assertEqual(len(self.sent), 1)

    async def test_scope_and_wake_policy(self):
        cases = [
            (event(), True), (event(text="前文三叶后文"), True),
            (event(text="", mentions=("900",)), True),
            (event(kind="private", group_id=None, text="无名字"), True),
            (event(kind="private", group_id=None, sender_id="101"), False),
            (event(platform_id="other"), False), (event(bot_self_id="901"), False),
            (event(group_id="999"), False), (event(sender_id="900"), False),
            (event(text="", mentions=("all",)), False), (event(text=""), False),
            (event(post_type="notice"), False), (event(kind="other"), False),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(self.service.eligible(item), expected)
        disabled = GateService(settings(enabled=False), self.store)
        self.assertFalse(disabled.eligible(event()))
        empty = GateService(settings(admin_private_ids=frozenset(),
                                     allowed_group_ids=frozenset()), self.store)
        self.assertFalse(empty.eligible(event()))
        self.assertFalse(empty.eligible(event(kind="private")))

    async def test_or_wakes_create_one_logical_send_and_receipt(self):
        self.service.matcher = WakeMatcher(("三叶",), (r"^测试唤醒",), self.logs.append)
        result = await self.service.accept(event(text="测试唤醒三叶", mentions=("900",)), self.send)
        self.assertEqual(result, "ACCEPTED")
        await self.idle()
        self.assertEqual(self.sent, [("1", NEUTRAL_REPLY)])
        self.assertEqual(self.saved()["status"], "SENT")
        self.assertEqual(self.saved()["outgoing_message_id"], "-41")

    async def test_concurrent_duplicates_and_canonical_ids(self):
        results = await asyncio.gather(*(self.service.accept(event(i), self.send)
                                        for i in ["1", "001", " 1 ", 1] * 4))
        await self.idle()
        self.assertEqual(results.count("ACCEPTED"), 1)
        self.assertEqual(results.count("DUPLICATE"), 15)
        self.assertEqual(len(self.sent), 1)

    async def test_distinct_ids_allow_identical_text(self):
        await self.service.accept(event("1"), self.send)
        await self.service.accept(event("2"), self.send)
        await self.idle()
        self.assertEqual([item[0] for item in self.sent], ["1", "2"])

    async def test_missing_id_does_not_send(self):
        for incoming in [None, "", "0"]:
            self.assertEqual(await self.service.accept(event(incoming), self.send), "INVALID")
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.saved())

    async def test_failure_missing_receipt_and_transport_never_retry(self):
        cases = [(DefiniteSendFailure("secret-body"), "FAILED"),
                 (RuntimeError("secret-body"), "UNKNOWN"), ({}, "UNKNOWN"),
                 ({"message_id": 0}, "UNKNOWN"), (None, "UNKNOWN")]
        for i, (result, expected) in enumerate(cases, 1):
            async def sender(item, text, result=result):
                if isinstance(result, Exception):
                    raise result
                return result
            item = event(str(i), text="三叶 secret-body")
            await self.service.accept(item, sender)
            await self.idle()
            self.assertEqual(self.saved(str(i))["status"], expected)
            self.assertEqual(await self.service.accept(item, sender), "DUPLICATE")
        self.assertNotIn("secret-body", json.dumps(self.logs, ensure_ascii=False))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertNotIn("secret-body", "\n".join(conn.iterdump()))

    async def test_timeout_late_result_cannot_become_sent(self):
        self.service.settings = settings(send_timeout_seconds=0.05)
        started, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def stubborn(item, text):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            returned.set()
            return {"message_id": 42}

        await self.service.accept(event(), stubborn)
        await asyncio.wait_for(started.wait(), 5)
        await self.idle()
        self.assertEqual(self.saved()["status"], "UNKNOWN")
        self.assertEqual(self.saved()["reason"], "send_timeout")
        release.set()
        await asyncio.wait_for(returned.wait(), 5)
        await asyncio.sleep(0)
        self.assertEqual(self.saved()["status"], "UNKNOWN")
        self.assertIsNone(self.saved()["outgoing_message_id"])
        self.assertEqual(await self.service.accept(event(), self.send), "DUPLICATE")

    async def test_persistent_dedup_and_restart_reconciliation(self):
        await self.service.accept(event("1"), self.send)
        await self.idle()
        await self.service.close()
        staging = ReceiptStore(self.path)
        await staging.open()
        await staging.claim(event("2"))
        await staging.transition(event("2"), "ACCEPTED", "IN_FLIGHT", "dispatch")
        await staging.claim(event("3"))
        await staging.close()
        self.store = ReceiptStore(self.path)
        await self.store.open()
        self.service = GateService(settings(), self.store)
        for incoming in ["1", "2", "3"]:
            self.assertEqual(await self.service.accept(event(incoming), self.send), "DUPLICATE")
        self.assertEqual(self.saved("2")["status"], "UNKNOWN")
        self.assertEqual(self.saved("3")["status"], "ABORTED")
        self.assertEqual(len(self.sent), 1)

    async def test_listener_admits_while_session_send_waits_and_bounds_pending(self):
        self.service.settings = settings(max_pending=2, send_timeout_seconds=2)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def delayed(item, text):
            calls.append(item.incoming_message_id)
            entered.set()
            await release.wait()
            return {"message_id": 40 + len(calls)}

        await self.service.accept(event("1"), delayed)
        await asyncio.wait_for(entered.wait(), 5)
        result = await asyncio.wait_for(self.service.accept(event("2"), delayed), 0.5)
        self.assertEqual(result, "ACCEPTED")
        self.assertEqual(calls, ["1"])
        self.assertEqual(await self.service.accept(event("3"), delayed), "REJECTED")
        release.set()
        await self.idle()
        self.assertEqual(calls, ["1", "2"])
        self.assertEqual(self.saved("3")["status"], "REJECTED")

    async def test_global_parallel_limit(self):
        self.service.settings = settings(send_timeout_seconds=2)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def delayed(item, text):
            calls.append(item.incoming_message_id)
            if len(calls) == 3:
                entered.set()
            await release.wait()
            return {"message_id": 42}

        for i in range(4):
            await self.service.accept(event(str(i + 1), group_id=str(200 + i)), delayed)
        await asyncio.wait_for(entered.wait(), 5)
        self.assertEqual(len(calls), 3)
        release.set()
        await self.idle()
        self.assertEqual(len(calls), 4)

    async def test_close_cancels_pending_and_inflight_and_is_idempotent(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def delayed(item, text):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        await self.service.accept(event("1"), delayed)
        await asyncio.wait_for(entered.wait(), 5)
        await self.service.accept(event("2"), delayed)
        await self.service.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.saved("1")["status"], "UNKNOWN")
        self.assertEqual(self.saved("2")["status"], "ABORTED")
        self.assertFalse(self.service._workers)
        self.assertFalse(self.service._sends)
        self.assertEqual(await self.service.accept(event("3"), self.send), "IGNORED")
        await self.service.close()

    async def test_close_waits_for_admission_claim(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_claim = self.store.claim

        async def paused_claim(item):
            entered.set()
            await release.wait()
            return await original_claim(item)

        self.store.claim = paused_claim
        admission = asyncio.create_task(self.service.accept(event(), self.send))
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(self.service.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        self.assertEqual(await admission, "ABORTED")
        await closing
        self.assertEqual(self.saved()["status"], "ABORTED")
        self.assertEqual(self.sent, [])

    async def test_dynamic_disable_blocks_queued_send(self):
        enabled = True
        self.service.enabled = lambda: enabled
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(item, text):
            self.sent.append(item.incoming_message_id)
            entered.set()
            await release.wait()
            return {"message_id": 42}

        await self.service.accept(event("1"), delayed)
        await asyncio.wait_for(entered.wait(), 5)
        await self.service.accept(event("2"), delayed)
        enabled = False
        release.set()
        await self.idle()
        self.assertEqual(self.sent, ["1"])
        self.assertEqual(self.saved("2")["status"], "ABORTED")


if __name__ == "__main__":
    unittest.main()
