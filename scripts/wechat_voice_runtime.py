"""Owned local speech processes and bounded audio jobs; no native operations."""
from __future__ import annotations

from concurrent.futures import Future
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/wechat-voice.local.json"
DATA = ROOT / "runtime/wechat-native/voice"
JOB_ID = re.compile(r"[a-f0-9]{32}")


class SpeechError(RuntimeError):
    """Static diagnostics only; no request bodies or native account identifiers."""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_settings(path=CONFIG):
    path = Path(path)
    if not path.exists():
        return {"enabled": False}
    if path.stat().st_size > 8192:
        raise SpeechError("voice_config_too_large")
    settings = json.loads(path.read_text("utf-8-sig"))
    expected = {"enabled", "tts_model_root", "reference_audio", "asr_python", "asr_model_root"}
    if not isinstance(settings, dict) or set(settings) != expected or type(settings["enabled"]) is not bool:
        raise SpeechError("invalid_voice_config")
    for field in expected - {"enabled"}:
        value = settings[field]
        if not isinstance(value, str) or not Path(value).is_absolute() or "\0" in value:
            raise SpeechError("invalid_voice_path")
    return settings


class SpeechProcess:
    def __init__(self, kind, executable, config, jobs, *, popen=subprocess.Popen, capacity=4):
        self.kind, self.jobs, self.capacity = kind, Path(jobs).resolve(), capacity
        self.state = "starting"
        self.reason = ""
        self._lock = threading.Lock()
        self._pending = {}
        self._closed = False
        self._ready = threading.Event()
        command = [str(executable), "-s", "-B", "-X", "utf8", "-u",
                   str(ROOT / "scripts/wechat_speech_worker.py"), kind,
                   "--config", str(Path(config).resolve()), "--jobs", str(self.jobs),
                   "--owner", str(os.getpid())]
        self.process = popen(command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._reader = threading.Thread(target=self._read, daemon=True, name="speech-" + kind)
        self._reader.start()

    def _fail(self, reason):
        with self._lock:
            if not self._closed:
                self.state, self.reason = "failed", reason
            pending = tuple(self._pending.values())
            self._pending.clear()
            self._ready.set()
        for future in pending:
            if not future.done():
                future.set_exception(SpeechError(reason))

    def _read(self):
        try:
            for line in iter(lambda: self.process.stdout.readline(16385), ""):
                if len(line.encode("utf-8")) > 16384 or not line.endswith("\n"):
                    raise SpeechError("speech_protocol_invalid")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise SpeechError("speech_protocol_invalid")
                if message.get("type") == "ready" and message.get("kind") == self.kind:
                    with self._lock:
                        if self.state != "starting":
                            raise SpeechError("speech_protocol_invalid")
                        self.state = "ready"
                        self._ready.set()
                    continue
                if message.get("type") == "failed":
                    raise SpeechError("speech_model_load_failed")
                if (message.get("type") != "result" or type(message.get("ok")) is not bool
                        or not isinstance(message.get("id"), str)):
                    raise SpeechError("speech_protocol_invalid")
                with self._lock:
                    future = self._pending.pop(message["id"], None)
                if future is None:
                    raise SpeechError("speech_result_unmatched")
                if not future.done():
                    if message["ok"] and isinstance(message.get("result"), dict):
                        future.set_result(message)
                    else:
                        future.set_exception(SpeechError("speech_job_failed"))
            self._fail("speech_process_exited")
        except Exception:
            self._fail("speech_process_failed")

    def wait_ready(self, timeout=300):
        self._ready.wait(timeout)
        return self.state == "ready"

    def submit(self, job, **fields):
        if not isinstance(job, str) or JOB_ID.fullmatch(job) is None:
            raise SpeechError("invalid_speech_job")
        payload = json.dumps({"id": job, **fields}, ensure_ascii=True) + "\n"
        if len(payload.encode("utf-8")) > 16384:
            raise SpeechError("speech_request_too_large")
        with self._lock:
            if self._closed or self.state != "ready" or self.process.poll() is not None:
                raise SpeechError("speech_not_ready")
            if len(self._pending) >= self.capacity or job in self._pending:
                raise SpeechError("speech_queue_full")
            future = Future()
            self._pending[job] = future
            try:
                self.process.stdin.write(payload)
                self.process.stdin.flush()
            except Exception:
                self._pending.pop(job, None)
                raise SpeechError("speech_submit_failed") from None
            return future

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.state = "closed"
            try:
                self.process.stdin.write('{"op":"close"}\n')
                self.process.stdin.flush()
            except (OSError, ValueError):
                pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()  # This owned process only performs model computation.
            self.process.wait(timeout=10)
        self._fail("speech_process_closed")
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        self._reader.join(timeout=1)


class VoiceRuntime:
    def __init__(self, config=CONFIG, *, directory=DATA, process_factory=SpeechProcess):
        self.config = Path(config).resolve()
        self.settings = read_settings(config)
        self.asr = self.tts = None
        self.jobs = {}
        self._closed = False
        self.directory = Path(directory).resolve() / uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=False)
        if self.settings.get("enabled") is not True:
            return
        try:
            self.asr = process_factory("asr", self.settings["asr_python"], config, self.directory)
            self.tts = process_factory("tts", Path(self.settings["tts_model_root"]) / "env/python.exe",
                                       config, self.directory)
        except Exception:
            self.close()
            raise

    def status(self):
        return {"enabled": self.settings.get("enabled") is True,
                "asr": self.asr.state if self.asr else "disabled",
                "tts": self.tts.state if self.tts else "disabled",
                "jobs": len(self.jobs)}

    def _new(self):
        if self._closed or len(self.jobs) >= 8:
            raise SpeechError("voice_runtime_busy")
        job = uuid.uuid4().hex
        path = self.directory / job
        path.mkdir()
        self.jobs[job] = {"path": path, "future": None, "cancelled": False, "kind": ""}
        return job, path

    def recognize(self, payload):
        if self.asr is None or self.asr.state != "ready":
            raise SpeechError("asr_not_ready")
        import pysilk
        job, path = self._new()
        try:
            pcm = io.BytesIO()
            pysilk.decode(io.BytesIO(payload), pcm, sample_rate=16000)
            raw = pcm.getvalue()
            if not raw or len(raw) % 2 or len(raw) > 16000 * 2 * 60:
                raise SpeechError("decoded_audio_invalid")
            with wave.open(str(path / "input.wav"), "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(raw)
            self.jobs[job].update(kind="asr", future=self.asr.submit(job))
            return job
        except Exception:
            self.cleanup(job)
            raise

    def synthesize(self, text):
        if self.tts is None or self.tts.state != "ready":
            raise SpeechError("tts_not_ready")
        job, path = self._new()
        try:
            revision = sha256(self.settings["reference_audio"])
            self.jobs[job].update(kind="tts", reference_sha256=revision,
                future=self.tts.submit(job, text=text, reference_sha256=revision))
            return job
        except Exception:
            self.cleanup(job)
            raise

    def result(self, job):
        state = self.jobs[job]
        future = state["future"]
        if future is None or not future.done():
            return None
        value = future.result()
        if state["cancelled"]:
            raise SpeechError("voice_job_cancelled")
        return value

    def encoded(self, job):
        """Validate the completed WAV and take one immutable local SILK snapshot."""
        import pysilk
        state = self.jobs[job]
        result = self.result(job)
        if result is None or state["kind"] != "tts":
            raise SpeechError("voice_not_prepared")
        revision = state["reference_sha256"]
        if (sha256(self.settings["reference_audio"]) != revision
                or result["result"].get("reference_sha256") != revision):
            raise SpeechError("voice_reference_changed")
        path = state["path"] / "speech.wav"
        from scripts.indextts_worker import wav_info
        info = wav_info(path)
        if info["sha256"] != result["result"].get("sha256"):
            raise SpeechError("voice_audio_changed")
        with wave.open(str(path), "rb") as stream:
            pcm = stream.readframes(stream.getnframes())
        pcm += b"\0" * (-len(pcm) % 960)
        target = state["path"] / "speech.silk"
        with target.open("xb") as output:
            pysilk.encode(io.BytesIO(pcm), output, 24000, 24000, tencent=True)
        return {"path": target, "duration_ms": len(pcm) // 960 * 20,
                "sha256": sha256(target), "reference_sha256": revision}

    def cancel(self, job):
        if job in self.jobs:
            self.jobs[job]["cancelled"] = True
            future = self.jobs[job]["future"]
            if future is None or future.done():
                self.cleanup(job)

    def reap(self):
        for job, state in tuple(self.jobs.items()):
            if state["cancelled"] and (state["future"] is None or state["future"].done()):
                self.cleanup(job)

    def cleanup(self, job):
        state = self.jobs.get(job)
        if state is None:
            return
        future = state["future"]
        if not self._closed and future is not None and not future.done():
            state["cancelled"] = True
            return
        path = state["path"]
        if (path.resolve() != path or path.parent != self.directory
                or not JOB_ID.fullmatch(path.name)):
            raise SpeechError("voice_cleanup_path_invalid")
        for name in ("input.wav", "generated.wav", "speech.wav", "speech.silk"):
            child = path / name
            if child.resolve() != child:
                raise SpeechError("voice_cleanup_path_alias")
            child.unlink(missing_ok=True)
        path.rmdir()
        self.jobs.pop(job)

    def close(self):
        if self._closed:
            return
        self._closed = True
        for process in (self.asr, self.tts):
            if process is not None:
                process.close()
        for job in tuple(self.jobs):
            self.cleanup(job)
        self.directory.rmdir()
