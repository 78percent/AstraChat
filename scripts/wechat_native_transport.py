"""Pinned native transport for one authorized experiment; imports are lazy.

This module never starts helper's bot/plugins or its automatic unload watchdog.
Raw messages and send results must stay in the caller's private, in-memory path.
"""
from __future__ import annotations

import ctypes
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


UPSTREAM_COMMIT = "a81c495770b397646ba0661118927c83589c41fc"
VOICE_COMMIT = "233c29ca061ac03e9ef4717ade8712dab941d1f7"
CORE_SIZE = 204305968
CORE_SHA256 = "e0428a7b691853de496532523c2c09aa095f90941c37c9f4cfb3f137f3c66556"
DLLS = {
    "native/build_text/text_bridge.dll": (102912, "ad614a0a833c0c08374141f078a3a8bcfd388f325ee024364b1c462b70824bfd"),
    "native/build/selftest_bridge.dll": (37888, "457e87f9b12ae0c18203de372de0ef870440d76d5007b005350064c59db0d387"),
}
BRIDGE_NAMES = frozenset({"text_bridge.dll", "selftest_bridge.dll"})
VOICE_DLLS = {**DLLS, "native/build_text/text_bridge.dll":
              (140288, "5d30b352e75fe259531487474956a41d01304698007d822ee82d855ec8bb628c")}


class TransportError(RuntimeError):
    """Static diagnostic code; never wrap native exception text or identities."""


def _digest(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stat(path: Path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_ino


def _git(root: Path, *args):
    try:
        result = subprocess.run(["git", "-C", str(root), *args], check=False,
                                capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        raise TransportError("upstream_git_unavailable") from None
    if result.returncode:
        raise TransportError("upstream_git_check_failed")
    return result.stdout


def verify_upstream(root: Path, commit=UPSTREAM_COMMIT):
    """Read files/Git only; do not import or execute upstream code."""
    root = Path(root).resolve(strict=True)
    if commit not in (UPSTREAM_COMMIT, VOICE_COMMIT):
        raise TransportError("unsupported_upstream_commit")
    # These four Git queries are independent and read-only. Join them in the
    # original validation order so simultaneous failures retain their existing
    # diagnostic priority. Native calls and every full fingerprint check remain
    # on their original path; no result is cached between runtime checks.
    queries = (
        ("rev-parse", "HEAD"),
        ("rev-parse", "--show-toplevel"),
        ("diff", "--name-only", "HEAD", "--"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    )
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="native-source-check") as pool:
        checks = [pool.submit(_git, root, *args) for args in queries]
        if checks[0].result().decode("ascii").strip() != commit:
            raise TransportError("upstream_commit_mismatch")
        if Path(checks[1].result().decode("utf-8").strip()).resolve() != root:
            raise TransportError("upstream_root_mismatch")
        # HEAD covers staged and unstaged tracked changes. Runtime caches are untracked.
        if checks[2].result():
            raise TransportError("upstream_tracked_changes")
        untracked = checks[3].result()
    for raw in untracked.split(b"\0"):
        if not raw:
            continue
        parts = Path(raw.decode("utf-8")).parts
        if parts and (parts[0] == ".runtime" or "__pycache__" in parts):
            continue
        raise TransportError("upstream_untracked_files")
    for relative, (size, digest) in (VOICE_DLLS if commit == VOICE_COMMIT else DLLS).items():
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or path.stat().st_size != size or _digest(path) != digest:
            raise TransportError("bridge_fingerprint_mismatch")
    return {"upstream_commit": commit, "source_clean": True,
            "bridge_fingerprints_match": True}


def _check_module_cache(root):
    for name, module in tuple(sys.modules.items()):
        if name in {"run_selftest", "filehelper_service"} or name == "wxbot_framework" or name.startswith("wxbot_framework."):
            source = getattr(module, "__file__", None)
            if not source or not Path(source).resolve().is_relative_to(root):
                raise TransportError("upstream_module_cache_conflict")


class NativeTransport:
    def __init__(self, upstream: Path, expected_self=None, *, commit=UPSTREAM_COMMIT, private_peer=None):
        self.upstream = Path(upstream).resolve()
        self.expected_self = expected_self
        if commit not in (UPSTREAM_COMMIT, VOICE_COMMIT):
            raise TransportError("unsupported_upstream_commit")
        from scripts.wechat_native_bridge_core import person
        if private_peer is not None and (commit != VOICE_COMMIT or not person(private_peer)
                                         or private_peer == expected_self):
            raise TransportError("invalid_private_binding")
        self.commit, self.private_peer = commit, private_peer
        self.pid = None
        self.self_id = None
        self.loaded = False  # Intent means a module may reside even if start raised.
        self.pending = False  # Unknown remote execution: never automatically unload.
        self.started = False
        self.channel = None
        self.guard = None
        self._api = None
        self._core_path = None
        self._core_base = None
        self._stats = {}
        self._used = False
        self._remove_attempted = False
        self._close_result = None

    def _load_api(self):
        _check_module_cache(self.upstream)
        if self._api is not None:
            return
        for path in (self.upstream, self.upstream / "native"):
            value = str(path)
            if value not in sys.path:
                sys.path.insert(0, value)
        try:
            native = importlib.import_module("run_selftest")
            receive = importlib.import_module("filehelper_service")
            account = importlib.import_module("wxbot_framework.account")
            lifetime = importlib.import_module("wxbot_framework.bridge_lifecycle")
        except Exception:
            raise TransportError("native_import_failed") from None
        _check_module_cache(self.upstream)
        if Path(native.TEXT_DLL).resolve() != (self.upstream / "native/build_text/text_bridge.dll").resolve():
            raise TransportError("native_module_path_mismatch")
        self._api = SimpleNamespace(native=native, receive=receive, account=account, lifetime=lifetime)

    def _read_account(self, core_base):
        native = self._api.native
        handle = native.K.OpenProcess(0x410, False, self.pid)
        if not handle:
            raise TransportError("account_read_open_failed")
        try:
            def read(address, size):
                if type(size) is not int or not 0 < size <= 1024 * 1024:
                    raise TransportError("account_read_size_invalid")
                buffer = ctypes.create_string_buffer(size)
                count = ctypes.c_size_t()
                okay = native.K.ReadProcessMemory(handle, address, buffer, size, ctypes.byref(count))
                if not okay or count.value != size:
                    raise TransportError("account_read_failed")
                return buffer.raw
            account = self._api.account.read_current_account(read, core_base)
            if not account:
                raise TransportError("account_not_ready")
            return self._api.account.validate_account(account)
        except TransportError:
            raise
        except Exception:
            raise TransportError("account_read_failed") from None
        finally:
            native.K.CloseHandle(handle)

    def _core(self, modules):
        value = modules.get("weixin.dll")
        if not value or len(value) < 2:
            raise TransportError("core_not_loaded")
        path = Path(value[1]).resolve(strict=True)
        return value[0], path

    def _verify_core(self, modules, *, full):
        base, path = self._core(modules)
        if self._core_path is not None and (path != self._core_path or base != self._core_base):
            raise TransportError("core_module_changed")
        paths = [path, *(self.upstream / relative for relative in DLLS)]
        if full:
            if path.stat().st_size != CORE_SIZE or _digest(path) != CORE_SHA256:
                raise TransportError("client_fingerprint_mismatch")
            if self._core_path is None:
                self._core_path, self._core_base = path, base
            self._stats = {str(item.resolve()): _stat(item) for item in paths}
        else:
            if not self._stats or any(self._stats.get(str(item.resolve())) != _stat(item) for item in paths):
                raise TransportError("pinned_file_changed")
        return base

    def _check_runtime(self, *, full=False, require_loaded=False, require_absent=False):
        if full:
            verify_upstream(self.upstream, self.commit)
        _check_module_cache(self.upstream)
        modules = self._api.native.modules(self.pid)
        base = self._verify_core(modules, full=full)
        present = BRIDGE_NAMES.intersection(modules)
        if require_absent and present:
            raise TransportError("existing_bridge_refused")
        if require_loaded:
            entry = modules.get("text_bridge.dll")
            if (present != {"text_bridge.dll"} or not entry
                    or Path(entry[1]).resolve() != Path(self._api.native.TEXT_DLL).resolve()):
                raise TransportError("loaded_bridge_mismatch")
        if self._read_account(base) != self.self_id:
            raise TransportError("account_changed")
        return modules

    def preflight(self):
        if self._used or self.loaded or self.pending:
            raise TransportError("transport_already_used")
        try:
            report = verify_upstream(self.upstream, self.commit)
            self._load_api()
            candidates = self._api.native.wechat_candidates()
            if len(candidates) != 1 or type(candidates[0]) is not int:
                raise TransportError("wechat_process_not_unique")
            self.pid = candidates[0]
            modules = self._api.native.modules(self.pid)
            if BRIDGE_NAMES.intersection(modules):
                raise TransportError("existing_bridge_refused")
            base = self._verify_core(modules, full=True)
            account = self._read_account(base)
            if self.expected_self is not None and account != self.expected_self:
                raise TransportError("expected_account_mismatch")
            self.self_id = account
            return {**report, "client_fingerprint_match": True, "unique_process": True,
                    "account_ready": True, "existing_bridge": False,
                    "native_module_loaded": False, "ready": True}
        except TransportError:
            raise
        except Exception:
            raise TransportError("native_preflight_failed") from None

    def start(self):
        if self._used or self.loaded or self.pending:
            raise TransportError("transport_already_used")
        if self.pid is None or self.self_id is None:
            self.preflight()
        self._used = True
        try:
            self._check_runtime(full=True, require_absent=True)
            self.guard = self._api.lifetime.BridgeGuard(self.pid)
            # Recheck under the process-wide mutex. No auto-unload watchdog.
            self._check_runtime(full=True, require_absent=True)
            self.loaded = True
            try:
                outcome = self._api.native.run("--listen", pid=self.pid,
                                              bridge_path=self._api.native.TEXT_DLL,
                                              preserve_report=False)
            except BaseException:
                self.pending = True
                raise TransportError("native_start_execution_unknown") from None
            if (not isinstance(outcome, dict) or type(outcome.get("code")) is not int
                    or type(outcome.get("pid")) is not int or outcome["pid"] != self.pid):
                self.pending = True
                raise TransportError("native_start_result_unknown")
            if outcome["code"] != 0:
                raise TransportError("native_start_rejected")
            self._check_runtime(require_loaded=True)
            self.channel = self._api.receive.ReceiveChannel(
                self.pid, dll=self._api.native.TEXT_DLL,
                poll_export=(b"WxPollMediaMessageV6" if self.commit == VOICE_COMMIT else b"WxPollMediaMessageV5"),
                message_type=getattr(self._api.receive, "ReceivedMediaMessageV6" if self.commit == VOICE_COMMIT else "ReceivedMediaMessageV5"),
            )
            self.started = True
            return {"started": True, "native_module_loaded": True, "watchdog_started": False}
        except TransportError:
            raise
        except BaseException:
            raise TransportError("native_start_setup_failed") from None

    def _active(self):
        if self.channel is not None and getattr(self.channel, "pending", False):
            self.pending = True
        if self.pending:
            raise TransportError("native_execution_unknown")
        if not self.started or not self.loaded or self.channel is None:
            raise TransportError("native_transport_not_started")

    def poll(self):
        self._active()
        try:
            self._check_runtime(require_loaded=True)
        except TransportError:
            raise
        except Exception:
            raise TransportError("runtime_check_failed") from None
        try:
            return self.channel.poll()
        except BaseException:
            self.pending = True
            self.started = False
            raise TransportError("native_poll_execution_unknown") from None

    def send(self, room, text):
        self._active()
        if not (self.private_peer and room == self.private_peer) and (not isinstance(room, str) or not room.endswith("@chatroom")
                or room == "@chatroom" or room.count("@") != 1
                or not room.isascii() or len(room) > 127
                or any(not (c.isalnum() or c in "_@.-") for c in room)):
            raise TransportError("invalid_group_target")
        if not isinstance(text, str) or not text or "\0" in text or len(text.encode("utf-8")) > 1023:
            raise TransportError("invalid_neutral_text")
        try:
            self._check_runtime(full=True, require_loaded=True)
        except TransportError:
            raise
        except Exception:
            raise TransportError("runtime_check_failed") from None
        try:
            result = self._api.native.run("--dispatch", pid=self.pid,
                                         bridge_path=self._api.native.TEXT_DLL,
                                         target=room, text=text, keep_loaded=True,
                                         preserve_report=False)
        except BaseException:
            self.pending = True
            self.started = False
            raise TransportError("native_send_execution_unknown") from None
        if (not isinstance(result, dict) or type(result.get("code")) is not int
                or type(result.get("pid")) is not int or result["pid"] != self.pid
                or type(result.get("cleanup_code")) is not int or result["cleanup_code"] != 0):
            self.pending = True
            self.started = False
            raise TransportError("native_send_completion_unknown")
        return result

    def send_voice(self, room, path, duration_ms):
        """Send a prepared Tencent SILK snapshot; never retry uncertain execution."""
        self._active()
        if not (self.private_peer and room == self.private_peer) and (not isinstance(room, str) or not room.endswith("@chatroom")
                or room == "@chatroom" or room.count("@") != 1
                or not room.isascii() or len(room) > 127
                or any(not (c.isalnum() or c in "_@.-") for c in room)):
            raise TransportError("invalid_group_target")
        if type(duration_ms) is not int or not 0 < duration_ms <= 60000 or duration_ms % 20:
            raise TransportError("invalid_voice_duration")
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise TransportError("invalid_voice_path")
        self._check_runtime(full=True, require_loaded=True)
        try:
            result = self._api.native.run(
                "--dispatch", pid=self.pid, bridge_path=self._api.native.TEXT_DLL,
                target=room, voice_path=str(path), voice_duration_ms=duration_ms,
                account=self.self_id, keep_loaded=True, preserve_report=False)
        except BaseException:
            self.pending = True
            self.started = False
            raise TransportError("native_voice_execution_unknown") from None
        report = result.get("report") if isinstance(result, dict) else None
        if not (isinstance(report, dict) and result.get("success") is True
                and type(result.get("pid")) is int and result["pid"] == self.pid
                and type(result.get("code")) is int and result["code"] == 0
                and type(result.get("cleanup_code")) is int and result["cleanup_code"] == 0
                and report.get("completion_success") is True
                and report.get("target") == room and report.get("text") == str(path)
                and report.get("voice_path") == str(path)
                and type(report.get("duration_ms")) is int and report["duration_ms"] == duration_ms
                and type(report.get("message_type")) is int and report["message_type"] == 34
                and report.get("sent") is True and report.get("operation") == "voice_send"):
            self.pending = True
            self.started = False
            raise TransportError("native_voice_completion_unknown")
        return {"state": "NATIVE_COMPLETED", "duration_ms": duration_ms,
                "recipient_delivery_confirmed": False}

    def close(self):
        if self._close_result is not None:
            return dict(self._close_result)
        self.started = False
        def finish(state, *, unloaded=False, reason=""):
            self._close_result = {"state": state, "dll_unloaded": unloaded,
                                  "manual_recovery_required": state != "CLEAN",
                                  "reason": reason, "pending": self.pending}
            return dict(self._close_result)
        if self.channel is not None and getattr(self.channel, "pending", False):
            self.pending = True
        if self.pending:
            # Keep channel buffer, process handle, DLL and mutex alive in this process.
            return finish("UNKNOWN", reason="native_execution_unknown")
        if not self.loaded:
            try:
                if self.guard is not None:
                    self.guard.close()
                    self.guard = None
                return finish("CLEAN", unloaded=True, reason="never_loaded")
            except BaseException:
                return finish("UNKNOWN", unloaded=True, reason="guard_release_failed")
        if self._remove_attempted:
            return finish("UNKNOWN", reason="remove_already_attempted")
        try:
            self._check_runtime(full=True, require_loaded=True)
            if self.channel is not None:
                if getattr(self.channel, "pending", False):
                    self.pending = True
                    return finish("UNKNOWN", reason="channel_execution_unknown")
                self.channel.close()
                self.channel = None
            self._remove_attempted = True
            outcome = self._api.native.run("--remove", pid=self.pid,
                                          bridge_path=self._api.native.TEXT_DLL,
                                          preserve_report=False)
            if (not isinstance(outcome, dict) or type(outcome.get("code")) is not int
                    or type(outcome.get("pid")) is not int or outcome["pid"] != self.pid
                    or outcome["code"] != 0 or outcome.get("dll_unloaded") is not True):
                self.pending = True
                return finish("UNKNOWN", reason="native_remove_unconfirmed")
            if BRIDGE_NAMES.intersection(self._api.native.modules(self.pid)):
                self.pending = True
                return finish("UNKNOWN", reason="bridge_still_loaded")
            self.loaded = False
            if self.guard is not None:
                self.guard.close()
                self.guard = None
            return finish("CLEAN", unloaded=True, reason="module_absence_confirmed")
        except BaseException:
            self.pending = True
            return finish("UNKNOWN", unloaded=not self.loaded, reason="native_cleanup_unknown")
