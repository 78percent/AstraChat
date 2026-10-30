"""Synthetic pacing tests; no live credentials, models or QQ messages."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.gate import ConfigurationError, GateSettings, ReceiptStore, WakeMatcher
from plugins.astrbot_plugin_roleplay_gateway.reply_pacing import ReplyPacing, stable_uniform
from plugins.astrbot_plugin_roleplay_gateway.stream_reply import ReplyLimits
import test_roleplay_stream as roleplay_fixtures
from test_roleplay_stream import fixture_event, reply_stream
from test_provider_gate import FakeStream, response


def fast_policy(**changes):
    return replace(ReplyPacing(enabled=True, probability=1,
                               first_delay_min=.015, first_delay_max=.025,
                               first_delay_mean=.02, first_delay_std=.003,
                               interval_min=.005, interval_max=.01,
                               interval_mean=.0075, interval_std=.0015,
                               cooldown_seconds=0, window_seconds=.6, max_rounds=100), **changes)


class PacingConfigurationTests(unittest.TestCase):
    def test_old_configuration_remains_disabled_and_defaults_are_explicit(self):
        self.assertFalse(GateSettings.from_mapping({}).reply_pacing.enabled)
        policy = ReplyPacing()
        self.assertEqual((policy.probability, policy.max_bubbles, policy.max_characters), (.6, 4, 125))
        self.assertEqual((policy.cooldown_seconds, policy.window_seconds, policy.max_rounds), (15, 60, 3))
        self.assertEqual(replace(policy, enabled=True).validate_deadlines(
            first=4, generation=25, total=30, send_timeout=8), 18)

    def test_invalid_settings_rejected_instead_of_silently_disabled(self):
        bad = [None, [], {"enabled": "yes"}, {"unknown": 1}, {"probability": -1},
               {"probability": 1.01}, {"probability": True}, {"first_delay_std": 0},
               {"interval_std": float("nan")}, {"first_delay_min": 10},
               {"first_delay_mean": 30}, {"max_bubbles": 0}, {"max_characters": 1.5},
               {"max_rounds": True}, {"window_seconds": 0}, {"cooldown_seconds": 61},
               {"enabled": True, "max_bubbles": 16}]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ConfigurationError):
                GateSettings.from_mapping({"reply_pacing": raw})
        with self.assertRaises(ConfigurationError):
            GateSettings.from_mapping({"reply_pacing": {"enabled": True}, "send_timeout_seconds": 20})

    def test_real_truncated_distribution_is_stable_and_not_clamped(self):
        policy = ReplyPacing()
        samples = [policy.sample(str(i), "first_delay") for i in range(5000)]
        self.assertTrue(all(5 < value < 10 for value in samples))
        self.assertAlmostEqual(statistics.mean(samples), 7.5, delta=.06)
        self.assertLess(statistics.stdev(samples), 1.25)
        self.assertEqual(samples, [policy.sample(str(i), "first_delay") for i in range(5000)])
        self.assertNotEqual(stable_uniform("platform-a\01"), stable_uniform("platform-b\01"))


class PacingStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory(prefix="pacing-store-", dir=ROOT / "tests")
        self.path = Path(self.temp.name) / "receipts.sqlite3"
        self.store = ReceiptStore(self.path)
        await self.store.open()
        self.policy = ReplyPacing(enabled=True, probability=1)

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def decide(self, message, when, policy=None):
        await self.store.claim(message, purpose="roleplay")
        return await self.store.decide_pacing(message, policy or self.policy, now=when)

    async def test_parallel_cooldown_and_sessions_are_independent(self):
        decisions = await asyncio.gather(*(self.decide(fixture_event(str(i)), 100) for i in range(1, 4)))
        self.assertEqual(sum(item.selected for item in decisions), 1)
        self.assertEqual([d.reason for d in decisions].count("cooldown"), 2)
        other = await self.decide(fixture_event("4", kind="private"), 100)
        another = await self.decide(fixture_event("5", kind="private", sender="101"), 100)
        self.assertTrue(other.selected and another.selected)

    async def test_rolling_quota_boundary_and_failure_does_not_refund(self):
        for incoming, at in [("1", 100), ("2", 115), ("3", 130)]:
            event = fixture_event(incoming)
            self.assertTrue((await self.decide(event, at)).selected)
            await self.store.transition(event, "ACCEPTED", "FAILED", "synthetic_model_failure")
        self.assertEqual((await self.decide(fixture_event("4"), 145)).reason, "quota")
        self.assertTrue((await self.decide(fixture_event("5"), 160)).selected)

    async def test_probability_zero_and_one_replay_across_restart(self):
        message = fixture_event("1")
        skipped = await self.decide(message, 100, replace(self.policy, probability=0))
        self.assertEqual(skipped.reason, "probability")
        self.assertEqual((await self.store.get(message.platform_id, "1"))["status"], "SKIPPED")
        await self.store.close()
        self.store = ReceiptStore(self.path)
        await self.store.open()
        self.assertFalse(await self.store.claim(message, purpose="roleplay"))
        self.assertEqual(await self.store.decide_pacing(message, self.policy, now=200), skipped)
        self.assertTrue((await self.decide(fixture_event("2"), 200)).selected)

    async def test_stable_sixty_percent_across_same_text_distinct_messages(self):
        policy = replace(self.policy, cooldown_seconds=0, max_rounds=100)
        selected = []
        for index in range(200):
            item = fixture_event(str(index + 1), kind="private", sender=str(1000 + index))
            selected.append((await self.decide(item, 100, replace(policy, probability=.6))).selected)
        self.assertGreater(sum(selected), 90)
        self.assertLess(sum(selected), 150)

    async def test_claim_crash_aborts_without_resampling_and_reserved_quota_survives_restart(self):
        undecided, admitted = fixture_event("1"), fixture_event("2")
        await self.store.claim(undecided, purpose="roleplay")
        await self.decide(admitted, 100)
        await self.store.close()
        self.store = ReceiptStore(self.path)
        await self.store.open()
        for item in (undecided, admitted):
            self.assertFalse(await self.store.claim(item, purpose="roleplay"))
            self.assertEqual((await self.store.get(item.platform_id, item.incoming_message_id))["status"], "ABORTED")
        self.assertEqual((await self.decide(fixture_event("3"), 101)).reason, "cooldown")
        with self.assertRaises(RuntimeError):
            await self.store.decide_pacing(undecided, self.policy, now=102)

    async def test_migration_preserves_old_rows_and_separates_legacy_denominator(self):
        await self.store.close()
        old_path = Path(self.temp.name) / "version1.sqlite3"
        with closing(sqlite3.connect(old_path)) as db, db:
            db.execute("""CREATE TABLE receipts (platform_id TEXT NOT NULL, incoming_message_id TEXT NOT NULL,
                bot_self_id TEXT NOT NULL, sender_id TEXT NOT NULL, group_id TEXT, kind TEXT NOT NULL,
                status TEXT NOT NULL, reason TEXT NOT NULL, received_at REAL NOT NULL, updated_at REAL NOT NULL,
                outgoing_message_id TEXT, PRIMARY KEY(platform_id,incoming_message_id))""")
            db.execute("INSERT INTO receipts VALUES ('old','1','900','100',NULL,'private','SENT','old',1,2,'3')")
            db.execute("PRAGMA user_version=1")
        self.store = ReceiptStore(old_path)
        await self.store.open()
        old = await self.store.get("old", "1")
        self.assertEqual((old["status"], old["outgoing_message_id"], old["purpose"]), ("SENT", "3", "legacy"))


class PacingServiceTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the existing synthetic provider/database fixture, not its test cases.
    open_stores = roleplay_fixtures.RoleplayStreamTests.open_stores
    factory = roleplay_fixtures.RoleplayStreamTests.factory
    make_service = roleplay_fixtures.RoleplayStreamTests.make_service
    asyncTearDown = roleplay_fixtures.RoleplayStreamTests.asyncTearDown
    send = roleplay_fixtures.RoleplayStreamTests.send
    dispatch = roleplay_fixtures.RoleplayStreamTests.dispatch
    payload = roleplay_fixtures.RoleplayStreamTests.payload

    async def asyncSetUp(self):
        await roleplay_fixtures.RoleplayStreamTests.asyncSetUp(self)
        await self.reconfigure(fast_policy())

    async def reconfigure(self, policy):
        await self.service.close()
        self.settings = replace(self.settings, reply_pacing=policy)
        await self.open_stores()
        self.service = self.make_service()

    async def saved(self, incoming="1"):
        return await self.receipts.get("fixture-qq", incoming)

    async def test_zero_probability_all_wakes_skip_without_model_or_memory(self):
        await self.reconfigure(fast_policy(probability=0))
        self.service.matcher = WakeMatcher(("机器人",), (r"^regex-only$",), lambda _: None)
        items = [fixture_event("1", kind="private"),
                 fixture_event("2", text="显式提及"),
                 replace(fixture_event("3"), mentions=()),
                 replace(fixture_event("4", text="regex-only", group="201"), mentions=())]
        for item in items:
            self.assertEqual(await self.dispatch(item), "SKIPPED")
            self.assertEqual((await self.saved(item.incoming_message_id))["reason"], "pacing_probability")
            self.assertFalse((await self.memory.snapshot(self.service.namespace(item))).turns)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.outputs, [])
        self.assertEqual(await self.dispatch(items[0]), "DUPLICATE")
        self.assertEqual(await self.dispatch(fixture_event("5", sender="999", kind="private")), "IGNORED")
        self.assertIsNone(await self.saved("5"))

    async def test_static_management_bypasses_zero_probability_and_quota(self):
        await self.reconfigure(fast_policy(probability=0))
        event = fixture_event(kind="private", text="/记忆 查看")
        await self.dispatch(event)
        self.assertTrue(self.outputs)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual((await self.saved())["purpose"], "management")
        with closing(sqlite3.connect(self.base / "receipts.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pacing_decisions").fetchone()[0], 0)

    async def test_pending_rejection_is_in_full_denominator_before_policy(self):
        self.service.settings = replace(self.service.settings, max_pending=0)
        self.assertEqual(await self.dispatch(), "REJECTED")
        row = await self.saved()
        self.assertEqual((row["purpose"], row["reason"]), ("roleplay", "pending_limit"))
        self.assertEqual(self.backend.calls, [])

    async def test_policy_exception_settles_claim_without_starting_model(self):
        with patch.object(self.receipts, "decide_pacing", side_effect=RuntimeError("fixture")):
            with self.assertRaises(RuntimeError):
                await self.service.accept(fixture_event(), self.send)
        row = await self.saved()
        self.assertEqual((row["purpose"], row["status"], row["reason"]),
                         ("roleplay", "ABORTED", "admission_policy_failed"))
        self.assertFalse(self.service._queued_epochs)
        self.assertEqual(self.backend.calls, [])

    async def test_exact_four_complete_records_stop_before_unfinished_json_tail(self):
        items = ["甲。乙。", "丙。", "丁。", "戊。"]
        raw = '{"speech":[' + ','.join(json.dumps(x, ensure_ascii=False) for x in items) + ',"unfinished'
        stream = FakeStream([response(raw)])
        self.backend.streams.append(stream)
        await self.dispatch()
        self.assertEqual([text for _, text in self.outputs], items)
        self.assertEqual((await self.saved())["reason"], "policy_limit")
        self.assertTrue(stream.close_completed)
        self.assertEqual(stream.close_calls, 1)
        snapshot = await self.memory.snapshot(self.service.namespace(fixture_event()))
        self.assertFalse(snapshot.turns[0].complete)
        self.assertEqual([row.text for row in snapshot.turns[0].replies], items)

    async def test_exact_character_boundary_keeps_sentence_and_stops_provider(self):
        first, second = "甲" * 59 + "。", "乙" * 64 + "。"
        self.backend.streams.append(reply_stream(first + "\n\n" + second + "\n\n尾巴。"))
        await self.dispatch()
        self.assertEqual(sum(len(text) for _, text in self.outputs), 125)
        self.assertEqual([text for _, text in self.outputs], [first, second])
        self.assertEqual((await self.saved())["reason"], "policy_limit")

    async def test_next_complete_sentence_over_limit_is_not_partly_sent(self):
        first, second = "甲" * 69 + "。", "乙" * 69 + "。"
        self.backend.streams.append(reply_stream(first + "\n\n" + second + "\n\n不要发送。"))
        await self.dispatch()
        self.assertEqual([text for _, text in self.outputs], [first])
        self.assertEqual((await self.saved())["reason"], "policy_limit")

    async def test_overlong_first_sentence_counts_unanswered_without_placeholder(self):
        self.backend.streams.append(reply_stream("甲" * 125 + "。"))
        await self.dispatch()
        row = await self.saved()
        self.assertEqual((row["reason"], row["confirmed_segments"], row["first_ack_at"]),
                         ("policy_limit", 0, None))
        self.assertEqual(self.outputs, [])
        self.assertFalse((await self.memory.snapshot(self.service.namespace(fixture_event()))).turns)

    async def test_unknown_send_does_not_enter_history_or_first_ack_metrics(self):
        async def uncertain(event, text):
            return {}
        self.backend.streams.append(reply_stream("完整。\n\n下一句。"))
        await self.dispatch(sender=uncertain)
        self.assertEqual((await self.saved())["status"], "UNKNOWN")
        self.assertIsNone((await self.saved())["first_ack_at"])
        self.assertFalse((await self.memory.snapshot(self.service.namespace(fixture_event()))).turns)

    async def test_ingress_delay_overlaps_fast_generation_and_ack_timestamp_is_not_final(self):
        await self.reconfigure(fast_policy(first_delay_min=.10, first_delay_max=.12,
                                          first_delay_mean=.11, first_delay_std=.005,
                                          interval_min=.04, interval_max=.05,
                                          interval_mean=.045, interval_std=.002))
        event = replace(fixture_event(), received_at=time.time())
        sends = []
        async def sender(item, text):
            sends.append(time.time())
            return await self.send(item, text)
        self.backend.streams.append(reply_stream("第一句。\n\n第二句。"))
        await self.dispatch(event, sender=sender)
        row = await self.saved()
        self.assertGreaterEqual(sends[0] - event.received_at, .095)
        self.assertLess(sends[0] - event.received_at, .25)
        self.assertGreaterEqual(sends[1] - sends[0], .039)
        self.assertLess(abs(row["first_ack_at"] - sends[0]), .025)
        self.assertLess(row["first_ack_at"], sends[1])
        self.assertEqual(row["confirmed_segments"], 2)

    async def test_old_ingress_does_not_add_fresh_delay(self):
        await self.reconfigure(fast_policy(first_delay_min=.4, first_delay_max=.5,
                                          first_delay_mean=.45, first_delay_std=.025))
        start = time.monotonic()
        event = replace(fixture_event(), received_at=time.time() - 1)
        await self.dispatch(event)
        self.assertLess(time.monotonic() - start, .3)
        self.assertGreater((await self.saved())["first_ack_at"] - event.received_at, 1)

    async def test_slow_generation_after_target_sends_without_second_wait(self):
        await self.reconfigure(fast_policy(first_delay_min=.08, first_delay_max=.10,
                                          first_delay_mean=.09, first_delay_std=.005))
        stream = reply_stream(block=True)
        self.backend.streams.append(stream)
        event = replace(fixture_event(), received_at=time.time())
        await self.service.accept(event, self.send)
        await asyncio.wait_for(stream.entered.wait(), 1)
        await asyncio.sleep(.13)
        released = time.monotonic()
        stream.release.set()
        await asyncio.wait_for(self.service.wait_idle(), 2)
        self.assertLess(time.monotonic() - released, .07)
        self.assertEqual(len(self.outputs), 1)

    async def test_epoch_clear_during_first_wait_prevents_dispatch_and_history(self):
        await self.reconfigure(fast_policy(first_delay_min=.15, first_delay_max=.2,
                                          first_delay_mean=.175, first_delay_std=.01))
        event = fixture_event()
        await self.service.accept(event, self.send)
        await asyncio.wait_for(self.backend.create_entered.wait(), 1)
        await asyncio.sleep(.03)
        await self.service.clear_memory(self.service.namespace(event))
        await asyncio.wait_for(self.service.wait_idle(), 2)
        self.assertEqual(self.outputs, [])
        self.assertFalse((await self.memory.snapshot(self.service.namespace(event))).turns)

    async def test_shutdown_during_first_wait_leaves_no_late_send(self):
        await self.reconfigure(fast_policy(first_delay_min=.15, first_delay_max=.2,
                                          first_delay_mean=.175, first_delay_std=.01))
        await self.service.accept(fixture_event(), self.send)
        await asyncio.wait_for(self.backend.create_entered.wait(), 1)
        await asyncio.sleep(.02)
        await self.service.close()
        await asyncio.sleep(.2)
        self.assertEqual(self.outputs, [])
        self.assertFalse(self.service._first_send_targets)

    async def test_unfinished_provider_is_not_policy_limit_when_under_budget(self):
        self.backend.streams.append(FakeStream([response('{"speech":["完整一句。",')]))
        await self.dispatch()
        self.assertNotEqual((await self.saved())["reason"], "policy_limit")

    async def test_same_session_rounds_remain_serial_during_paced_sending(self):
        self.backend.streams.extend([reply_stream("一甲。\n\n一乙。"), reply_stream("二甲。\n\n二乙。")])
        admitted = await asyncio.gather(self.service.accept(fixture_event("1"), self.send),
                                       self.service.accept(fixture_event("2"), self.send))
        self.assertEqual(admitted, ["ACCEPTED", "ACCEPTED"])
        await asyncio.wait_for(self.service.wait_idle(), 2)
        self.assertEqual([incoming for incoming, _ in self.outputs], ["1", "1", "2", "2"])

    async def test_policy_provider_cleanup_failure_does_not_claim_success(self):
        stream = reply_stream("一。\n\n二。\n\n三。\n\n四。", fail_close=True)
        self.backend.streams.append(stream)
        self.expected_close_error = True
        await self.dispatch()
        self.assertTrue(self.service.blocked)
        self.assertNotEqual((await self.saved())["status"], "SENT")
        # A validated first bubble can already be awaiting its scheduled send
        # when transport cleanup fails. Keep its real ACK, never claim a whole
        # reply or send the remaining buffered records after detecting failure.
        self.assertLessEqual(len(self.outputs), 1)
        if self.outputs:
            self.assertEqual(self.outputs[0][1], "一。")
        self.assertEqual((await self.saved())["confirmed_segments"], len(self.outputs))
        self.assertTrue(stream.close_completed is False)

    async def test_total_deadline_cuts_paced_tail_and_retains_confirmed_prefix(self):
        await self.service.close()
        self.limits = ReplyLimits(first_receipt_seconds=.25, generation_seconds=.35,
                                  total_seconds=.4, cleanup_seconds=.15, flush_seconds=.01)
        self.settings = replace(self.settings, send_timeout_seconds=.2,
                                reply_pacing=fast_policy(max_bubbles=2, interval_min=.28, interval_max=.30,
                                                         interval_mean=.29, interval_std=.003))
        await self.open_stores()
        self.service = self.make_service()
        self.backend.streams.append(reply_stream("一。\n\n二。\n\n三。\n\n四。"))
        async def slow_ack(event, text):
            # The second submission cannot begin before .15 + .28 = .43s,
            # beyond the .4s total limit. Do not put the assertion on an ACK
            # deadline race where a real late SENT is conservatively excluded
            # from the coordinator's committable prefix.
            await asyncio.sleep(.15)
            return await self.send(event, text)
        await self.dispatch(sender=slow_ack)
        row = await self.saved()
        self.assertLess(row["confirmed_segments"], 4)
        self.assertGreater(row["confirmed_segments"], 0)
        self.assertIn(row["reason"], {"send_deadline_unknown", "total_deadline"})
        snapshot = await self.memory.snapshot(self.service.namespace(fixture_event()))
        self.assertEqual(len(snapshot.turns[0].replies), row["confirmed_segments"])
        self.assertFalse(snapshot.turns[0].complete)

    async def test_missing_ingress_timestamp_uses_admission_time_not_epoch_zero(self):
        event = replace(fixture_event(), received_at=float("nan"))
        before = time.time()
        await self.dispatch(event)
        row = await self.saved()
        self.assertGreaterEqual(row["received_at"], before)
        self.assertLess(row["first_ack_at"] - row["received_at"], 1)


if __name__ == "__main__":
    unittest.main()
