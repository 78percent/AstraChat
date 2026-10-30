"""Explicit local synthesis / one-shot bound-group voice experiment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.indextts_worker import wav_info
from scripts.wechat_native_bridge import (
    DATA, UPSTREAM, atomic_status, default_transport, load_settings, single_bridge,
)
from scripts.control_native_wechat import control, running, status
from scripts.wechat_native_transport import verify_upstream, _check_module_cache

RUNS = ROOT / "runtime/indextts/runs"


def synthesize(config_path):
    config = json.loads(Path(config_path).read_text("utf-8-sig"))
    root = Path(config["model_root"]).resolve(strict=True)
    reference = Path(config["reference_audio"])
    reference = (reference if reference.is_absolute() else root / reference).resolve(strict=True)
    python = root / "env/python.exe"
    if not reference.is_file() or not python.is_file() or not (root / "ffmpeg.exe").is_file():
        raise ValueError("synthesis_files_missing")
    text = config["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 200 or "\0" in text:
        raise ValueError("invalid_synthesis_text")
    timeout = config.get("timeout_seconds", 900)
    if type(timeout) is not int or not 30 <= timeout <= 1800:
        raise ValueError("invalid_synthesis_timeout")
    run_id = uuid.uuid4().hex
    directory = RUNS / run_id
    directory.mkdir(parents=True)
    atomic_status(directory / "request.json", {"model_root": str(root),
                  "reference_audio": str(reference), "text": text})
    print(json.dumps({"run_id": run_id, "state": "synthesizing"}), flush=True)
    with (directory / "worker.log").open("wb") as log:
        try:
            process = subprocess.run([str(python), "-B", "-X", "utf8", "-u",
                                      str(ROOT / "scripts/indextts_worker.py"),
                                      str(directory / "request.json")],
                                     cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     timeout=timeout,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            atomic_status(directory / "synthesis.json", {"state": "failed", "reason": "timeout"})
            raise RuntimeError("synthesis_timeout") from None
    if process.returncode != 0:
        raise RuntimeError("synthesis_failed_see_worker_log")
    result = json.loads((directory / "synthesis.json").read_text("utf-8"))
    if result.get("state") != "synthesized" or result.get("audio") != wav_info(directory / "wechat.wav"):
        raise RuntimeError("synthesis_output_mismatch")
    return {"run_id": run_id, **result}


def run_directory(run_id):
    if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
        raise ValueError("invalid_run_id")
    directory = (RUNS / run_id).resolve(strict=True)
    if directory.parent != RUNS.resolve():
        raise ValueError("run_directory_outside_root")
    return directory


def deliver_prepared(directory, voice, settings, *, state_directory=DATA,
                     controller=control, is_running=running, read_status=status,
                     transport_factory=default_transport):
    """Native-only phase. Model/format failures never reach this function."""
    state_directory = Path(state_directory)
    receipt_path = directory / "delivery.json"
    claim_path = directory / "send-intent.json"
    if not settings.enabled:
        raise RuntimeError("bridge_disabled")
    with single_bridge(state_directory / "voice-test.lock"):
        if claim_path.exists():
            raise RuntimeError("run_already_claimed_no_resend")
        previous_running = is_running()
        previous = read_status()
        if (previous.get("manual_recovery_required") or previous.get("native_pending")
                or (not previous_running and previous.get("cleanup_required"))):
            raise RuntimeError("native_recovery_required")
        # Exclusive create + fsync remains authoritative even if the process dies.
        with claim_path.open("x", encoding="utf-8") as stream:
            json.dump({"created_at": time.time(), "duration_ms": voice.duration_ms,
                       "previous_listener_running": previous_running}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        receipt = {"state": "PREPARING", "recipient_delivery_confirmed": False,
                   "previous_listener_running": previous_running}
        atomic_status(receipt_path, receipt)
        if previous_running:
            stopped = controller("stop")
            if (stopped.get("process_lock_held") or stopped.get("cleanup_required")
                    or stopped.get("manual_recovery_required") or stopped.get("native_pending")):
                receipt.update(state="FAILED_BEFORE_DISPATCH", reason="listener_stop_unconfirmed")
                atomic_status(receipt_path, receipt)
                return receipt
        # A later GUI stop request must not be undone by our restoration.
        stop_file = state_directory / "bridge.stop"
        stop_stamp = stop_file.stat().st_mtime_ns if stop_file.exists() else None
        cleanup = None
        transport = None
        try:
            with single_bridge(state_directory / "bridge.lock"):
                transport = transport_factory(settings)
                transport.preflight()
                atomic_status(state_directory / "bridge-status.json", {
                    "stage": "voice_testing", "reason": "voice_experiment",
                    "connected": False, "updated_at": time.time(),
                    "cleanup_required": True, "manual_recovery_required": False,
                    "native_pending": False})
                try:
                    transport.start()
                    receipt.update(state="IN_FLIGHT", dispatched_at=time.time())
                    atomic_status(receipt_path, receipt)
                    started = time.perf_counter()
                    result = transport.send_voice(settings.allowed_room, voice.path, voice.duration_ms)
                    receipt.update(result, send_seconds=round(time.perf_counter() - started, 3))
                except BaseException as error:
                    receipt.update(state="UNKNOWN" if transport.pending or receipt["state"] == "IN_FLIGHT"
                                   else "FAILED_BEFORE_DISPATCH", error_type=type(error).__name__)
                finally:
                    cleanup = transport.close()
                    receipt["cleanup"] = cleanup
                    clean = cleanup.get("state") == "CLEAN"
                    atomic_status(state_directory / "bridge-status.json", {
                        "stage": "stopped" if clean else "unknown",
                        "reason": "voice_experiment_finished" if clean else "voice_cleanup_unknown",
                        "connected": False, "updated_at": time.time(),
                        "cleanup_required": not clean, "manual_recovery_required": not clean,
                        "native_pending": bool(transport.pending)})
                    atomic_status(receipt_path, receipt)
        except Exception as error:
            receipt.update(state="FAILED_BEFORE_DISPATCH" if receipt["state"] == "PREPARING"
                           else receipt["state"], error_type=type(error).__name__)
            # Preflight has no native side effect; an occupied lock is never taken over.
            if transport is not None and not transport.loaded and not transport.pending:
                cleanup = {"state": "CLEAN"}
        now_stamp = stop_file.stat().st_mtime_ns if stop_file.exists() else None
        if (previous_running and cleanup and cleanup.get("state") == "CLEAN"
                and now_stamp == stop_stamp):
            restored = controller("start")
            receipt["listener_restored"] = bool(
                restored.get("connected") and restored.get("stage") == "running" and is_running())
        else:
            receipt["listener_restored"] = False
        atomic_status(receipt_path, receipt)
        return receipt


def send(run_id):
    directory = run_directory(run_id)
    synthesis = json.loads((directory / "synthesis.json").read_text("utf-8"))
    if synthesis.get("state") != "synthesized" or synthesis.get("audio") != wav_info(directory / "wechat.wav"):
        raise RuntimeError("audio_not_verified")
    settings = load_settings()
    verify_upstream(UPSTREAM)
    _check_module_cache(UPSTREAM)
    sys.path.insert(0, str(UPSTREAM))
    from wxbot_framework.voices import prepare_voice
    # Finish encoding before stopping the listener. Snapshot stays alive through cleanup.
    with prepare_voice(directory / "wechat.wav", directory / "encoded") as voice:
        return {"run_id": run_id, **deliver_prepared(directory, voice, settings)}


def main():
    parser = argparse.ArgumentParser(description="本地 IndexTTS 独立微信语音测试")
    parser.add_argument("action", choices=("synthesize", "send", "run"))
    parser.add_argument("--config", type=Path, default=ROOT / "config/indextts.local.json")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        if args.action == "send":
            result = send(args.run_id)
        else:
            result = synthesize(args.config)
            if args.action == "run":
                result = send(result["run_id"])
    except Exception as error:
        print(json.dumps({"state": "failed", "error_type": type(error).__name__,
                          "reason": str(error) if isinstance(error, RuntimeError) else "invalid_configuration"}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("state") in ("synthesized", "NATIVE_COMPLETED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
