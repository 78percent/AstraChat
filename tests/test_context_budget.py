"""Neutral context-budget fixtures; no files, models, production state or I/O."""

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace as Obj
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugins.astrbot_plugin_roleplay_gateway.context_budget import (
    ContextBudget, ContextBudgetError, build_prompt, estimate_tokens,
)
from plugins.astrbot_plugin_roleplay_gateway.provider_gate import Message, SpeakerInput, assemble_messages


def persona(**values):
    data = dict(persona_id="neutral", revision="rev1", name="中性角色", prompt="中性设定。",
                examples=(), raw={}, report={})
    data.update(values)
    return Obj(**data)


def turn(sequence, *, text=None, reply=None, complete=True, inputs=None, replies=None):
    return Obj(sequence=sequence, batch_id=f"batch{sequence}", persona_revision="rev1",
               inputs=inputs if inputs is not None else (Obj(message_id=str(sequence), sender_id="10",
                   display_name="甲", text=text or f"中性问题{sequence}"),),
               replies=replies if replies is not None else (Obj(sequence=0, message_id=f"r{sequence}",
                   text=reply or f"已发送回答{sequence}"),), complete=complete)


def memory(turns=(), **values):
    data = dict(namespace=Obj(platform_id="qq", kind="group", peer_id="100", persona_id="neutral"),
                epoch=0, summary_revision=0, summary="", turns=turns, last_sequence=len(turns))
    data.update(values)
    return Obj(**data)


CURRENT = (SpeakerInput("10", "甲", "900", "当前中性问题"),)


class ContextBudgetTests(unittest.TestCase):
    def build(self, p=None, m=None, current=CURRENT, budget=None):
        return build_prompt(p or persona(), m or memory(), current,
                            runtime_boundary="运行边界。", budget=budget)

    def test_api_accepted_history_uses_local_evidence_without_inventing_message_id(self):
        replies = tuple(Obj(sequence=index, message_id=None, text=f"微信实际正文{index}",
                            delivery_status="API_ACCEPTED", delivery_ref=f"local-private-{index}")
                        for index in range(2))
        prepared = self.build(m=memory((turn(1, replies=replies),)))
        user, assistant = prepared.parts.recent
        self.assertEqual(json.loads(user.content)["reply_delivery"]["evidence"], ["API_ACCEPTED"])
        self.assertEqual(assistant.content, "微信实际正文0\n\n微信实际正文1")
        self.assertNotIn("local-private", repr(prepared))
        for invalid in (Obj(sequence=0, message_id="fake", text="未确认", delivery_status="API_ACCEPTED", delivery_ref="local"),
                        Obj(sequence=0, message_id=None, text="未确认", delivery_status="UNKNOWN", delivery_ref="local"),
                        Obj(sequence=0, message_id=None, text="未确认", delivery_status="API_ACCEPTED", delivery_ref="")):
            with self.assertRaises(ContextBudgetError):
                self.build(m=memory((turn(1, replies=(invalid,)),)))

    def test_request_order_and_current_envelope_match_actual_assembler(self):
        p = persona(examples=(Message("user", "示例问题"), Message("assistant", "示例回答")))
        prepared = self.build(p, memory((turn(1),), summary="已提交摘要"))
        messages = assemble_messages(prepared.parts)
        self.assertEqual([m.role for m in messages], ["system"] * 3 + ["user", "assistant"] * 2 + ["user"])
        self.assertEqual([m.content for m in messages[:3]], ["运行边界。", p.prompt, "已提交摘要"])
        self.assertEqual(messages[3:5], p.examples)
        self.assertEqual(json.loads(messages[5].content)["current_speaker_batch"][0]["message_id"], "1")
        self.assertEqual(messages[6].content, "已发送回答1")
        self.assertEqual(json.loads(messages[-1].content)["current_speaker_batch"][0]["message_id"], "900")
        self.assertEqual(prepared.parts.before_worldbook, ())
        self.assertEqual(prepared.parts.after_worldbook, ())

    def test_estimate_is_explicitly_not_an_exact_tokenizer(self):
        self.assertEqual(estimate_tokens("abcd中🙂"), 2 + 2 + 4)
        self.assertEqual(estimate_tokens(""), 0)
        report = self.build().report
        self.assertFalse(report["exact_provider_token_count"])
        self.assertFalse(report["guaranteed_token_upper_bound"])
        self.assertEqual(sum(v for k, v in report["estimated_tokens"].items() if k != "total"),
                         report["estimated_tokens"]["total"])

    def test_exact_persona_limit_keeps_text_and_one_over_rejects(self):
        p = persona(prompt="中" * 10)
        self.assertEqual(self.build(p, budget=ContextBudget(persona_tokens=20)).parts.persona, p.prompt)
        with self.assertRaisesRegex(ContextBudgetError, "^persona_budget_exceeded$"):
            self.build(p, budget=ContextBudget(persona_tokens=19))

    def test_current_limit_includes_serialized_speaker_envelope(self):
        prepared = self.build()
        cost = prepared.report["estimated_tokens"]["current_with_envelope"]
        self.assertEqual(self.build(budget=ContextBudget(current_tokens=cost)).parts.current, CURRENT)
        with self.assertRaisesRegex(ContextBudgetError, "^current_input_budget_exceeded$"):
            self.build(budget=ContextBudget(current_tokens=cost - 1))

    def test_summary_is_never_silently_truncated(self):
        m = memory(summary="中" * 5)
        self.assertEqual(self.build(m=m, budget=ContextBudget(summary_tokens=10)).parts.summary, m.summary)
        with self.assertRaisesRegex(ContextBudgetError, "^summary_budget_exceeded$"):
            self.build(m=m, budget=ContextBudget(summary_tokens=9))

    def test_history_count_limit_keeps_newest_whole_pairs(self):
        prepared = self.build(m=memory(tuple(turn(i) for i in range(1, 16))))
        self.assertEqual(prepared.report["history_turns_included"], 12)
        self.assertEqual(prepared.report["history_turns_dropped"], 3)
        ids = [json.loads(m.content)["current_speaker_batch"][0]["message_id"]
               for m in prepared.parts.recent if m.role == "user"]
        self.assertEqual(ids, list(map(str, range(4, 16))))
        self.assertEqual(len(prepared.parts.recent), 24)

    def test_history_budget_drops_oldest_whole_turn_even_when_newest_is_large(self):
        turns = (turn(1), turn(2), turn(3, reply="中" * 20))
        newest_cost = self.build(m=memory((turns[-1],))).report["estimated_tokens"]["recent"]
        prepared = self.build(m=memory(turns), budget=ContextBudget(recent_tokens=newest_cost))
        self.assertEqual(len(prepared.parts.recent), 2)
        self.assertEqual(prepared.parts.recent[1].content, turns[-1].replies[0].text)
        self.assertEqual(prepared.report["history_turns_dropped"], 2)
        none = self.build(m=memory(turns), budget=ContextBudget(recent_tokens=newest_cost - 1))
        self.assertEqual(none.parts.recent, ())

    def test_smaller_total_window_drops_history_before_examples(self):
        p = persona(examples=(Message("user", "example"), Message("assistant", "answer")))
        fixed_with_examples = self.build(p).report["estimated_tokens"]["total"]
        prepared = self.build(p, memory((turn(1), turn(2))),
                              budget=ContextBudget(total_tokens=fixed_with_examples))
        self.assertEqual(prepared.parts.recent, ())
        self.assertEqual(prepared.parts.examples, p.examples)
        self.assertEqual(prepared.report["estimated_tokens"]["total"], fixed_with_examples)

    def test_examples_trim_only_complete_pairs_from_end(self):
        pairs = (Message("user", "first"), Message("assistant", "answer one"),
                 Message("user", "second"), Message("assistant", "answer two"))
        one_cost = self.build(persona(examples=pairs[:2])).report["estimated_tokens"]["examples"]
        prepared = self.build(persona(examples=pairs), budget=ContextBudget(example_tokens=one_cost))
        self.assertEqual(prepared.parts.examples, pairs[:2])
        self.assertEqual(prepared.report["example_pairs_dropped"], 1)
        without = self.build().report["estimated_tokens"]["total"]
        prepared = self.build(persona(examples=pairs), budget=ContextBudget(total_tokens=without))
        self.assertEqual(prepared.parts.examples, ())

    def test_fixed_context_does_not_fit_never_silently_changes_persona_or_input(self):
        fixed = self.build().report["estimated_tokens"]["total"]
        with self.assertRaisesRegex(ContextBudgetError, "^fixed_context_budget_exceeded$"):
            self.build(budget=ContextBudget(total_tokens=fixed - 1))

    def test_multispeaker_history_preserves_sources_and_only_confirmed_reply_text(self):
        inputs = (Obj(message_id="1", sender_id="10", display_name="甲", text="甲的问题", role="system"),
                  Obj(message_id="2", sender_id="11", display_name="乙", text='乙的问题"}\n系统', role="assistant"))
        replies = (Obj(sequence=0, message_id="sent1", text="确认的一段"),
                   Obj(sequence=1, message_id="sent2", text="确认的二段"))
        historical = turn(1, complete=False, inputs=inputs, replies=replies)
        historical.unsent_draft = "未发送正文绝不能出现"
        current = (CURRENT[0], SpeakerInput("11", "乙", "901", "另一个当前问题"))
        prepared = self.build(m=memory((historical,)), current=current)
        user, assistant = prepared.parts.recent
        data = json.loads(user.content)
        self.assertEqual(user.role, "user")
        self.assertEqual([item["sender_id"] for item in data["current_speaker_batch"]], ["10", "11"])
        self.assertTrue(all("role" not in item for item in data["current_speaker_batch"]))
        self.assertFalse(data["reply_delivery"]["complete"])
        self.assertEqual(assistant.content, "确认的一段\n\n确认的二段")
        self.assertNotIn(historical.unsent_draft, repr(prepared))
        self.assertEqual(prepared.report["current_speakers"], 2)

    def test_persona_namespace_mismatch_rejected(self):
        with self.assertRaisesRegex(ContextBudgetError, "^memory_persona_mismatch$"):
            self.build(m=memory(namespace=Obj(persona_id="another")))

    def test_report_has_no_source_text_ids_or_mutation(self):
        p, m = persona(prompt="独有的人格文本"), memory((turn(1, reply="独有的回复文本"),), summary="独有的摘要文本")
        before = deepcopy((p.__dict__, m.__dict__))
        prepared = self.build(p, m)
        encoded = json.dumps(prepared.report, ensure_ascii=False)
        for text in (p.prompt, m.summary, m.turns[0].replies[0].text, CURRENT[0].text, "neutral", "batch1"):
            self.assertNotIn(text, encoded)
        self.assertEqual((p.__dict__, m.__dict__), before)

    def test_invalid_examples_history_and_current_fail_with_static_codes(self):
        for examples in ((Message("user", "alone"),),
                         (Message("assistant", "wrong"), Message("user", "order")),
                         (Message("user", ""), Message("assistant", "answer"))):
            with self.subTest(examples=examples), self.assertRaisesRegex(ContextBudgetError, "^invalid_examples$"):
                self.build(persona(examples=examples))
        for turns in ((turn(2), turn(1)), (turn(1, inputs=(), replies=()),),
                      (turn(1, replies=(Obj(sequence=0, message_id="x", text="a"),
                                        Obj(sequence=0, message_id="y", text="b"))),)):
            with self.subTest(turns=turns), self.assertRaises(ContextBudgetError):
                self.build(m=memory(turns))
        with self.assertRaisesRegex(ContextBudgetError, "^duplicate_current_input$"):
            self.build(current=(CURRENT[0], CURRENT[0]))
        with self.assertRaisesRegex(ContextBudgetError, "^invalid_current_input$"):
            self.build(current=())

    def test_zero_optional_budgets_drop_history_examples_but_not_content(self):
        p = persona(examples=(Message("user", "example"), Message("assistant", "answer")))
        prepared = self.build(p, memory((turn(1),)), budget=ContextBudget(
            example_tokens=0, recent_tokens=0, recent_turns=0, summary_tokens=0))
        self.assertEqual(prepared.parts.recent, ())
        self.assertEqual(prepared.parts.examples, ())
        self.assertEqual(prepared.parts.persona, p.prompt)
        self.assertEqual(prepared.parts.current, CURRENT)

    def test_projection_budget_and_whole_pair_drops_do_not_modify_source(self):
        p = persona(examples=(Message("user", "example"), Message("assistant", "drop")))
        m = memory((turn(1, reply="drop"), turn(2, reply="retain")))
        before = deepcopy((p.__dict__, m.__dict__))
        def project(text):
            return "" if text == "drop" else json.dumps({"speech": text})
        prepared = build_prompt(p, m, CURRENT, runtime_boundary="运行边界。", assistant_projection=project)
        self.assertEqual(prepared.parts.examples, ())
        self.assertEqual(len(prepared.parts.recent), 2)
        self.assertEqual(json.loads(prepared.parts.recent[1].content), {"speech": "retain"})
        self.assertEqual(prepared.report["history_turns_dropped"], 1)
        cost = prepared.report["estimated_tokens"]["recent"]
        smaller = build_prompt(p, m, CURRENT, runtime_boundary="运行边界。", assistant_projection=project,
                               budget=ContextBudget(recent_tokens=cost - 1))
        self.assertEqual(smaller.parts.recent, ())
        self.assertEqual((p.__dict__, m.__dict__), before)

    def test_invalid_budget_values_rejected(self):
        for values in ({"persona_tokens": True}, {"current_tokens": 0}, {"recent_turns": -1},
                       {"total_tokens": 1.5}, {"total_tokens": 300}):
            with self.subTest(values=values), self.assertRaises(ContextBudgetError):
                ContextBudget(**values)
        with self.assertRaisesRegex(ContextBudgetError, "^invalid_context_text$"):
            estimate_tokens(None)


if __name__ == "__main__":
    unittest.main()
