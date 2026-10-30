"""Neutral synthetic delivery only; SQLite files live in TemporaryDirectory."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.segment_delivery import (
    AttemptToken, DefiniteSendFailure, InputRef, SegmentLedger, SegmentSender,
)
from plugins.astrbot_plugin_roleplay_gateway.gate import ApiAccepted


TOKEN = AttemptToken("fixture-qq", "group:100", "batch-a", 0, 0)
REFS = (InputRef("101", "201"), InputRef("102", "202"))


class StubbornSend:
    def __init__(self, response=None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0
        self.response = response if response is not None else {"message_id": 333}

    async def __call__(self, text):
        self.calls += 1
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
        return self.response


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "segments.sqlite"
        self.ledger = SegmentLedger(self.path)
        await self.ledger.open()
        self.senders = []
        self.stubborn = []
        self.current = TOKEN
        self.calls = []

    async def asyncTearDown(self):
        for fake in self.stubborn:
            fake.release.set()
        for sender in self.senders:
            await sender.close()
        await self.ledger.close()
        self.directory.cleanup()

    async def receipt(self, text):
        self.calls.append(text)
        return {"message_id": 300 + len(self.calls)}

    def sender(self, send_call=None, **kwargs):
        token = kwargs.pop("token", TOKEN)
        sender = SegmentSender(self.ledger, token, send_call or self.receipt,
                               kwargs.pop("is_current", lambda token: token == self.current),
                               segment_interval=kwargs.pop("segment_interval", 0),
                               send_timeout=kwargs.pop("send_timeout", 0.05),
                               close_timeout=kwargs.pop("close_timeout", 0.01), **kwargs)
        self.senders.append(sender)
        return sender

    async def test_weixin_api_acceptance_has_local_ref_no_platform_id_and_no_retry(self):
        async def accepted(text):
            self.calls.append(text)
            return ApiAccepted()
        refs = (InputRef("native:msg-a", "wx-user", "weixin"),)
        sender = self.sender(accepted, id_kind="weixin")
        first = await sender.send("中性第一句。", attribution=refs)
        second = await sender.send("中性第二句。", attribution=refs)
        self.assertEqual((first.status, second.status), ("API_ACCEPTED", "API_ACCEPTED"))
        self.assertIsNone(first.outgoing_message_id)
        self.assertTrue(first.commit_allowed)
        self.assertTrue(first.has_success_evidence)
        self.assertNotEqual(first.delivery_ref, second.delivery_ref)
        self.assertFalse(await sender.can_fallback())
        rows = await self.ledger.records(TOKEN)
        self.assertEqual(rows[0]["delivery_ref"], first.delivery_ref)
        self.assertIsNone(rows[0]["outgoing_message_id"])
        await sender.close()
        await self.ledger.close()
        self.ledger = SegmentLedger(self.path)
        await self.ledger.open()
        restored = self.sender(accepted, id_kind="weixin")
        duplicate = await restored.send("不应重发。", attribution=refs)
        self.assertTrue(duplicate.duplicate)
        self.assertFalse(duplicate.commit_allowed)
        self.assertEqual(duplicate.delivery_ref, first.delivery_ref)
        self.assertEqual(len(self.calls), 2)
        self.current = replace(TOKEN, attempt_generation=1)
        newer = self.sender(accepted, id_kind="weixin", token=self.current)
        self.assertEqual((await newer.send("不应回退。", attribution=refs)).reason, "batch_blocked")

    async def test_qq_input_and_receipt_validation_remain_strict(self):
        with self.assertRaises(ValueError):
            InputRef("native:msg", "wx-user")
        async def wrong_protocol(text):
            return ApiAccepted()
        result = await self.sender(wrong_protocol).send("中性。", attribution=REFS)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.commit_allowed)

    async def test_weixin_timeout_and_cancel_discard_late_api_acceptance(self):
        refs = (InputRef("msg-a", "wx-user", "weixin"),)
        for cancel in (False, True):
            token = replace(TOKEN, batch_id="wx-cancel" if cancel else "wx-timeout")
            fake = StubbornSend(ApiAccepted())
            self.stubborn.append(fake)
            sender = self.sender(fake, id_kind="weixin", token=token, is_current=lambda _: True,
                                 send_timeout=1 if cancel else .05)
            task = asyncio.create_task(sender.send("未知结果。", attribution=refs))
            await fake.entered.wait()
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                self.assertEqual((await task).status, "UNKNOWN")
            self.assertFalse(await sender.can_fallback())
            fake.release.set()
            for _ in range(10):
                await asyncio.sleep(0)
            row = (await self.ledger.records(token))[0]
            self.assertEqual(row["status"], "UNKNOWN")
            self.assertIsNone(row["outgoing_message_id"])
            self.assertEqual(fake.calls, 1)

    async def test_v1_qq_segment_ledger_migrates_without_changing_evidence(self):
        await self.ledger.close()
        old_path = Path(self.directory.name) / "old-segments.sqlite"
        with closing(sqlite3.connect(old_path)) as db, db:
            db.execute("""CREATE TABLE segments (
                platform_id TEXT NOT NULL, session_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, attempt_generation INTEGER NOT NULL, memory_epoch INTEGER NOT NULL,
                attribution_json TEXT NOT NULL, claim_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('UNSENT','IN_FLIGHT','SENT','UNKNOWN','FAILED')),
                reason TEXT NOT NULL, outgoing_message_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(platform_id,session_id,batch_id,attempt_generation,sequence))""")
            db.execute("INSERT INTO segments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (*TOKEN.batch_key, 0, 0, 0, '[["101","201"]]', "legacy-local",
                        "SENT", "acknowledged", "-123", 1.0, 2.0))
            db.execute(f"PRAGMA application_id={SegmentLedger._APPLICATION_ID}")
            db.execute("PRAGMA user_version=1")
        self.ledger = SegmentLedger(old_path)
        await self.ledger.open()
        row = (await self.ledger.records(TOKEN))[0]
        self.assertEqual((row["status"], row["outgoing_message_id"], row["updated_at"]), ("SENT", "-123", 2.0))
        self.assertEqual(row["delivery_ref"], "legacy-local")
        with closing(sqlite3.connect(old_path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    async def test_two_confirmed_segments_survive_later_stream_failure(self):
        sender = self.sender()
        async def neutral_stream():
            yield "First neutral sentence."
            yield "Second neutral sentence."
            raise ConnectionError("synthetic stream interruption")
        outcomes = []
        with self.assertRaises(ConnectionError):
            async for text in neutral_stream():
                outcomes.append(await sender.send(text, attribution=REFS))
        self.assertEqual([item.status for item in outcomes], ["SENT", "SENT"])
        self.assertTrue(all(item.commit_allowed for item in outcomes))
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(await sender.can_fallback())
        rows = await self.ledger.records(TOKEN)
        self.assertEqual([row["sequence"] for row in rows], [0, 1])
        self.assertEqual(json.loads(rows[0]["attribution_json"]), [["101", "201"], ["102", "202"]])
        self.assertEqual(rows[0]["memory_epoch"], 0)
        self.assertEqual(rows[0]["attempt_generation"], 0)
        for row in rows:
            self.assertNotIn("text", row)
            self.assertNotIn("First neutral", json.dumps(row))

    async def test_definite_first_failure_allows_fallback_but_stops_sender(self):
        async def rejected(text):
            self.calls.append(text)
            raise DefiniteSendFailure("synthetic reject")
        sender = self.sender(rejected)
        self.assertTrue(await sender.can_fallback())
        outcome = await sender.send("First.", attribution=REFS)
        self.assertEqual(outcome.status, "FAILED")
        self.assertTrue(await sender.can_fallback())
        later = await sender.send("Never send.", attribution=REFS)
        self.assertEqual(later.reason, "sender_stopped")
        self.assertEqual(len(self.calls), 1)

    async def test_definite_failure_after_sent_never_allows_fallback(self):
        async def second_fails(text):
            self.calls.append(text)
            if len(self.calls) > 1:
                raise DefiniteSendFailure()
            return {"message_id": -33}
        sender = self.sender(second_fails)
        self.assertEqual((await sender.send("One.", attribution=REFS)).outgoing_message_id, "-33")
        self.assertEqual((await sender.send("Two.", attribution=REFS)).status, "FAILED")
        self.assertFalse(await sender.can_fallback())

    async def test_explicit_new_generation_can_start_zero_after_definite_failure(self):
        async def rejected(text):
            raise DefiniteSendFailure()
        original = self.sender(rejected)
        self.assertEqual((await original.send("First attempt.", attribution=REFS)).status, "FAILED")
        self.assertTrue(await original.can_fallback())
        self.current = replace(TOKEN, attempt_generation=1)
        replacement = self.sender(token=self.current)
        outcome = await replacement.send("Explicit fallback.", attribution=REFS)
        self.assertEqual(outcome.sequence, 0)
        self.assertEqual(outcome.status, "SENT")
        self.assertFalse(outcome.duplicate)
        rows = await self.ledger.records(TOKEN)
        self.assertEqual([(row["attempt_generation"], row["sequence"], row["status"]) for row in rows],
                         [(0, 0, "FAILED"), (1, 0, "SENT")])

    async def test_unsent_old_generation_blocks_new_attempt(self):
        await self.ledger._reserve(TOKEN, 0, REFS, "old-unsent")
        self.current = replace(TOKEN, attempt_generation=1)
        replacement = self.sender(token=self.current)
        self.assertFalse(await replacement.can_fallback())
        outcome = await replacement.send("Must not overlap.", attribution=REFS)
        self.assertEqual(outcome.reason, "batch_blocked")
        self.assertFalse(outcome.commit_allowed)
        self.assertEqual(self.calls, [])

    async def test_claim_lost_during_ack_never_grants_memory_commit(self):
        # Another lifecycle owner has already conservatively settled the claim.
        original_transition = self.ledger._transition
        async def competing_transition(token, seq, claim, expected, status, reason, receipt=None):
            if status == "SENT":
                await original_transition(token, seq, claim, expected, "UNKNOWN", "external_settlement")
            return await original_transition(token, seq, claim, expected, status, reason, receipt)
        self.ledger._transition = competing_transition
        sender = self.sender()
        outcome = await sender.send("Neutral acknowledged text.", attribution=REFS)
        self.assertEqual(outcome.status, "UNKNOWN")
        self.assertEqual(outcome.reason, "claim_lost")
        self.assertFalse(outcome.commit_allowed)
        self.assertIsNone(outcome.outgoing_message_id)
        self.assertEqual((await self.ledger.records(TOKEN))[0]["status"], "UNKNOWN")

    async def test_timeout_late_ack_never_becomes_sent_or_committable(self):
        fake = StubbornSend()
        self.stubborn.append(fake)
        sender = self.sender(fake)
        outcome = await sender.send("Neutral delayed segment.", attribution=REFS)
        self.assertEqual(outcome.status, "UNKNOWN")
        self.assertFalse(outcome.commit_allowed)
        await fake.cancelled.wait()
        self.assertEqual(sender.pending_sends, 1)
        self.assertFalse(await sender.can_fallback())
        later = await sender.send("Must stop.", attribution=REFS)
        self.assertEqual(later.status, "FAILED")
        fake.release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(sender.pending_sends, 0)
        self.assertEqual(fake.calls, 1)
        rows = await self.ledger.records(TOKEN)
        self.assertEqual(rows[0]["status"], "UNKNOWN")
        self.assertIsNone(rows[0]["outgoing_message_id"])
        self.assertFalse(await sender.can_fallback())

    async def test_transport_failure_is_unknown_not_definite(self):
        async def broken(text):
            raise ConnectionError("private synthetic detail")
        sender = self.sender(broken)
        result = await sender.send("Neutral.", attribution=REFS)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.reason, "transport_error")
        self.assertFalse(await sender.can_fallback())
        self.assertNotIn("private", json.dumps(await self.ledger.records(TOKEN)))

    async def test_only_legal_actual_message_id_is_sent(self):
        for index, bad in enumerate((None, {}, {"message_id": 0}, {"message_id": True},
                                     {"message_id": "not-a-receipt"}, {"message_id": "１２"},
                                     {"message_id": 1.2})):
            token = replace(TOKEN, batch_id=f"invalid-{index}")
            async def invalid(text, result=bad):
                return result
            sender = self.sender(invalid, token=token, is_current=lambda token: True)
            outcome = await sender.send("Neutral.", attribution=REFS)
            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertFalse(outcome.commit_allowed)

    async def test_inflight_blocks_fallback(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(text):
            entered.set()
            await release.wait()
            return {"message_id": 80}
        sender = self.sender(blocked, send_timeout=1)
        task = asyncio.create_task(sender.send("Neutral.", attribution=REFS))
        await entered.wait()
        self.assertFalse(await sender.can_fallback())
        release.set()
        self.assertEqual((await task).status, "SENT")

    async def test_duplicate_sequence_not_resent_even_after_restart(self):
        sender = self.sender()
        await sender.send("Neutral original.", attribution=REFS)
        await sender.close()
        await self.ledger.close()
        self.ledger = SegmentLedger(self.path)
        await self.ledger.open()
        restored = self.sender()
        result = await restored.send("Different body cannot force retry.", attribution=REFS)
        self.assertTrue(result.duplicate)
        self.assertFalse(result.commit_allowed)
        self.assertEqual(result.outgoing_message_id, "301")
        self.assertEqual(len(self.calls), 1)

    async def test_restart_inflight_is_unknown_and_new_sequence_blocked(self):
        # Emulate the durable crash boundary, not an unobserved network success.
        await self.ledger._reserve(TOKEN, 0, REFS, "crash-fixture")
        await self.ledger._transition(TOKEN, 0, "crash-fixture", ("UNSENT",), "IN_FLIGHT", "dispatching")
        await self.ledger.close()
        self.ledger = SegmentLedger(self.path)
        await self.ledger.open()
        sender = self.sender()
        self.assertFalse(await sender.can_fallback())
        result = await sender.send("Never resend.", attribution=REFS, sequence=1)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertEqual(result.reason, "batch_blocked")
        self.assertEqual(self.calls, [])
        rows = await self.ledger.records(TOKEN)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "restart_in_flight")

    async def test_restart_unsent_is_failed_and_existing_sequence_not_reused(self):
        await self.ledger._reserve(TOKEN, 0, REFS, "reserved-fixture")
        await self.ledger.close()
        self.ledger = SegmentLedger(self.path)
        await self.ledger.open()
        sender = self.sender()
        self.assertTrue(await sender.can_fallback())
        result = await sender.send("Never retry old claim.", attribution=REFS)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(self.calls, [])

    async def test_epoch_changed_before_submission_does_not_send(self):
        sender = self.sender()
        self.current = replace(TOKEN, memory_epoch=1)
        result = await sender.send("Old neutral text.", attribution=REFS)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.reason, "stale_before_dispatch")
        self.assertFalse(result.commit_allowed)
        self.assertEqual(self.calls, [])

    async def test_epoch_changes_during_interval_no_next_submission(self):
        now = [0.0]
        async def sleep(delay):
            self.current = replace(TOKEN, memory_epoch=1)
            now[0] += delay
        sender = self.sender(segment_interval=1.5, clock=lambda: now[0], sleep=sleep)
        await sender.send("Current.", attribution=REFS)
        outcome = await sender.send("Stale second.", attribution=REFS)
        self.assertEqual(outcome.status, "FAILED")
        self.assertEqual(self.calls, ["Current."])

    async def test_epoch_change_inflight_records_delivery_not_memory_permission(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def sending(text):
            entered.set()
            await release.wait()
            return {"message_id": 901}
        sender = self.sender(sending, send_timeout=1)
        task = asyncio.create_task(sender.send("Old in-flight.", attribution=REFS))
        await entered.wait()
        self.current = replace(TOKEN, memory_epoch=1)
        release.set()
        outcome = await task
        self.assertEqual(outcome.status, "SENT")
        self.assertFalse(outcome.commit_allowed)
        self.assertEqual(outcome.reason, "stale_acknowledged")
        self.assertEqual((await self.ledger.records(TOKEN))[0]["outgoing_message_id"], "901")

    async def test_generation_change_blocks_old_attempt_and_route_after_first_send(self):
        sender = self.sender()
        await sender.send("Already sent.", attribution=REFS)
        self.current = replace(TOKEN, attempt_generation=1)
        outcome = await sender.send("Old generation.", attribution=REFS)
        self.assertEqual(outcome.status, "FAILED")
        replacement = self.sender(token=self.current)
        result = await replacement.send("Cannot switch after first send.", attribution=REFS, sequence=2)
        self.assertEqual(result.reason, "batch_blocked")
        self.assertFalse(result.commit_allowed)
        self.assertEqual(len(self.calls), 1)

    async def test_final_version_check_and_adapter_invocation_share_execution_turn(self):
        trace = []
        def current(token):
            trace.append("check")
            asyncio.get_running_loop().call_soon(trace.append, "scheduled")
            return True
        def invoked(text):
            trace.append("invoke")
            async def result():
                return {"message_id": 800}
            return result()
        sender = self.sender(invoked, is_current=current)
        await sender.send("Neutral.", attribution=REFS)
        index = trace.index("invoke")
        self.assertEqual(trace[index - 1], "check")

    async def test_cancel_inflight_is_unknown_and_no_late_receipt_update(self):
        fake = StubbornSend()
        self.stubborn.append(fake)
        sender = self.sender(fake, send_timeout=1)
        task = asyncio.create_task(sender.send("Neutral cancelled.", attribution=REFS))
        await fake.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.ledger.records(TOKEN))[0]["status"], "UNKNOWN")
        self.assertFalse(await sender.can_fallback())
        fake.release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual((await self.ledger.records(TOKEN))[0]["status"], "UNKNOWN")

    async def test_close_tracks_uncooperative_send_without_hanging(self):
        fake = StubbornSend()
        self.stubborn.append(fake)
        sender = self.sender(fake, send_timeout=1)
        task = asyncio.create_task(sender.send("Neutral close.", attribution=REFS))
        await fake.entered.wait()
        await asyncio.wait_for(sender.close(), timeout=0.5)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sender.pending_sends, 1)
        self.assertEqual((await self.ledger.records(TOKEN))[0]["status"], "UNKNOWN")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await sender.send("Never.", attribution=REFS)
        await sender.close()
        fake.release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        self.assertEqual(sender.pending_sends, 0)

    async def test_interval_is_from_receipt_and_rechecks_short_sleep(self):
        now, dispatch_times, sleeps = [10.0], [], []
        async def platform(text):
            dispatch_times.append(now[0])
            now[0] += 0.2
            return {"message_id": 70 + len(dispatch_times)}
        async def early_sleep(delay):
            sleeps.append(delay)
            now[0] += 0.5 if len(sleeps) == 1 else delay
        sender = self.sender(platform, segment_interval=1.5, clock=lambda: now[0], sleep=early_sleep)
        await sender.send("One.", attribution=REFS)
        await sender.send("Two.", attribution=REFS)
        self.assertEqual(len(sleeps), 2)
        self.assertGreaterEqual(dispatch_times[1] - (dispatch_times[0] + 0.2), 1.5)

    async def test_concurrent_calls_are_serial_and_ordered(self):
        active, maximum = 0, 0
        async def platform(text):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            await asyncio.sleep(0.005)
            self.calls.append(text)
            active -= 1
            return {"message_id": len(self.calls)}
        sender = self.sender(platform)
        results = await asyncio.gather(*(sender.send(str(i), attribution=REFS) for i in range(3)))
        self.assertEqual(maximum, 1)
        self.assertEqual([result.sequence for result in results], [0, 1, 2])
        self.assertEqual(self.calls, ["0", "1", "2"])

    async def test_platform_and_session_keys_do_not_collide(self):
        tokens = [TOKEN, replace(TOKEN, platform_id="fixture-wechat"), replace(TOKEN, session_id="private:100")]
        for token in tokens:
            sender = self.sender(token=token, is_current=lambda token: True)
            self.assertEqual((await sender.send("Neutral.", attribution=REFS)).status, "SENT")
        self.assertEqual(len(self.calls), 3)
        for token in tokens:
            self.assertEqual(len(await self.ledger.records(token)), 1)

    async def test_invalid_input_and_async_version_callback_fail_closed(self):
        sender = self.sender()
        for text, refs in (("  ", REFS), ("Neutral.", ()),
                           ("Neutral.", (InputRef("1", "2"), InputRef("1", "3")))):
            with self.assertRaises(ValueError):
                await sender.send(text, attribution=refs)
        async def bad_current(token):
            return True
        bad = self.sender(is_current=bad_current)
        with self.assertRaisesRegex(TypeError, "synchronous"):
            await bad.send("Never.", attribution=REFS)
        self.assertEqual(self.calls, [])
        self.assertEqual((await self.ledger.records(TOKEN))[0]["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
