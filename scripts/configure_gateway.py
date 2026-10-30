"""Register the neutral QQ receipt gate. Dry-run by default; stop AstrBot before --apply."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets

PLUGIN = "astrbot_plugin_roleplay_gateway"
PLATFORM = "qq-sakura"
PLUGIN_FILE = f"astrbot/config/{PLUGIN}_config.json"


class ConfigurationError(Exception):
    """Static error codes only; never include configuration values."""


def checked_path(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ConfigurationError("path_outside_root")
    return path


def read_optional(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def account_list(value: object) -> list[str]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", item) for item in value)
            or len(set(value)) != len(value)):
        raise ConfigurationError("invalid_account_list")
    return value


def make_plan(root: Path) -> dict:
    root = root.resolve()
    runtime = checked_path(root, "runtime")
    if not runtime.is_dir():
        raise ConfigurationError("runtime_required")
    inputs_path = checked_path(root, "config/connection-inputs.local.json")
    inputs_before = inputs_path.read_bytes()
    inputs = json.loads(inputs_before.decode("utf-8-sig"))
    qq = inputs["qq"]
    admins = account_list(qq["admin_account_ids"])
    private = account_list(qq["private_contact_allowlist"])
    groups = account_list(qq["test_group_ids"])
    bot = qq["bot_account_id"]
    if not isinstance(bot, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", bot):
        raise ConfigurationError("invalid_bot_account")
    if bot in admins or bot in private:
        raise ConfigurationError("bot_account_conflict")
    config_path = checked_path(runtime, "astrbot/cmd_config.json")
    plugin_path = checked_path(runtime, PLUGIN_FILE)
    config_before = config_path.read_bytes()
    config = json.loads(config_before.decode("utf-8-sig"))
    if config.get("provider_settings", {}).get("enable") is not False:
        raise ConfigurationError("native_provider_must_be_disabled")
    platforms = [item for item in config.get("platform", []) if item.get("id") == PLATFORM]
    if (len(platforms) != 1 or platforms[0].get("type") != "aiocqhttp"
            or platforms[0].get("enable") is not True):
        raise ConfigurationError("platform_mismatch")
    if set(account_list(config.get("admins_id"))) != set(admins):
        raise ConfigurationError("admin_mismatch")
    settings = config.get("platform_settings", {})
    whitelist = [f"{PLATFORM}:GroupMessage:{value}" for value in groups]
    whitelist += [f"{PLATFORM}:FriendMessage:{value}" for value in private]
    actual = settings.get("id_whitelist")
    if (settings.get("enable_id_white_list") is not True or not isinstance(actual, list)
            or set(actual) != set(whitelist) or len(actual) != len(whitelist)
            or settings.get("wl_ignore_admin_on_group", False) is not False
            or settings.get("wl_ignore_admin_on_friend", False) is not False):
        raise ConfigurationError("whitelist_mismatch")
    if config.get("plugin_set") not in ([], [PLUGIN]):
        raise ConfigurationError("other_plugins_conflict")
    desired = {"enabled": True, "mode": "neutral_receipt", "platform_id": PLATFORM,
               "bot_self_id": bot, "admin_private_ids": private, "allowed_group_ids": groups,
               "wake_names": ["三叶", "佐仓三叶"], "wake_regex": ["^测试唤醒[：:]?"],
               "send_timeout_seconds": 8}
    plugin_before = read_optional(plugin_path)
    if plugin_before is not None:
        if json.loads(plugin_before.decode("utf-8-sig")) != desired:
            raise ConfigurationError("existing_gateway_differs_manual_configuration_required")
        plugin_after = plugin_before
    else:
        plugin_after = (json.dumps(desired, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    rate_limit = settings.setdefault("rate_limit", {})
    if not isinstance(rate_limit, dict):
        raise ConfigurationError("invalid_rate_limit_settings")
    # ALL-event filtering runs after the native rate limiter; let the gate decide eligibility.
    rate_changed = type(rate_limit.get("count")) is not int or rate_limit.get("count") != 0
    changed = (config.get("plugin_set") != [PLUGIN]
               or config.get("disable_builtin_commands") is not True or rate_changed)
    rate_limit["count"] = 0
    config["plugin_set"] = [PLUGIN]
    config["disable_builtin_commands"] = True
    config_after = ((json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                    if changed else config_before)
    return {"root": root, "runtime": runtime, "read_only_guards": {inputs_path: inputs_before},
            "files": [(plugin_path, plugin_before, plugin_after),
                                           (config_path, config_before, config_after)]}


def atomic_replace(path: Path, data: bytes, expected: bytes | None) -> None:
    """Reserve absent targets exclusively, then replace complete bytes in the same directory."""
    if read_optional(path) != expected:
        raise ConfigurationError("runtime_changed_since_plan")
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(".gateway-" + secrets.token_hex(8) + ".tmp")
    reserved_new = False
    committed = False
    try:
        with staging.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is None:
            with path.open("xb"):
                pass
            reserved_new = True
        elif path.read_bytes() != expected:
            raise ConfigurationError("runtime_changed_since_plan")
        os.replace(staging, path)
        committed = True
    finally:
        staging.unlink(missing_ok=True)
        if reserved_new and not committed and read_optional(path) == b"":
            path.unlink()


def verify_read_only_guards(plan: dict) -> None:
    """Check declared project inputs without treating them as writable outputs.

    Call under the configuration lock before and after output replacements.
    These paths never enter backups or rollback; concurrent source edits belong
    to the caller and must be retained even when this configuration rolls back.
    """
    guards = plan.get("read_only_guards", {})
    if not isinstance(guards, dict):
        raise ConfigurationError("invalid_read_only_guards")
    if not guards:
        return
    root = plan.get("root")
    if not isinstance(root, Path) or not root.is_absolute():
        raise ConfigurationError("invalid_read_only_guard_root")
    try:
        root = root.resolve(strict=True)
        if not root.is_dir() or not plan["runtime"].resolve().is_relative_to(root):
            raise ConfigurationError("invalid_read_only_guard_root")
        outputs = {path.resolve() for path, _, _ in plan["files"]}
        for path, expected in guards.items():
            if not isinstance(path, Path) or not path.is_absolute() or not isinstance(expected, bytes):
                raise ConfigurationError("invalid_read_only_guards")
            if not path.is_relative_to(root):
                raise ConfigurationError("read_only_guard_outside_root")
            # Re-resolve on every check so a new symlink cannot escape the root
            # or turn an external input into a declared runtime output.
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ConfigurationError("read_only_guard_outside_root")
            if resolved in outputs:
                raise ConfigurationError("read_only_guard_conflicts_with_output")
            if not resolved.is_file() or path.read_bytes() != expected:
                raise ConfigurationError("read_only_guard_changed_since_plan")
    except (OSError, RuntimeError):
        raise ConfigurationError("read_only_guard_changed_since_plan") from None


def apply_plan(plan: dict) -> str:
    files = plan["files"]
    changes = [item for item in files if item[1] != item[2]]
    runtime = plan["runtime"]
    lock = checked_path(runtime, ".configure-gateway.lock")
    try:
        stream = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ConfigurationError("configuration_locked") from exc
    try:
        stream.close()
        verify_read_only_guards(plan)
        for path, before, _ in files:
            if not path.resolve().is_relative_to(runtime.resolve()):
                raise ConfigurationError("path_outside_root")
            if read_optional(path) != before:
                raise ConfigurationError("runtime_changed_since_plan")
        if not changes:
            return "already_configured"
        parent = checked_path(runtime, "backups")
        parent.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = parent / ("gateway-" + stamp + "-" + secrets.token_hex(4))
        backup.mkdir(mode=0o700)
        manifest = {"status": "prepared", "files": []}
        for path, before, _ in files:
            manifest["files"].append({"name": path.name, "existed": before is not None})
            if before is not None:
                with (backup / path.name).open("xb") as saved:
                    saved.write(before)
                    saved.flush()
                    os.fsync(saved.fileno())
        (backup / "status.json").write_text(json.dumps(manifest), "utf-8")
        replaced = []
        try:
            for path, before, after in changes:
                verify_read_only_guards(plan)
                atomic_replace(path, after, before)
                replaced.append((path, before, after))
            for path, _, after in files:
                if path.read_bytes() != after:
                    raise ConfigurationError("written_file_verification_failed")
            verify_read_only_guards(plan)
            manifest["status"] = "applied"
            (backup / "status.json").write_text(json.dumps(manifest), "utf-8")
            verify_read_only_guards(plan)
        except Exception as exc:
            incomplete = False
            for path, before, after in reversed(replaced):
                try:
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
                (backup / "status.json").write_text(json.dumps(manifest), "utf-8")
            except OSError:
                pass
            raise ConfigurationError(code) from exc
        return "applied"
    finally:
        lock.unlink(missing_ok=True)


def execute(root: Path, *, apply: bool = False) -> dict:
    plan = make_plan(root)
    changes = any(before != after for _, before, after in plan["files"])
    status = apply_plan(plan) if apply else ("dry_run" if changes else "already_configured")
    return {"status": status, "plugin": PLUGIN, "mode": "neutral_receipt",
            "native_provider_enabled": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = execute(args.root, apply=args.apply)
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, ConfigurationError):
            result["error_code"] = str(exc)
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
