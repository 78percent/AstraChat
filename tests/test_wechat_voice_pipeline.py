"""Failure and lifecycle contracts; no model, WeChat, or real sends."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from scripts.indextts_worker import wav_info
from scripts.wechat_native_transport import NativeTransport, TransportError
from scripts.wechat_voice_pipeline import deliver_prepared, run_directory, synthesize


class AudioTests(unittest.TestCase):
    def test_wrong_rate_empty_and_truncated_audio_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.wav"
            for rate, count in ((22050, 100), (24000, 0), (24000, 24000 * 61)):
                with wave.open(str(path), "wb") as audio:
                    audio.setparams((1, 2, rate, count, "NONE", "not compressed"))
                    audio.writeframes(b"\0\0" * count)
                with self.assertRaises(ValueError):
                    wav_info(path)
            with wave.open(str(path), "wb") as audio:
                audio.setparams((1, 2, 24000, 240, "NONE", "not compressed"))
                audio.writeframes(b"\0\0" * 240)
            self.assertEqual(wav_info(path)["duration_seconds"], .01)
            path.write_bytes(path.read_bytes()[:-4])
            with self.assertRaises(ValueError):
                wav_info(path)

    def test_path_traversal_is_refused(self):
        for identifier in (None, "../other", "f" * 31, "G" * 32):
            with self.assertRaises(ValueError):
                run_directory(identifier)

    def test_missing_reference_never_launches_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = directory / "config.json"
            config.write_text(json.dumps({"model_root": str(directory),
                                         "reference_audio": "missing.wav", "text": "test"}))
            with patch("scripts.wechat_voice_pipeline.subprocess.run") as process:
                with self.assertRaises(FileNotFoundError):
                    synthesize(config)
                process.assert_not_called()


class VoiceTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "voice.silk"
        self.path.write_bytes(b"fake test fixture")
        self.client = NativeTransport(Path(self.temp.name))
        self.client._active = Mock()
        self.client._check_runtime = Mock()
        self.client.pid, self.client.self_id = 31, "wxid_fixture"
        self.result = {"success": True, "pid": 31, "code": 0, "cleanup_code": 0,
                       "report": {"completion_success": True, "target": "1234@chatroom",
                                  "text": str(self.path), "voice_path": str(self.path),
                                  "duration_ms": 1000, "message_type": 34,
                                  "sent": True, "operation": "voice_send"}}
        self.run = Mock(return_value=self.result)
        self.client._api = SimpleNamespace(native=SimpleNamespace(TEXT_DLL=Path("fake.dll"), run=self.run))

    def test_matching_native_receipt_is_not_recipient_confirmation(self):
        result = self.client.send_voice("1234@chatroom", self.path, 1000)
        self.assertEqual(result["state"], "NATIVE_COMPLETED")
        self.assertFalse(result["recipient_delivery_confirmed"])
        self.assertEqual(self.run.call_args.kwargs["account"], "wxid_fixture")
        self.assertTrue(self.run.call_args.kwargs["keep_loaded"])
        self.assertFalse(self.run.call_args.kwargs["preserve_report"])

    def test_bound_private_voice_checks_the_same_receipt(self):
        self.client.private_peer = "wxid_peer"
        self.result["report"]["target"] = "wxid_peer"
        result = self.client.send_voice("wxid_peer", self.path, 1000)
        self.assertEqual(result["state"], "NATIVE_COMPLETED")
        self.assertEqual(self.run.call_args.kwargs["target"], "wxid_peer")
        with self.assertRaises(TransportError):self.client.send_voice("wxid_other", self.path, 1000)

    def test_invalid_duration_and_target_have_no_native_side_effect(self):
        for duration in (True, 0, 1001, 60020):
            with self.assertRaises(TransportError):
                self.client.send_voice("1234@chatroom", self.path, duration)
        with self.assertRaises(TransportError):
            self.client.send_voice("someone", self.path, 1000)
        self.run.assert_not_called()

    def test_wrong_receipt_preserves_unknown_execution(self):
        for key, value in (("target", "other@chatroom"), ("sent", False),
                           ("message_type", 1), ("duration_ms", 1020),
                           ("voice_path", "other.silk"), ("operation", "voice_probe")):
            old = self.result["report"][key]
            self.result["report"][key] = value
            self.client.pending = False
            with self.assertRaisesRegex(TransportError, "completion_unknown"):
                self.client.send_voice("1234@chatroom", self.path, 1000)
            self.assertTrue(self.client.pending)
            self.result["report"][key] = old

    def test_exception_does_not_unload_or_retry(self):
        self.run.side_effect = TimeoutError("private details")
        with self.assertRaisesRegex(TransportError, "execution_unknown"):
            self.client.send_voice("1234@chatroom", self.path, 1000)
        self.assertEqual(self.client.close()["state"], "UNKNOWN")
        self.assertEqual(self.run.call_count, 1)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "run"
        self.directory.mkdir()
        self.state = Path(self.temp.name) / "state"
        self.state.mkdir()
        self.settings = SimpleNamespace(enabled=True, allowed_room="1234@chatroom")
        self.voice = SimpleNamespace(path=self.directory / "voice.silk", duration_ms=1000)
        self.active = True
        self.calls = []
        self.unknown = False
        self.stop_failure = False
        self.request_later_stop = False
        self.transport = SimpleNamespace(loaded=False, pending=False, preflight=Mock())
        self.transport.start = Mock(side_effect=lambda: setattr(self.transport, "loaded", True))
        def send_voice(*args):
            self.assertTrue((self.directory / "send-intent.json").exists())
            self.assertFalse(self.active)
            if self.request_later_stop:
                (self.state / "bridge.stop").write_text("later stop")
            if self.unknown:
                self.transport.pending = True
                raise TransportError("native_voice_execution_unknown")
            return {"state": "NATIVE_COMPLETED", "recipient_delivery_confirmed": False}
        self.transport.send_voice = Mock(side_effect=send_voice)
        self.transport.close = Mock(side_effect=lambda: {"state": "UNKNOWN" if self.transport.pending else "CLEAN"})

    def controller(self, action):
        self.calls.append(action)
        self.active = action == "start" or self.stop_failure
        return {"stage": "running" if self.active else "stopped", "connected": self.active,
                "process_lock_held": self.active, "cleanup_required": self.stop_failure}

    def deliver(self):
        return deliver_prepared(self.directory, self.voice, self.settings,
                                state_directory=self.state, controller=self.controller,
                                is_running=lambda: self.active, read_status=lambda: {},
                                transport_factory=lambda _: self.transport)

    def test_success_restores_original_listener_and_does_not_resend(self):
        result = self.deliver()
        self.assertEqual(result["state"], "NATIVE_COMPLETED")
        self.assertTrue(result["listener_restored"])
        self.assertEqual(self.calls, ["stop", "start"])
        with self.assertRaisesRegex(RuntimeError, "no_resend"):
            self.deliver()
        self.assertEqual(self.transport.send_voice.call_count, 1)
        self.assertEqual(self.calls, ["stop", "start"])

    def test_unknown_does_not_restart_and_sets_recovery_flag(self):
        self.unknown = True
        result = self.deliver()
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(self.calls, ["stop"])
        value = json.loads((self.state / "bridge-status.json").read_text())
        self.assertTrue(value["manual_recovery_required"])
        with self.assertRaisesRegex(RuntimeError, "no_resend"):
            self.deliver()

    def test_stop_failure_has_no_native_side_effect(self):
        self.stop_failure = True
        self.assertEqual(self.deliver()["state"], "FAILED_BEFORE_DISPATCH")
        self.transport.start.assert_not_called()
        self.transport.send_voice.assert_not_called()

    def test_originally_stopped_listener_is_not_started(self):
        self.active = False
        self.assertEqual(self.deliver()["state"], "NATIVE_COMPLETED")
        self.assertEqual(self.calls, [])

    def test_later_stop_request_is_not_undone(self):
        self.request_later_stop = True
        result = self.deliver()
        self.assertFalse(result["listener_restored"])
        self.assertEqual(self.calls, ["stop"])


if __name__ == "__main__":
    unittest.main()
