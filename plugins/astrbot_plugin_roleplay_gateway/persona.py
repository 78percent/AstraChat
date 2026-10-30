"""Bounded persona-file parsing; no runtime configuration, execution or network.

The caller explicitly chooses an identity and binds a reviewed snapshot later.
Unknown fields and inactive features remain in ``raw`` and the compatibility
report. Parsing a card never grants tools, runs macros, fetches attachments or
changes its source. PNG image pixels are not decoded; only the container and
character metadata are validated.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import struct
from typing import Any
import zlib

from .provider_gate import Message


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_CORE_FIELDS = ("description", "personality", "scenario", "system_prompt")
_LABELS = {"description": "描述", "personality": "性格", "scenario": "场景", "system_prompt": "角色提示"}


class PersonaError(ValueError):
    """Static diagnostic code only; never includes source text or paths."""


@dataclass(frozen=True)
class PersonaSnapshot:
    persona_id: str
    revision: str
    name: str
    prompt: str
    examples: tuple[Message, ...] = ()
    raw: dict = field(default_factory=dict)
    report: dict = field(default_factory=dict)


def read_source(path: Path) -> bytes:
    """Read at most the declared limit, including when a file changes size."""
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
    except (OSError, ValueError):
        raise PersonaError("source_unreadable") from None
    if len(data) > MAX_SOURCE_BYTES:
        raise PersonaError("source_too_large")
    if not data:
        raise PersonaError("source_empty")
    return data


def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeError:
        raise PersonaError("invalid_utf8") from None


def _json(data: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PersonaError("duplicate_json_key")
            result[key] = value
        return result

    def constant(_):
        raise PersonaError("nonfinite_json_number")

    def number(token):
        value = float(token)
        if not math.isfinite(value):
            raise PersonaError("nonfinite_json_number")
        return value

    try:
        value = json.loads(_text(data), object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except PersonaError:
        raise
    except (ValueError, RecursionError):
        raise PersonaError("invalid_json") from None
    if not isinstance(value, dict):
        raise PersonaError("persona_object_required")
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and any(0xD800 <= ord(char) <= 0xDFFF for char in item):
            raise PersonaError("invalid_json_unicode")
    return value


def _required_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise PersonaError(code)
    return value


def _name(value: Any) -> str:
    value = _required_text(value, "invalid_persona_name")
    if len(value) > 200 or "\n" in value or "\r" in value:
        raise PersonaError("invalid_persona_name")
    return value.strip()


def _examples(value: Any) -> tuple[Message, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) % 2 or len(value) > 200:
        raise PersonaError("invalid_begin_dialogs")
    result = []
    for index, row in enumerate(value):
        role = "user" if index % 2 == 0 else "assistant"
        if not isinstance(row, dict) or row.get("role") != role:
            raise PersonaError("invalid_begin_dialogs")
        content = _required_text(row.get("content"), "invalid_begin_dialogs")
        result.append(Message(role, content))
    return tuple(result)


def _normalize(card: dict, override_name: str | None) -> tuple[str, str, tuple[Message, ...], dict]:
    """Map only explicit core fields; preserve all other source data untouched."""
    mapped: list[str] = []
    if "spec" in card:
        spec = card["spec"]
        versions = {"chara_card_v2": "2.0", "chara_card_v3": "3.0"}
        if not isinstance(spec, str) or spec not in versions or card.get("spec_version") != versions[spec]:
            raise PersonaError("unsupported_card_spec")
        data = card.get("data")
        if not isinstance(data, dict):
            raise PersonaError("invalid_card_data")
        source_format = "sillytavern_v2" if spec == "chara_card_v2" else "sillytavern_v3"
        mapped.extend(("spec", "spec_version", "data"))
        prefix = "data."
    elif "persona_id" in card or "begin_dialogs" in card:
        _required_text(card.get("persona_id"), "invalid_native_persona_id")
        data, source_format, prefix = card, "astrbot_json", ""
    else:
        data, source_format, prefix = card, "sillytavern_v1", ""

    raw_name = data.get("persona_id") if source_format == "astrbot_json" else data.get("name")
    selected_name = _name(raw_name)
    if override_name is not None:
        selected_name = override_name
    mapped.append(prefix + ("persona_id" if source_format == "astrbot_json" else "name"))
    sections = ["角色名：" + selected_name]
    has_content = False
    for key in _CORE_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str) or "\x00" in value:
            raise PersonaError("invalid_core_text_field")
        mapped.append(prefix + key)
        if value.strip():
            sections.append("# " + _LABELS[key] + "\n" + value)
            has_content = True
    if source_format == "astrbot_json":
        _required_text(data.get("system_prompt"), "missing_native_system_prompt")
    if not has_content:
        raise PersonaError("missing_persona_content")
    examples = ()
    if source_format == "astrbot_json" and "begin_dialogs" in data:
        examples = _examples(data["begin_dialogs"])
        mapped.append("begin_dialogs")

    preserved = [prefix + key for key in data if prefix + key not in mapped]
    if source_format == "astrbot_json" and isinstance(data.get("begin_dialogs"), list):
        preserved += [f"begin_dialogs[{index}].{key}" for index, row in enumerate(data["begin_dialogs"])
                      for key in row if key not in {"role", "content"}]
    if prefix:
        preserved += [key for key in card if key not in {"spec", "spec_version", "data"}]
    features = {
        "begin_dialogs": "mapped" if examples else "not_present_or_empty",
        "mes_example": "preserved_not_executed" if "mes_example" in data else "not_present",
        "first_mes": "preserved_not_executed" if "first_mes" in data else "not_present",
        "worldbook": "preserved_not_executed" if any(k in data for k in ("character_book", "worldbook")) else "not_present",
        "macros": "literal_not_expanded",
        "scripts_and_attachments": "not_executed_or_fetched",
        "tools_and_skills": "not_applied",
    }
    report = {"source_format": source_format, "mapped_fields": mapped,
              "preserved_only_fields": preserved, "features": features,
              "warnings": [], "name_overridden": override_name is not None}
    return selected_name, "\n\n".join(sections), examples, report


def _decompress(data: bytes) -> bytes:
    try:
        decoder = zlib.decompressobj()
        out = decoder.decompress(data, MAX_METADATA_BYTES + 1)
        if len(out) > MAX_METADATA_BYTES or decoder.unconsumed_tail:
            raise PersonaError("png_metadata_too_large")
        if not decoder.eof or decoder.unused_data:
            raise PersonaError("invalid_png_compression")
        return out
    except zlib.error:
        raise PersonaError("invalid_png_compression") from None


def _png_text(kind: bytes, payload: bytes) -> tuple[str, bytes] | None:
    key, separator, rest = payload.partition(b"\0")
    if not separator or not 1 <= len(key) <= 79:
        raise PersonaError("invalid_png_text_chunk")
    keyword = key.decode("latin-1")
    if keyword not in {"chara", "ccv3"}:
        return None
    if kind == b"tEXt":
        text = rest
    elif kind == b"zTXt":
        if not rest or rest[0] != 0:
            raise PersonaError("invalid_png_compression")
        text = _decompress(rest[1:])
    else:
        if len(rest) < 2 or rest[0] not in (0, 1) or rest[1] != 0:
            raise PersonaError("invalid_png_itxt")
        flag, rest = rest[0], rest[2:]
        language, separator, rest = rest.partition(b"\0")
        if not separator:
            raise PersonaError("invalid_png_itxt")
        translated, separator, text = rest.partition(b"\0")
        if not separator:
            raise PersonaError("invalid_png_itxt")
        try:
            language.decode("ascii")
            translated.decode("utf-8")
        except UnicodeError:
            raise PersonaError("invalid_png_itxt") from None
        if flag:
            text = _decompress(text)
    if len(text) > MAX_METADATA_BYTES:
        raise PersonaError("png_metadata_too_large")
    return keyword, text


def _png_metadata(source: bytes) -> dict[str, bytes]:
    if not source.startswith(_PNG):
        raise PersonaError("invalid_png_signature")
    offset, count = len(_PNG), 0
    seen_header = seen_pixels = seen_end = False
    metadata: dict[str, bytes] = {}
    while offset < len(source):
        count += 1
        if count > 10000 or len(source) - offset < 12:
            raise PersonaError("invalid_png_chunks")
        length = struct.unpack_from(">I", source, offset)[0]
        kind = source[offset + 4:offset + 8]
        if not re.fullmatch(rb"[A-Za-z]{4}", kind) or length > len(source) - offset - 12:
            raise PersonaError("invalid_png_chunks")
        payload = source[offset + 8:offset + 8 + length]
        expected = struct.unpack_from(">I", source, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected:
            raise PersonaError("invalid_png_crc")
        offset += length + 12
        if not seen_header:
            if kind != b"IHDR" or length != 13:
                raise PersonaError("invalid_png_header")
            width, height = struct.unpack_from(">II", payload)
            if not width or not height:
                raise PersonaError("invalid_png_header")
            seen_header = True
        elif kind == b"IHDR":
            raise PersonaError("duplicate_png_header")
        if kind == b"IDAT":
            seen_pixels = True
        if kind == b"IEND":
            if length or offset != len(source):
                raise PersonaError("invalid_png_end")
            seen_end = True
            break
        if kind in {b"tEXt", b"zTXt", b"iTXt"}:
            item = _png_text(kind, payload)
            if item:
                key, text = item
                if key in metadata:
                    raise PersonaError("duplicate_character_metadata")
                metadata[key] = text
    if not seen_header or not seen_pixels or not seen_end:
        raise PersonaError("incomplete_png")
    if not metadata:
        raise PersonaError("character_metadata_missing")
    return metadata


def _decode_card(encoded: bytes) -> dict:
    try:
        encoded = re.sub(rb"[ \t\r\n]", b"", encoded)
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise PersonaError("invalid_character_base64") from None
    if len(decoded) > MAX_METADATA_BYTES:
        raise PersonaError("character_metadata_too_large")
    return _json(decoded)


def _different_fields(left: dict, right: dict) -> list[str]:
    # Compare semantic data across V1/V2/V3 envelopes without copying values
    # into the compatibility report. Raw cards retain full source detail.
    # A bare V1 card may have an unrelated, preserved extension named "data".
    # Only validated V2/V3 envelopes use that key as the semantic payload.
    left = left["data"] if left.get("spec") in {"chara_card_v2", "chara_card_v3"} else left
    right = right["data"] if right.get("spec") in {"chara_card_v2", "chara_card_v3"} else right
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key)
                  or (key in left) != (key in right))


def load_persona(path: Path, *, persona_id: str, name: str | None = None) -> PersonaSnapshot:
    if not isinstance(persona_id, str) or not _IDENTITY.fullmatch(persona_id):
        raise PersonaError("invalid_persona_identity")
    override_name = _name(name) if name is not None else None
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in {".txt", ".md", ".markdown", ".json", ".png"}:
        raise PersonaError("unsupported_persona_file_type")
    source = read_source(path)
    revision = hashlib.sha256(source).hexdigest()
    if suffix in {".txt", ".md", ".markdown"}:
        if override_name is None:
            raise PersonaError("text_persona_name_required")
        prompt = _required_text(_text(source), "empty_text_persona")
        selected_name, examples = override_name, ()
        raw = {"text": prompt}
        report = {"source_format": "markdown" if suffix != ".txt" else "text",
                  "mapped_fields": ["text"], "preserved_only_fields": [],
                  "features": {"macros": "literal_not_expanded", "scripts_and_attachments": "not_executed_or_fetched",
                               "tools_and_skills": "not_applied"}, "warnings": []}
    elif suffix == ".json":
        raw = _json(source)
        selected_name, prompt, examples, report = _normalize(raw, override_name)
    else:
        metadata = _png_metadata(source)
        selected = "ccv3" if "ccv3" in metadata else "chara"
        try:
            card = _decode_card(metadata[selected])
            selected_name, prompt, examples, report = _normalize(card, override_name)
            if report["source_format"] == "astrbot_json" or (
                selected == "ccv3" and report["source_format"] != "sillytavern_v3"
            ):
                raise PersonaError("png_metadata_spec_mismatch")
        except PersonaError as exc:
            if selected == "ccv3":
                raise PersonaError("preferred_ccv3_invalid_" + str(exc)) from None
            raise
        cards = {selected: card}
        differences = []
        if selected == "ccv3" and "chara" in metadata:
            try:
                secondary = _decode_card(metadata["chara"])
                _, _, _, secondary_report = _normalize(secondary, None)
                if secondary_report["source_format"] == "astrbot_json":
                    raise PersonaError("png_metadata_spec_mismatch")
                cards["chara"] = secondary
                differences = _different_fields(card, secondary)
            except PersonaError:
                report["warnings"].append("secondary_chara_invalid_preserved")
        raw = {"selected_metadata": selected, "cards": cards,
               "encoded_metadata": {key: base64.b64encode(value).decode("ascii") for key, value in metadata.items()}}
        report["container"] = "png"
        report["selected_metadata"] = selected
        report["metadata_conflict_fields"] = differences
        report["metadata_fallback_performed"] = False
        report["image_pixels_decoded"] = False
    report.update(schema_version=1, source_sha256=revision, source_bytes=len(source),
                  raw_preserved=True, source_modified=False, runtime_bound=False)
    return PersonaSnapshot(persona_id, revision, selected_name, prompt, examples, raw, report)
