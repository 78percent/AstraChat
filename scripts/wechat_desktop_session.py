"""Detached lifetime owner for the desktop WeChat control window.

The monitor only requests cancellation. Startup and shutdown always run in
sequence on the main thread, including when Windows closes the console early.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import start_wechat_desktop as startup

DATA = startup.DATA


class SessionError(RuntimeError):
    """Static diagnostic code; never contains tool output or private input."""


def valid_session_id(value):
    try:
        return isinstance(value, str) and uuid.UUID(value).hex == value and int(value, 16) != 0
    except (ValueError, AttributeError, TypeError):
        return False


def stop_path(session_id):
    if not valid_session_id(session_id):
        raise SessionError("invalid_session_id")
    return DATA / ("desktop-stop-" + session_id)


def result_path(session_id):
    if not valid_session_id(session_id):
        raise SessionError("invalid_session_id")
    return DATA / ("desktop-session-" + session_id + ".result.json")


def reconnect_path(session_id):
    if not valid_session_id(session_id):
        raise SessionError("invalid_session_id")
    return DATA / ("desktop-reconnect-" + session_id)


def write_result(session_id, stage, reason, **fields):
    destination = result_path(session_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    value = {"session_id": session_id, "stage": stage, "reason": reason,
             "updated_at": time.time(), **fields}
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return value


class WindowsOwner:
    """A held process handle cannot accidentally follow a recycled Windows PID."""
    def __init__(self, pid, *, kernel=None):
        if type(pid) is not int or pid <= 0:
            raise SessionError("invalid_owner_pid")
        self.kernel = kernel if kernel is not None else ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only.
        if not self.handle:
            raise SessionError("owner_unavailable")

    def alive(self):
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result == 258:  # WAIT_TIMEOUT
            return True
        if result == 0:  # WAIT_OBJECT_0
            return False
        raise SessionError("owner_probe_failed")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def monitor_owner(owner, request, cancelled, finished):
    while not finished.is_set():
        try:
            closing = request.exists() or not owner.alive()
        except Exception:
            closing = True
        if closing:
            cancelled.set()
            return
        finished.wait(.2)


def clean_native_state(state):
    return (isinstance(state, dict)
            and state.get("process_lock_held") is False
            and state.get("connected") is False
            and state.get("cleanup_required") is False
            and state.get("manual_recovery_required") is False
            and state.get("native_pending") is False)


def stop_native():
    state = startup.native_control("stop")
    if not clean_native_state(state) and state.get("process_lock_held") is False:
        # A leftover status marker is cleared only after the existing transport
        # verifies the bound account and the absence of every native module.
        if state.get("cleanup_required") is True or state.get("manual_recovery_required") is True:
            probe = startup.native_check()
            if probe.get("ready") is True and probe.get("native_module_loaded") is False:
                startup.native_recover()
                state = startup.native_control("status")
    if not clean_native_state(state):
        raise SessionError("native_cleanup_unconfirmed")
    # Re-read after stop returns: require both released lock and durable clean
    # status before removing the backend the bridge may still need.
    if not clean_native_state(startup.native_control("status")):
        raise SessionError("native_cleanup_unconfirmed")


def docker_executable():
    command = shutil.which("docker.exe")
    if command:
        return command
    candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Docker/Docker/resources/bin/docker.exe"
    if candidate.is_file():
        return str(candidate)
    raise SessionError("docker_missing_during_stop")


def desktop_state(docker):
    # Once Desktop is gone, its status CLI may wait indefinitely instead of
    # returning "stopped". Use this only while its processes are still present.
    try:
        probe = startup.command([docker, "desktop", "status"], timeout=3)
    except startup.StartupError as error:
        if str(error) == "command_timeout":
            return "unknown"
        raise
    match = re.search(r"^\s*Status\s+(running|stopped)\s*$", probe.stdout,
                      re.MULTILINE | re.IGNORECASE)
    return match.group(1).casefold() if probe.returncode == 0 and match else "unknown"


def desktop_stopped(docker):
    return desktop_state(docker) == "stopped"


def desktop_processes_running():
    """Read process image names only; never kill a Desktop or backend process."""
    result = startup.command(["tasklist.exe", "/FO", "CSV", "/NH"], timeout=12)
    if result.returncode:
        raise SessionError("docker_process_query_failed")
    names = []
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) >= 2 and row[1].strip().isdigit():
            names.append(row[0].strip().casefold())
    if not names:
        raise SessionError("docker_process_query_failed")
    return any(name in {"docker desktop.exe", "com.docker.backend.exe"} for name in names)


def linux_engine_pipe_present():
    """Independent local engine evidence; an HTTP/CLI failure is not absence."""
    try:
        names = os.listdir("\\\\.\\pipe\\")
    except OSError:
        raise SessionError("docker_pipe_query_failed") from None
    return any(name.casefold() == "dockerdesktoplinuxengine" for name in names)


def desktop_absent():
    return not desktop_processes_running() and not linux_engine_pipe_present()


def normal_desktop_exit(docker, timeout=90):
    """Verify stable shutdown even if an earlier Desktop launch arrives late."""
    started = time.monotonic()
    deadline = started + timeout
    stop_error = None
    attempts = 0
    last_stop_at = started
    settle = 2 if getattr(startup, "SESSION_FIELDS", {}).get("docker_launch_requested") is True else .5

    def request_stop():
        nonlocal stop_error, attempts, last_stop_at
        attempts += 1
        last_stop_at = time.monotonic()
        try:
            # This is the same normal exit request, without the CLI's slow
            # synchronous waiter. We verify the actual OS state ourselves.
            result = startup.command([docker, "desktop", "stop", "--detach"], timeout=12)
            if result.returncode:
                stop_error = "docker_desktop_stop_failed"
        except startup.StartupError:
            stop_error = "docker_desktop_stop_timeout"

    request_stop()
    clean_samples = 0
    previous_state = None
    while time.monotonic() < deadline:
        processes_running = desktop_processes_running()
        engine_present = linux_engine_pipe_present()
        if not processes_running and not engine_present:
            clean_samples += 1
            if clean_samples >= 2 and time.monotonic() - started >= settle:
                return
        else:
            clean_samples = 0
        # A delayed boot can overtake the first stop request. Retry the normal
        # API when it actually becomes running, rather than treating a transient
        # "stopped" response during process creation as a completed shutdown.
        # Never call the known-slow status CLI once the Desktop processes have
        # disappeared. Two OS-level process/pipe samples are independent evidence.
        state = desktop_state(docker) if processes_running else "absent"
        if (attempts < 3 and state == "running"
                and (previous_state != "running" or time.monotonic() - last_stop_at >= 10)):
            request_stop()
        previous_state = state
        time.sleep(.5)
    raise SessionError(stop_error or "docker_desktop_stop_unconfirmed")


def stop_project_containers(docker):
    definitions = [([], ["astrbot", "napcat"], "astrbot-roleplay")]
    if (ROOT / "compose.snowluma.yaml").is_file():
        definitions.append((["-f", "compose.snowluma.yaml"], ["snowluma"], "astrbot-roleplay-snowluma"))
    for prefix, services, project in definitions:
        result = startup.command([docker, "compose", *prefix, "stop", "--timeout", "20", *services], timeout=55)
        if result.returncode:
            raise SessionError("project_container_stop_failed")
        running = startup.command([docker, "ps", "--filter", "label=com.docker.compose.project=" + project,
                                   "--format", "{{.Names}}"], timeout=12)
        if running.returncode or running.stdout.strip():
            raise SessionError("project_container_stop_unconfirmed")


def shutdown_services(docker=None):
    """Normal unload first, then project containers, then the explicitly requested Desktop exit."""
    startup.record("stopping", "wechat_unload", message="正在停止微信监听……",
                   connected=None, process_lock_held=None, heartbeat_updated_at=None)
    stop_native()
    docker = docker or docker_executable()
    launch_pending = getattr(startup, "SESSION_FIELDS", {}).get("docker_launch_requested") is True
    if not launch_pending and desktop_absent():
        time.sleep(.5)
        if desktop_absent():
            return
    try:
        engine = startup.command([docker, "info", "--format", "{{.OSType}}"], timeout=12)
    except startup.StartupError as error:
        if str(error) != "command_timeout":
            raise
        # Docker may still be cold-starting (or its engine may be stuck). A
        # timed-out read-only probe must not prevent the Desktop's normal exit.
        engine = None
    if engine is not None and engine.returncode == 0:
        if engine.stdout.strip() != "linux":
            raise SessionError("docker_wrong_engine_during_stop")
        startup.record("stopping", "project_containers", message="正在停止机器人服务……")
        stop_project_containers(docker)
    # During an early-window-close the Desktop may still be booting, so its
    # engine API can be absent. Its own normal stop command cancels that boot.
    startup.record("stopping", "docker_desktop", message="正在退出 Docker Desktop……")
    normal_desktop_exit(docker)


def stopped_fields():
    return {"process_lock_held": False, "connected": False,
            "heartbeat_updated_at": None, "dashboard_ready": False}


def failed_stop_fields():
    """Do not carry a ready snapshot's online flags into a stop failure."""
    fields = {"process_lock_held": None, "connected": None,
              "heartbeat_updated_at": None, "dashboard_ready": None}
    try:
        state = startup.native_control("status")
        held = state.get("process_lock_held")
        fields["process_lock_held"] = held if type(held) is bool else None
        fields["connected"] = (state.get("connected") is True) if held is True else (False if held is False else None)
        if held is True:
            fields["heartbeat_updated_at"] = state.get("updated_at")
    except Exception:
        pass
    return fields


def notify_stop_failed():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
    user32.MessageBoxW.restype = ctypes.c_int
    user32.MessageBoxW(None, "机器人服务尚未确认完全停止，已保留未完成的服务。\n请重新打开启动脚本查看状态。",
                      "三叶微信停止未完成", 0x10)


def error_code(error, fallback):
    if isinstance(error, (SessionError, startup.StartupError, startup.BridgeError)):
        return startup.safe_reason({"reason": str(error)}, fallback)
    return fallback


def reconnect_native():
    """Explicit UI request only; keep all native mutation on the session thread."""
    startup.check_cancel()
    startup.record("reconnecting", "wechat_reconnecting", connected=False,
                   heartbeat_updated_at=None, message="正在重新连接微信监听……")
    stop_native()
    startup.check_cancel()
    docker = startup.validate_local()
    container_started = startup.start_astrbot(docker)
    startup.wait_backend(container_started)
    startup.check_cancel()
    # ensure_native performs full source/account/module verification and only
    # recovers a leftover marker after it confirms the native module is absent.
    first = startup.ensure_native()
    final = startup.wait_heartbeat(first)
    startup.record("ready", "wechat_connected", process_lock_held=True, connected=True,
                   heartbeat_updated_at=final["updated_at"],
                   message="微信监听已连接。")


def watch_session(session_id, cancelled):
    """Report live health; never silently reinject after a transport failure."""
    reconnect = reconnect_path(session_id)
    reconnect_error = None
    while not cancelled.is_set():
        if reconnect.exists():
            reconnect.unlink(missing_ok=True)
            startup.check_cancel()
            try:
                reconnect_native()
                reconnect_error = None
            except Exception as error:
                if cancelled.is_set():
                    return
                reason = error_code(error, "reconnect_failed")
                reconnect_error = reason
                actual = failed_stop_fields()
                actual.pop("dashboard_ready", None)
                startup.record("degraded", reason, **actual,
                               message="微信监听未连接，请查看状态后重新连接。")
        if cancelled.is_set():
            return
        try:
            state = startup.native_control("status")
            healthy = startup.native_ready(state)
            held = state.get("process_lock_held")
            fields = {"process_lock_held": held if type(held) is bool else None,
                      "connected": healthy,
                      "voice": state.get("voice", {}),
                      "heartbeat_updated_at": state.get("updated_at") if held is True else None}
            if healthy:
                reconnect_error = None
                startup.record("ready", "wechat_connected", **fields,
                               message="微信监听已连接。")
            else:
                reason = reconnect_error or ("bridge_heartbeat_stalled" if held is True and state.get("connected") is True else "bridge_disconnected")
                startup.record("degraded", reason, **fields,
                               message="微信监听已断开，点击“重新连接”恢复。")
        except Exception as error:
            if cancelled.is_set():
                return
            startup.record("degraded", error_code(error, "bridge_status_failed"),
                           process_lock_held=None, connected=None, heartbeat_updated_at=None,
                           message="无法确认微信监听状态，点击“重新连接”检查。")
        cancelled.wait(1)


def run_session(owner_pid, session_id, *, owner_factory=None, stop_only=False):
    request = stop_path(session_id)
    fields = {"owner_pid": owner_pid, "supervisor_pid": os.getpid()}
    owner_factory = owner_factory or WindowsOwner
    # The session lock, not the console's lifetime, serializes both start and
    # cleanup. A second console cannot become owner while this one is stopping.
    try:
        lease = startup.single_bridge(DATA / "desktop-start.lock")
        lease.__enter__()
    except startup.BridgeError:
        write_result(session_id, "busy", "session_already_running", **fields)
        return 3
    cancelled, finished = threading.Event(), threading.Event()
    monitor = None
    old_cancel = getattr(startup, "CANCEL_EVENT", None)
    old_fields = getattr(startup, "SESSION_FIELDS", {})
    startup.CANCEL_EVENT = cancelled
    startup.SESSION_FIELDS = {"session_id": session_id, **fields}
    startup_error = None
    try:
        with owner_factory(owner_pid) as owner:
            monitor = threading.Thread(target=monitor_owner, args=(owner, request, cancelled, finished),
                                       name="wechat-desktop-owner", daemon=True)
            monitor.start()
            try:
                if request.exists() or not owner.alive():
                    cancelled.set()
                    raise startup.StartupError("startup_cancelled")
                if not stop_only:
                    startup.start()
                    # Keep readiness tied to a live lock and advancing heartbeat.
                    watch_session(session_id, cancelled)
            except Exception as error:
                startup_error = error_code(error, "unexpected_startup_error")
            finally:
                # No stop runs until start() and all its mutating calls return.
                startup.CANCEL_EVENT = None
                stop_started = time.monotonic()
                try:
                    shutdown_services()
                except Exception as error:
                    reason = error_code(error, "unexpected_stop_error")
                    actual = failed_stop_fields()
                    startup.record("stop_failed", reason, startup_error=startup_error,
                                   message="停止未完成，请重新打开启动脚本查看状态。", **actual)
                    write_result(session_id, "stop_failed", reason, startup_error=startup_error,
                                 shutdown_seconds=round(time.monotonic() - stop_started, 3), **fields, **actual)
                    try:
                        owner_alive = owner.alive()
                    except Exception:
                        owner_alive = False
                    if not owner_alive:
                        notify_stop_failed()
                    return 1
                duration = round(time.monotonic() - stop_started, 3)
                startup.record("stopped", "all_services_stopped", startup_error=startup_error,
                               shutdown_seconds=duration, message="微信监听、机器人服务和 Docker Desktop 已停止。",
                               **stopped_fields())
                write_result(session_id, "stopped", "all_services_stopped", startup_error=startup_error,
                             shutdown_seconds=duration, **fields, **stopped_fields())
                return 0 if startup_error in {None, "startup_cancelled"} else 1
    except Exception as error:
        # Owner handle creation can fail before any startup side effect. Avoid
        # stopping someone else's already-running deployment in that case.
        reason = error_code(error, "session_owner_failed")
        write_result(session_id, "failed", reason, **fields)
        return 1
    finally:
        finished.set()
        if monitor is not None:
            monitor.join(timeout=1)
        startup.CANCEL_EVENT = old_cancel
        startup.SESSION_FIELDS = old_fields
        lease.__exit__(None, None, None)


def main(argv=None):
    parser = argparse.ArgumentParser(description="三叶微信桌面窗口的后台生命周期管理")
    parser.add_argument("--owner-pid", type=int, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--stop-only", action="store_true", help="仅重试正常关闭所有服务")
    args = parser.parse_args(argv)
    if args.owner_pid <= 0 or not valid_session_id(args.session_id):
        parser.error("invalid owner or session")
    # pythonw normally receives redirected handles from spawn_session. Also
    # support a direct hidden launch, where Python sets the standard streams
    # to None; startup.emit() and unexpected exceptions must remain observable.
    if sys.stdout is None or sys.stderr is None:
        DATA.mkdir(parents=True, exist_ok=True)
        log = (DATA / ("desktop-supervisor-" + args.session_id + ".log")).open(
            "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
    return run_session(args.owner_pid, args.session_id, stop_only=args.stop_only)


if __name__ == "__main__":
    raise SystemExit(main())
