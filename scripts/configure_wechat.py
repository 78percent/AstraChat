"""定向配置原生微信私聊；默认 dry-run。--apply 前停止 AstrBot，保留 SnowLuma。"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path


def _module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway = _module("_wechat_gateway_config", "configure_gateway.py")
checker = _module("_wechat_runtime_check", "check_runtime.py")
PLATFORM = "wechat-sakura"
FIELDS = ("wechat_enabled", "wechat_platform_id", "wechat_allowed_private_ids", "wechat_admin_ids")


class ConfigurationError(ValueError):
    """只包含静态错误代码，不含登录资料。"""


def _read(path: Path) -> tuple[bytes, dict]:
    data = path.read_bytes()
    value = json.loads(data.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ConfigurationError("configuration_must_be_object")
    return data, value


def _encode(value: dict, before: bytes) -> bytes:
    if value == json.loads(before.decode("utf-8-sig")):
        return before
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def make_plan(root: Path, *, operation: str = "prepare", peer_id: str | None = None) -> dict:
    if operation not in {"prepare", "enable", "disable"}:
        raise ConfigurationError("invalid_operation")
    if operation != "enable" and peer_id is not None:
        raise ConfigurationError("peer_id_requires_enable")
    root = root.resolve()
    runtime = gateway.checked_path(root, "runtime")
    # Compare current QQ boundaries before constructing any mutation.
    checker.check_runtime(root)
    plugin_path = gateway.checked_path(runtime, gateway.PLUGIN_FILE)
    config_path = gateway.checked_path(runtime, "astrbot/cmd_config.json")
    gate_before, gate = _read(plugin_path)
    config_before, config = _read(config_path)
    original_gate, original_config = copy.deepcopy(gate), copy.deepcopy(config)
    if gate.get("mode") != "roleplay_stream":
        raise ConfigurationError("wechat_requires_roleplay_mode")
    if gate.get("wechat_platform_id", PLATFORM) != PLATFORM:
        raise ConfigurationError("wechat_platform_mismatch")
    gate.setdefault("wechat_enabled", False)
    gate.setdefault("wechat_platform_id", PLATFORM)
    gate.setdefault("wechat_allowed_private_ids", [])
    gate.setdefault("wechat_admin_ids", [])
    if operation == "enable":
        if (not isinstance(peer_id, str) or not 1 <= len(peer_id) <= 128
                or any(c.isspace() or not c.isprintable() or c == ":" for c in peer_id)):
            raise ConfigurationError("invalid_wechat_peer")
        platforms = [p for p in config.get("platform", [])
                     if isinstance(p, dict) and p.get("id") == PLATFORM]
        if len(platforms) != 1 or platforms[0].get("type") != "weixin_oc":
            raise ConfigurationError("native_wechat_node_required")
        node = platforms[0]
        if (node.get("enable") is not True
                or not checker.nonempty_secret(node.get("weixin_oc_token"))
                or not checker.nonempty_secret(node.get("weixin_oc_account_id"))):
            raise ConfigurationError("native_wechat_login_required")
        # Native ingress owns context tokens; a user must have sent first.
        contexts = node.get("weixin_oc_context_tokens")
        if not isinstance(contexts, dict) or not checker.nonempty_secret(contexts.get(peer_id)):
            raise ConfigurationError("peer_must_send_to_native_bot_first")
        gate.update(wechat_enabled=True, wechat_allowed_private_ids=[peer_id], wechat_admin_ids=[peer_id])
    elif operation == "disable":
        gate["wechat_enabled"] = False

    settings = config["platform_settings"]
    previous_entries = [f"{PLATFORM}:FriendMessage:{v}"
                        for v in original_gate.get("wechat_allowed_private_ids", [])]
    entries = [entry for entry in settings["id_whitelist"] if entry not in previous_entries]
    if gate["wechat_enabled"]:
        entries += [f"{PLATFORM}:FriendMessage:{v}" for v in gate["wechat_allowed_private_ids"]]
    settings["id_whitelist"] = entries
    # No native credentials, account state, providers or QQ settings are edited.
    if config.get("platform") != original_config.get("platform"):
        raise ConfigurationError("native_state_changed")
    checker.check_runtime(root, gateway_override=gate, config_override=config)
    guards = {}
    for relative in ("config/connection-inputs.local.json", "runtime/inputs.snapshot.json"):
        path = gateway.checked_path(root, relative)
        guards[path] = path.read_bytes()
    return {"root": root, "runtime": runtime, "read_only_guards": guards,
            "files": [(plugin_path, gate_before, _encode(gate, gate_before)),
                      (config_path, config_before, _encode(config, config_before))],
            "operation": operation,
            "changed_fields": [key for key in FIELDS if original_gate.get(key) != gate.get(key)],
            "wechat_enabled": gate["wechat_enabled"],
            "allowed_private_count": len(gate["wechat_allowed_private_ids"])}


def execute(root: Path, *, operation: str = "prepare", peer_id: str | None = None,
            apply: bool = False) -> dict:
    plan = make_plan(root, operation=operation, peer_id=peer_id)
    changed = any(before != after for _, before, after in plan["files"])
    status = gateway.apply_plan(plan) if apply else ("dry_run" if changed else "already_configured")
    if apply:
        checker.check_runtime(root)
    return {"ok": True, "status": status, "operation": operation,
            "wechat_enabled": plan["wechat_enabled"],
            "allowed_private_count": plan["allowed_private_count"],
            "changed_fields": plan["changed_fields"], "qq_scope_unchanged": True,
            "native_login_state_preserved": True, "credentials_exported": False,
            "live_login_or_delivery_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--operation", choices=("prepare", "enable", "disable"), default="prepare")
    parser.add_argument("--peer-id", help="用户主动发送验证消息后，在原生状态中确认的微信对端ID")
    parser.add_argument("--apply", action="store_true", help="备份并原子写入；先停止AstrBot")
    args = parser.parse_args(argv)
    try:
        result = execute(args.root, operation=args.operation, peer_id=args.peer_id, apply=args.apply)
    except checker.RuntimeCheckError as exc:
        result = {"ok": False, "code": exc.code, "credentials_exported": False}
    except (ConfigurationError, gateway.ConfigurationError) as exc:
        result = {"ok": False, "code": str(exc), "credentials_exported": False}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        result = {"ok": False, "code": "configuration_failed", "credentials_exported": False}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
