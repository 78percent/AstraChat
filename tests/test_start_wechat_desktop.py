"""Offline desktop-start behavior checks. No native, Docker, or network calls."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from scripts import start_wechat_desktop as launcher

REAL_COMMAND = launcher.command
REAL_NATIVE_CONTROL = launcher.native_control


class FakeClock:
    def __init__(self):
        self.wall = 1000.0
        self.elapsed = 0.0

    def sleep(self, seconds):
        self.wall += seconds
        self.elapsed += seconds


def ready_state(stamp=1000.0, **changes):
    state = dict(process_lock_held=True, stage="running", connected=True,
                 manual_recovery_required=False, native_pending=False,
                 cleanup_required=True, updated_at=stamp)
    state.update(changes)
    return state


def stopped_state(**changes):
    state = dict(process_lock_held=False, stage="stopped", connected=False,
                 manual_recovery_required=False, native_pending=False,
                 cleanup_required=False, updated_at=900.0)
    state.update(changes)
    return state


CLEAN_PROBE = dict(ready=True, enabled=True, native_module_loaded=False)


class DesktopStartupTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = FakeClock()
        self.stack.enter_context(patch.object(launcher, "CANCEL_EVENT", None))
        self.stack.enter_context(patch.object(launcher, "SESSION_FIELDS", {}))
        self.stack.enter_context(patch.object(launcher, "_STARTED_AT", None))
        self.stack.enter_context(patch.object(launcher.time, "time", side_effect=lambda: self.clock.wall))
        self.stack.enter_context(patch.object(launcher.time, "monotonic", side_effect=lambda: self.clock.elapsed))
        self.stack.enter_context(patch.object(launcher.time, "sleep", side_effect=self.clock.sleep))
        self.stack.enter_context(patch.object(launcher, "command", side_effect=AssertionError("Unexpected real command")))
        self.stack.enter_context(patch.object(launcher.subprocess, "Popen", side_effect=AssertionError("Unexpected process launch")))
        self.stack.enter_context(patch.object(launcher.urllib.request, "build_opener", side_effect=AssertionError("Unexpected network call")))
        self.record = self.stack.enter_context(patch.object(launcher, "record"))
        self.emit = self.stack.enter_context(patch.object(launcher, "emit"))

    def test_ready_requires_live_lock_connection_and_recent_heartbeat(self):
        self.assertTrue(launcher.native_ready(ready_state()))
        for changes in (
            {"process_lock_held": False}, {"connected": False}, {"updated_at": 970},
            {"updated_at": 1004}, {"updated_at": float("nan")}, {"updated_at": True},
            {"manual_recovery_required": True}, {"native_pending": True}, {"stage": "failed"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(launcher.native_ready(ready_state(**changes)))

    def test_running_cleanup_marker_never_causes_recovery_or_duplicate_start(self):
        state = ready_state(cleanup_required=True)
        with patch.object(launcher, "native_control", return_value=state) as control, \
                patch.object(launcher, "native_check") as probe, \
                patch.object(launcher, "native_recover") as recover, \
                patch.object(launcher, "open_wechat_if_absent") as open_client:
            self.assertEqual(launcher.ensure_native(), state)
        control.assert_called_once_with("status")
        probe.assert_not_called()
        recover.assert_not_called()
        open_client.assert_not_called()

    def test_wrong_account_waits_until_clean_bound_login_before_starting(self):
        events = []
        checks = iter((dict(stage="failed", reason="expected_account_mismatch"), CLEAN_PROBE))

        def probe():
            value = next(checks)
            events.append("correct_account" if value.get("ready") else "wrong_account")
            return value

        def control(action):
            if action == "start":
                events.append("start")
                return {"stage": "running", "started": True}
            if "start" in events:
                return ready_state(self.clock.wall)
            return stopped_state()

        with patch.object(launcher, "native_check", side_effect=probe), \
                patch.object(launcher, "native_control", side_effect=control), \
                patch.object(launcher, "open_wechat_if_absent"), \
                patch.object(launcher, "native_recover") as recover:
            self.assertTrue(launcher.native_ready(launcher.ensure_native()))
        self.assertEqual(events, ["wrong_account", "correct_account", "start"])
        self.assertEqual(self.clock.elapsed, 3)
        recover.assert_not_called()
        self.record.assert_any_call("waiting_for_wechat", "expected_account_mismatch")

    def test_stale_cleanup_recovers_only_after_clean_preflight(self):
        events = []
        stale = stopped_state(stage="running", connected=True, cleanup_required=True)

        def control(action):
            events.append(action)
            if action == "start":
                return {"stage": "running"}
            return ready_state(self.clock.wall) if "start" in events else stale

        def probe():
            events.append("clean_preflight")
            return CLEAN_PROBE

        with patch.object(launcher, "native_control", side_effect=control), \
                patch.object(launcher, "native_check", side_effect=probe), \
                patch.object(launcher, "native_recover", side_effect=lambda: events.append("recover")), \
                patch.object(launcher, "open_wechat_if_absent"):
            self.assertTrue(launcher.native_ready(launcher.ensure_native()))
        self.assertEqual(events, ["status", "clean_preflight", "status", "recover", "start", "status"])

    def test_existing_native_module_waits_without_recovery_injection_or_force_stop(self):
        stale = stopped_state(cleanup_required=True)
        with patch.object(launcher, "native_control", return_value=stale) as control, \
                patch.object(launcher, "native_check", return_value={"stage": "failed", "reason": "existing_bridge_refused"}), \
                patch.object(launcher, "native_recover") as recover, \
                patch.object(launcher, "open_wechat_if_absent"):
            with self.assertRaisesRegex(launcher.StartupError, "^login_timeout$"):
                launcher.ensure_native(login_timeout=4)
        control.assert_called_once_with("status")
        recover.assert_not_called()
        self.record.assert_called_once_with("waiting_for_wechat", "existing_bridge_refused")

    def test_environment_fingerprint_failure_does_not_retry_or_start(self):
        with patch.object(launcher, "native_control", return_value=stopped_state()) as control, \
                patch.object(launcher, "native_check", return_value={"stage": "failed", "reason": "client_fingerprint_mismatch"}) as probe, \
                patch.object(launcher, "native_recover") as recover, \
                patch.object(launcher, "open_wechat_if_absent"):
            with self.assertRaisesRegex(launcher.StartupError, "^client_fingerprint_mismatch$"):
                launcher.ensure_native()
        control.assert_called_once_with("status")
        probe.assert_called_once_with()
        recover.assert_not_called()
        self.assertEqual(self.clock.elapsed, 0)

    def test_process_started_during_preflight_is_reused(self):
        live = ready_state()
        with patch.object(launcher, "native_control", side_effect=[stopped_state(cleanup_required=True), live]) as control, \
                patch.object(launcher, "native_check", return_value=CLEAN_PROBE), \
                patch.object(launcher, "native_recover") as recover, \
                patch.object(launcher, "open_wechat_if_absent"):
            self.assertEqual(launcher.ensure_native(), live)
        self.assertEqual([call.args[0] for call in control.call_args_list], ["status", "status"])
        recover.assert_not_called()

    def test_busy_unhealthy_process_is_not_replaced(self):
        with patch.object(launcher, "native_control", return_value=ready_state(updated_at=800)) as control, \
                patch.object(launcher, "native_check") as probe, \
                patch.object(launcher, "native_recover") as recover:
            with self.assertRaisesRegex(launcher.StartupError, "^bridge_busy$"):
                launcher.ensure_native()
        self.assertTrue(all(call.args == ("status",) for call in control.call_args_list))
        probe.assert_not_called()
        recover.assert_not_called()

    def backend_fixture(self, stamp=905, ready=True):
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        path = Path(directory) / "runtime-status.json"
        path.write_text(json.dumps(dict(terminated=False, updated_at_unix=stamp,
                                       services={"native": dict(ready=ready, enabled=True)})), encoding="utf-8")
        self.stack.enter_context(patch.object(launcher, "PLUGIN_STATUS", path))
        return path

    def test_backend_rejects_snapshot_from_previous_container_boot(self):
        self.backend_fixture(stamp=905)
        with patch.object(launcher.urllib.request, "build_opener") as opener:
            self.assertFalse(launcher.backend_ready(950))
        opener.assert_not_called()

    def test_backend_requires_native_endpoint_401_in_addition_to_current_snapshot(self):
        self.backend_fixture(stamp=905)
        for status, expected in ((401, True), (403, False), (404, False), (500, False)):
            with self.subTest(status=status):
                opener = Mock()
                opener.open.side_effect = urllib.error.HTTPError("local", status, "test", {}, None)
                with patch.object(launcher.urllib.request, "build_opener", return_value=opener):
                    self.assertEqual(launcher.backend_ready(900), expected)
                opener.open.assert_called_once_with("http://127.0.0.1:6198/wechat-native/ws", timeout=2)

    def test_backend_snapshot_is_not_treated_as_a_periodic_heartbeat(self):
        self.backend_fixture(stamp=100)
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("local", 401, "test", {}, None)
        with patch.object(launcher.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(launcher.backend_ready(99))

    def start_dependencies(self, final):
        self.stack.enter_context(patch.object(launcher, "validate_local", return_value="docker.exe"))
        self.stack.enter_context(patch.object(launcher, "ensure_docker"))
        self.stack.enter_context(patch.object(launcher, "start_astrbot", return_value=900))
        self.stack.enter_context(patch.object(launcher, "wait_backend"))
        self.stack.enter_context(patch.object(launcher, "prepare_wechat", return_value=CLEAN_PROBE))
        self.stack.enter_context(patch.object(launcher, "dashboard_ready", return_value=True))
        self.workbench = self.stack.enter_context(patch.object(launcher, "open_workbench"))
        self.stack.enter_context(patch.object(launcher, "ensure_native", return_value=ready_state(1000)))
        self.stack.enter_context(patch.object(launcher, "native_control", return_value=final))

    def test_success_requires_heartbeat_to_advance(self):
        self.start_dependencies(ready_state(1000.5))
        launcher.start()
        successes = [call for call in self.record.call_args_list if call.args == ("ready", "wechat_connected")]
        self.assertEqual(len(successes), 1)
        self.assertTrue(successes[0].kwargs["process_lock_held"])
        self.assertTrue(successes[0].kwargs["connected"])
        self.assertEqual(successes[0].kwargs["heartbeat_updated_at"], 1000.5)
        self.assertIn("ready", successes[0].kwargs["timings"])
        self.workbench.assert_called_once_with()

    def test_fresh_but_unchanged_heartbeat_does_not_publish_ready(self):
        self.start_dependencies(ready_state(1000))
        with self.assertRaisesRegex(launcher.StartupError, "^bridge_heartbeat_stalled$"):
            launcher.start()
        self.assertFalse(any(call.args[0] == "ready" for call in self.record.call_args_list))

    def test_advanced_timestamp_without_lock_does_not_publish_ready(self):
        self.start_dependencies(ready_state(1005, process_lock_held=False))
        with self.assertRaisesRegex(launcher.StartupError, "^bridge_heartbeat_stalled$"):
            launcher.start()
        self.assertFalse(any(call.args[0] == "ready" for call in self.record.call_args_list))

    def test_only_existing_astrbot_service_is_started(self):
        replies = [SimpleNamespace(returncode=0, stdout="abc123\n"),
                   SimpleNamespace(returncode=0, stdout="")]
        state = dict(Running=True, StartedAt="2026-10-26T01:02:03.123456789Z")
        with patch.object(launcher, "command", side_effect=replies) as command, \
                patch.object(launcher, "json_command", side_effect=[dict(Running=False), state]):
            self.assertIsInstance(launcher.start_astrbot("docker.exe"), float)
        self.assertEqual([call.args[0] for call in command.call_args_list], [
            ["docker.exe", "compose", "ps", "--all", "--quiet", "astrbot"],
            ["docker.exe", "start", "abc123"],
        ])

    def test_running_astrbot_is_inspected_without_redundant_start(self):
        state = dict(Running=True, StartedAt="2026-10-26T01:02:03Z")
        with patch.object(launcher, "command", return_value=SimpleNamespace(returncode=0, stdout="abc123\n")) as command, \
                patch.object(launcher, "json_command", return_value=state) as inspect:
            self.assertIsInstance(launcher.start_astrbot("docker.exe"), float)
        command.assert_called_once_with(["docker.exe", "compose", "ps", "--all", "--quiet", "astrbot"])
        inspect.assert_called_once_with(["docker.exe", "inspect", "--format", "{{json .State}}", "abc123"])

    def test_wait_heartbeat_returns_on_next_heartbeat_without_fixed_delay(self):
        with patch.object(launcher, "native_control", side_effect=[ready_state(1000), ready_state(1000.25)]):
            final = launcher.wait_heartbeat(ready_state(1000))
        self.assertEqual(final["updated_at"], 1000.25)
        self.assertEqual(self.clock.elapsed, .25)

    def test_cold_start_old_heartbeat_is_not_accepted_as_new(self):
        with patch.object(launcher, "native_control", return_value=ready_state(990)):
            with self.assertRaisesRegex(launcher.StartupError, "^bridge_heartbeat_stalled$"):
                launcher.wait_heartbeat(ready_state(990), timeout=1)

    def test_wait_cancellation_interrupts_delay(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(launcher, "CANCEL_EVENT", cancelled):
            with self.assertRaisesRegex(launcher.StartupError, "^startup_cancelled$"):
                launcher.wait(99)
        self.assertEqual(self.clock.elapsed, 0)

    def test_mutating_command_completes_before_cancellation_is_raised(self):
        cancelled = threading.Event()
        events = []
        def mutate(*_, **__):
            events.append("mutation_started")
            cancelled.set()
            events.append("mutation_completed")
            return SimpleNamespace(returncode=0, stdout="")
        with patch.object(launcher, "CANCEL_EVENT", cancelled), \
                patch.object(launcher.subprocess, "run", side_effect=mutate) as run:
            with self.assertRaisesRegex(launcher.StartupError, "^startup_cancelled$"):
                REAL_COMMAND(["docker.exe", "start", "abc123"])
            events.append("cancel_observed")
        self.assertEqual(events, ["mutation_started", "mutation_completed", "cancel_observed"])
        run.assert_called_once()

    def test_native_start_completes_before_cancellation_is_raised(self):
        cancelled = threading.Event()
        events = []
        def mutate(action):
            self.assertEqual(action, "start")
            cancelled.set()
            events.append("native_control_returned")
            return ready_state()
        with patch.object(launcher, "CANCEL_EVENT", cancelled), \
                patch.object(launcher, "local_control", side_effect=mutate):
            with self.assertRaisesRegex(launcher.StartupError, "^startup_cancelled$"):
                REAL_NATIVE_CONTROL("start")
            events.append("cancel_observed")
        self.assertEqual(events, ["native_control_returned", "cancel_observed"])

    def test_cancelled_command_does_not_begin_mutation(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(launcher, "CANCEL_EVENT", cancelled), patch.object(launcher.subprocess, "run") as run:
            with self.assertRaisesRegex(launcher.StartupError, "^startup_cancelled$"):
                REAL_COMMAND(["docker.exe", "start", "abc123"])
        run.assert_not_called()

    def test_preflight_reuses_running_host_without_account_probe(self):
        with patch.object(launcher, "native_control", return_value=ready_state()), \
                patch.object(launcher, "open_wechat_if_absent") as open_client, \
                patch.object(launcher, "native_check") as probe:
            self.assertIsNone(launcher.prepare_wechat())
        open_client.assert_not_called()
        probe.assert_not_called()

    def test_main_hands_lifetime_to_gui(self):
        with patch("scripts.astrbot_launcher.main", return_value=7) as run_gui, \
                patch.object(launcher, "start") as start:
            self.assertEqual(launcher.main([]), 7)
        run_gui.assert_called_once_with([])
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
