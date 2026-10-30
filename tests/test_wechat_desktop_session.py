"""Offline lifetime/cleanup checks; never operate real Docker or WeChat."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import wechat_desktop_session as session

SESSION = "9e68b239701e4ae5a14db836f2766485"
PROCESS_PROBE = session.desktop_processes_running
PIPE_PROBE = session.linux_engine_pipe_present


def clean_state(**changes):
    value = dict(stage="stopped", process_lock_held=False, connected=False,
                 cleanup_required=False, manual_recovery_required=False, native_pending=False)
    value.update(changes)
    return value


def reply(stdout="", code=0):
    return SimpleNamespace(stdout=stdout, returncode=code)


class FakeOwner:
    def __init__(self):
        self.running = True
        self.closed = False

    def alive(self):
        return self.running

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.directory / "compose.snowluma.yaml").touch()
        self.stack.enter_context(patch.object(session, "DATA", self.directory))
        self.stack.enter_context(patch.object(session, "ROOT", self.directory))
        self.stack.enter_context(patch.object(session.startup, "SESSION_FIELDS", {}, create=True))
        self.processes = self.stack.enter_context(patch.object(session, "desktop_processes_running", return_value=False))
        self.pipe = self.stack.enter_context(patch.object(session, "linux_engine_pipe_present", return_value=False))
        self.elapsed = 0
        self.stack.enter_context(patch.object(session.time, "monotonic", side_effect=lambda: self.elapsed))
        self.stack.enter_context(patch.object(session.time, "sleep", side_effect=self.advance))
        self.record = self.stack.enter_context(patch.object(session.startup, "record"))
        self.command = self.stack.enter_context(patch.object(session.startup, "command",
                                                             side_effect=AssertionError("Unexpected real command")))
        self.control = self.stack.enter_context(patch.object(session.startup, "native_control",
                                                             side_effect=AssertionError("Unexpected native call")))
        self.probe = self.stack.enter_context(patch.object(session.startup, "native_check"))
        self.recover = self.stack.enter_context(patch.object(session.startup, "native_recover"))
        self.notify = self.stack.enter_context(patch.object(session, "notify_stop_failed"))

    def advance(self, seconds):
        self.elapsed += seconds

    def test_clean_state_requires_all_flags_explicitly_false(self):
        self.assertTrue(session.clean_native_state(clean_state()))
        for key in ("process_lock_held", "connected", "cleanup_required", "manual_recovery_required", "native_pending"):
            with self.subTest(key=key):
                self.assertFalse(session.clean_native_state(clean_state(**{key: True})))
                value = clean_state()
                del value[key]
                self.assertFalse(session.clean_native_state(value))

    def test_stop_rechecks_released_lock_and_durable_clean_status(self):
        self.control.side_effect = [clean_state(), clean_state()]
        session.stop_native()
        self.assertEqual([call.args for call in self.control.call_args_list], [("stop",), ("status",)])
        self.probe.assert_not_called()
        self.recover.assert_not_called()

    def test_busy_or_pending_native_stops_no_backend(self):
        for state in (clean_state(process_lock_held=True), clean_state(native_pending=True)):
            with self.subTest(state=state):
                self.control.side_effect = None
                self.control.return_value = state
                with self.assertRaisesRegex(session.SessionError, "native_cleanup_unconfirmed"):
                    session.shutdown_services("docker.exe")
        self.command.assert_not_called()
        self.recover.assert_not_called()

    def test_old_marker_requires_absent_native_module_before_recovery(self):
        self.control.side_effect = [clean_state(cleanup_required=True), clean_state(), clean_state()]
        self.probe.return_value = dict(ready=True, native_module_loaded=False)
        session.stop_native()
        self.probe.assert_called_once_with()
        self.recover.assert_called_once_with()

    def test_existing_native_module_is_never_force_removed(self):
        self.control.side_effect = None
        self.control.return_value = clean_state(cleanup_required=True)
        self.probe.return_value = dict(ready=False, reason="existing_bridge_refused")
        with self.assertRaisesRegex(session.SessionError, "native_cleanup_unconfirmed"):
            session.shutdown_services("docker.exe")
        self.recover.assert_not_called()
        self.command.assert_not_called()

    def test_lock_reappearing_after_stop_blocks_backend_shutdown(self):
        self.control.side_effect = [clean_state(), clean_state(process_lock_held=True)]
        with self.assertRaisesRegex(session.SessionError, "native_cleanup_unconfirmed"):
            session.shutdown_services("docker.exe")
        self.command.assert_not_called()

    def test_normal_shutdown_orders_unload_project_services_and_desktop(self):
        events = []

        def control(action):
            events.append(("native", action))
            return clean_state()

        self.control.side_effect = control
        self.processes.side_effect = [True, False, False]
        responses = iter([reply("linux\n"), reply(), reply(), reply(), reply(), reply()])

        def command(arguments, **_):
            events.append(("docker", arguments))
            return next(responses)

        self.command.side_effect = command
        session.shutdown_services("docker.exe")
        self.assertEqual(events[:2], [("native", "stop"), ("native", "status")])
        commands = [entry[1] for entry in events if entry[0] == "docker"]
        self.assertEqual(commands[1], ["docker.exe", "compose", "stop", "--timeout", "20", "astrbot", "napcat"])
        self.assertEqual(commands[3], ["docker.exe", "compose", "-f", "compose.snowluma.yaml", "stop", "--timeout", "20", "snowluma"])
        self.assertEqual(commands[-1], ["docker.exe", "desktop", "stop", "--detach"])
        self.assertFalse(any(args[1:3] == ["desktop", "status"] for args in commands))
        self.assertFalse(any("--force" in args for args in commands))

    def test_project_still_running_blocks_desktop_shutdown(self):
        self.control.side_effect = None
        self.control.return_value = clean_state()
        self.processes.return_value = True
        self.command.side_effect = [reply("linux"), reply(), reply("astrbot-roleplay-astrbot-1")]
        with self.assertRaisesRegex(session.SessionError, "project_container_stop_unconfirmed"):
            session.shutdown_services("docker.exe")
        self.assertFalse(any(call.args[0][1:3] == ["desktop", "stop"] for call in self.command.call_args_list))

    def test_early_close_cancels_desktop_boot_without_assuming_api_failure_means_stopped(self):
        self.control.side_effect = None
        self.control.return_value = clean_state()
        self.processes.side_effect = [True, False, False]
        self.command.side_effect = [reply(code=1), reply()]
        session.shutdown_services("docker.exe")
        self.assertEqual([call.args[0] for call in self.command.call_args_list], [
            ["docker.exe", "info", "--format", "{{.OSType}}"],
            ["docker.exe", "desktop", "stop", "--detach"],
        ])

    def test_desktop_status_requires_positive_stopped_response(self):
        for response, expected in ((reply("Status stopped"), True), (reply("Status running"), False),
                                   (reply("Status stopped", code=1), False), (reply(code=1), False),
                                   (reply("Error: Status stopped was not received"), False)):
            self.command.side_effect = None
            self.command.return_value = response
            self.assertEqual(session.desktop_stopped("docker.exe"), expected)

    def test_engine_probe_timeout_still_uses_normal_desktop_shutdown(self):
        self.control.side_effect = None
        self.control.return_value = clean_state()
        self.processes.side_effect = [True, False, False]
        self.command.side_effect = [session.startup.StartupError("command_timeout"), reply()]
        session.shutdown_services("docker.exe")
        self.assertEqual(self.command.call_args_list[-1].args[0],
                         ["docker.exe", "desktop", "stop", "--detach"])
        self.assertFalse(any("compose" in call.args[0] for call in self.command.call_args_list))

    def test_non_timeout_engine_probe_error_is_not_silently_ignored(self):
        self.control.side_effect = None
        self.control.return_value = clean_state()
        self.processes.return_value = True
        self.command.side_effect = [session.startup.StartupError("command_failed")]
        with self.assertRaisesRegex(session.startup.StartupError, "command_failed"):
            session.shutdown_services("docker.exe")
        self.assertFalse(any(call.args[0][1:3] == ["desktop", "stop"] for call in self.command.call_args_list))

    def test_late_docker_boot_cannot_escape_an_early_stop(self):
        session.startup.SESSION_FIELDS["docker_launch_requested"] = True
        session.startup.SESSION_FIELDS["docker_launch_pid"] = 4242
        self.control.side_effect = None
        self.control.return_value = clean_state()
        # The first stopped response races the newly-created Desktop process.
        # The process then becomes running after the first normal stop request.
        self.command.side_effect = [reply(code=1), reply(), reply("Status stopped"), reply("Status running"), reply()]
        self.processes.side_effect = [True, True, False, False, False]
        session.shutdown_services("docker.exe")
        stops = [call.args[0] for call in self.command.call_args_list if call.args[0][1:3] == ["desktop", "stop"]]
        self.assertEqual(len(stops), 2)
        self.assertTrue(all("--force" not in command for command in stops))
        self.assertEqual(self.processes.call_count, 5)
        self.assertGreaterEqual(self.elapsed, 2)

    def test_stopped_status_with_a_live_backend_never_counts_as_exit(self):
        self.command.side_effect = None
        self.command.return_value = reply("Status stopped")
        self.processes.side_effect = [True, True, False, False]
        session.normal_desktop_exit("docker.exe")
        self.assertEqual(self.processes.call_count, 4)

    def test_absent_processes_and_pipe_do_not_call_unresponsive_status_cli(self):
        self.command.side_effect = [reply()]
        session.normal_desktop_exit("docker.exe")
        self.assertEqual(self.command.call_args_list[0].args[0], ["docker.exe", "desktop", "stop", "--detach"])
        self.assertEqual(self.command.call_count, 1)
        self.assertEqual(self.processes.call_count, 2)
        self.assertEqual(self.pipe.call_count, 2)

    def test_status_timeout_while_process_alive_is_unknown_not_stop_failure(self):
        self.processes.side_effect = [True, False, False]
        self.command.side_effect = [reply(), session.startup.StartupError("command_timeout")]
        session.normal_desktop_exit("docker.exe")
        self.assertEqual(self.command.call_args_list[-1].kwargs["timeout"], 3)
        self.assertEqual(self.processes.call_count, 3)

    def test_engine_pipe_prevents_process_absence_from_becoming_false_success(self):
        self.command.side_effect = [reply()]
        self.pipe.side_effect = [True, True, False, False]
        session.normal_desktop_exit("docker.exe")
        self.assertEqual(self.pipe.call_count, 4)
        self.assertEqual(self.command.call_count, 1)

    def test_already_absent_without_pending_launch_requires_two_os_samples(self):
        self.control.side_effect = None
        self.control.return_value = clean_state()
        session.shutdown_services("docker.exe")
        self.command.assert_not_called()
        self.assertEqual(self.processes.call_count, 2)
        self.assertEqual(self.pipe.call_count, 2)

    def test_pending_launch_still_requests_stop_and_settles_when_initially_absent(self):
        session.startup.SESSION_FIELDS["docker_launch_requested"] = True
        self.control.side_effect = None
        self.control.return_value = clean_state()
        self.command.side_effect = [reply(code=1), reply()]
        session.shutdown_services("docker.exe")
        self.assertEqual(self.command.call_args_list[-1].args[0], ["docker.exe", "desktop", "stop", "--detach"])
        self.assertGreaterEqual(self.elapsed, 2)

    def test_pipe_probe_reads_exact_engine_pipe_and_fails_on_query_error(self):
        with patch.object(session.os, "listdir", return_value=["system", "dockerDesktopLinuxEngine"]):
            self.assertTrue(PIPE_PROBE())
        with patch.object(session.os, "listdir", return_value=["dockerDesktopLinuxEngine-suffix", "system"]):
            self.assertFalse(PIPE_PROBE())
        with patch.object(session.os, "listdir", side_effect=OSError("private details")):
            with self.assertRaisesRegex(session.SessionError, "^docker_pipe_query_failed$"):
                PIPE_PROBE()

    def test_process_probe_recognizes_only_exact_docker_image_names(self):
        self.command.side_effect = None
        for output, expected in ((
            '"System","4","Services","0","1 K"\n"Docker Desktop.exe","22","Console","1","1 K"', True),
            ('"com.docker.backend.exe","23","Console","1","1 K"', True),
            ('"docker.exe","24","Console","1","1 K"', False)):
            self.command.return_value = reply(output)
            self.assertEqual(PROCESS_PROBE(), expected)
        self.command.return_value = reply("")
        with self.assertRaisesRegex(session.SessionError, "docker_process_query_failed"):
            PROCESS_PROBE()

    def test_session_lock_stays_held_until_cleanup_finishes(self):
        owner = FakeOwner()

        def start():
            session.startup.CANCEL_EVENT.set()

        def stop():
            with self.assertRaises(session.startup.BridgeError):
                with session.startup.single_bridge(self.directory / "desktop-start.lock"):
                    pass
            self.assertIsNone(session.startup.CANCEL_EVENT)

        with patch.object(session.startup, "start", side_effect=start), patch.object(session, "shutdown_services", side_effect=stop):
            self.assertEqual(session.run_session(42, SESSION, owner_factory=lambda _: owner), 0)
        with session.startup.single_bridge(self.directory / "desktop-start.lock"):
            pass
        result = json.loads(session.result_path(SESSION).read_text("utf-8"))
        self.assertEqual(result["stage"], "stopped")
        self.assertFalse(result["process_lock_held"])
        self.assertFalse(result["connected"])
        self.assertFalse(result["dashboard_ready"])
        self.assertIsNone(result["heartbeat_updated_at"])
        terminal_fields = self.record.call_args.kwargs
        self.assertFalse(terminal_fields["process_lock_held"])
        self.assertFalse(terminal_fields["connected"])
        self.assertFalse(terminal_fields["dashboard_ready"])
        self.assertIsNone(terminal_fields["heartbeat_updated_at"])
        self.assertTrue(owner.closed)

    def test_owner_monitor_never_stops_in_parallel_with_start(self):
        owner = FakeOwner()
        sequence = []
        main_thread = threading.get_ident()

        def start():
            sequence.append("start")
            owner.running = False
            self.assertTrue(session.startup.CANCEL_EVENT.wait(2))
            sequence.append("start_return")

        def stop():
            self.assertEqual(threading.get_ident(), main_thread)
            sequence.append("stop")

        with patch.object(session.startup, "start", side_effect=start), patch.object(session, "shutdown_services", side_effect=stop):
            self.assertEqual(session.run_session(42, SESSION, owner_factory=lambda _: owner), 0)
        self.assertEqual(sequence, ["start", "start_return", "stop"])

    def test_cancellation_during_start_is_cleaned_up_and_reported(self):
        owner = FakeOwner()
        with patch.object(session.startup, "start", side_effect=session.startup.StartupError("startup_cancelled")), \
                patch.object(session, "shutdown_services") as shutdown:
            self.assertEqual(session.run_session(42, SESSION, owner_factory=lambda _: owner), 0)
        shutdown.assert_called_once_with()
        result = json.loads(session.result_path(SESSION).read_text("utf-8"))
        self.assertEqual(result["stage"], "stopped")
        self.assertEqual(result["startup_error"], "startup_cancelled")

    def test_start_failure_retains_static_reason_after_successful_cleanup(self):
        owner = FakeOwner()
        with patch.object(session.startup, "start", side_effect=session.startup.StartupError("astrbot_not_ready")), \
                patch.object(session, "shutdown_services"):
            self.assertEqual(session.run_session(42, SESSION, owner_factory=lambda _: owner), 1)
        result = json.loads(session.result_path(SESSION).read_text("utf-8"))
        self.assertEqual(result["stage"], "stopped")
        self.assertEqual(result["startup_error"], "astrbot_not_ready")

    def test_stop_failure_after_console_close_persists_result_and_notifies(self):
        owner = FakeOwner()

        def start():
            owner.running = False
            session.startup.CANCEL_EVENT.set()

        with patch.object(session.startup, "start", side_effect=start), \
                patch.object(session, "shutdown_services", side_effect=session.SessionError("native_cleanup_unconfirmed")):
            self.assertEqual(session.run_session(42, SESSION, owner_factory=lambda _: owner), 1)
        self.notify.assert_called_once_with()
        result = json.loads(session.result_path(SESSION).read_text("utf-8"))
        self.assertEqual(result["stage"], "stop_failed")
        self.assertEqual(result["reason"], "native_cleanup_unconfirmed")
        self.assertIsNone(result["connected"])
        self.assertIsNone(result["dashboard_ready"])

    def test_failed_stop_refreshes_native_flags_and_marks_dashboard_unknown(self):
        self.control.side_effect = None
        self.control.return_value = dict(process_lock_held=False, connected=True, updated_at=123)
        fields = session.failed_stop_fields()
        self.assertFalse(fields["process_lock_held"])
        self.assertFalse(fields["connected"])
        self.assertIsNone(fields["heartbeat_updated_at"])
        self.assertIsNone(fields["dashboard_ready"])
        self.control.return_value = dict(process_lock_held=True, connected=True, updated_at=456)
        fields = session.failed_stop_fields()
        self.assertTrue(fields["process_lock_held"])
        self.assertTrue(fields["connected"])
        self.assertEqual(fields["heartbeat_updated_at"], 456)

    def test_busy_session_does_not_touch_active_status_or_services(self):
        with session.startup.single_bridge(self.directory / "desktop-start.lock"), \
                patch.object(session.startup, "start") as start, patch.object(session, "shutdown_services") as stop:
            self.assertEqual(session.run_session(42, SESSION), 3)
        start.assert_not_called()
        stop.assert_not_called()
        self.record.assert_not_called()
        self.assertEqual(json.loads(session.result_path(SESSION).read_text("utf-8"))["stage"], "busy")

    def test_owner_handle_failure_prevents_any_start_or_stop(self):
        with patch.object(session.startup, "start") as start, patch.object(session, "shutdown_services") as stop:
            code = session.run_session(42, SESSION, owner_factory=Mock(side_effect=session.SessionError("owner_unavailable")))
        self.assertEqual(code, 1)
        start.assert_not_called()
        stop.assert_not_called()
        self.record.assert_not_called()

    def test_owner_handle_retains_64bit_handle_and_closes_it(self):
        kernel = SimpleNamespace(OpenProcess=Mock(return_value=0x100000001),
                                 WaitForSingleObject=Mock(side_effect=[258, 0]), CloseHandle=Mock(return_value=True))
        with session.WindowsOwner(42, kernel=kernel) as owner:
            self.assertTrue(owner.alive())
            self.assertFalse(owner.alive())
            self.assertEqual(owner.handle, 0x100000001)
        kernel.OpenProcess.assert_called_once_with(0x00100000, False, 42)
        kernel.CloseHandle.assert_called_once_with(0x100000001)

    def test_session_paths_reject_traversal_or_noncanonical_identifiers(self):
        for value in ("../x", "", "0" * 32, SESSION.upper(), str(None)):
            with self.subTest(value=value), self.assertRaises(session.SessionError):
                session.stop_path(value)

    def test_stop_only_never_starts_or_connects_services(self):
        with patch.object(session.startup, "start") as start, \
                patch.object(session, "watch_session") as watch, \
                patch.object(session, "shutdown_services") as stop:
            self.assertEqual(session.run_session(42, SESSION, owner_factory=lambda _: FakeOwner(), stop_only=True), 0)
        start.assert_not_called()
        watch.assert_not_called()
        stop.assert_called_once_with()
        self.assertEqual(json.loads(session.result_path(SESSION).read_text("utf-8"))["stage"], "stopped")

    def test_lost_bridge_marks_degraded_without_automatic_reinjection_or_shutdown(self):
        cancelled = threading.Event()
        self.control.side_effect = None
        self.control.return_value = clean_state()
        with patch.object(cancelled, "wait", side_effect=lambda _: cancelled.set()), \
                patch.object(session, "reconnect_native") as reconnect, \
                patch.object(session, "shutdown_services") as stop:
            session.watch_session(SESSION, cancelled)
        reconnect.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(self.record.call_args.args, ("degraded", "bridge_disconnected"))
        self.assertFalse(self.record.call_args.kwargs["connected"])

    def test_stalled_heartbeat_is_not_ready_even_with_held_lock(self):
        cancelled = threading.Event()
        self.control.side_effect = None
        self.control.return_value = dict(stage="running", process_lock_held=True, connected=True,
                                         updated_at=1, manual_recovery_required=False, native_pending=False)
        with patch.object(cancelled, "wait", side_effect=lambda _: cancelled.set()):
            session.watch_session(SESSION, cancelled)
        self.assertEqual(self.record.call_args.args, ("degraded", "bridge_heartbeat_stalled"))
        self.assertFalse(self.record.call_args.kwargs["connected"])

    def test_reconnect_requires_explicit_request_and_keeps_static_failure_reason(self):
        cancelled = threading.Event()
        session.reconnect_path(SESSION).write_text("reconnect")
        self.control.side_effect = None
        self.control.return_value = clean_state()
        with patch.object(cancelled, "wait", side_effect=lambda _: cancelled.set()), \
                patch.object(session, "reconnect_native", side_effect=session.startup.StartupError("native_disabled")) as reconnect:
            session.watch_session(SESSION, cancelled)
        reconnect.assert_called_once_with()
        self.assertFalse(session.reconnect_path(SESSION).exists())
        self.assertEqual(self.record.call_args.args, ("degraded", "native_disabled"))

    def test_close_takes_priority_over_queued_reconnect(self):
        cancelled = threading.Event()
        cancelled.set()
        session.reconnect_path(SESSION).write_text("reconnect")
        with patch.object(session, "reconnect_native") as reconnect:
            session.watch_session(SESSION, cancelled)
        reconnect.assert_not_called()
        self.control.assert_not_called()

    def test_reconnect_stops_before_full_safe_start_and_heartbeat(self):
        events = []
        def step(name, result=None):
            def run(*args, **kwargs):
                events.append(name)
                return result
            return run
        with patch.object(session, "stop_native", side_effect=step("stop")), \
                patch.object(session.startup, "validate_local", side_effect=step("validate", "docker.exe")), \
                patch.object(session.startup, "start_astrbot", side_effect=step("backend", 123)), \
                patch.object(session.startup, "wait_backend", side_effect=step("backend_ready")), \
                patch.object(session.startup, "ensure_native", side_effect=step("full_preflight_start", {"updated_at": 1})), \
                patch.object(session.startup, "wait_heartbeat", side_effect=step("heartbeat", {"updated_at": 2})):
            session.reconnect_native()
        self.assertEqual(events, ["stop", "validate", "backend", "backend_ready", "full_preflight_start", "heartbeat"])
        self.assertEqual(self.record.call_args.args, ("ready", "wechat_connected"))

    def test_close_during_reconnect_prevents_any_new_start(self):
        cancelled = threading.Event()
        with patch.object(session.startup, "CANCEL_EVENT", cancelled), \
                patch.object(session, "stop_native", side_effect=cancelled.set), \
                patch.object(session.startup, "validate_local") as validate:
            with self.assertRaisesRegex(session.startup.StartupError, "startup_cancelled"):
                session.reconnect_native()
        validate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
