"""Behavioral tests for the disconnected, synchronous text buffer."""

from pathlib import Path
import sys
import unittest

import regex

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.segment_buffer import TextSegmenter


class SegmentBufferTests(unittest.TestCase):
    def test_slow_unpunctuated_stream_keeps_first_deadline(self):
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed("甲", 0), [])
        self.assertEqual(buffer.feed("乙", 0.3), [])
        self.assertEqual(buffer.feed("丙", 0.7), [])
        self.assertEqual(buffer.next_due, 0.8)
        self.assertEqual(buffer.poll(0.799), [])
        self.assertEqual(buffer.poll(0.8), ["甲乙丙"])
        self.assertIsNone(buffer.next_due)
        self.assertEqual(buffer.poll(0.8), [])

    def test_arrivals_survive_length_cut_without_resetting_remainder(self):
        buffer = TextSegmenter(max_characters=3)
        self.assertEqual(buffer.feed("甲乙", 0), [])
        self.assertEqual(buffer.feed("丙丁", 0.7), ["甲乙丙"])
        self.assertAlmostEqual(buffer.next_due, 1.5)
        self.assertEqual(buffer.poll(1.49), [])
        self.assertEqual(buffer.poll(1.5), ["丁"])

    def test_sentence_remainder_uses_its_real_arrival(self):
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed("第一句。剩余", 1), ["第一句。"])
        self.assertEqual(buffer.feed("文字", 1.7), [])
        self.assertEqual(buffer.poll(1.8), ["剩余文字"])

    def test_sentence_marks_and_quotes_preserve_layout(self):
        text = '他说：“你好？！”  她答：「好。」\n继续'
        buffer = TextSegmenter()
        emitted = buffer.feed(text, 0)
        self.assertEqual(emitted, ['他说：“你好？！”  ', '她答：「好。」\n'])
        emitted += buffer.finish()
        self.assertEqual("".join(emitted), text)
        self.assertEqual(emitted[-1], "继续")

    def test_comma_colon_and_single_newline_do_not_force_fragments(self):
        buffer = TextSegmenter()
        text = "甲：你好，\n乙也在这里"
        self.assertEqual(buffer.feed(text, 0), [])
        self.assertEqual(buffer.finish(), [text])

    def test_blank_lines_and_split_crlf_are_paragraphs(self):
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed("第一段\r", 0), [])
        self.assertEqual(buffer.feed("\n\r", 0.1), [])
        self.assertEqual(buffer.feed("\n第二段", 0.2), ["第一段\r\n\r\n"])
        self.assertEqual(buffer.finish(), ["第二段"])
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed("甲\n \n乙\u2029丙", 0), ["甲\n \n", "乙\u2029"])
        self.assertEqual(buffer.finish(), ["丙"])

    def test_decimal_version_ip_and_url_points_are_not_sentence_breaks(self):
        examples = ["价格 3.14 元", "v4.29.1", "127.0.0.1", "https://example.com/a?x=1.2", "www.example.com", "a.b@example.com"]
        for text in examples:
            with self.subTest(text=text):
                buffer = TextSegmenter()
                for char in text:
                    self.assertEqual(buffer.feed(char, 0), [])
                self.assertEqual(buffer.finish(), [text])

    def test_ascii_period_needs_context_but_chinese_sentence_is_eager(self):
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed("Hello.", 0), [])
        self.assertEqual(buffer.feed('” Next', 0.1), ['Hello.” '])
        self.assertEqual(buffer.finish(), ["Next"])
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed("你好。", 0), ["你好。"])

    def test_long_text_exact_thresholds_and_no_duplicate_or_missing_text(self):
        for size in [119, 120, 121, 240, 241, 1001]:
            with self.subTest(size=size):
                text = "字" * size
                buffer = TextSegmenter()
                segments = buffer.feed(text, 0)
                self.assertEqual(len(segments), size // 120)
                segments += buffer.finish()
                self.assertEqual("".join(segments), text)
                self.assertTrue(all(len(regex.findall(r"\X", part)) <= 120 for part in segments))

    def test_whitespace_does_not_count_or_restart_a_content_deadline(self):
        buffer = TextSegmenter(max_characters=3)
        self.assertEqual(buffer.feed(" \t\n", 0), [])
        self.assertIsNone(buffer.next_due)
        self.assertEqual(buffer.feed("甲 乙", 10), [])
        self.assertEqual(buffer.feed("\n", 10.7), [])
        self.assertEqual(buffer.next_due, 10.8)
        self.assertEqual(buffer.feed(" 丙 ", 10.8), [" \t\n甲 乙\n 丙 "])
        self.assertEqual(buffer.finish(), [])

    def test_only_isolated_final_whitespace_is_discarded(self):
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed("正文。", 0), ["正文。"])
        self.assertEqual(buffer.feed(" \n\t", 0.1), [])
        self.assertEqual(buffer.poll(9), [])
        self.assertEqual(buffer.finish(), [])
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed(" \n\t", 0), [])
        self.assertEqual(buffer.finish(), [])

    def test_buffered_unicode_clusters_are_not_split(self):
        clusters = ["e\u0301", "👍🏽", "👨‍👩‍👧‍👦", "🇨🇳", "1️⃣", "한", "क्ष"]
        for cluster in clusters:
            with self.subTest(cluster=cluster):
                buffer = TextSegmenter(max_characters=2)
                for codepoint in cluster:
                    self.assertEqual(buffer.feed(codepoint, 0), [])
                self.assertEqual(buffer.feed("甲", 0), [cluster + "甲"])
                self.assertEqual(buffer.finish(), [])

    def test_eager_boundary_cannot_reattach_future_combining_mark(self):
        buffer = TextSegmenter(max_characters=1)
        first = buffer.feed("e", 0)
        later = buffer.feed("\u0301", 0.1)
        self.assertEqual(first, ["e"])
        self.assertEqual("".join(first + later + buffer.finish()), "e\u0301")

    def test_chunk_partitions_preserve_text_and_segment_order(self):
        text = '开头 e\u0301 和👩‍💻。接着 3.14 与 https://x.example/a?x=1.2\n\n结尾'
        for width in range(1, len(text) + 1):
            buffer = TextSegmenter(max_characters=9)
            emitted = []
            for start in range(0, len(text), width):
                emitted += buffer.feed(text[start:start + width], 0)
            emitted += buffer.finish()
            self.assertEqual("".join(emitted), text)
            self.assertTrue(all(part.strip() for part in emitted))
            self.assertTrue(all(sum(not c.isspace() for c in regex.findall(r"\X", part)) <= 9 for part in emitted))

    def test_finish_is_idempotent_and_terminal(self):
        buffer = TextSegmenter()
        self.assertEqual(buffer.feed("已有内容", 0), [])
        self.assertEqual(buffer.finish(), ["已有内容"])
        self.assertEqual(buffer.finish(), [])
        self.assertEqual(buffer.poll(1), [])
        self.assertIsNone(buffer.next_due)
        with self.assertRaisesRegex(ValueError, "finished"):
            buffer.feed("迟到文字", 1)

    def test_invalid_inputs_and_clock_changes_do_not_mutate_buffer(self):
        buffer = TextSegmenter()
        buffer.feed("已有", 1)
        for now in [-1, 0.5, float("nan"), float("inf"), True, "1"]:
            with self.subTest(now=now), self.assertRaises((TypeError, ValueError)):
                buffer.feed("不得进入", now)
            self.assertEqual(buffer.next_due, 1.8)
        with self.assertRaises(TypeError):
            buffer.feed(None, 9)
        self.assertEqual(buffer.feed("文字", 1), [])
        self.assertEqual(buffer.poll(1.8), ["已有文字"])
        for now in [False, -1, float("nan")]:
            with self.assertRaises((TypeError, ValueError)):
                buffer.poll(now)

    def test_invalid_configuration(self):
        for size in [True, 0, -1, 1.5, "120"]:
            with self.assertRaises((TypeError, ValueError)):
                TextSegmenter(max_characters=size)
        for wait in [True, 0, -1, float("nan"), float("inf"), "0.8"]:
            with self.assertRaises((TypeError, ValueError)):
                TextSegmenter(max_wait_seconds=wait)


if __name__ == "__main__":
    unittest.main()
