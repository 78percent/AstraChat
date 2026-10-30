"""Offline synthetic tests; no wxauto import, desktop access, or real chats."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.wechat_desktop_probe_core import ObservedMessage, ProbeState


class ProbeCoreTests(unittest.TestCase):
    def setUp(self):
        self.probe = ProbeState("synthetic-test-group", "N0NC3_42")

    def message(self, content="ordinary synthetic text", *, ui_id="ui-1",
                attr="friend", type="text", sender="synthetic-member"):
        return ObservedMessage(ui_id, attr, type, sender, content)

    def observe(self, messages, now=10.0, *, chat_name="synthetic-test-group", chat_type="group"):
        return self.probe.observe(chat_name, chat_type, messages, now)

    def admit(self):
        self.assertFalse(self.observe([], 10.0))
        self.assertTrue(self.observe([self.message(self.probe.command)], 11.0))

    def test_message_is_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.message().content = "changed"

    def test_constructor_rejects_invalid_target_and_nonce(self):
        for target in ("", "   ", "group\nname", 3):
            with self.subTest(target=target), self.assertRaises(ValueError):
                ProbeState(target, "N0NC3_42")
        for nonce in ("", "short", "nonce space", "x" * 65, None):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                ProbeState("test", nonce)

    def test_first_snapshot_only_establishes_baseline(self):
        self.assertFalse(self.observe([self.message()]))
        self.assertEqual(self.probe.state, "WAITING")
        self.assertEqual(self.probe.summary()["counts"]["baseline_messages"], 1)
        self.assertFalse(self.observe([self.message()], 11))
        self.assertEqual(self.probe.summary()["counts"]["admissions"], 0)

    def test_visible_startup_command_rejects_run_instead_of_replaying(self):
        self.assertFalse(self.observe([self.message(self.probe.command)]))
        self.assertEqual(self.probe.state, "FAILED_BEFORE_DISPATCH")
        self.assertEqual(self.probe.reason, "nonce_already_visible")
        self.assertFalse(self.observe([self.message(self.probe.command, ui_id="changed")], 11))

    def test_startup_nonce_collision_with_malformed_id_still_rejects(self):
        self.assertFalse(self.observe([self.message(self.probe.command, ui_id="")]))
        self.assertEqual(self.probe.reason, "nonce_already_visible")

    def test_startup_reply_collision_rejects_run(self):
        self.assertFalse(self.observe([self.message(self.probe.reply, attr="self")]))
        self.assertEqual(self.probe.reason, "nonce_already_visible")

    def test_new_exact_member_command_admits_only_once(self):
        self.admit()
        self.assertFalse(self.observe([self.message(self.probe.command)], 12))
        self.assertFalse(self.observe([self.message(self.probe.command, ui_id="ui-2")], 13))
        self.assertEqual(self.probe.summary()["counts"]["admissions"], 1)
        self.assertEqual(self.probe.state, "ADMITTED")

    def test_own_command_system_and_nontext_do_not_admit(self):
        self.observe([], 10)
        messages = [self.message(self.probe.command, ui_id="a", attr="self"),
                    self.message(self.probe.command, ui_id="b", attr="system"),
                    self.message(self.probe.command, ui_id="c", type="image"),
                    self.message(self.probe.command, ui_id="d", attr="unknown")]
        self.assertFalse(self.observe(messages, 11))
        self.assertEqual(self.probe.state, "WAITING")

    def test_similar_text_and_different_nonce_do_not_admit(self):
        self.observe([], 10)
        messages = [self.message(self.probe.command + " extra", ui_id="a"),
                    self.message(" " + self.probe.command, ui_id="b"),
                    self.message("三叶 桌面测试 other_nonce", ui_id="c")]
        self.assertFalse(self.observe(messages, 11))

    def test_missing_id_or_sender_does_not_admit(self):
        self.observe([], 10)
        messages = [self.message(self.probe.command, ui_id=""),
                    self.message(self.probe.command, ui_id="bad id"),
                    self.message(self.probe.command, ui_id="valid", sender=""),
                    self.message(self.probe.command, ui_id="valid2", type=None),
                    self.message(self.probe.command, ui_id="valid3", sender="bad\nname")]
        self.assertFalse(self.observe(messages, 11))
        self.assertEqual(self.probe.state, "WAITING")
        self.assertEqual(self.probe.summary()["counts"]["invalid_observations"], 5)

    def test_wrong_group_does_not_consume_message_iterable(self):
        def forbidden():
            raise AssertionError("must not inspect wrong-group messages")
            yield self.message()
        self.assertFalse(self.observe(forbidden(), chat_name="other-group"))
        self.assertEqual(self.probe.reason, "target_mismatch")

    def test_matching_name_but_private_chat_is_rejected(self):
        self.assertFalse(self.observe([], chat_type="friend"))
        self.assertEqual(self.probe.state, "FAILED_BEFORE_DISPATCH")

    def test_target_change_after_dispatch_is_unknown_without_observing_reply(self):
        self.admit()
        self.probe.mark_dispatched(12)
        self.assertFalse(self.observe([self.message(self.probe.reply, attr="self")],
                                      13, chat_name="wrong-group"))
        self.assertEqual(self.probe.state, "UNKNOWN")

    def test_malformed_snapshot_is_rejected_before_any_admission(self):
        self.observe([], 10)
        def broken():
            yield self.message(self.probe.command)
            raise RuntimeError("synthetic private error text")
        self.assertFalse(self.observe(broken(), 11))
        self.assertEqual(self.probe.reason, "read_error")
        self.assertEqual(self.probe.summary()["counts"]["admissions"], 0)

    def test_dispatch_requires_admission_and_cannot_repeat(self):
        with self.assertRaises(ValueError):
            self.probe.mark_dispatched(10)
        self.admit()
        self.probe.mark_dispatched(12)
        with self.assertRaises(ValueError):
            self.probe.mark_dispatched(13)
        self.assertEqual(self.probe.summary()["counts"]["dispatches"], 1)

    def test_member_echo_is_not_outgoing_evidence(self):
        self.admit()
        self.probe.mark_dispatched(12)
        self.assertFalse(self.observe([self.message(self.probe.reply, ui_id="echo")], 13))
        self.assertEqual(self.probe.state, "DISPATCHED")

    def test_partial_self_reply_is_not_success(self):
        self.admit()
        self.probe.mark_dispatched(12)
        self.assertFalse(self.observe([self.message(self.probe.reply[:-1], ui_id="self-1", attr="self")], 13))
        self.assertEqual(self.probe.state, "DISPATCHED")

    def test_matching_new_self_text_provides_only_ui_observed_evidence(self):
        self.admit()
        self.probe.mark_dispatched(11.25)
        self.assertFalse(self.observe([self.message(self.probe.reply, ui_id="self-1", attr="self")], 12.5))
        self.assertEqual(self.probe.state, "UI_OBSERVED")
        self.assertTrue(self.probe.terminal)
        report = self.probe.summary()
        self.assertEqual(report["evidence"], "desktop_ui_observation_only")
        self.assertEqual(report["timings_seconds"]["command_observed_to_ui_observed"], 1.5)
        self.assertEqual(report["timings_seconds"]["command_observed_to_dispatch"], .25)
        self.assertEqual(report["timings_seconds"]["dispatch_to_ui_observed"], 1.25)

    def test_reply_before_dispatch_invalidates_admission(self):
        self.observe([], 10)
        self.assertFalse(self.observe([
            self.message(self.probe.command),
            self.message(self.probe.reply, ui_id="self-1", attr="self"),
        ], 11))
        self.assertEqual(self.probe.state, "FAILED_BEFORE_DISPATCH")
        self.assertEqual(self.probe.reason, "reply_seen_before_dispatch")

    def test_error_before_dispatch_is_definite_no_send(self):
        self.admit()
        self.probe.fail("send_error", 12)
        self.assertEqual(self.probe.state, "FAILED_BEFORE_DISPATCH")
        self.assertEqual(self.probe.summary()["counts"]["dispatches"], 0)

    def test_error_or_stop_after_dispatch_is_unknown_and_never_retried(self):
        for method in ("fail", "stop"):
            with self.subTest(method=method):
                self.setUp()
                self.admit()
                self.probe.mark_dispatched(12)
                getattr(self.probe, method)("timeout", 13)
                self.assertEqual(self.probe.state, "UNKNOWN")
                self.assertFalse(self.observe([self.message(self.probe.command, ui_id="repeat")], 14))
                self.assertFalse(self.observe([self.message(self.probe.reply, attr="self")], 15))
                self.assertEqual(self.probe.state, "UNKNOWN")
                with self.assertRaises(ValueError):
                    self.probe.mark_dispatched(16)

    def test_stop_after_success_preserves_evidence(self):
        self.admit()
        self.probe.mark_dispatched(12)
        self.observe([self.message(self.probe.reply, attr="self", ui_id="self-1")], 13)
        before = self.probe.summary()
        self.probe.stop("user_stop", 14)
        self.assertEqual(self.probe.summary(), before)

    def test_invalid_clock_is_fail_closed(self):
        for now in (float("nan"), float("inf"), -1, True, "11"):
            with self.subTest(now=now):
                self.setUp()
                self.assertFalse(self.observe([], now))
                self.assertEqual(self.probe.reason, "invalid_clock")
                self.assertEqual(self.probe.state, "FAILED_BEFORE_DISPATCH")

    def test_backwards_clock_after_dispatch_is_unknown(self):
        self.admit()
        self.probe.mark_dispatched(12)
        self.assertFalse(self.observe([], 11.5))
        self.assertEqual(self.probe.state, "UNKNOWN")

    def test_bad_dispatch_time_cannot_authorize_ui_send(self):
        self.admit()
        with self.assertRaises(ValueError):
            self.probe.mark_dispatched(9)
        self.assertEqual(self.probe.state, "FAILED_BEFORE_DISPATCH")
        self.assertEqual(self.probe.summary()["counts"]["dispatches"], 0)

    def test_report_omits_identifiers_body_nonce_and_arbitrary_error(self):
        self.observe([self.message("private content", sender="private sender")], 10)
        self.probe.fail("sensitive exception includes private content", 11)
        text = json.dumps(self.probe.summary(), ensure_ascii=False)
        for private in ("synthetic-test-group", "N0NC3_42", "private content", "private sender", "ui-1", "sensitive"):
            self.assertNotIn(private, text)
        self.assertEqual(self.probe.reason, "external_failure")

    def test_report_counter_copy_cannot_mutate_internal_state(self):
        report = self.probe.summary()
        report["counts"]["dispatches"] = 7
        self.assertEqual(self.probe.summary()["counts"]["dispatches"], 0)


if __name__ == "__main__":
    unittest.main()
