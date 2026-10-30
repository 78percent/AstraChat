"""Pure regressions for incremental native-JSON direct-dialogue output."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.astrbot_plugin_roleplay_gateway.dialogue_output import DialogueFormatError, DialogueStream


def record(*texts, **kwargs):
    return json.dumps({"speech": list(texts)}, ensure_ascii=False, **kwargs)


class DialogueOutputTests(unittest.TestCase):
    def parse(self, chunks, **kwargs):
        parser = DialogueStream(**kwargs)
        result = []
        for chunk in chunks:
            result.extend(parser.feed(chunk))
        parser.finish()
        return result

    def test_first_item_before_second_item_and_object_end(self):
        parser = DialogueStream()
        self.assertEqual(parser.feed('{"speech":["累了就歇会儿，我陪你聊。"'), ())
        self.assertEqual(parser.feed(','), ("累了就歇会儿，我陪你聊。",))
        self.assertEqual(parser.feed('"后面的'), ())
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_truncated_record"):
            parser.finish()

    def test_last_item_before_object_end(self):
        parser = DialogueStream()
        self.assertEqual(parser.feed('{"speech":["你好。"]'), ("你好。",))
        self.assertEqual(parser.feed('}'), ())
        parser.finish()

    def test_invalid_item_boundary_never_releases_pending_speech(self):
        for suffix in ('x', ':', '{', '"', '}', ';'):
            with self.subTest(suffix=suffix):
                parser = DialogueStream()
                self.assertEqual(parser.feed('{"speech":["你好。"'), ())
                with self.assertRaisesRegex(DialogueFormatError, "dialogue_invalid_item_delimiter"):
                    parser.feed(suffix)

    def test_every_possible_chunk_boundary(self):
        source = record('“先歇会儿。”', '他说“你好”，我回答：欢迎。\n下一行。')
        expected = ["先歇会儿。", '他说“你好”，我回答：欢迎。\n下一行。']
        for offset in range(len(source) + 1):
            with self.subTest(offset=offset):
                self.assertEqual(self.parse([source[:offset], source[offset:]]), expected)

    def test_character_chunks_quotes_backslashes_and_braces(self):
        text = '这个词叫"hello"，路径是 C:\\hello，符号是 {。'
        self.assertEqual(self.parse(list(record(text))), [text])

    def test_unicode_escapes_and_emoji(self):
        source = json.dumps({"speech": ["你好😊。"]}, ensure_ascii=True)
        self.assertEqual(self.parse(list(source)), ["你好😊。"])
        self.assertEqual(self.parse(['{"\\u0073peech":["你好。"]}']), ["你好。"])

    def test_outer_quotes_and_dollars_removed(self):
        for text in ('“你好。$”', '"你好。$"', "'你好。$'", '「你好。$」',
                     '『你好。$』', '‘你好。$’', ' $ “\'你好。$\'” $ '):
            with self.subTest(text=text):
                self.assertEqual(self.parse(list(record(text))), ["你好。"])

    def test_internal_quotes_preserved(self):
        for text in ('你说的“青柚灯塔”我记住了。', '“勇气”就是“不怕”',
                     '"courage" means "bravery"', "'a' is not 'b'"):
            with self.subTest(text=text):
                self.assertEqual(self.parse([record(text)]), [text])

    def test_nested_quotes_and_apostrophe(self):
        for source, expected in (('“你说的“青柚灯塔”我记住了。”', '你说的“青柚灯塔”我记住了。'),
                                 ("'I'm here.'", "I'm here.")):
            with self.subTest(source=source):
                self.assertEqual(self.parse([record(source)]), [expected])

    def test_json_whitespace_and_multiple_items(self):
        source = ' \t\r\n{ "speech" \t: [ "你好。" \r\n, "我在这里。" ] }\n'
        self.assertEqual(self.parse(list(source)), ["你好。", "我在这里。"])

    def test_raw_observed_failure_rejected(self):
        source = ('“累了就坐下来歇会儿，我陪你聊。”\n$我顿了一下，抬眼看你。\n'
                  '$“摸摸尾巴——这个真没有。\n我头上是有耳朵，身后可什么都没长。”\n'
                  '$“换个方式吧，想说什么，我听着。”')
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_expected_record"):
            DialogueStream().feed(source)

    def test_no_plain_markdown_or_legacy_jsonl_fallback(self):
        for source in ('你好。', '```json\n' + record("你好。") + '\n```',
                       '$' + record("你好。"), '[' + record("你好。") + ']',
                       '{"speech":"你好。"}', '{"speech":"第一句。"}\n{"speech":"第二句。"}'):
            with self.subTest(source=source):
                with self.assertRaises(DialogueFormatError):
                    self.parse([source])

    def test_unique_speech_key_and_nonempty_string_array(self):
        for source in ('{}', '{"speech":null}', '{"speech":12}', '{"speech":true}',
                       '{"speech":[]}', '{"speech":[null]}', '{"speech":[1]}',
                       '{"speech":[true]}', '{"speech":[{"speech":"你好。"}]}',
                       '{"speech":[["你好。"]]}', '{"speech":["你好。"],"action":"抬眼"}',
                       '{"speech":["你好。"],"speech":["重复。"]}',
                       '{"other":["你好。"]}', '{speech:["你好。"]}',
                       '{"speech":["你好。",]}', '{"speech":["你好。"] ,}'):
            with self.subTest(source=source):
                with self.assertRaises(DialogueFormatError):
                    self.parse([source])

    def test_invalid_later_item_after_valid_prefix(self):
        parser = DialogueStream()
        self.assertEqual(parser.feed('{"speech":["第一句。",'), ("第一句。",))
        with self.assertRaises(DialogueFormatError):
            parser.feed('{"action":"抬眼"}]}')
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_stream_failed"):
            parser.finish()

    def test_empty_cleaned_body(self):
        for text in ("", " \r\n ", "$", '“$”', '""'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(DialogueFormatError, "dialogue_empty_speech"):
                    self.parse([record(text)])

    def test_stage_delimiters_rejected_not_deleted(self):
        for text in ('（抬眼看你）你好。', '你好。（笑）', '*笑*你好。',
                     '你好。*抬眼*', '[smiles]你好。', '你好。【笑】', '明天（周一）再聊。'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(DialogueFormatError, "dialogue_stage_delimiter"):
                    self.parse([record(text)])

    def test_no_false_claim_of_semantic_narration_detection(self):
        text = "我顿了一下，抬眼看你。"
        self.assertEqual(self.parse([record(text)]), [text])

    def test_all_truncated_states_fail_without_flushing(self):
        for source in ('{', '{"spe', '{"speech"', '{"speech":', '{"speech":[',
                       '{"speech":["你好', '{"speech":["你好\\', '{"speech":["你好。"'):
            with self.subTest(source=source):
                parser = DialogueStream()
                self.assertEqual(parser.feed(source), ())
                with self.assertRaisesRegex(DialogueFormatError, "dialogue_truncated_record"):
                    parser.finish()
        parser = DialogueStream()
        self.assertEqual(parser.feed('{"speech":["第一句。"]'), ("第一句。",))
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_truncated_record"):
            parser.finish()

    def test_trailing_content_and_second_object(self):
        for suffix in (record("第二句。"), "旁白。", ",", "\u00a0"):
            with self.subTest(suffix=suffix):
                parser = DialogueStream()
                self.assertEqual(parser.feed(record("第一句。")), ("第一句。",))
                with self.assertRaises(DialogueFormatError):
                    parser.feed(suffix)

    def test_record_bound_counts_raw_escaped_string(self):
        size = len(json.dumps("你好。", ensure_ascii=False))
        self.assertEqual(self.parse([record("你好。")], max_record_characters=size), ["你好。"])
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_record_limit"):
            self.parse(list(record("你好。")), max_record_characters=size - 1)
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_record_limit"):
            self.parse(['{"speech":["\\u4f60"]}'], max_record_characters=7)

    def test_decoded_speech_length_bounded(self):
        self.assertEqual(self.parse([record("a" * 120)]), ["a" * 120])
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_speech_limit"):
            self.parse([record("a" * 121)])

    def test_total_bound_includes_syntax_whitespace_and_items(self):
        source = record("你好。", "再见。")
        self.assertEqual(self.parse([source], max_total_characters=len(source)), ["你好。", "再见。"])
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_total_limit"):
            self.parse(list(source), max_total_characters=len(source) - 1)
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_total_limit"):
            self.parse([" " * 51], max_total_characters=50)

    def test_failure_terminal_and_errors_do_not_echo_content(self):
        parser = DialogueStream()
        with self.assertRaises(DialogueFormatError) as caught:
            parser.feed("PRIVATE_CONTENT")
        self.assertNotIn("PRIVATE_CONTENT", str(caught.exception))
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_stream_failed"):
            parser.feed(record("不能恢复。"))
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_stream_failed"):
            parser.finish()

    def test_finish_idempotent_and_no_more_input(self):
        parser = DialogueStream()
        parser.feed(record("你好。"))
        parser.finish()
        parser.finish()
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_stream_finished"):
            parser.feed(" ")

    def test_empty_reply_and_invalid_input_type(self):
        parser = DialogueStream()
        self.assertEqual(parser.feed(""), ())
        self.assertEqual(parser.feed(" \n"), ())
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_empty_output"):
            parser.finish()
        with self.assertRaisesRegex(DialogueFormatError, "dialogue_invalid_delta"):
            DialogueStream().feed(None)

    def test_invalid_limits_escapes_and_characters(self):
        for value in (0, -1, True, 1.5, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(DialogueFormatError, "invalid_dialogue_limits"):
                    DialogueStream(max_record_characters=value)
                with self.assertRaisesRegex(DialogueFormatError, "invalid_dialogue_limits"):
                    DialogueStream(max_total_characters=value)
        for source in ('{"speech":["\\u0000"]}', '{"speech":["\\ud800"]}'):
            with self.assertRaisesRegex(DialogueFormatError, "dialogue_invalid_character"):
                self.parse([source])
        for source in ('{"speech":["\\q"]}', '{"speech":["a\nb"]}'):
            with self.assertRaisesRegex(DialogueFormatError, "dialogue_invalid_json"):
                self.parse([source])


if __name__ == "__main__":
    unittest.main()
