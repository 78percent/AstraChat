"""Select a reviewed neutral gateway mode; dry-run by default.

Stop AstrBot before --apply. This tool changes only mode/provider_id in the
existing gateway JSON, with backups and compare-before-replace guards. It does
not start Docker, log into QQ, call a model, bind a persona or replace secrets.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(
        "_roleplay_stream_" + name, Path(__file__).with_name(name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _sibling("check_runtime")
gateway = _sibling("configure_gateway")
ConfigurationError = gateway.ConfigurationError
PROVIDER_ID = checker.STREAM_PROVIDER_ID
MODES = ("neutral_receipt", "neutral_stream")


def make_plan(root: Path, mode: str = "neutral_stream") -> dict:
    if mode not in MODES:
        raise ConfigurationError("unsupported_neutral_mode")
    root = root.resolve()
    runtime = checker.local_path(root, "runtime", directory=True)
    plugin_path = checker.local_path(root, "runtime/" + gateway.PLUGIN_FILE)
    config_path = checker.local_path(root, "runtime/astrbot/cmd_config.json")
    env_path = checker.local_path(root, "runtime/astrbot.env")
    # Snapshot every file that determines the model selection/authentication.
    # Guard files are copied into the private backup, but never rewritten.
    before = {path: path.read_bytes() for path in (plugin_path, config_path, env_path)}
    current = checker.read_json(root, "runtime/" + gateway.PLUGIN_FILE)
    config = checker.read_json(root, "runtime/astrbot/cmd_config.json")
    if config.get("plugin_set") != [gateway.PLUGIN]:
        raise ConfigurationError("existing_reviewed_gateway_required")
    if current.get("mode") not in (*MODES, checker.ROLEPLAY_MODE):
        raise ConfigurationError("existing_gateway_mode_unsupported")
    if current.get("provider_id", PROVIDER_ID) != PROVIDER_ID:
        raise ConfigurationError("existing_gateway_provider_unsupported")
    desired = dict(current)
    desired["mode"] = mode
    if mode == "neutral_stream":
        desired["provider_id"] = PROVIDER_ID
    # Validate the planned mode rather than the old provider. This permits a
    # safe rollback to fixed receipts even if the stream provider was damaged.
    checker.check_runtime(root, gateway_override=desired)
    if any(path.read_bytes() != value for path, value in before.items()):
        raise ConfigurationError("runtime_changed_during_preflight")
    changed = current != desired
    after = ((json.dumps(desired, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
             if changed else before[plugin_path])
    return {"runtime": runtime, "mode": mode, "enabled": desired["enabled"],
            "changed_fields": [field for field in ("mode", "provider_id") if current.get(field) != desired.get(field)],
            "files": [(plugin_path, before[plugin_path], after),
                      (config_path, before[config_path], before[config_path]),
                      (env_path, before[env_path], before[env_path])]}


def apply_plan(plan: dict) -> str:
    """Reuse gateway's exclusive lock, private backups and atomic rollback."""
    return gateway.apply_plan(plan)


def execute(root: Path, *, mode: str = "neutral_stream", apply: bool = False) -> dict:
    plan = make_plan(root, mode)
    changes = any(before != after for _, before, after in plan["files"])
    status = apply_plan(plan) if apply else ("dry_run" if changes else "already_configured")
    return {"status": status, "plugin": gateway.PLUGIN, "mode": mode,
            "enabled": plan["enabled"], "provider_id": PROVIDER_ID,
            "changed_fields": plan["changed_fields"],
            "native_provider_enabled": False, "persona_binding_changed": False,
            "credentials_changed": False, "qq_login_verified": False,
            "astrbot_must_be_stopped_before_apply": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mode", choices=MODES, default="neutral_stream")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = execute(args.root, mode=args.mode, apply=args.apply)
    except Exception as error:
        result = {"status": "failed", "error_type": type(error).__name__}
        if isinstance(error, ConfigurationError):
            result["error_code"] = str(error)
        elif isinstance(error, checker.RuntimeCheckError):
            result["error_code"] = error.code
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
