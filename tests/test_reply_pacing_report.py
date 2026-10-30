"""Synchronous report tests using disposable metadata-only SQLite fixtures."""

from __future__ import annotations

import argparse
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import report_reply_pacing as report


SECRET = "must-never-export-secret-or-message-content"


def receipt(**changes):
    row = dict(status="SENT", reason="complete", received_at=100.0,
               started_at=101.0, first_ack_at=108.0, confirmed_segments=1,
               has_decision=1, selected=1, decision_reason="selected")
    row.update(changes)
    return row


class SummaryTests(unittest.TestCase):
    def test_all_and_selected_denominators_are_separate(self):
        rows = [receipt() for _ in range(50)]
        rows += [receipt(first_ack_at=111.0) for _ in range(10)]
        rows += [receipt(status="SKIPPED", reason="pacing_probability", started_at=None,
                         first_ack_at=None, confirmed_segments=0, selected=0,
                         decision_reason="probability") for _ in range(40)]
        result = report.summarize(rows)
        self.assertEqual(result["rates"]["all_valid_within_ten_seconds"],
                         {"numerator": 50, "denominator": 100, "ratio": .5})
        self.assertEqual(result["rates"]["selected_within_ten_seconds"],
                         {"numerator": 50, "denominator": 60, "ratio": 50 / 60})
        self.assertEqual(result["counts"]["probability_skipped"], 40)
        self.assertEqual(result["counts"]["unanswered"], 40)
        self.assertEqual(result["counts"]["other_failures"], 0)

    def test_each_policy_skip_is_retained_without_becoming_failure(self):
        rows = [receipt(status="SKIPPED", reason="pacing_" + reason, started_at=None,
                        first_ack_at=None, confirmed_segments=0, selected=0,
                        decision_reason=reason) for reason in ("probability", "cooldown", "quota")]
        counts = report.summarize(rows)["counts"]
        self.assertEqual(counts["valid_wakes"], 3)
        self.assertEqual(counts["policy_skipped"], 3)
        self.assertEqual([counts[key + "_skipped"] for key in
                          ("probability", "cooldown", "quota")], [1, 1, 1])
        self.assertEqual(counts["selected"], 0)
        self.assertEqual(counts["other_failures"], 0)

    def test_pending_rejection_unrecorded_and_disabled_are_not_dropped(self):
        no_decision = dict(has_decision=0, selected=None, decision_reason=None)
        rows = [receipt(status="REJECTED", reason="pending_limit", started_at=None,
                        first_ack_at=None, confirmed_segments=0, **no_decision),
                receipt(status="ACCEPTED", started_at=None, first_ack_at=None,
                        confirmed_segments=0, **no_decision),
                receipt(**no_decision),
                receipt(status="FAILED", first_ack_at=None, confirmed_segments=0)]
        result = report.summarize(rows)
        self.assertEqual(result["counts"]["valid_wakes"], 4)
        self.assertEqual(result["counts"]["selected"], 2)
        self.assertEqual(result["counts"]["selected_without_policy_record"], 1)
        self.assertEqual(result["counts"]["decision_not_recorded"], 2)
        self.assertEqual(result["counts"]["other_failures"], 2)
        self.assertEqual(result["counts"]["selected_other_failures"], 1)
        self.assertEqual(result["counts"]["active"], 1)
        self.assertEqual(result["counts"]["unanswered"], 3)
        self.assertEqual(result["counts"]["terminal_unanswered"], 2)

    def test_timing_uses_ingress_including_queue_and_exact_ten_boundary(self):
        result = report.summarize([receipt(started_at=109, first_ack_at=110),
                                   receipt(started_at=110, first_ack_at=111)])
        self.assertEqual(result["counts"]["all_within_ten_seconds"], 1)
        self.assertEqual(result["selected_first_ack_seconds"]["max"], 11)
        self.assertEqual(result["selected_first_ack_seconds"]["p50"], 10.5)

    def test_quantiles_only_use_valid_ack_samples_and_keep_failures_in_rate(self):
        rows = [receipt(first_ack_at=100 + latency) for latency in (1, 2, 10, 20)]
        rows.append(receipt(status="FAILED", first_ack_at=None, confirmed_segments=0))
        result = report.summarize(rows)
        self.assertEqual(result["selected_first_ack_seconds"]["samples"], 4)
        self.assertEqual(result["selected_first_ack_seconds"]["p50"], 6)
        self.assertEqual(result["selected_first_ack_seconds"]["p95"], 18.5)
        self.assertEqual(result["selected_first_ack_seconds"]["max"], 20)
        self.assertEqual(result["rates"]["selected_within_ten_seconds"]["denominator"], 5)

    def test_no_samples_produces_nulls_not_fake_zero(self):
        for rows in ([], [receipt(status="FAILED", first_ack_at=None, confirmed_segments=0)]):
            result = report.summarize(rows)
            self.assertEqual(result["selected_first_ack_seconds"]["samples"], 0)
            for key in ("p50", "p95", "max"):
                self.assertIsNone(result["selected_first_ack_seconds"][key])
        self.assertIsNone(report.summarize([])["rates"]["selection"]["ratio"])

    def test_bad_clocks_and_missing_ack_are_diagnosed(self):
        rows = [receipt(first_ack_at=99), receipt(first_ack_at=None),
                receipt(received_at=float("nan")), receipt(first_ack_at=float("inf")),
                receipt(confirmed_segments=0)]
        result = report.summarize(rows)
        self.assertEqual(result["diagnostics"]["invalid_or_negative_ack_latency"], 3)
        self.assertEqual(result["diagnostics"]["invalid_received_at"], 1)
        self.assertEqual(result["diagnostics"]["missing_ack_time"], 2)
        self.assertEqual(result["diagnostics"]["ack_time_without_segment_count"], 1)
        self.assertEqual(result["selected_first_ack_seconds"]["samples"], 1)
        self.assertEqual(result["rates"]["selected_within_ten_seconds"]["denominator"], 5)
        json.dumps(result, allow_nan=False)

    def test_policy_limit_distinguishes_sent_from_no_answer(self):
        rows = [receipt(status="PARTIAL", reason="policy_limit"),
                receipt(status="FAILED", reason="policy_limit", first_ack_at=None,
                        confirmed_segments=0),
                receipt(status="UNKNOWN", reason="send_unknown", first_ack_at=None,
                        confirmed_segments=0)]
        counts = report.summarize(rows)["counts"]
        self.assertEqual(counts["policy_limited"], 2)
        self.assertEqual(counts["policy_limited_with_answer"], 1)
        self.assertEqual(counts["other_failures"], 2)
        self.assertEqual(counts["unanswered"], 2)

    def test_unknown_states_and_reasons_never_leak(self):
        rows = [receipt(status=SECRET, reason=SECRET),
                receipt(status="SKIPPED", reason="pacing_" + SECRET, has_decision=0,
                        selected=None, decision_reason=None, started_at=None,
                        first_ack_at=None, confirmed_segments=0),
                receipt(selected=SECRET, decision_reason=SECRET)]
        result = report.summarize(rows)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["diagnostics"]["unknown_status"], 1)
        self.assertEqual(result["diagnostics"]["invalid_decision"], 2)
        self.assertEqual(result["counts"]["other_policy_skipped"], 1)
        self.assertEqual(result["counts"]["other_failures"], 1)

    def test_conflicting_skip_and_delivery_remain_visible(self):
        result = report.summarize([receipt(status="SKIPPED", selected=0,
                                           decision_reason="probability")])
        self.assertEqual(result["diagnostics"]["selection_outcome_conflict"], 1)
        self.assertEqual(result["counts"]["acknowledged"], 1)
        self.assertEqual(result["counts"]["selected"], 0)

    def test_recovered_receipts_are_diagnosed_without_inventing_selection_or_timing(self):
        rows = [receipt(status="API_ACCEPTED", reason="recovered_from_confirmed_delivery",
                        started_at=None, first_ack_at=None, confirmed_segments=2,
                        has_decision=0, selected=None, decision_reason=None)]
        result = report.summarize(rows)
        self.assertEqual(result["diagnostics"]["recovered_receipts"], 1)
        self.assertEqual(result["diagnostics"]["missing_ack_time"], 1)
        self.assertEqual(result["counts"]["acknowledged"], 1)
        self.assertEqual(result["counts"]["selected"], 0)
        self.assertEqual(result["counts"]["decision_not_recorded"], 1)
        self.assertEqual(result["rates"]["selection"], {"numerator": 0, "denominator": 1, "ratio": 0.0})
        self.assertEqual(result["all_first_ack_seconds"]["samples"], 0)
        self.assertIsNone(result["all_first_ack_seconds"]["p95"])
        self.assertEqual(report.summarize([receipt(reason="recovered_from_confirmed_delivery_extra")])
                         ["diagnostics"]["recovered_receipts"], 0)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="reply-pacing-report-", dir=ROOT / "tests")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / report.LEDGER
        self.path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE receipts (
                    platform_id TEXT, incoming_message_id TEXT, purpose TEXT,
                    status TEXT, reason TEXT, received_at REAL, started_at REAL,
                    first_ack_at REAL, confirmed_segments INTEGER, secret_body TEXT,
                    PRIMARY KEY(platform_id, incoming_message_id));
                CREATE TABLE pacing_decisions (
                    platform_id TEXT, incoming_message_id TEXT, selected INTEGER,
                    reason TEXT, session_id TEXT, policy_json TEXT,
                    PRIMARY KEY(platform_id, incoming_message_id));
                PRAGMA user_version=2;
            """)
        self.serial = 0

    def add(self, purpose="roleplay", **changes):
        self.serial += 1
        row = receipt(**changes)
        message = f"{SECRET}-{self.serial}"
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?)", (
                SECRET, message, purpose, row["status"], row["reason"], row["received_at"],
                row["started_at"], row["first_ack_at"], row["confirmed_segments"], SECRET))
            if row["has_decision"]:
                db.execute("INSERT INTO pacing_decisions VALUES (?,?,?,?,?,?)", (
                    SECRET, message, row["selected"], row["decision_reason"], SECRET, SECRET))
            db.commit()

    def test_mixed_purposes_only_roleplay_enters_denominator(self):
        for purpose in ("roleplay", "management", "neutral", "legacy", SECRET, None):
            self.add(purpose)
        result = report.build_report(self.root)
        self.assertEqual(result["counts"]["valid_wakes"], 1)
        self.assertEqual(result["excluded_all_time_counts"],
                         {"management": 1, "neutral": 1, "legacy_unclassified": 1,
                          "unknown_purpose": 2})
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertFalse(result["evidence_limits"]["client_visible_delivery_verified"])
        self.assertFalse(result["evidence_limits"]["formal_95_percent_under_ten_seconds_acceptance"])

    def test_wechat_uses_separate_ledger_and_truthful_api_evidence(self):
        self.add(status="API_ACCEPTED")
        wechat_path = self.root / report.WECHAT_LEDGER
        wechat_path.parent.mkdir(parents=True)
        wechat_path.write_bytes(self.path.read_bytes())
        self.add(status="SENT")
        result = report.build_report(self.root, channel="wechat")
        self.assertEqual(result["counts"]["valid_wakes"], 1)
        self.assertEqual(result["counts"]["acknowledged"], 1)
        self.assertEqual(result["diagnostics"]["unknown_status"], 0)
        self.assertEqual(result["acceptance_evidence"], "api_accepted_without_platform_message_id")
        self.assertEqual(result["evidence_limits"]["latency"], "original_ingress_to_first_native_api_acceptance")
        self.assertFalse(result["evidence_limits"]["client_visible_delivery_verified"])
        self.assertEqual(report.build_report(self.root)["counts"]["valid_wakes"], 2)

    def test_unknown_report_channel_is_rejected(self):
        with self.assertRaisesRegex(report.ReportError, "^invalid_channel$"):
            report.build_report(self.root, channel="arbitrary/path")

    def test_receipt_schema_three_remains_read_only_and_supported(self):
        self.add(status="API_ACCEPTED")
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("ALTER TABLE receipts ADD COLUMN delivery_ref TEXT; PRAGMA user_version=3;")
        before = self.path.read_bytes()
        self.assertEqual(report.build_report(self.root)["counts"]["acknowledged"], 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_left_join_retains_predecision_failures(self):
        self.add(status="REJECTED", reason="pending_limit", started_at=None,
                 first_ack_at=None, confirmed_segments=0, has_decision=0)
        result = report.build_report(self.root)
        self.assertEqual(result["counts"]["valid_wakes"], 1)
        self.assertEqual(result["counts"]["decision_not_recorded"], 1)
        self.assertEqual(result["counts"]["other_failures"], 1)

    def test_window_is_inclusive_start_exclusive_end_on_ingress(self):
        for received in (99, 100, 199, 200):
            self.add(received_at=received, first_ack_at=received + 2)
        self.add("management", received_at=1)
        result = report.build_report(self.root, since=100, until=200)
        self.assertEqual(result["counts"]["valid_wakes"], 2)
        self.assertEqual(result["excluded_all_time_counts"]["management"], 1)

    def test_recovered_window_explicitly_disclaims_selection_and_slo_evidence(self):
        self.add(received_at=100, first_ack_at=107)
        self.add(status="API_ACCEPTED", reason="recovered_from_confirmed_delivery",
                 received_at=200, started_at=None, first_ack_at=None, confirmed_segments=2,
                 has_decision=0, selected=None, decision_reason=None)
        keys = ("historical_policy_and_latency_evidence_incomplete_due_to_recovery",
                "recovered_period_rates_are_not_selection_or_slo_acceptance_evidence")
        complete_window = report.build_report(self.root, until=200)
        recovered_window = report.build_report(self.root)
        for key in keys:
            self.assertFalse(complete_window["evidence_limits"][key])
            self.assertTrue(recovered_window["evidence_limits"][key])
        self.assertEqual(recovered_window["diagnostics"]["recovered_receipts"], 1)
        self.assertEqual(recovered_window["counts"]["valid_wakes"], 2)
        self.assertEqual(recovered_window["counts"]["selected"], 1)
        self.assertEqual(recovered_window["counts"]["acknowledged"], 2)
        self.assertEqual(recovered_window["rates"]["selection"]["denominator"], 2)
        self.assertEqual(recovered_window["all_first_ack_seconds"]["samples"], 1)
        self.assertFalse(recovered_window["evidence_limits"]["formal_95_percent_under_ten_seconds_acceptance"])

    def test_invalid_window_fails_without_reading_database(self):
        for values in (dict(since=2, until=2), dict(since=3, until=2),
                       dict(since=-1), dict(until=float("inf")), dict(since=True)):
            with self.subTest(values=values), self.assertRaisesRegex(report.ReportError, "^invalid_time_window$"):
                report.build_report(self.root, **values)

    def test_missing_database_is_not_created(self):
        missing = self.root / "missing"
        with self.assertRaisesRegex(report.ReportError, "^ledger_missing$"):
            report.build_report(missing)
        self.assertFalse(missing.exists())

    def test_wrong_version_or_missing_schema_returns_static_error(self):
        for sql in ("PRAGMA user_version=1", "PRAGMA user_version=2; DROP TABLE pacing_decisions"):
            with closing(sqlite3.connect(self.path)) as db:
                db.executescript(sql)
            with self.assertRaisesRegex(report.ReportError, "^pacing_schema_unavailable$"):
                report.build_report(self.root)

    def test_corrupt_database_returns_no_payload(self):
        self.path.write_text(SECRET, encoding="utf-8")
        with self.assertRaisesRegex(report.ReportError, "^ledger_read_failed$"):
            report.build_report(self.root)

    def test_database_is_read_only_and_body_columns_are_never_read(self):
        self.add()
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        original_connect = sqlite3.connect
        operations = []

        def connect(path, **kwargs):
            self.assertTrue(path.endswith("?mode=ro"))
            self.assertTrue(kwargs["uri"])
            db = original_connect(path, **kwargs)

            def authorize(action, table, column, database, trigger):
                operations.append(action)
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and column in ("secret_body", "session_id", "policy_json"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            db.set_authorizer(authorize)
            return db

        with patch.object(report.sqlite3, "connect", side_effect=connect):
            result = report.build_report(self.root)
        self.assertEqual(result["counts"]["valid_wakes"], 1)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
        self.assertIn(sqlite3.SQLITE_READ, operations)
        self.assertFalse(result["chat_body_read_or_exported"])
        self.assertFalse(result["network_or_qq_calls"])

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        # These disposable fixture databases have no Linux writer. Production
        # Windows CLI keeps the guard; explicit tests below verify that boundary.
        with redirect_stdout(stdout), redirect_stderr(stderr), patch.object(report, "_host_requires_runtime", return_value=False):
            code = report.main(["--root", str(self.root), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_windows_cli_requires_runtime_before_any_sqlite_access(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (redirect_stdout(stdout), redirect_stderr(stderr),
              patch.object(report, "_host_requires_runtime", return_value=True),
              patch.object(report, "build_report") as build,
              patch.object(report.sqlite3, "connect") as connect,
              patch.object(report.subprocess, "run") as execute):
            code = report.main(["--root", str(self.root), "--channel", "wechat_native"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["code"], "runtime_required_use_--runtime")
        build.assert_not_called()
        connect.assert_not_called()
        execute.assert_not_called()

    def test_runtime_exec_uses_stdin_existing_service_and_never_host_sqlite(self):
        self.add()
        expected = report.build_report(self.root)
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(expected), stderr="")
        with (patch.object(report.subprocess, "run", return_value=completed) as execute,
              patch.object(report.sqlite3, "connect", side_effect=AssertionError("host SQLite forbidden"))):
            result = report.build_runtime_report(self.root, since=100, until=200)
        args, kwargs = execute.call_args
        command = args[0]
        self.assertEqual(command[-5:], ["exec", "-T", "astrbot", "python", "-"])
        self.assertEqual(command[:2], ["docker", "compose"])
        self.assertNotIn("up", command)
        self.assertNotIn("restart", command)
        self.assertNotIn("shell", kwargs)
        self.assertIn("Path('/AstrBot/data')", kwargs["input"])
        self.assertIn("plugin_data/astrbot_plugin_roleplay_gateway/receipts.sqlite3", kwargs["input"])
        self.assertIn("'since': 100", kwargs["input"])
        self.assertIn("'until': 200", kwargs["input"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(result["execution_location"], "astrbot_container")
        self.assertEqual(result["counts"], expected["counts"])

    def test_runtime_cli_saves_only_aggregate_report_without_host_database_reads(self):
        self.add()
        expected = report.build_report(self.root)
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(expected), stderr="")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (redirect_stdout(stdout), redirect_stderr(stderr),
              patch.object(report, "_host_requires_runtime", return_value=True),
              patch.object(report.subprocess, "run", return_value=completed),
              patch.object(report.sqlite3, "connect", side_effect=AssertionError("host SQLite forbidden"))):
            code = report.main(["--root", str(self.root), "--runtime", "--report", "reports/runtime.json"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue()), json.loads((self.root / "reports/runtime.json").read_text("utf-8")))
        self.assertNotIn(SECRET, stdout.getvalue())

    def test_runtime_docker_failures_are_static_and_do_not_fall_back(self):
        cases = [OSError(SECRET), subprocess.TimeoutExpired("docker", 30, output=SECRET),
                 SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET),
                 SimpleNamespace(returncode=0, stdout=SECRET, stderr=SECRET),
                 SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "secret": SECRET}), stderr="")]
        for value in cases:
            options = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
            stdout, stderr = io.StringIO(), io.StringIO()
            with (self.subTest(value=type(value).__name__), redirect_stdout(stdout), redirect_stderr(stderr),
                  patch.object(report.subprocess, "run", **options),
                  patch.object(report.sqlite3, "connect") as connect):
                code = report.main(["--root", str(self.root), "--runtime"])
            self.assertEqual(code, 1)
            connect.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn(SECRET, stderr.getvalue())
            self.assertIn(json.loads(stderr.getvalue())["code"],
                          {"runtime_report_unavailable", "runtime_report_failed", "runtime_report_invalid"})

    def test_offline_reader_observes_committed_wal_without_checkpoint(self):
        self.add()
        with closing(sqlite3.connect(self.path)) as writer:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE receipts SET confirmed_segments=3,first_ack_at=107")
            writer.commit()
            self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 0)
            result = report.build_report(self.root)
            self.assertEqual(result["counts"]["acknowledged"], 1)
            self.assertEqual(result["selected_first_ack_seconds"]["p50"], 7.0)

    def test_cli_optional_report_is_json_and_does_not_modify_runtime(self):
        self.add()
        before = self.path.read_bytes()
        code, stdout, stderr = self.run_cli("--report", "reports/pacing/result.json")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), json.loads((self.root / "reports/pacing/result.json").read_text("utf-8")))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn(SECRET, stdout)

    def test_cli_rejects_runtime_database_and_other_report_locations(self):
        before = self.path.read_bytes()
        for target in (str(report.LEDGER), "runtime/pacing.json", "reports/../runtime/pacing.json",
                       "reports/data.sqlite3", "reports", "outside.json"):
            with self.subTest(target=target):
                code, stdout, stderr = self.run_cli("--report", target)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["code"], "report_must_be_inside_reports")
        self.assertEqual(self.path.read_bytes(), before)

    def test_report_existing_hard_link_does_not_modify_database(self):
        target = self.root / "reports/existing.json"
        target.parent.mkdir()
        try:
            os.link(self.path, target)
        except OSError as error:
            self.skipTest(f"hard links unavailable: {type(error).__name__}")
        before = self.path.read_bytes()
        code, stdout, stderr = self.run_cli("--report", "reports/existing.json")
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(json.loads(target.read_text("utf-8"))["ok"])

    def test_reports_symlink_cannot_redirect_into_runtime(self):
        try:
            (self.root / "reports").symlink_to(self.root / "runtime", target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {type(error).__name__}")
        code, stdout, stderr = self.run_cli("--report", "reports/pacing.json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stderr)["code"], "report_must_be_inside_reports")
        self.assertFalse((self.root / "runtime/pacing.json").exists())

    def test_nested_symlink_cannot_redirect_into_runtime(self):
        (self.root / "reports").mkdir()
        try:
            (self.root / "reports/nested").symlink_to(self.root / "runtime", target_is_directory=True)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {type(error).__name__}")
        code, stdout, stderr = self.run_cli("--report", "reports/nested/pacing.json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stderr)["code"], "report_must_be_inside_reports")
        self.assertFalse((self.root / "runtime/pacing.json").exists())


class TimeTests(unittest.TestCase):
    def test_timezone_required_and_offsets_are_equivalent(self):
        self.assertEqual(report.parse_time("2026-10-25T18:00:00+08:00"),
                         report.parse_time("2026-10-25T10:00:00Z"))
        for value in ("2026-10-25T18:00:00", "2026-10-25", SECRET, "1960-01-01T00:00:00Z"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                report.parse_time(value)


if __name__ == "__main__":
    unittest.main()
