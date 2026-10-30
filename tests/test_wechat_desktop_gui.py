"""Window-close and repeated-launch regressions using real Tk and Windows locks."""
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch, Mock

from scripts import wechat_desktop_gui as gui


@unittest.skipUnless(os.name == "nt", "Windows desktop launcher")
class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.object(gui, "DATA", Path(self.temp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.window = gui.ControlWindow(self.root)
        # Tests invoke begin explicitly without running the scheduled callback.
        self.root.after_cancel(self.window.start_timer)
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = patch.object(gui.subprocess, "Popen", return_value=self.process)
        self.popen.start()
        self.addCleanup(self.popen.stop)
        self.window.begin()

    def write_result(self, **value):
        self.window.result_file.write_text(json.dumps(value), encoding="utf-8")

    def test_close_waits_for_verified_shutdown_and_keeps_window_alive(self):
        self.window.close_requested()
        self.assertTrue(self.root.winfo_exists())
        self.assertTrue(self.window.stopping)
        self.assertFalse(self.window.finished)
        self.assertTrue((gui.DATA / ("desktop-stop-" + self.window.session_id)).exists())
        self.window.tick()
        self.assertTrue(self.root.winfo_exists())
        self.write_result(stage="stopped", startup_error=None)
        self.window.tick()
        self.assertEqual(self.window.stage, "stopped")
        self.assertTrue(self.window.finished)

    def test_failed_shutdown_keeps_window_and_retries_only_shutdown(self):
        self.write_result(stage="stop_failed", reason="docker_desktop_stop_unconfirmed")
        self.window.tick()
        self.assertTrue(self.root.winfo_exists())
        original = self.window.session_id
        self.window.close_requested()
        self.assertNotEqual(self.window.session_id, original)
        self.assertTrue(self.window.stopping)
        self.assertIn("--stop-only", gui.subprocess.Popen.call_args.args[0])

    def test_duplicate_window_cannot_spawn_a_second_session(self):
        first, second = gui.WindowLease(), gui.WindowLease()
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            self.assertTrue((gui.DATA / "desktop-window.show").exists())
        finally:
            first.close()
            second.close()

    def test_explicit_reconnect_request_is_session_specific(self):
        self.window.reconnect_requested()
        self.assertTrue((gui.DATA / ("desktop-reconnect-" + self.window.session_id)).exists())
        self.window.close_requested()
        self.assertEqual(str(self.window.reconnect.cget("state")), "disabled")

    def test_supervisor_is_console_free_and_logs_failures(self):
        call = gui.subprocess.Popen.call_args
        self.assertEqual(Path(call.args[0][0]).name, "pythonw.exe")
        self.assertTrue(call.kwargs["creationflags"] & gui.subprocess.CREATE_NO_WINDOW)
        self.assertFalse(call.kwargs["creationflags"] & gui.subprocess.DETACHED_PROCESS)
        self.assertIsNotNone(call.kwargs["stderr"])

    def test_lost_supervisor_never_claims_clean_shutdown(self):
        self.process.poll.return_value = 1
        self.window.tick()
        self.window.tick()
        self.assertEqual(self.window.stage, "failed")
        self.assertIn("异常退出", self.window.message.get())

    def test_disconnected_state_is_visible(self):
        self.window.render({"stage": "degraded", "connected": False,
                            "dashboard_ready": True, "message": "微信监听已断开"})
        self.assertEqual(self.window.title.get(), "微信监听已断开")
        self.assertEqual(str(self.window.reconnect.cget("state")), "normal")

    def test_close_before_delayed_begin_cancels_start_without_spawning(self):
        other = tk.Tk()
        other.withdraw()
        window = gui.ControlWindow(other)
        calls = gui.subprocess.Popen.call_count
        with patch.object(other, "destroy") as destroy:
            window.close_requested()
            destroy.assert_called_once_with()
        self.assertIsNone(window.start_timer)
        self.assertIsNone(window.session_id)
        self.assertEqual(gui.subprocess.Popen.call_count, calls)
        other.destroy()

    def test_pending_close_cannot_be_overwritten_by_previous_ready_snapshot(self):
        self.window.close_requested()
        self.window.render({"stage": "ready", "heartbeat_updated_at": time.time(),
                            "dashboard_ready": True, "message": "已连接"})
        self.assertEqual(self.window.title.get(), "正在关闭")
        self.assertEqual(str(self.window.stop.cget("state")), "disabled")
        self.assertEqual(str(self.window.reconnect.cget("state")), "disabled")

    def test_pending_reconnect_cannot_be_overwritten_or_requeued(self):
        self.window.reconnect_requested()
        self.window.render({"stage": "ready", "heartbeat_updated_at": time.time(),
                            "message": "已连接"})
        self.assertEqual(self.window.title.get(), "正在重新连接")
        marker = gui.DATA / ("desktop-reconnect-" + self.window.session_id)
        marker.write_text("first request")
        self.window.reconnect_requested()
        self.assertEqual(marker.read_text(), "first request")

    def test_expired_ready_snapshot_does_not_remain_connected(self):
        self.window.render({"stage": "ready", "heartbeat_updated_at": time.time() - 60,
                            "dashboard_ready": True})
        self.assertEqual(self.window.stage, "degraded")
        self.assertIn("过期", self.window.message.get())

    def test_busy_waits_for_previous_session_then_starts_without_stopping_it(self):
        self.write_result(stage="busy")
        self.window.tick()
        calls = gui.subprocess.Popen.call_count
        old_session = self.window.session_id
        self.assertTrue(self.window.waiting_for_session)
        with patch.object(gui, "session_available", return_value=False):
            self.window.tick()
        self.assertEqual(gui.subprocess.Popen.call_count, calls)
        self.assertEqual(list(gui.DATA.glob("desktop-stop-*")), [])
        with patch.object(gui, "session_available", return_value=True):
            self.window.tick()
        self.assertNotEqual(self.window.session_id, old_session)
        self.assertEqual(gui.subprocess.Popen.call_count, calls + 1)
        self.assertNotIn("--stop-only", gui.subprocess.Popen.call_args.args[0])

    def test_closing_busy_window_does_not_stop_previous_session(self):
        self.write_result(stage="busy")
        self.window.tick()
        with patch.object(self.root, "destroy") as destroy:
            self.window.close_requested()
            destroy.assert_called_once_with()
        self.assertEqual(list(gui.DATA.glob("desktop-stop-*")), [])

    def test_callback_exception_requests_orderly_stop_and_keeps_window(self):
        with patch.object(gui.sys, "stderr"):
            self.window.callback_failed(ValueError, ValueError("private"), None)
        self.assertTrue(self.window.stopping)
        self.assertTrue(self.root.winfo_exists())
        self.assertTrue((gui.DATA / ("desktop-stop-" + self.window.session_id)).exists())
        self.assertNotIn("private", self.window.message.get())

    def test_failed_spawn_does_not_retain_previous_process_handle(self):
        with patch.object(gui.subprocess, "Popen", side_effect=OSError):
            self.window.begin(stop_only=True)
        self.assertIsNone(self.window.process)
        self.assertTrue(self.window.finished)


if __name__ == "__main__":
    unittest.main()
