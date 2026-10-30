"""Read-only legacy history projection using nonsexual regression fixtures."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.astrbot_plugin_roleplay_gateway.dialogue_history import encode_dialogue_history


def speeches(text):
    encoded = encode_dialogue_history(text)
    return json.loads(encoded)["speech"] if encoded else []


class DialogueHistoryTests(unittest.TestCase):
    def test_real_failed_reply_drops_unquoted_narration(self):
        original = (
            '“累了就坐下来歇会儿，我陪你聊。”\n'
            '$我顿了一下，抬眼看你。\n'
            '$“摸摸尾巴——这个真没有。\n'
            '我头上是有耳朵，身后可什么都没长。”\n'
            '$“换个方式吧，想说什么，我听着。”'
        )
        self.assertEqual(speeches(original), [
            "累了就坐下来歇会儿，我陪你聊。", "摸摸尾巴——这个真没有。",
            "我头上是有耳朵，身后可什么都没长。", "换个方式吧，想说什么，我听着。",
        ])
        self.assertIn("$我顿了一下", original)  # Immutable source remains intact.

    def test_cross_bubble_quotes_and_parenthesized_stage(self):
        source = '“明天的散步啊……\n\n代号就定‘三号月见草’吧。”$（认真地点头）$“有正事吗？”'
        self.assertEqual(speeches(source), ["明天的散步啊……", "代号就定‘三号月见草’吧。", "有正事吗？"])

    def test_plain_inline_quotes_and_parenthetical_explanation_are_kept(self):
        for source in ('他说“下雨了”是什么意思？', '这个概率（例如百分之五十）并不代表一定发生。', '报价是 $12。', '报价是 $12，名字是“蓝天”。'):
            with self.subTest(source=source):
                self.assertEqual(speeches(source), [source])

    def test_leading_quoted_terms_do_not_lose_connecting_facts(self):
        for source in (
            '“勇气”就是“不怕”', '“青柚灯塔”才是明天散步的代号。',
            '"rain" 的中文是 "雨"。', '“你好。”这句话表示问候。',
            '（举例）“勇气”就是“不怕”',
        ):
            with self.subTest(source=source):
                self.assertEqual(''.join(speeches(source)), source)

    def test_complete_quoted_records_still_project_without_stage_guessing(self):
        self.assertEqual(speeches('“你好。”\n\n“明天见。”'), ['你好。', '明天见。'])
        self.assertEqual(speeches('“你好。”$她点头。'), ['你好。'])
        # A leading parenthesis followed by unquoted words is ambiguous. Keep
        # it rather than guessing whether it is stage direction or explanation.
        self.assertEqual(speeches('（她点头）明天见。'), ['（她点头）明天见。'])

    def test_escaped_ascii_quote_and_curly_nested_quote(self):
        self.assertEqual(speeches('"请记住 \\"蓝天\\"。"$她点头。'), ['请记住 "蓝天"。'])
        self.assertEqual(speeches('“请记住“蓝天”这个名字。”'), ['请记住“蓝天”这个名字。'])

    def test_stage_only_or_truncated_wrapper_is_omitted(self):
        for source in ('', ' \n ', '$我顿了一下，抬眼看你。', '（她点了点头）', '*她点了点头*', '**她点了点头**', '“尚未闭合的台词', '“完整。”$“未完'):
            with self.subTest(source=source):
                self.assertEqual(encode_dialogue_history(source), '')

    def test_leading_stage_with_speech_keeps_only_quoted_words(self):
        self.assertEqual(speeches('（她点头）“明天见。”'), ['明天见。'])
        self.assertEqual(speeches('*点头* $"明天见。"'), ['明天见。'])

    def test_plain_action_words_are_not_keyword_filtered(self):
        source = '你可以抬眼看看窗外，也可以站起来走走。'
        self.assertEqual(speeches(source), [source])

    def test_short_records_preserve_content_and_grapheme_clusters(self):
        source = '甲' * 59 + '👩\u200d💻' + '乙' * 70
        parts = speeches(source)
        self.assertEqual(''.join(parts), source)
        self.assertEqual(parts[0], '甲' * 59 + '👩\u200d💻')
        self.assertEqual(len(parts), 3)
        encoded = json.loads(encode_dialogue_history(source))
        self.assertEqual(set(encoded), {'speech'})
        self.assertIsInstance(encoded['speech'], list)
        self.assertTrue(all(isinstance(part, str) for part in encoded['speech']))

    def test_input_shape_error_has_static_message(self):
        with self.assertRaisesRegex(TypeError, '^invalid_dialogue_history$'):
            encode_dialogue_history(None)


if __name__ == '__main__':
    unittest.main()
