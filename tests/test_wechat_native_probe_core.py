"""Synthetic native-message policy tests; no DLL, desktop, or network access."""

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.wechat_native_probe_core import NativeProbe


class NativeProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = NativeProbe("wxid_synthetic_bot", "abcd1234")

    def raw(self, **changes):
        values = {"type": 1, "receiver": b"wxid_synthetic_bot", "sender": b"123456@chatroom",
                  "text": ("wxid_synthetic_member:\n" + self.probe.command).encode("utf-8"),
                  "native_message_id": 9007199254740997, "sequence": 1}
        values.update(changes)
        return SimpleNamespace(**values)

    def dispatched(self):
        self.assertTrue(self.probe.observe(self.raw(), 10.0))
        self.probe.mark_dispatched(10.25)

    def outcome(self):
        return {"success": True, "code": 0, "cleanup_code": 0,
                "report": {"completion_success": True, "target": self.probe.room, "text": self.probe.reply}}

    def test_exact_member_command_binds_actual_room_and_preserves_uint64_id(self):
        self.assertTrue(self.probe.observe(self.raw(), 10))
        self.assertEqual(self.probe.state, "ADMITTED")
        self.assertEqual(self.probe.room, "123456@chatroom")
        self.assertEqual(self.probe.incoming_id, "9007199254740997")
        self.assertEqual(self.probe.summary()["counts"]["admissions"], 1)

    def test_only_exact_new_nonce_binds_room(self):
        for body in ("ordinary unrelated text", "三叶 原生测试 old_nonce", self.probe.command + " "):
            with self.subTest(body=body):
                self.setUp()
                self.assertFalse(self.probe.observe(self.raw(text=("wxid_member:\n" + body).encode()), 10))
                self.assertIsNone(self.probe.room)
                self.assertIsNone(self.probe.incoming_id)
                self.assertEqual(self.probe.state, "WAITING")

    def test_prebound_room_rejects_other_room_before_reading_text(self):
        self.probe = NativeProbe("wxid_synthetic_bot", "abcd1234", "99999@chatroom")
        class ForbiddenText:
            type = 1
            receiver = b"wxid_synthetic_bot"
            sender = b"123456@chatroom"
            @property
            def text(self):
                raise AssertionError("unrelated body was accessed")
        self.assertFalse(self.probe.observe(ForbiddenText(), 10))
        self.assertEqual(self.probe.room, "99999@chatroom")
        self.assertTrue(self.probe.observe(self.raw(sender=b"99999@chatroom"), 11))

    def test_rejected_metadata_never_accesses_body(self):
        cases = [{"type": 3}, {"type": 10002}, {"type": True}, {"receiver": b"wxid_other"},
                 {"sender": b"wxid_private_friend"}, {"sender": b"test@chatroom"},
                 {"sender": b"123456@chatroom\x00"}, {"native_message_id": 0},
                 {"native_message_id": True}, {"sequence": 0}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.setUp()
                values = vars(self.raw(**changes))
                del values["text"]
                class ForbiddenText:
                    @property
                    def text(self):
                        raise AssertionError("unrelated body was accessed")
                raw = ForbiddenText()
                raw.__dict__.update(values)
                self.assertFalse(self.probe.observe(raw, 10))
                self.assertEqual(self.probe.summary()["counts"]["text_rejected"], 0)

    def test_self_member_cannot_bind_or_trigger(self):
        raw = self.raw(text=("wxid_synthetic_bot:\n" + self.probe.command).encode())
        self.assertFalse(self.probe.observe(raw, 10))
        self.assertIsNone(self.probe.room)
        self.assertEqual(self.probe.summary()["counts"]["self_rejected"], 1)

    def test_outgoing_group_direction_is_not_incoming(self):
        self.assertFalse(self.probe.observe(self.raw(sender=b"wxid_synthetic_bot", receiver=b"123456@chatroom"), 10))
        self.assertIsNone(self.probe.room)

    def test_invalid_member_prefixes_are_rejected(self):
        for member in ("", "some name", "123@chatroom", "weixin", "filehelper", "gh_public", "中文昵称"):
            with self.subTest(member=member):
                self.setUp()
                self.assertFalse(self.probe.observe(self.raw(text=(member + ":\n" + self.probe.command).encode()), 10))
                self.assertIsNone(self.probe.room)

    def test_malformed_body_or_missing_member_separator_is_rejected(self):
        for body in (None, "not bytes", b"\xff", b"", b"x" * 1024,
                     ("wxid_member:" + self.probe.command).encode(),
                     ("wxid_member:\r\n" + self.probe.command).encode(),
                     ("wxid_member:\n" + self.probe.command).encode() + b"\0"):
            with self.subTest(body=body):
                self.setUp()
                self.assertFalse(self.probe.observe(self.raw(text=body), 10))
                self.assertIsNone(self.probe.room)

    def test_native_ids_must_be_nonzero_uint64_and_sequence_is_separate(self):
        for native_id in (0, -1, 2**64, True, "123", None):
            with self.subTest(native_id=native_id):
                self.setUp()
                self.assertFalse(self.probe.observe(self.raw(native_message_id=native_id), 10))
        self.setUp()
        self.assertTrue(self.probe.observe(self.raw(native_message_id=2**64 - 1, sequence=7), 10))
        self.assertEqual(self.probe.incoming_id, str(2**64 - 1))

    def test_repeated_native_id_cannot_replace_earlier_body(self):
        self.assertFalse(self.probe.observe(self.raw(text=b"wxid_member:\nordinary"), 10))
        self.assertFalse(self.probe.observe(self.raw(), 11))
        self.assertEqual(self.probe.summary()["counts"]["duplicate_ids"], 1)
        self.assertTrue(self.probe.observe(self.raw(native_message_id=99), 12))

    def test_admitted_run_cannot_admit_second_id_or_second_room(self):
        self.assertTrue(self.probe.observe(self.raw(), 10))
        self.assertFalse(self.probe.observe(self.raw(native_message_id=99, sender=b"99999@chatroom"), 11))
        self.assertEqual(self.probe.room, "123456@chatroom")
        self.assertEqual(self.probe.summary()["counts"]["admissions"], 1)

    def test_dispatch_requires_admission_and_is_once_only(self):
        with self.assertRaises(ValueError):
            self.probe.mark_dispatched(10)
        self.dispatched()
        with self.assertRaises(ValueError):
            self.probe.mark_dispatched(11)
        self.assertEqual(self.probe.summary()["counts"]["dispatches"], 1)

    def test_full_completion_is_not_recipient_confirmation_or_outgoing_id(self):
        self.dispatched()
        self.assertTrue(self.probe.complete(self.outcome(), 11.5))
        self.assertEqual(self.probe.state, "NATIVE_COMPLETED")
        report = self.probe.summary()
        self.assertFalse(report["recipient_delivery_confirmed"])
        self.assertIsNone(report["outgoing_platform_message_id"])
        self.assertEqual(report["timings_seconds"]["command_observed_to_native_completion"], 1.5)
        self.assertEqual(report["timings_seconds"]["dispatch_to_native_completion"], 1.25)

    def test_each_completion_condition_is_required_and_boolean_codes_are_rejected(self):
        mutations = [("success", False), ("success", 1), ("code", False), ("code", "0"), ("code", 1),
                     ("cleanup_code", False), ("cleanup_code", 170), ("cleanup_code", None),
                     ("report", None)]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                self.setUp()
                self.dispatched()
                outcome = self.outcome()
                outcome[key] = value
                self.assertFalse(self.probe.complete(outcome, 12))
                self.assertEqual(self.probe.state, "UNKNOWN")

    def test_completion_report_matches_both_target_and_exact_reply(self):
        for key, value in (("completion_success", 1), ("completion_success", False),
                           ("target", "99999@chatroom"), ("text", "unrelated text")):
            with self.subTest(key=key, value=value):
                self.setUp()
                self.dispatched()
                outcome = self.outcome()
                outcome["report"][key] = value
                self.assertFalse(self.probe.complete(outcome, 12))
                self.assertEqual(self.probe.state, "UNKNOWN")

    def test_missing_fields_or_nonmapping_outcomes_are_unknown(self):
        for outcome in (None, True, {"success": True}, ["success"], "success"):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.dispatched()
                self.assertFalse(self.probe.complete(outcome, 12))
                self.assertEqual(self.probe.state, "UNKNOWN")

    def test_completion_without_dispatch_cannot_create_success(self):
        with self.assertRaises(ValueError):
            self.probe.complete(self.outcome(), 10)
        self.assertTrue(self.probe.observe(self.raw(), 10))
        with self.assertRaises(ValueError):
            self.probe.complete(self.outcome(), 11)

    def test_timeout_or_stop_after_dispatch_is_unknown_and_late_success_does_not_revive(self):
        for method in ("fail", "stop"):
            with self.subTest(method=method):
                self.setUp()
                self.dispatched()
                success = self.outcome()
                getattr(self.probe, method)("native_timeout", 12)
                self.assertEqual(self.probe.state, "UNKNOWN")
                self.assertFalse(self.probe.complete(success, 13))
                self.assertFalse(self.probe.observe(self.raw(native_message_id=2), 14))
                with self.assertRaises(ValueError):
                    self.probe.mark_dispatched(15)

    def test_stop_before_dispatch_is_stopped_and_cannot_admit(self):
        self.probe.stop("user_stop", 10)
        self.assertEqual(self.probe.state, "STOPPED")
        self.assertFalse(self.probe.observe(self.raw(), 11))

    def test_stop_preserves_completed_evidence(self):
        self.dispatched()
        self.probe.complete(self.outcome(), 12)
        before = copy.deepcopy(self.probe.summary())
        self.probe.stop("user_stop", 13)
        self.assertEqual(self.probe.summary(), before)

    def test_invalid_and_backwards_clock_fail_closed(self):
        for now in (None, True, -1, float("nan"), float("inf")):
            with self.subTest(now=now):
                self.setUp()
                self.assertFalse(self.probe.observe(self.raw(), now))
                self.assertEqual(self.probe.state, "STOPPED")
        self.setUp()
        self.dispatched()
        self.assertFalse(self.probe.complete(self.outcome(), 9))
        self.assertEqual(self.probe.state, "UNKNOWN")

    def test_summary_omits_room_member_native_id_body_and_arbitrary_exception(self):
        self.dispatched()
        self.probe.fail("private exception wxid_member with message text", 12)
        serialized = json.dumps(self.probe.summary(), ensure_ascii=False)
        for value in (self.probe.self_id, self.probe.room, self.probe.incoming_id,
                      "wxid_synthetic_member", "abcd1234", self.probe.command, self.probe.reply, "private exception"):
            self.assertNotIn(value, serialized)
        self.assertEqual(self.probe.reason, "external_failure")

    def test_summary_counts_are_detached_copy(self):
        self.probe.summary()["counts"]["dispatches"] = 99
        self.assertEqual(self.probe.summary()["counts"]["dispatches"], 0)

    def test_configuration_requires_real_person_and_numeric_room_ids(self):
        for self_id in ("", "weixin", "filehelper", "gh_public", "1234@chatroom", "some name"):
            with self.subTest(self_id=self_id), self.assertRaises(ValueError):
                NativeProbe(self_id, "abcd1234")
        for room in ("test", "name@chatroom", "0@chatroom", "123@chatroom\n", 123):
            with self.subTest(room=room), self.assertRaises(ValueError):
                NativeProbe("wxid_bot", "abcd1234", room)
        for nonce in ("", "x" * 65, "invalid whitespace", None):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                NativeProbe("wxid_bot", nonce)


if __name__ == "__main__":
    unittest.main()
