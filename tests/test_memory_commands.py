"""Static commands, permissions, persistence and lifecycle; synthetic data only."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, patch

import test_roleplay_stream as fixture
from plugins.astrbot_plugin_roleplay_gateway.gate import DefiniteSendFailure
from plugins.astrbot_plugin_roleplay_gateway.memory_commands import (
    CommandError, DENIED, FAILED, is_memory_command, parse_command,
)
from plugins.astrbot_plugin_roleplay_gateway.memory_store import MemoryInput, MemoryReply, Namespace


class MemoryCommandParserTests(unittest.TestCase):
    def test_prefix_is_anchored_and_malformed_reserved_prefix_is_owned(self):
        for text in ("/记忆 查看", "  /记忆 编辑 test", "/记忆", "/记忆查看"):
            self.assertTrue(is_memory_command(text))
        for text in (None, 3, "请解释 /记忆 查看", "ordinary", "/memory 查看"):
            self.assertFalse(is_memory_command(text))
        with self.assertRaises(CommandError):
            parse_command("/记忆查看")

    def test_actions_and_explicit_targets_are_exact(self):
        for text, action in (("查看", "view"), ("清空摘要", "clear_summary"), ("清空全部", "clear_all")):
            self.assertEqual(parse_command("/记忆 " + text).action, action)
        group = parse_command("/记忆 群 200 编辑 第一段\n第二段")
        self.assertEqual((group.kind, group.peer_id, group.action, group.text),
                         ("group", "200", "edit", "第一段\n第二段"))
        self.assertEqual(parse_command("/记忆 私聊 100 查看").kind, "private")

    def test_unknown_partial_or_malformed_commands_fail(self):
        for text in ("/记忆", "/记忆 重置", "/记忆 查看 extra", "/记忆 编辑", "/记忆 群 查看",
                     "/记忆 群 ２０0 查看", "/记忆 群 0200 查看", "/记忆 群 -200 查看",
                     "/记忆 私聊 0 清空全部", "/记忆 清空所有", "/记忆 编辑 a\0b"):
            with self.subTest(text=text), self.assertRaises(CommandError):
                parse_command(text)

    def test_edit_uses_actual_summary_budget_without_silent_clipping(self):
        text = "气" * 600
        self.assertEqual(parse_command("/记忆 编辑 " + text).text, text)
        with self.assertRaisesRegex(CommandError, "summary_budget_exceeded"):
            parse_command("/记忆 编辑 " + text + "气")
        self.assertEqual(len(parse_command("/记忆 编辑 " + "a" * 3600).text), 3600)
        with self.assertRaisesRegex(CommandError, "summary_budget_exceeded"):
            parse_command("/记忆 编辑 " + "a" * 3601)


class MemoryCommandTests(unittest.IsolatedAsyncioTestCase):
    open_stores = fixture.RoleplayStreamTests.open_stores
    factory = fixture.RoleplayStreamTests.factory
    make_service = fixture.RoleplayStreamTests.make_service
    asyncTearDown = fixture.RoleplayStreamTests.asyncTearDown
    send = fixture.RoleplayStreamTests.send
    dispatch = fixture.RoleplayStreamTests.dispatch
    restart = fixture.RoleplayStreamTests.restart
    payload = fixture.RoleplayStreamTests.payload

    async def asyncSetUp(self):
        await fixture.RoleplayStreamTests.asyncSetUp(self)
        self.settings = replace(self.settings, memory_admin_ids=frozenset({"101"}))
        self.service.settings = self.settings

    def event(self, incoming="1", text="/记忆 查看", *, sender="100", kind="private", group="200"):
        return fixture.fixture_event(incoming, sender=sender, kind=kind, group=group, text=text)

    async def seed(self, namespace, *, batch="seed", summary="合成摘要"):
        snapshot = await self.memory.snapshot(namespace)
        await self.memory.commit_turn(namespace, expected_epoch=snapshot.epoch,
                                      batch_id=batch, persona_revision="revision-one",
                                      inputs=(MemoryInput(batch, "100", "合成用户", "合成问题"),),
                                      replies=(MemoryReply(0, "800", "合成已发回复"),), complete=True)
        if summary:
            await self.memory.edit_summary(namespace, summary)

    async def test_private_self_edit_view_and_clear_never_call_model_or_record_commands(self):
        event = self.event(text="/记忆 编辑 合成天气摘要")
        await self.dispatch(event)
        await self.dispatch(self.event("2"))
        snapshot = await self.memory.snapshot(self.service.namespace(event))
        self.assertEqual(snapshot.summary, "合成天气摘要")
        self.assertEqual(snapshot.turns, ())
        self.assertIn("摘要已更新", self.outputs[0][1])
        self.assertIn("合成天气摘要", self.outputs[1][1])
        self.assertEqual(self.backend.calls, [])
        receipt = await self.receipts.get(event.platform_id, "1")
        self.assertEqual(receipt["status"], "SENT")
        self.assertTrue(receipt["reason"].startswith("memory_command_edit_completed_"))
        self.assertIsNotNone(receipt["outgoing_message_id"])

    async def test_nonadmin_cannot_target_other_private_or_group_or_manage_current_group(self):
        other = Namespace("fixture-qq", "private", "101", "neutral-guide")
        await self.seed(other, summary="另一会话秘密标记")
        for index, event in enumerate((
            self.event(text="/记忆 私聊 101 查看"), self.event(text="/记忆 群 200 清空全部"),
            self.event(text="/记忆 编辑 未授权变更", kind="group"),
            self.event(text="/记忆 查看", kind="group"),
        ), 1):
            await self.dispatch(replace(event, incoming_message_id=str(index)))
        self.assertTrue(all(text == DENIED for _, text in self.outputs))
        self.assertNotIn("另一会话秘密标记", repr(self.outputs))
        self.assertEqual((await self.memory.snapshot(other)).summary, "另一会话秘密标记")
        self.assertEqual(self.backend.calls, [])

    async def test_admin_private_target_uses_whitelist_and_replies_to_origin(self):
        captured = []
        async def sender(event, text):
            captured.append((event.kind, event.sender_id, event.group_id, text))
            return {"message_id": 1234 + len(captured)}
        event = self.event(text="/记忆 群 200 编辑 管理员群摘要", sender="101")
        await self.dispatch(event, sender=sender)
        group = Namespace("fixture-qq", "group", "200", "neutral-guide")
        self.assertEqual((await self.memory.snapshot(group)).summary, "管理员群摘要")
        self.assertEqual(captured[0][:3], ("private", "101", None))
        await self.dispatch(self.event("2", "/记忆 私聊 100 编辑 管理员私聊摘要", sender="101"))
        self.assertEqual((await self.memory.snapshot(Namespace("fixture-qq", "private", "100", "neutral-guide"))).summary,
                         "管理员私聊摘要")
        for index, target in enumerate(("群 999", "私聊 999"), 3):
            await self.dispatch(self.event(str(index), "/记忆 " + target + " 查看", sender="101"))
            self.assertEqual(self.outputs[-1][1], DENIED)

    async def test_admin_in_group_can_only_manage_current_group(self):
        await self.dispatch(self.event(text="/记忆 编辑 当前群摘要", sender="101", kind="group"))
        group = Namespace("fixture-qq", "group", "200", "neutral-guide")
        self.assertEqual((await self.memory.snapshot(group)).summary, "当前群摘要")
        for index, target in enumerate(("私聊 100", "群 201", "群 200"), 2):
            await self.dispatch(self.event(str(index), "/记忆 " + target + " 查看", sender="101", kind="group"))
            self.assertEqual(self.outputs[-1][1], DENIED)

    async def test_empty_admin_list_does_not_promote_private_allowlist(self):
        self.service.settings = replace(self.settings, memory_admin_ids=frozenset())
        await self.dispatch(self.event(text="/记忆 查看", sender="101", kind="group"))
        self.assertEqual(self.outputs[-1][1], DENIED)
        await self.dispatch(self.event("2", "/记忆 编辑 本人摘要", sender="101"))
        self.assertIn("摘要已更新", self.outputs[-1][1])

    async def test_all_ingress_boundaries_and_real_group_mention_are_required(self):
        base = self.event(kind="group", sender="101")
        cases = (replace(base, mentions=()), replace(base, mentions=("all",)),
                 replace(base, platform_id="other"), replace(base, bot_self_id="901"),
                 replace(base, sender_id="900"), replace(base, group_id="999"),
                 replace(base, post_type="notice"), self.event(sender="999"))
        for event in cases:
            self.assertEqual(await self.service.accept(event, self.send), "IGNORED")
        self.assertEqual(await self.service.accept(replace(base, incoming_message_id=None), self.send), "INVALID")
        self.enabled = False
        self.assertEqual(await self.service.accept(base, self.send), "IGNORED")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.outputs, [])

    async def test_oversize_edit_preserves_prior_summary_and_malformed_command_never_reaches_model(self):
        event = self.event()
        namespace = self.service.namespace(event)
        await self.memory.edit_summary(namespace, "保留摘要")
        await self.dispatch(replace(event, text="/记忆 编辑 " + "气" * 601))
        self.assertIn("未修改", self.outputs[-1][1])
        self.assertEqual((await self.memory.snapshot(namespace)).summary, "保留摘要")
        await self.dispatch(self.event("2", "/记忆查看"))
        self.assertIn("用法", self.outputs[-1][1])
        self.assertEqual(self.backend.calls, [])

    async def test_clear_summary_preserves_history_and_epoch_while_all_clear_invalidates(self):
        event = self.event(text="/记忆 清空摘要")
        namespace = self.service.namespace(event)
        await self.seed(namespace)
        before = await self.memory.snapshot(namespace)
        await self.dispatch(event)
        summary_only = await self.memory.snapshot(namespace)
        self.assertEqual(summary_only.summary, "")
        self.assertEqual(summary_only.epoch, before.epoch)
        self.assertEqual(summary_only.turns, before.turns)
        self.assertGreater(summary_only.summary_revision, before.summary_revision)
        with patch.object(self.service, "clear_memory", wraps=self.service.clear_memory) as clear:
            await self.dispatch(self.event("2", "/记忆 清空全部"))
            clear.assert_awaited_once_with(namespace)
        all_clear = await self.memory.snapshot(namespace)
        self.assertEqual(all_clear.turns, ())
        self.assertEqual(all_clear.epoch, before.epoch + 1)
        self.assertIn("近期对话保留", self.outputs[0][1])
        self.assertIn("摘要与近期对话已清空", self.outputs[1][1])

    async def test_redelivery_after_restart_does_not_clear_new_memory(self):
        event = self.event(text="/记忆 清空全部")
        namespace = self.service.namespace(event)
        await self.dispatch(event)
        await self.seed(namespace, batch="after-clear", summary="清空后新摘要")
        before = await self.memory.snapshot(namespace)
        await self.restart()
        self.assertEqual(await self.dispatch(event), "DUPLICATE")
        self.assertEqual(await self.memory.snapshot(namespace), before)
        self.assertEqual(len(self.outputs), 1)

    async def test_same_target_view_delivery_finishes_before_clear_confirmation(self):
        event = self.event()
        namespace = self.service.namespace(event)
        await self.memory.edit_summary(namespace, "旧摘要标记")
        entered, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        async def delayed_sender(item, text):
            if item.incoming_message_id == "1":
                entered.set()
                await release.wait()
            return await self.send(item, text)
        await self.service.accept(event, delayed_sender)
        await asyncio.wait_for(entered.wait(), 1)
        await self.service.accept(self.event("2", "/记忆 清空全部"), delayed_sender)
        await asyncio.sleep(.03)
        self.assertEqual((await self.memory.snapshot(namespace)).summary, "旧摘要标记")
        self.assertEqual(self.outputs, [])
        release.set()
        await asyncio.wait_for(self.service.wait_idle(), 2)
        self.assertEqual([item[0] for item in self.outputs], ["1", "2"])
        self.assertIn("旧摘要标记", self.outputs[0][1])
        self.assertNotIn("旧摘要标记", self.outputs[1][1])

    async def test_all_memory_failures_send_fixed_failure_not_success(self):
        cases = (("查看", self.memory, "snapshot"), ("编辑 内容", self.memory, "edit_summary"),
                 ("清空摘要", self.memory, "clear_summary"), ("清空全部", self.service, "clear_memory"))
        for index, (action, owner, method) in enumerate(cases, 1):
            with patch.object(owner, method, AsyncMock(side_effect=RuntimeError("private synthetic exception text"))):
                await self.dispatch(self.event(str(index), "/记忆 " + action))
            self.assertEqual(self.outputs[-1][1], FAILED)
        self.assertNotIn("private synthetic exception text", repr(self.logs) + repr(self.outputs))
        self.assertEqual(self.backend.calls, [])

    async def test_missing_or_rejected_delivery_is_never_recorded_as_sent(self):
        async def rejected(*args):
            raise DefiniteSendFailure()
        async def no_receipt(*args):
            return {"message_id": None}
        for index, sender, status in (("1", rejected, "FAILED"), ("2", no_receipt, "UNKNOWN")):
            await self.dispatch(self.event(index, "/记忆 编辑 成功落库"), sender=sender)
            row = await self.receipts.get("fixture-qq", index)
            self.assertEqual(row["status"], status)
            self.assertIsNone(row["outgoing_message_id"])

    async def test_clear_command_does_not_wait_for_running_or_queued_roleplay(self):
        blocked = fixture.reply_stream("清空前旧模型回复", block=True)
        self.backend.streams.append(blocked)
        self.release_events.append(blocked.release)
        event = fixture.fixture_event("10")
        await self.service.accept(event, self.send)
        await asyncio.wait_for(blocked.entered.wait(), 1)
        await self.service.accept(fixture.fixture_event("11"), self.send)
        await self.service.accept(self.event("12", "/记忆 清空全部", sender="101", kind="group"), self.send)
        await asyncio.wait_for(self.service.memory_commands.wait_idle(), 1)
        self.assertEqual(len(self.outputs), 1)
        self.assertIn("已清空", self.outputs[0][1])
        blocked.release.set()
        await asyncio.wait_for(self.service.wait_idle(), 3)
        self.assertEqual(len(self.outputs), 1)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual((await self.memory.snapshot(self.service.namespace(event))).turns, ())

    async def test_plain_message_bypasses_command_worker(self):
        with patch.object(self.service.memory_commands, "accept", AsyncMock(side_effect=AssertionError("command path"))):
            await self.dispatch(self.event(text="请简短解释彩虹"))
        self.assertEqual(len(self.backend.calls), 1)

    async def test_close_during_clear_never_sends_success_and_waits_for_epoch_settlement(self):
        event = self.event(text="/记忆 清空全部")
        namespace = self.service.namespace(event)
        entered, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        original = self.memory.clear_all
        async def delayed_clear(target):
            snapshot = await original(target)
            entered.set()
            await release.wait()
            return snapshot
        with patch.object(self.memory, "clear_all", delayed_clear):
            await self.service.accept(event, self.send)
            await asyncio.wait_for(entered.wait(), 1)
            self.expected_close_error = True
            with self.assertRaisesRegex(RuntimeError, "memory_command_worker_unsettled"):
                await self.service.close()
            self.assertTrue(self.guard.blocked)
            self.assertIsNotNone(self.memory._connection)
            self.assertEqual(self.outputs, [])
            release.set()
            await asyncio.wait_for(self.service.memory_commands.wait_idle(), 2)
        self.assertEqual(self.service._memory_epochs[namespace], 1)
        self.assertEqual(self.outputs, [])
        self.assertEqual((await self.receipts.get("fixture-qq", "1"))["status"], "UNKNOWN")
        await self.service.close()

    async def test_uncooperative_send_blocks_reload_and_late_receipt_is_not_sent(self):
        self.service.settings = replace(self.settings, send_timeout_seconds=.03)
        entered, release = asyncio.Event(), asyncio.Event()
        self.release_events.append(release)
        async def uncooperative(event, text):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return {"message_id": 98765}
        await self.service.accept(self.event(), uncooperative)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(self.service.memory_commands.wait_idle(), 2)
        self.assertTrue(self.guard.blocked)
        self.expected_close_error = True
        with self.assertRaisesRegex(RuntimeError, "memory_command_send_unsettled"):
            await self.service.close()
        self.assertIsNotNone(self.memory._connection)
        self.assertIn(self.service, self.guard.retained)
        self.assertEqual((await self.receipts.get("fixture-qq", "1"))["status"], "UNKNOWN")
        release.set()
        await asyncio.sleep(.03)
        self.assertEqual((await self.receipts.get("fixture-qq", "1"))["status"], "UNKNOWN")
        await self.service.close()


if __name__ == "__main__":
    unittest.main()
