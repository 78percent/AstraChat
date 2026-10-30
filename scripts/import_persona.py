"""Inspect a persona file or explicitly export a new bundle; never bind runtime.

Dry-run is the default. --apply requires --output and creates a new JSON file,
including original source bytes and normalized data. Existing files and the
production runtime directory cannot be used as output targets.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.persona import PersonaError, load_persona, read_source


def execute(source: Path, *, persona_id: str, name: str | None = None,
            output: Path | None = None, apply: bool = False) -> dict:
    snapshot = load_persona(source, persona_id=persona_id, name=name)
    if apply and output is None:
        raise PersonaError("output_required_for_apply")
    target = None
    if output is not None:
        target = Path(output).resolve()
        if target.suffix.lower() != ".json":
            raise PersonaError("bundle_json_required")
        if target.is_relative_to((ROOT / "runtime").resolve()):
            raise PersonaError("runtime_output_forbidden")
        if target.exists():
            raise PersonaError("output_already_exists")
    if apply:
        original = read_source(source)
        if hashlib.sha256(original).hexdigest() != snapshot.revision:
            raise PersonaError("source_changed_during_export")
        bundle = {"schema_version": 1, "snapshot": asdict(snapshot),
                  "source": {"filename": Path(source).name, "encoding": "base64",
                             "bytes": base64.b64encode(original).decode("ascii")}}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(bundle, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        except OSError:
            raise PersonaError("bundle_write_failed") from None
    # Console diagnostics intentionally contain no prompt, raw fields or name.
    return {"status": "exported" if apply else "dry_run", "source_format": snapshot.report["source_format"],
            "revision": snapshot.revision, "prompt_characters": len(snapshot.prompt),
            "example_messages": len(snapshot.examples),
            "preserved_only_field_count": len(snapshot.report["preserved_only_fields"]),
            "source_modified": False, "runtime_bound": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--persona-id", required=True)
    parser.add_argument("--name")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = execute(args.source, persona_id=args.persona_id, name=args.name,
                         output=args.output, apply=args.apply)
    except Exception as exc:
        result = {"status": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, PersonaError):
            result["error_code"] = str(exc)
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
