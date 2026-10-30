"""Persona parsing using temporary neutral fixtures only; no runtime files."""

import base64
from dataclasses import FrozenInstanceError
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway import persona as p


def card(version=2, **fields):
    data = {"name": "巡林员", "description": "喜爱森林与观星。", "personality": "耐心温和。",
            "scenario": "正在观察秋天的树叶。", **fields}
    if version == 1:
        return data
    return {"spec": f"chara_card_v{version}", "spec_version": f"{version}.0", "data": data}


def encoded(value):
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def metadata(keyword, value, kind=b"tEXt", compressed=False):
    text = value if isinstance(value, bytes) else encoded(value)
    key = keyword.encode("ascii") + b"\0"
    if kind == b"zTXt":
        payload = key + b"\0" + zlib.compress(text)
    elif kind == b"iTXt":
        payload = key + bytes((int(compressed), 0)) + b"zh\0\0" + (zlib.compress(text) if compressed else text)
    else:
        payload = key + text
    return chunk(kind, payload)


def png(*chunks):
    return (p._PNG + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + b"".join(chunks) + chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + chunk(b"IEND", b""))


class PersonaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="persona-neutral-", dir=ROOT / "tests")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def load(self, value, suffix=".json", **kwargs):
        path = self.root / ("neutral" + suffix)
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        path.write_bytes(data)
        result = p.load_persona(path, persona_id="forest_guide", **kwargs)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(result.revision, hashlib.sha256(data).hexdigest())
        return result

    def test_text_and_markdown_preserve_body_bom_and_crlf_semantics(self):
        body = "# 巡林员\r\n  喜爱森林。\r\n\r\n{{user}} 可询问天气。\r\n"
        for suffix in (".txt", ".md", ".markdown"):
            with self.subTest(suffix=suffix):
                result = self.load(b"\xef\xbb\xbf" + body.encode(), suffix, name="巡林员")
                self.assertEqual(result.prompt, body)
                self.assertEqual(result.raw, {"text": body})
                self.assertEqual(result.examples, ())
                self.assertEqual(result.report["features"]["macros"], "literal_not_expanded")
                self.assertFalse(result.report["runtime_bound"])

    def test_text_requires_explicit_name_and_nonblank_body(self):
        for data, kwargs, error in ((b"hello", {}, "text_persona_name_required"),
                                    (b" \r\n", {"name": "巡林员"}, "empty_text_persona")):
            with self.assertRaisesRegex(p.PersonaError, error):
                self.load(data, ".txt", **kwargs)

    def test_text_and_markdown_reject_invalid_utf8_and_nul(self):
        for suffix in (".txt", ".md", ".markdown"):
            for data, code in ((b"\xff", "invalid_utf8"), (b"guide\0", "empty_text_persona")):
                with self.subTest(suffix=suffix, code=code), self.assertRaisesRegex(p.PersonaError, code):
                    self.load(data, suffix, name="巡林员")

    def test_v1_v2_v3_core_fields_and_explicit_identity(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                source = card(version, system_prompt="回答保持简短。")
                result = self.load(source)
                self.assertEqual(result.persona_id, "forest_guide")
                self.assertEqual(result.name, "巡林员")
                for value in ("喜爱森林与观星。", "耐心温和。", "正在观察秋天的树叶。", "回答保持简短。"):
                    self.assertIn(value, result.prompt)
                self.assertEqual(result.raw, source)
                self.assertEqual(result.report["source_format"], f"sillytavern_v{version}")

    def test_native_examples_are_paired_and_unknown_settings_never_apply(self):
        source = {"persona_id": "source-id", "system_prompt": "解释星空。",
                  "begin_dialogs": [{"role": "user", "content": "你好", "unknown": 7},
                                    {"role": "assistant", "content": "晚上好。"}],
                  "tools": ["run_shell"], "skills": ["network"], "provider": "unapproved", "future": {"x": 2}}
        result = self.load(source, name="观星员")
        self.assertEqual(result.name, "观星员")
        self.assertEqual([(m.role, m.content) for m in result.examples], [("user", "你好"), ("assistant", "晚上好。")])
        for value in ("run_shell", "unapproved", "network"):
            self.assertNotIn(value, result.prompt)
        self.assertEqual(result.raw, source)
        for key in ("tools", "skills", "provider", "future", "begin_dialogs[0].unknown"):
            self.assertIn(key, result.report["preserved_only_fields"])

    def test_invalid_native_examples_fail_instead_of_becoming_history(self):
        cases = ["hello", [{}], [{"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}],
                 [{"role": "user", "content": ""}, {"role": "assistant", "content": "b"}],
                 [{"role": "user", "content": []}, {"role": "assistant", "content": "b"}]]
        for examples in cases:
            with self.subTest(examples=examples), self.assertRaisesRegex(p.PersonaError, "invalid_begin_dialogs"):
                self.load({"persona_id": "native", "system_prompt": "中性说明", "begin_dialogs": examples})

    def test_unknown_extensions_openings_and_worldbooks_are_retained_only(self):
        source = card(3, mes_example="example-only", first_mes="opening-only",
                      alternate_greetings=["alternate-only"], character_book={"entries": [{"content": "world-only"}]},
                      extensions={"script": "script-only"}, assets=[{"uri": "file:///unread"}],
                      custom_nested={"secret_instruction": "never-executed"})
        result = self.load(source)
        self.assertEqual(result.raw, source)
        for value in ("example-only", "opening-only", "world-only", "script-only", "file:///unread"):
            self.assertNotIn(value, result.prompt)
        self.assertEqual(result.report["features"]["worldbook"], "preserved_not_executed")
        self.assertIn("data.extensions", result.report["preserved_only_fields"])

    def test_snapshot_fields_are_frozen(self):
        result = self.load(card())
        with self.assertRaises(FrozenInstanceError):
            result.prompt = "changed"

    def test_unknown_spec_type_version_and_missing_core_are_static_errors(self):
        cases = [({"spec": [], "data": {}}, "unsupported_card_spec"),
                 ({"spec": "chara_card_v2", "spec_version": "9.0", "data": {}}, "unsupported_card_spec"),
                 ({"spec": "chara_card_v2", "spec_version": "2.0", "data": []}, "invalid_card_data"),
                 ({"name": "巡林员"}, "missing_persona_content"),
                 ({"name": "巡林员", "description": []}, "invalid_core_text_field"),
                 ({"persona_id": "native", "description": "x"}, "missing_native_system_prompt")]
        for source, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(p.PersonaError, error):
                self.load(source)

    def test_each_json_card_requires_name_and_valid_core_even_with_override(self):
        for version in (1, 2, 3):
            for invalid_name in (None, "", 7):
                with self.subTest(version=version, invalid_name=invalid_name):
                    with self.assertRaisesRegex(p.PersonaError, "invalid_persona_name"):
                        self.load(card(version, name=invalid_name), name="覆盖名称")
            for core_field in ("description", "personality", "scenario", "system_prompt"):
                with self.subTest(version=version, core_field=core_field):
                    with self.assertRaisesRegex(p.PersonaError, "invalid_core_text_field"):
                        self.load(card(version, **{core_field: []}))
            with self.subTest(version=version, content="empty"):
                with self.assertRaisesRegex(p.PersonaError, "missing_persona_content"):
                    self.load(card(version, description="", personality="", scenario=""))

    def test_json_duplicate_keys_nonfinite_array_and_invalid_utf8_rejected(self):
        for source, error in ((b'{"name":"a","name":"b"}', "duplicate_json_key"),
                              (b'{"x": NaN}', "nonfinite_json_number"),
                              (b'{"x": 1e9999}', "nonfinite_json_number"),
                              (b'{"x": "\\ud800"}', "invalid_json_unicode"),
                              (b'[]', "persona_object_required"), (b'{', "invalid_json"),
                              (b'\xff', "invalid_utf8")):
            with self.subTest(error=error), self.assertRaisesRegex(p.PersonaError, error):
                self.load(source)

    def test_bounded_read_and_unsupported_extension(self):
        with patch.object(p, "MAX_SOURCE_BYTES", 20):
            with self.assertRaisesRegex(p.PersonaError, "source_too_large"):
                self.load(b"x" * 21, ".txt", name="巡林员")
        with self.assertRaisesRegex(p.PersonaError, "unsupported_persona_file_type"):
            self.load(b"text", ".py", name="巡林员")

    def test_invalid_names_and_identity_do_not_echo_input(self):
        path = self.root / "neutral.txt"
        path.write_text("中性设定", encoding="utf-8")
        for identity in ("", "../escape", "colon:name", "x" * 100):
            with self.assertRaisesRegex(p.PersonaError, "invalid_persona_identity"):
                p.load_persona(path, persona_id=identity, name="巡林员")
        for name in ("", "\nextra", "x" * 201, 3):
            with self.assertRaisesRegex(p.PersonaError, "invalid_persona_name"):
                p.load_persona(path, persona_id="safe", name=name)

    def test_png_text_ztxt_and_itxt_metadata_are_supported(self):
        for keyword, version in (("chara", 1), ("chara", 2), ("ccv3", 3)):
            for kind, compressed in ((b"tEXt", False), (b"zTXt", True), (b"iTXt", False), (b"iTXt", True)):
                with self.subTest(keyword=keyword, version=version, kind=kind, compressed=compressed):
                    result = self.load(png(metadata(keyword, card(version), kind, compressed)), ".png")
                    self.assertEqual(result.name, "巡林员")
                    self.assertEqual(result.report["container"], "png")
                    self.assertEqual(result.report["selected_metadata"], keyword)
                    self.assertEqual(result.raw["cards"], {keyword: card(version)})
                    self.assertFalse(result.report["image_pixels_decoded"])

    def test_png_valid_v3_preferred_both_raw_cards_and_conflicts_preserved(self):
        v2, v3 = card(2), card(3, description="喜爱海岸与潮汐。")
        result = self.load(png(metadata("chara", v2), metadata("ccv3", v3)), ".png")
        self.assertIn("喜爱海岸与潮汐。", result.prompt)
        self.assertEqual(result.raw["cards"], {"chara": v2, "ccv3": v3})
        self.assertEqual(result.report["metadata_conflict_fields"], ["description"])
        self.assertEqual(result.report["selected_metadata"], "ccv3")
        self.assertFalse(result.report["metadata_fallback_performed"])

    def test_png_v1_data_extension_is_preserved_and_not_unwrapped(self):
        for extension in ([], None, 7, {"name": "extension-only"}):
            with self.subTest(extension=extension):
                v1 = card(1, data=extension)
                v3 = card(3, description="喜爱海岸与潮汐。")
                result = self.load(png(metadata("chara", v1), metadata("ccv3", v3)), ".png")
                self.assertEqual(result.raw["cards"], {"chara": v1, "ccv3": v3})
                self.assertEqual(result.report["selected_metadata"], "ccv3")
                self.assertEqual(result.report["metadata_conflict_fields"], ["data", "description"])
                self.assertIn("喜爱海岸与潮汐。", result.prompt)

    def test_png_invalid_preferred_v3_never_falls_back(self):
        for broken in (b"not-base64!", base64.b64encode(b"{"), encoded(card(2))):
            with self.subTest(broken=broken), self.assertRaisesRegex(p.PersonaError, "preferred_ccv3_invalid_"):
                self.load(png(metadata("chara", card()), metadata("ccv3", broken)), ".png")

    def test_png_invalid_secondary_is_preserved_and_reported(self):
        source = b"invalid!"
        result = self.load(png(metadata("chara", source), metadata("ccv3", card(3))), ".png")
        self.assertIn("secondary_chara_invalid_preserved", result.report["warnings"])
        self.assertEqual(base64.b64decode(result.raw["encoded_metadata"]["chara"]), source)

    def test_png_crc_missing_metadata_duplicate_and_truncation_rejected(self):
        good = png(metadata("chara", card()))
        damaged = bytearray(good)
        damaged[29] ^= 1
        cases = [(bytes(damaged), "invalid_png_crc"), (png(), "character_metadata_missing"),
                 (png(metadata("chara", card()), metadata("chara", card())), "duplicate_character_metadata"),
                 (good[:-12], "incomplete_png"), (good + b"extra", "invalid_png_end")]
        for source, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(p.PersonaError, error):
                self.load(source, ".png")

    def test_png_compression_expansion_is_bounded(self):
        source = png(metadata("ccv3", b"x" * 10000, b"zTXt"))
        with patch.object(p, "MAX_METADATA_BYTES", 128):
            with self.assertRaisesRegex(p.PersonaError, "png_metadata_too_large"):
                self.load(source, ".png")

    def test_png_corrupt_compressed_data_and_duplicate_header_rejected(self):
        with self.assertRaisesRegex(p.PersonaError, "invalid_png_compression"):
            self.load(png(chunk(b"zTXt", b"chara\0\0invalid")), ".png")
        with self.assertRaisesRegex(p.PersonaError, "duplicate_png_header"):
            self.load(png(chunk(b"IHDR", bytes(13))), ".png")

    def test_cli_dry_run_then_export_preserves_original_and_never_binds(self):
        spec = importlib.util.spec_from_file_location("persona_import_cli", ROOT / "scripts/import_persona.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        source = self.root / "neutral.txt"
        original = "  中性角色。\r\n".encode("utf-8")
        source.write_bytes(original)
        output = self.root / "export.json"
        dry = cli.execute(source, persona_id="safe", name="巡林员", output=output)
        self.assertEqual(dry["status"], "dry_run")
        self.assertFalse(output.exists())
        result = cli.execute(source, persona_id="safe", name="巡林员", output=output, apply=True)
        self.assertFalse(result["runtime_bound"])
        bundle = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(base64.b64decode(bundle["source"]["bytes"]), original)
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaisesRegex(p.PersonaError, "output_already_exists"):
            cli.execute(source, persona_id="safe", name="巡林员", output=output, apply=True)
        with self.assertRaisesRegex(p.PersonaError, "runtime_output_forbidden"):
            cli.execute(source, persona_id="safe", name="巡林员", output=ROOT / "runtime/no-write.json", apply=True)


if __name__ == "__main__":
    unittest.main()
