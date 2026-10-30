"""QQ display regressions using neutral strings, without model/network calls."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.astrbot_plugin_roleplay_gateway.qq_text import format_qq_bubble
from plugins.astrbot_plugin_roleplay_gateway.segment_buffer import TextSegmenter


class QqTextTests(unittest.TestCase):
    def test_late_paragraph_separator_has_no_blank_bubble_header(self):
        buffer = TextSegmenter()
        raw = []
        for i, chunk in enumerate(("第一句。", "\n\n第二句。", "\n\n第三句。")):
            raw.extend(buffer.feed(chunk, i * 0.1))
        raw.extend(buffer.finish())
        self.assertEqual(raw, ["第一句。", "\n\n第二句。", "\n\n第三句。"])
        self.assertEqual(list(map(format_qq_bubble, raw)), ["第一句。", "第二句。", "第三句。"])

    def test_same_text_chunked_every_way_keeps_same_visible_sentences(self):
        text = "第一句。\r\n\r\n第二句。\r\n\r\n第三句。"
        for width in range(1, len(text) + 1):
            buffer = TextSegmenter()
            raw = []
            for start in range(0, len(text), width):
                raw.extend(buffer.feed(text[start:start + width], 0))
            raw.extend(buffer.finish())
            self.assertEqual(list(map(format_qq_bubble, raw)), ["第一句。", "第二句。", "第三句。"])

    def test_indent_inline_spaces_and_trailing_spaces_remain(self):
        body = "  缩进的正文  中间空格  "
        self.assertEqual(format_qq_bubble(" \t\r\n\n" + body + "\n \t\n"), body)
        self.assertEqual(format_qq_bubble(body), body)

    def test_internal_paragraphs_and_code_layout_remain(self):
        body = "标题\n\n    第一行\n    第二行\n\n```\n    code\n```"
        self.assertEqual(format_qq_bubble("\n\n" + body + "\n\n"), body)

    def test_unicode_line_separators_only_at_edges(self):
        body = "甲\u2028乙\u2029丙"
        self.assertEqual(format_qq_bubble("\u2028\u2029" + body + "\u2029"), body)

    def test_empty_and_no_linebreak_text_is_unchanged(self):
        for text in ("", " \t", "正常内容。", "  缩进", "正文  "):
            self.assertEqual(format_qq_bubble(text), text)


if __name__ == "__main__":
    unittest.main()
