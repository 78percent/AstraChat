"""Select one explicit persona for the reviewed roleplay stream; dry-run first.

Stop AstrBot before --apply. The source remains untouched; a byte-identical
runtime copy is selected with an exact hash. This performs format and budget
checks, not content-safety approval, model calls or QQ sends.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from plugins.astrbot_plugin_roleplay_gateway.persona import load_persona, read_source
from plugins.astrbot_plugin_roleplay_gateway.context_budget import build_prompt
from plugins.astrbot_plugin_roleplay_gateway.memory_store import MemorySnapshot, Namespace
from plugins.astrbot_plugin_roleplay_gateway.provider_gate import SpeakerInput
from plugins.astrbot_plugin_roleplay_gateway.roleplay_stream import ROLEPLAY_BOUNDARY


def _sibling(name: str):
    spec = importlib.util.spec_from_file_location(
        "_roleplay_persona_" + name, Path(__file__).with_name(name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _sibling("check_runtime")
gateway = _sibling("configure_gateway")
ConfigurationError = gateway.ConfigurationError


def _source_path(root: Path, source: Path) -> Path:
    try:
        source = Path(source)
        path = (source if source.is_absolute() else root / source).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ConfigurationError("persona_source_missing") from None
    if not path.is_relative_to(root) or not path.is_file():
        raise ConfigurationError("persona_source_outside_project")
    if path.suffix.lower() not in {".txt", ".md", ".markdown", ".json", ".png"}:
        raise ConfigurationError("unsupported_persona_source_type")
    return path


def make_plan(root: Path, source: Path, *, persona_id: str, name: str) -> dict:
    root = root.resolve()
    runtime = checker.local_path(root, "runtime", directory=True)
    plugin_path = checker.local_path(root, "runtime/" + gateway.PLUGIN_FILE)
    config_path = checker.local_path(root, "runtime/astrbot/cmd_config.json")
    env_path = checker.local_path(root, "runtime/astrbot.env")
    authorization_paths = (checker.local_path(root, "config/connection-inputs.local.json"),
                           checker.local_path(root, "runtime/inputs.snapshot.json"))
    authorization_guards = {path: path.read_bytes() for path in authorization_paths}
    guards = {path: path.read_bytes() for path in (plugin_path, config_path, env_path)}
    current = checker.read_json(root, "runtime/" + gateway.PLUGIN_FILE)
    config = checker.read_json(root, "runtime/astrbot/cmd_config.json")
    if config.get("plugin_set") != [gateway.PLUGIN]:
        raise ConfigurationError("existing_reviewed_gateway_required")
    if current.get("mode") not in ("neutral_receipt", "neutral_stream", checker.ROLEPLAY_MODE):
        raise ConfigurationError("existing_gateway_mode_unsupported")
    if current.get("provider_id", checker.STREAM_PROVIDER_ID) != checker.STREAM_PROVIDER_ID:
        raise ConfigurationError("existing_gateway_provider_unsupported")
    # Keep an existing roleplay mode while validating its current snapshot:
    # enabled WeChat entries require it. The new candidate is checked separately.
    # First-time neutral imports still validate the neutral streaming boundary.
    preflight_mode = (checker.ROLEPLAY_MODE if current.get("mode") == checker.ROLEPLAY_MODE
                      else "neutral_stream")
    preflight_gate = {**current, "mode": preflight_mode, "provider_id": checker.STREAM_PROVIDER_ID}
    checker.check_runtime(root, gateway_override=preflight_gate)
    qq = checker.qq_identity(checker.read_json(root, "config/connection-inputs.local.json"))
    memory_admin_ids = checker.confirmed_memory_admins(config, qq)
    path = _source_path(root, source)
    before = read_source(path)
    digest = hashlib.sha256(before).hexdigest()
    suffix = ".md" if path.suffix.lower() == ".markdown" else path.suffix.lower()
    filename = f"{persona_id}-{digest}{suffix}"
    desired = {**current, "mode": checker.ROLEPLAY_MODE, "provider_id": checker.STREAM_PROVIDER_ID,
               "memory_admin_ids": memory_admin_ids,
               "persona_id": persona_id, "persona_name": name,
               "persona_filename": filename, "persona_sha256": digest}
    checker.validate_persona_fields(desired)
    persona = load_persona(path, persona_id=persona_id, name=name)
    memory = MemorySnapshot(Namespace(checker.PLATFORM_ID, "private", "preflight", persona_id),
                            0, 0, "", (), 0)
    prepared = build_prompt(persona, memory, (SpeakerInput("preflight", "预检", "1", "你好"),),
                            runtime_boundary=ROLEPLAY_BOUNDARY)
    if read_source(path) != before or persona.revision != digest:
        raise ConfigurationError("persona_source_changed_during_preflight")
    if any(guard.read_bytes() != value for guard, value in (guards | authorization_guards).items()):
        raise ConfigurationError("runtime_changed_during_preflight")
    target = gateway.checked_path(runtime, "roleplay/personas/" + filename)
    if target.resolve().parent != (runtime / "roleplay/personas").resolve():
        raise ConfigurationError("persona_target_outside_directory")
    target_before = gateway.read_optional(target)
    if target_before is not None and target_before != before:
        raise ConfigurationError("existing_persona_copy_hash_mismatch")
    after = ((json.dumps(desired, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
             if current != desired else guards[plugin_path])
    # An already-selected runtime copy is an unchanged file in the output
    # verification set; every other source is an exclusively read-only guard.
    read_only_guards = authorization_guards | ({path: before} if path != target else {})
    return {"root": root, "runtime": runtime, "source_path": path, "source_before": before,
            "authorization_guards": authorization_guards, "memory_admin_ids": memory_admin_ids,
            "read_only_guards": read_only_guards,
            "persona_id": persona_id, "persona_name": persona.name,
            "persona_filename": filename, "persona_sha256": digest,
            "enabled": desired["enabled"], "source_format": persona.report.get("source_format"),
            "budget_report": prepared.report,
            "changed_fields": [field for field in ("mode", "provider_id", "memory_admin_ids", "persona_id", "persona_name",
                                                   "persona_filename", "persona_sha256")
                               if current.get(field) != desired.get(field)],
            "files": [(target, target_before, before), (plugin_path, guards[plugin_path], after),
                      (config_path, guards[config_path], guards[config_path]),
                      (env_path, guards[env_path], guards[env_path])]}


def apply_plan(plan: dict) -> str:
    for path, before in plan["authorization_guards"].items():
        try:
            current = path.read_bytes()
        except OSError:
            raise ConfigurationError("authorization_changed_since_plan") from None
        if current != before:
            raise ConfigurationError("authorization_changed_since_plan")
    path = _source_path(plan["root"], plan["source_path"])
    if path != plan["source_path"] or read_source(path) != plan["source_before"]:
        raise ConfigurationError("persona_source_changed_since_plan")
    return gateway.apply_plan(plan)


def execute(root: Path, source: Path, *, persona_id: str, name: str, apply: bool = False) -> dict:
    plan = make_plan(root, source, persona_id=persona_id, name=name)
    changes = any(before != after for _, before, after in plan["files"])
    status = apply_plan(plan) if apply else ("dry_run" if changes else "already_configured")
    if apply:
        checker.check_runtime(root)
    return {"status": status, "mode": checker.ROLEPLAY_MODE, "enabled": plan["enabled"],
            "provider_id": checker.STREAM_PROVIDER_ID,
            "memory_admin_ids": plan["memory_admin_ids"],
            "persona_id": plan["persona_id"], "persona_name": plan["persona_name"],
            "persona_filename": plan["persona_filename"], "persona_sha256": plan["persona_sha256"],
            "source_format": plan["source_format"], "budget_report": plan["budget_report"],
            "changed_fields": plan["changed_fields"], "source_modified": False,
            "credentials_changed": False, "native_provider_enabled": False,
            "content_safety_verified": False, "runtime_import_applied": bool(apply),
            "qq_login_verified": False, "astrbot_must_be_stopped_before_apply": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=CODE_ROOT)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--persona-id", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = execute(args.root, args.source, persona_id=args.persona_id, name=args.name, apply=args.apply)
    except Exception as error:
        result = {"status": "failed", "error_type": type(error).__name__}
        if isinstance(error, (ConfigurationError, ValueError)):
            code = str(error)
            # Parser errors are static codes; never print arbitrary exception text.
            if code and len(code) <= 160 and all(char in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in code):
                result["error_code"] = code
        if isinstance(error, checker.RuntimeCheckError):
            result["error_code"] = error.code
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
