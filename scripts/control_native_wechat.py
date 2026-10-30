"""User-facing local lifecycle controls; no credentials or conversation output."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.wechat_native_bridge import DATA, single_bridge
from scripts.wechat_native_bridge_core import BridgeError


def status():
    try:
        value = json.loads((DATA / "bridge-status.json").read_text("utf-8"))
        return {key: value.get(key) for key in (
            "stage", "reason", "connected", "updated_at", "cleanup_required",
            "manual_recovery_required", "native_pending", "counts", "ack_results", "last_ack", "last_event", "voice")}
    except (OSError, ValueError):
        return {"stage": "not_started"}


def running():
    try:
        with single_bridge(DATA / "bridge.lock"):
            return False
    except BridgeError:
        return True


def control(action):
    DATA.mkdir(parents=True, exist_ok=True)
    if action == "status":
        return {"process_lock_held": running(), **status()}
    if action == "stop":
        (DATA / "bridge.stop").write_text("stop requested\n", "utf-8")
        until = time.monotonic() + 45
        while running() and time.monotonic() < until:
            time.sleep(.5)
        return {"stop_requested": True, "process_lock_held": running(), **status()}
    if running():
        return {"already_running": True, **status()}
    current = status()
    if current.get("cleanup_required") or current.get("manual_recovery_required"):
        raise BridgeError("manual_recovery_required_before_start")
    config = ROOT / "config/wechat-native-bridge.local.json"
    if not config.is_file():
        raise BridgeError("bridge_configuration_missing")
    sentinel = DATA / "bridge.stop"
    if sentinel.exists():
        sentinel.unlink()
    python = ROOT / ".runtime/wechat-native/.venv/Scripts/python.exe"
    command = [str(python), "-X", "utf8", "-u", str(ROOT / "scripts/wechat_native_bridge.py"),
               "--mode", "run"]
    started_at = time.time()
    with (DATA / "bridge-process.log").open("ab") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    until = time.monotonic() + 35
    while process.poll() is None and time.monotonic() < until:
        value = status()
        if (running() and value.get("stage") == "running"
                and isinstance(value.get("updated_at"), (int, float))
                and value["updated_at"] >= started_at):
            return {"started": True, **value}
        time.sleep(.5)
    return {"started": process.poll() is None, "exit_code": process.poll(), **status()}


def main():
    parser = argparse.ArgumentParser(description="真实微信群本机桥接启动、停止与状态")
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    try:
        result = control(args.action)
    except BridgeError as exc:
        result = {"stage": "failed", "reason": str(exc)}
    except Exception:
        result = {"stage": "failed", "reason": "local_control_failed"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("stage") in {"failed", "unknown"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
