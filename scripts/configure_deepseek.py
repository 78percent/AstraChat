"""Register the verified official provider; dry-run by default. Stop AstrBot before --apply."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit


SOURCE_ID = "deepseek-official"
PROVIDER_ID = "deepseek-flash-official"
MODEL = "deepseek-flash"
ENV_NAME = "ROLEPLAY_DEEPSEEK_API_KEY"


class ConfigurationError(Exception):
    """Only static, non-sensitive error codes may be passed to this class."""


def checked_child(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ConfigurationError("path_outside_root")
    if must_exist and not path.is_file():
        raise ConfigurationError("required_file_missing")
    return path


def validate_evidence(report: dict) -> None:
    expected = {
        "scope": "official_api_only_no_qq_latency_claim",
        "runtime": "astrbot_container_linux",
        "models_http_status": 200,
        "generation_http_status": 200,
        "requested_model": MODEL,
        "actual_model": MODEL,
    }
    if any(report.get(key) != value for key, value in expected.items()):
        raise ConfigurationError("successful_container_evidence_required")
    if any(report.get(key) is not True for key in ("authenticated", "success", "stream_done")):
        raise ConfigurationError("successful_container_evidence_required")
    count = report.get("nonempty_content_chunks")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ConfigurationError("successful_container_evidence_required")


def official_base(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigurationError("official_endpoint_required")
    try:
        parts = urlsplit(value)
        allowed = (parts.scheme == "https" and parts.hostname == "api.deepseek.com"
                   and parts.port in (None, 443) and parts.username is None and parts.password is None
                   and parts.path in ("", "/", "/v1", "/v1/") and not parts.query and not parts.fragment)
    except ValueError:
        allowed = False
    if not allowed:
        raise ConfigurationError("official_endpoint_required")
    return value.rstrip("/")


def set_env_value(original: bytes, key: str) -> bytes:
    # Official keys need no shell expansion; rejecting other characters also blocks dotenv injection.
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", key):
        raise ConfigurationError("invalid_api_key_shape")
    text = original.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    pattern = re.compile(r"^\s*(?:export\s+)?" + ENV_NAME + r"\s*=")
    indexes = [i for i, line in enumerate(lines) if pattern.match(line)]
    if len(indexes) > 1:
        raise ConfigurationError("duplicate_api_key_environment_entries")
    assignment = f"{ENV_NAME}={key}{newline}"
    if indexes:
        lines[indexes[0]] = assignment
    else:
        if lines and not lines[-1].endswith(("\r", "\n")):
            lines[-1] += newline
        lines.append(assignment)
    return "".join(lines).encode("utf-8")


def upsert(entries: list, desired: dict) -> list:
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ConfigurationError("invalid_provider_list")
    matches = [i for i, entry in enumerate(entries) if entry.get("id") == desired["id"]]
    if len(matches) > 1:
        raise ConfigurationError("duplicate_provider_id")
    updated = list(entries)
    if matches:
        updated[matches[0]] = desired
    else:
        updated.append(desired)
    return updated


def make_plan(root: Path) -> dict:
    root = root.resolve()
    runtime = root / "runtime"
    if not runtime.resolve().is_relative_to(root) or not runtime.is_dir():
        raise ConfigurationError("local_runtime_required")
    report = json.loads(checked_child(root, "reports/deepseek-connectivity.json").read_text("utf-8-sig"))
    validate_evidence(report)
    access = json.loads(checked_child(root, "config/model-access.local.json").read_text("utf-8-sig"))
    official = access["deepseek_official"]
    base = official_base(official.get("base_url"))
    key = official.get("api_key")
    if not isinstance(key, str):
        raise ConfigurationError("missing_api_key")
    key = key.strip()
    config_path = checked_child(runtime, "astrbot/cmd_config.json")
    env_path = checked_child(runtime, "astrbot.env")
    original_config = config_path.read_bytes()
    original_env = env_path.read_bytes()
    config = json.loads(original_config.decode("utf-8-sig"))
    source = {"id": SOURCE_ID, "provider": "deepseek", "type": "openai_chat_completion",
              "provider_type": "chat_completion", "enable": True,
              "key": ["$" + ENV_NAME], "api_base": base, "timeout": 30,
              "proxy": "", "custom_headers": {}}
    provider = {"id": PROVIDER_ID, "provider_source_id": SOURCE_ID, "enable": True,
                "model": MODEL, "custom_extra_body": {"thinking": {"type": "disabled"}}}
    config["provider_sources"] = upsert(config.get("provider_sources", []), source)
    config["provider"] = upsert(config.get("provider", []), provider)
    settings = config.setdefault("provider_settings", {})
    if not isinstance(settings, dict):
        raise ConfigurationError("invalid_provider_settings")
    settings["enable"] = False
    # All unrelated settings, including tools, search, and persona bindings, are preserved.
    new_config = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    new_env = set_env_value(original_env, key)
    if key.encode("utf-8") in new_config:
        raise ConfigurationError("plaintext_key_in_config_refused")
    return {"runtime": runtime, "files": [(env_path, original_env, new_env),
                                           (config_path, original_config, new_config)]}


def atomic_write(path: Path, data: bytes) -> None:
    staging = path.with_name(".configure-" + secrets.token_hex(8) + ".tmp")
    try:
        with staging.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        if staging.exists():
            staging.unlink()


def apply_plan(plan: dict) -> str:
    files = [(path, before, after) for path, before, after in plan["files"] if before != after]
    if not files:
        return "already_configured"
    runtime = plan["runtime"]
    backup_parent = checked_child(runtime, "backups", must_exist=False)
    backup_parent.mkdir(exist_ok=True)
    lock = checked_child(runtime, ".configure-deepseek.lock", must_exist=False)
    try:
        lock_stream = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ConfigurationError("configuration_locked") from exc
    try:
        with lock_stream:
            lock_stream.write("configure_deepseek\n")
        for path, before, _ in files:
            if path.read_bytes() != before:
                raise ConfigurationError("runtime_changed_since_plan")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = backup_parent / ("deepseek-" + stamp + "-" + secrets.token_hex(4))
        backup.mkdir(mode=0o700)
        for path, before, _ in files:
            with (backup / path.name).open("xb") as stream:
                stream.write(before)
                stream.flush()
                os.fsync(stream.fileno())
        replaced: list[tuple[Path, bytes]] = []
        try:
            for path, before, after in files:
                if path.read_bytes() != before:
                    raise ConfigurationError("runtime_changed_since_plan")
                atomic_write(path, after)
                replaced.append((path, before))
            for path, _, after in files:
                if path.read_bytes() != after:
                    raise ConfigurationError("written_file_verification_failed")
            (backup / "status.json").write_text(json.dumps({"status": "applied"}), "utf-8")
        except Exception as exc:
            rollback_failed = False
            for path, before in reversed(replaced):
                try:
                    atomic_write(path, before)
                except Exception:
                    rollback_failed = True
            status = "apply_failed_rollback_incomplete" if rollback_failed else "apply_failed_rolled_back"
            try:
                (backup / "status.json").write_text(json.dumps({"status": status}), "utf-8")
            except OSError:
                pass
            raise ConfigurationError(status) from exc
        return "applied"
    finally:
        lock.unlink(missing_ok=True)


def execute(root: Path, *, apply: bool = False) -> dict:
    plan = make_plan(root)
    changed = [path.name for path, before, after in plan["files"] if before != after]
    status = apply_plan(plan) if apply else ("dry_run" if changed else "already_configured")
    return {"status": status, "source_id": SOURCE_ID, "provider_id": PROVIDER_ID,
            "model_id": MODEL, "changed_fields": changed, "provider_settings.enable": False,
            "key_storage": "environment_reference", "persona_binding": "unchanged"}


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
