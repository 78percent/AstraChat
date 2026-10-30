"""Plan native WeChat bridge settings; dry-run unless --apply is explicit.

The deployment operator must stop AstrBot and the native bridge before applying.
This script only reads/writes configuration; it never starts, stops, or contacts
services, opens persona/model credential files, or performs a native operation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from plugins.astrbot_plugin_roleplay_gateway.gate import (
    ConfigurationError as GateConfigurationError, native_group_id, native_person_id,
)
from plugins.astrbot_plugin_roleplay_gateway.native_settings import settings_from_config
from scripts.configure_gateway import (
    ConfigurationError, atomic_replace, checked_path, read_optional, verify_read_only_guards,
)
from scripts.wechat_native_bridge_core import BridgeError, BridgeSettings

PLATFORM = "wechat-native-sakura"
URL = "ws://127.0.0.1:6198/wechat-native/ws"
PROBE_FILE = "config/wechat-native.local.json"
PLUGIN_FILE = "runtime/astrbot/config/astrbot_plugin_roleplay_gateway_config.json"
HOST_FILE = "config/wechat-native-bridge.local.json"


def _json_object(data: bytes, code: str) -> dict:
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise ConfigurationError(code) from None
    if not isinstance(value, dict):
        raise ConfigurationError(code)
    return value


def _token(value: object) -> str | None:
    if value == "":
        return None
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ConfigurationError("invalid_existing_bridge_token")
    return value


def _string_list(value: object, key: str, *, maximum: int = 128) -> list[str]:
    if (not isinstance(value, list) or len(value) > 32
            or any(not isinstance(item, str) or not item.strip() or len(item) > maximum
                   or any(char in item for char in "\0\r\n") for item in value)
            or len(set(value)) != len(value)):
        raise ConfigurationError("invalid_" + key)
    return list(value)


def _path(root: Path, relative: str) -> Path:
    path = checked_path(root, relative)
    # Do not let a symlink/junction redirect one of the two exact output files.
    if path.resolve() != path:
        raise ConfigurationError("configuration_path_alias")
    return path


def _encoded(before: bytes | None, current: dict, desired: dict) -> bytes:
    if before is not None and current == desired:
        return before
    return (json.dumps(desired, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def make_plan(root: Path = ROOT) -> dict:
    root = root.resolve(strict=True)
    runtime = _path(root, "runtime")
    if not runtime.is_dir():
        raise ConfigurationError("runtime_required")
    probe_path = _path(root, PROBE_FILE)
    plugin_path = _path(root, PLUGIN_FILE)
    host_path = _path(root, HOST_FILE)
    probe_before = probe_path.read_bytes()
    probe = _json_object(probe_before, "invalid_probe_config")
    if (probe.get("native_trial_authorized") is not True
            or probe.get("target_group_label") != "test"):
        raise ConfigurationError("probe_authorization_required")
    account, room, commit = (probe.get(key) for key in ("expected_self", "allowed_room", "upstream_commit"))
    if native_person_id(account) is None or native_group_id(room) is None:
        raise ConfigurationError("verified_probe_binding_required")
    if not isinstance(commit, str) or re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ConfigurationError("invalid_upstream_commit")
    plugin_before = plugin_path.read_bytes()
    plugin = _json_object(plugin_before, "invalid_gateway_config")
    host_before = read_optional(host_path)
    host = _json_object(host_before, "invalid_bridge_config") if host_before is not None else {}
    # Preserve an already verified speech deployment when refreshing base settings.
    from scripts.wechat_native_bridge_core import VOICE_COMMIT
    if host.get("upstream_commit") == VOICE_COMMIT:
        bound = BridgeSettings.parse(host)
        if bound.expected_self != account or bound.allowed_room != room:
            raise ConfigurationError("voice_binding_changed")
        commit = VOICE_COMMIT
    plugin_token = _token(plugin.get("native_bridge_token", ""))
    host_token = _token(host.get("token", ""))
    if plugin_token and host_token and not secrets.compare_digest(plugin_token, host_token):
        raise ConfigurationError("bridge_token_mismatch")
    token_reused = bool(plugin_token or host_token)
    token = plugin_token or host_token or secrets.token_hex(32)
    admins = _string_list(plugin.get("native_admin_ids", []), "native_admin_ids")
    if any(native_person_id(item) is None for item in admins):
        raise ConfigurationError("invalid_native_admin_ids")
    names = _string_list(plugin.get("native_wake_names", plugin.get("wake_names", ["三叶", "佐仓三叶"])),
                         "native_wake_names")
    patterns = _string_list(plugin.get("native_wake_regex", plugin.get("wake_regex", [])),
                            "native_wake_regex", maximum=512)
    desired_plugin = {**plugin,
        "native_enabled": True, "native_platform_id": PLATFORM,
        "native_self_id": account, "native_allowed_group_ids": [room],
        "native_admin_ids": admins, "native_wake_names": names,
        "native_wake_regex": patterns, "native_reply_probability": 1.0,
        "native_bridge_token": token,
    }
    desired_host = {**host, "enabled": True, "platform_id": PLATFORM,
                    "expected_self": account, "allowed_room": room, "token": token,
                    "url": URL, "upstream_commit": commit}
    # Preview must enforce the exact same contract as both production readers.
    # A syntactically valid plan is insufficient if either service would refuse it.
    try:
        settings_from_config(desired_plugin)
    except GateConfigurationError:
        raise ConfigurationError("invalid_native_runtime_settings") from None
    try:
        BridgeSettings.parse(desired_host)
    except BridgeError:
        raise ConfigurationError("invalid_bridge_runtime_settings") from None
    return {
        "root": root, "runtime": runtime,
        "read_only_guards": {probe_path: probe_before},
        "files": [(plugin_path, plugin_before, _encoded(plugin_before, plugin, desired_plugin)),
                  (host_path, host_before, _encoded(host_before, host, desired_host))],
        "summary": {"group_count": 1, "admin_count": len(admins), "wake_name_count": len(names),
                    "wake_regex_count": len(patterns), "reply_probability": 1.0,
                    "token_reused": token_reused, "service_stop_required": True},
    }


def _verify_plan_paths(plan: dict) -> None:
    root, runtime = plan["root"], plan["runtime"]
    if (not isinstance(root, Path) or not root.is_absolute() or root.resolve() != root
            or runtime != _path(root, "runtime") or not runtime.is_dir()):
        raise ConfigurationError("invalid_plan_root")
    if len(plan["files"]) != 2:
        raise ConfigurationError("invalid_plan_outputs")
    expected_paths = [_path(root, PLUGIN_FILE), _path(root, HOST_FILE)]
    for (path, before, after), expected in zip(plan["files"], expected_paths):
        if (path != expected or (before is not None and not isinstance(before, bytes))
                or not isinstance(after, bytes)):
            raise ConfigurationError("invalid_plan_outputs")
    if set(plan["read_only_guards"]) != {_path(root, PROBE_FILE)}:
        raise ConfigurationError("invalid_plan_guards")


def _verify_snapshots(plan: dict, replaced: list) -> None:
    _verify_plan_paths(plan)
    verify_read_only_guards(plan)
    replacements = {path: after for path, _, after in replaced}
    for path, before, _ in plan["files"]:
        if read_optional(path) != replacements.get(path, before):
            raise ConfigurationError("configuration_changed_since_plan")


def _manifest(path: Path, value: dict) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def apply_plan(plan: dict) -> str:
    """Atomically replace each file, with a shared lock and guarded rollback.

    Two files cannot form one filesystem transaction. Persisted backups and a
    manifest support recovery if the process/machine dies between replacements.
    Concurrent user edits are retained, including when rollback is incomplete.
    """
    _verify_plan_paths(plan)
    lock = _path(plan["root"], "runtime/.configure-gateway.lock")
    try:
        stream = lock.open("x", encoding="utf-8")
    except FileExistsError:
        raise ConfigurationError("configuration_locked") from None
    try:
        stream.close()
        _verify_snapshots(plan, [])
        changes = [item for item in plan["files"] if item[1] != item[2]]
        if not changes:
            return "already_configured"
        parent = _path(plan["root"], "runtime/backups")
        parent.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = parent / ("native-wechat-" + stamp + "-" + secrets.token_hex(4))
        backup.mkdir(mode=0o700)
        manifest = {"status": "prepared", "files": []}
        for path, before, _ in plan["files"]:
            manifest["files"].append({"path": path.relative_to(plan["root"]).as_posix(),
                                      "backup_name": path.name, "existed": before is not None})
            if before is not None:
                with (backup / path.name).open("xb") as saved:
                    saved.write(before)
                    saved.flush()
                    os.fsync(saved.fileno())
        status_path = backup / "status.json"
        _manifest(status_path, manifest)
        replaced = []
        try:
            for path, before, after in changes:
                _verify_snapshots(plan, replaced)
                atomic_replace(path, after, before)
                replaced.append((path, before, after))
            _verify_snapshots(plan, replaced)
            manifest["status"] = "applied"
            _manifest(status_path, manifest)
            _verify_snapshots(plan, replaced)
        except BaseException as exc:
            incomplete = False
            for path, before, after in reversed(replaced):
                try:
                    _verify_plan_paths(plan)
                    if before is None:
                        if path.read_bytes() != after:
                            raise ConfigurationError("rollback_target_changed")
                        path.unlink()
                    else:
                        atomic_replace(path, before, after)
                except Exception:
                    incomplete = True
            code = "apply_failed_rollback_incomplete" if incomplete else "apply_failed_rolled_back"
            manifest["status"] = code
            try:
                _manifest(status_path, manifest)
            except OSError:
                pass
            raise ConfigurationError(code) from exc
        return "applied"
    finally:
        lock.unlink(missing_ok=True)


def execute(root: Path = ROOT, *, apply: bool = False) -> dict:
    plan = make_plan(root)
    changed = sum(before != after for _, before, after in plan["files"])
    status = apply_plan(plan) if apply else ("dry_run" if changed else "already_configured")
    return {"status": status, "changed_file_count": changed, **plan["summary"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true", help="Write configuration; services must already be stopped.")
    args = parser.parse_args(argv)
    try:
        result = execute(args.root, apply=args.apply)
    except ConfigurationError as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
