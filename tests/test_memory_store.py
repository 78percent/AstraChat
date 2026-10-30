"""Persistent-memory contracts using only temporary SQLite and neutral text."""

from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.memory_store import (
    MemoryInput, MemoryReply, MemoryStore, Namespace,
)


def inputs(message_id="100", text="记住一个中性标记：银杏。", sender_id="20"):
    return (MemoryInput(message_id, sender_id, "测试者", text),)


def replies(message_id="200", text="记住了这个标记。", sequence=0):
    return (MemoryReply(sequence, message_id, text),)


class MemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="memory-store-", dir=ROOT / "tests")
        self.path = Path(self.temp.name) / "memory.sqlite3"
        self.store = MemoryStore(self.path)
        await self.store.open()
        self.namespace = Namespace("qq-one", "private", "20", "neutral-role")

    async def asyncTearDown(self):
        await self.store.close()
        self.temp.cleanup()

    async def commit(self, batch="batch-1", *, namespace=None, epoch=0,
                     revision="revision-1", actual_inputs=None, actual_replies=None, complete=True):
        return await self.store.commit_turn(
            namespace or self.namespace, expected_epoch=epoch, batch_id=batch,
            persona_revision=revision,
            inputs=inputs() if actual_inputs is None else actual_inputs,
            replies=replies() if actual_replies is None else actual_replies,
            complete=complete,
        )

    async def test_new_namespace_starts_empty_and_read_does_not_change_revision(self):
        first = await self.store.snapshot(self.namespace)
        second = await self.store.snapshot(self.namespace)
        self.assertEqual(first, second)
        self.assertEqual((first.epoch, first.summary_revision, first.summary, first.turns,
                          first.last_sequence), (0, 0, "", (), 0))

    async def test_api_accepted_memory_preserves_null_id_and_distinct_local_refs(self):
        wx = Namespace("fixture-wechat", "private", "wx-user", "neutral-role")
        accepted = (MemoryReply(0, None, "第一句。", "API_ACCEPTED", "local-a"),
                    MemoryReply(1, None, "第二句。", "API_ACCEPTED", "local-b"))
        self.assertTrue(await self.commit(namespace=wx, actual_replies=accepted))
        await self.store.close()
        await self.store.open()
        self.assertEqual((await self.store.snapshot(wx)).turns[0].replies, accepted)
        self.assertEqual((await self.store.snapshot(self.namespace)).turns, ())
        with self.assertRaises(ValueError):
            await self.commit("duplicate-local", namespace=wx,
                              actual_replies=(accepted[0], replace(accepted[1], delivery_ref="local-a")))
        for values in ((0, "fake-id", "正文", "API_ACCEPTED", "local"),
                       (0, None, "正文", "API_ACCEPTED", None),
                       (0, None, "正文", "UNKNOWN", "local")):
            with self.assertRaises(ValueError):
                MemoryReply(*values)

    async def test_legacy_reply_json_without_evidence_fields_remains_readable(self):
        await self.commit()
        await self.store.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            encoded = json.loads(db.execute("SELECT replies_json FROM memory_turns").fetchone()[0])
            self.assertEqual(set(encoded[0]), {"sequence", "message_id", "text"})
            db.execute("UPDATE memory_turns SET replies_json=?", (json.dumps([
                {"sequence": 0, "message_id": "200", "text": "旧版实际回复。"}], ensure_ascii=False),))
        await self.store.open()
        actual = (await self.store.snapshot(self.namespace)).turns[0].replies[0]
        self.assertEqual((actual.delivery_status, actual.delivery_ref, actual.message_id), ("SENT", None, "200"))

    async def test_committed_turn_and_manual_summary_survive_reopen(self):
        self.assertTrue(await self.commit())
        expected = await self.store.edit_summary(self.namespace, "测试者的标记是银杏。")
        await self.store.close()
        self.store = MemoryStore(self.path)
        await self.store.open()
        restored = await self.store.snapshot(self.namespace)
        self.assertEqual(restored, expected)
        self.assertEqual(restored.turns[0].inputs, inputs())
        self.assertEqual(restored.turns[0].replies, replies())

    async def test_duplicate_batch_is_once_even_after_restart(self):
        self.assertTrue(await self.commit())
        self.assertFalse(await self.commit(actual_replies=replies("201", "重复不应覆盖。")))
        await self.store.close()
        await self.store.open()
        self.assertFalse(await self.commit())
        snapshot = await self.store.snapshot(self.namespace)
        self.assertEqual(snapshot.last_sequence, 1)
        self.assertEqual(snapshot.turns[0].replies, replies())

    async def test_same_text_with_distinct_batches_is_not_deduplicated(self):
        await self.commit()
        await self.commit("batch-2", actual_inputs=inputs("101"), actual_replies=replies("201"))
        snapshot = await self.store.snapshot(self.namespace)
        self.assertEqual([turn.sequence for turn in snapshot.turns], [1, 2])

    async def test_platform_kind_peer_and_persona_isolation(self):
        alternatives = (
            replace(self.namespace, platform_id="qq-two"),
            replace(self.namespace, platform_id="wechat-one"),
            replace(self.namespace, kind="group"),
            replace(self.namespace, peer_id="21"),
            replace(self.namespace, persona_id="other-role"),
        )
        await self.commit()
        for namespace in alternatives:
            self.assertEqual((await self.store.snapshot(namespace)).turns, ())
        await self.commit("batch-2", namespace=alternatives[0])
        await self.store.edit_summary(self.namespace, "原命名空间摘要")
        self.assertEqual((await self.store.snapshot(alternatives[0])).summary, "")

    async def test_namespace_components_cannot_collide_by_string_join(self):
        left = Namespace("qq:a", "private", "b", "role")
        right = Namespace("qq", "private", "a:b", "role")
        await self.commit(namespace=left)
        self.assertEqual((await self.store.snapshot(right)).turns, ())

    async def test_group_members_share_turns_and_keep_speaker_attribution(self):
        group = replace(self.namespace, kind="group")
        members = (MemoryInput("101", "20", "甲", "甲的中性提问"),
                   MemoryInput("102", "21", "乙", "乙的中性提问"))
        await self.commit(namespace=group, actual_inputs=members)
        turn = (await self.store.snapshot(group)).turns[0]
        self.assertEqual(turn.inputs, members)
        self.assertEqual((await self.store.snapshot(self.namespace)).turns, ())

    async def test_persona_revision_update_keeps_existing_memory(self):
        await self.commit()
        await self.commit("batch-2", revision="revision-2",
                          actual_inputs=inputs("101"), actual_replies=replies("201"))
        snapshot = await self.store.snapshot(self.namespace)
        self.assertEqual([turn.persona_revision for turn in snapshot.turns],
                         ["revision-1", "revision-2"])
        self.assertEqual(snapshot.epoch, 0)

    async def test_partial_turn_contains_only_passed_confirmed_segments(self):
        confirmed = (MemoryReply(0, "201", "已经确认的第一段。"),
                     MemoryReply(1, "202", "已经确认的第二段。"))
        await self.commit(actual_replies=confirmed, complete=False)
        turn = (await self.store.snapshot(self.namespace)).turns[0]
        self.assertFalse(turn.complete)
        self.assertEqual(turn.replies, confirmed)
        self.assertEqual(len(turn.replies), 2)

    async def test_empty_unknown_or_duplicate_receipt_collection_is_rejected(self):
        invalid = ((), [], ("UNKNOWN",),
                   (MemoryReply(0, "201", "甲"), MemoryReply(1, "201", "乙")),
                   (MemoryReply(1, "201", "甲"), MemoryReply(0, "202", "乙")),
                   (MemoryReply(0, "201", "甲"), MemoryReply(0, "202", "乙")))
        for value in invalid:
            with self.assertRaisesRegex(ValueError, "invalid_confirmed_memory_replies"):
                await self.commit(actual_replies=value)
        self.assertEqual((await self.store.snapshot(self.namespace)).turns, ())
        for value in (None, "", "\n", 0):
            with self.assertRaises(ValueError):
                MemoryReply(0, value, "中性文本")

    async def test_clear_summary_retains_history_epoch_and_advances_floor(self):
        await self.commit()
        before = await self.store.edit_summary(self.namespace, "待清理摘要")
        after = await self.store.clear_summary(self.namespace)
        self.assertEqual(after.summary, "")
        self.assertEqual(after.summary_revision, before.summary_revision + 1)
        self.assertEqual((after.epoch, after.turns, after.last_sequence),
                         (before.epoch, before.turns, before.last_sequence))
        with closing(sqlite3.connect(self.path)) as db, db:
            floor, watermark = db.execute(
                "SELECT summary_source_floor,summary_source_watermark FROM memory_namespaces"
            ).fetchone()
        self.assertEqual((floor, watermark), (before.last_sequence, before.last_sequence))
        self.assertTrue(await self.commit("later", actual_inputs=inputs("101"),
                                          actual_replies=replies("201")))

    async def test_clear_all_removes_history_and_fences_old_epoch(self):
        other = replace(self.namespace, peer_id="other")
        await self.commit()
        await self.commit(namespace=other)
        await self.store.edit_summary(self.namespace, "旧摘要")
        after = await self.store.clear_all(self.namespace)
        self.assertEqual((after.epoch, after.summary, after.turns), (1, "", ()))
        self.assertEqual(after.summary_revision, 2)
        self.assertFalse(await self.commit("stale", epoch=0))
        self.assertTrue(await self.commit("new", epoch=1))
        fresh = await self.store.snapshot(self.namespace)
        self.assertEqual(len(fresh.turns), 1)
        self.assertGreater(fresh.turns[0].sequence, after.last_sequence)
        self.assertEqual(len((await self.store.snapshot(other)).turns), 1)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM memory_turns WHERE peer_id='20' AND epoch=0"
            ).fetchone()[0], 0)

    async def test_summary_edit_preserves_body_layout_and_increments_revision(self):
        text = "第一段\n\n  第二段带缩进  "
        first = await self.store.edit_summary(self.namespace, text)
        second = await self.store.edit_summary(self.namespace, "第二次手动摘要")
        self.assertEqual(first.summary, text)
        self.assertEqual(second.summary_revision, first.summary_revision + 1)
        self.assertEqual(second.epoch, first.epoch)
        with self.assertRaisesRegex(ValueError, "invalid_memory_text"):
            await self.store.edit_summary(self.namespace, "")

    async def test_recent_window_is_ordered_and_does_not_delete_older_turns(self):
        for number in range(15):
            await self.commit(f"batch-{number}", actual_inputs=inputs(str(100 + number)),
                              actual_replies=replies(str(200 + number)))
        recent = await self.store.snapshot(self.namespace)
        self.assertEqual([turn.sequence for turn in recent.turns], list(range(4, 16)))
        self.assertEqual(len((await self.store.snapshot(self.namespace, limit=100)).turns), 15)
        metadata = await self.store.snapshot(self.namespace, limit=0)
        self.assertEqual((metadata.turns, metadata.last_sequence), ((), 15))

    async def test_concurrent_duplicate_commit_across_connections_is_atomic(self):
        second = MemoryStore(self.path)
        await second.open()
        try:
            results = await asyncio.gather(*[
                store.commit_turn(self.namespace, expected_epoch=0, batch_id="same-batch",
                                  persona_revision="revision-1", inputs=inputs(), replies=replies(),
                                  complete=True)
                for store in (self.store, second, self.store, second)
            ])
            self.assertEqual(results.count(True), 1)
            snapshot = await second.snapshot(self.namespace)
            self.assertEqual((snapshot.last_sequence, len(snapshot.turns)), (1, 1))
        finally:
            await second.close()

    async def test_concurrent_clear_and_commit_cannot_leave_old_epoch_history(self):
        await self.commit()
        await asyncio.gather(self.store.clear_all(self.namespace),
                             self.commit("racing", epoch=0))
        snapshot = await self.store.snapshot(self.namespace)
        self.assertEqual((snapshot.epoch, snapshot.turns), (1, ()))

    async def test_repeated_cancellation_waits_for_started_database_work(self):
        started, release = threading.Event(), threading.Event()
        original = self.store._commit_turn_sync

        def delayed(*args):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test_release_timeout")
            return original(*args)

        with patch.object(self.store, "_commit_turn_sync", delayed):
            task = asyncio.create_task(self.commit())
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
        # Cancellation did not abandon the thread; the atomic write has settled.
        self.assertEqual(len((await self.store.snapshot(self.namespace)).turns), 1)

    async def test_schema_identity_wal_and_busy_timeout(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0],
                             MemoryStore._APPLICATION_ID)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store._connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    async def test_foreign_and_future_schema_are_rejected_without_overwrite(self):
        foreign = Path(self.temp.name) / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as db, db:
            db.execute("CREATE TABLE foreign_data(value TEXT)")
            db.execute("INSERT INTO foreign_data VALUES ('keep')")
        store = MemoryStore(foreign)
        with self.assertRaisesRegex(ValueError, "unsupported_memory_schema"):
            await store.open()
        with closing(sqlite3.connect(foreign)) as db, db:
            self.assertEqual(db.execute("SELECT value FROM foreign_data").fetchone()[0], "keep")
        await self.store.close()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA user_version=2")
        with self.assertRaisesRegex(ValueError, "unsupported_memory_schema"):
            await self.store.open()
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

    async def test_invalid_inputs_fail_before_any_turn_is_committed(self):
        for namespace in (None, "qq:private:20"):
            with self.assertRaises(ValueError):
                await self.store.snapshot(namespace)
        for limit in (-1, 1001, True, "12"):
            with self.assertRaises(ValueError):
                await self.store.snapshot(self.namespace, limit)
        for epoch in (-1, True, "0"):
            with self.assertRaises(ValueError):
                await self.commit(epoch=epoch)
        for value in ((), [], ("not-an-input",), inputs() + inputs()):
            with self.assertRaises(ValueError):
                await self.commit(actual_inputs=value)
        with self.assertRaises(ValueError):
            await self.commit(complete=1)
        self.assertEqual((await self.store.snapshot(self.namespace)).turns, ())

    async def test_close_is_idempotent_and_closed_operations_fail(self):
        await self.store.close()
        await self.store.close()
        with self.assertRaisesRegex(RuntimeError, "memory_store_closed"):
            await self.store.snapshot(self.namespace)


class MemoryValueTests(unittest.TestCase):
    def test_invalid_fields_raise_static_errors_without_echoing_text(self):
        for make in (
            lambda: Namespace("", "private", "20", "role"),
            lambda: Namespace("qq", "notification", "20", "role"),
            lambda: MemoryInput("1", "20", "测试", ""),
            lambda: MemoryReply(-1, "2", "测试"),
            lambda: MemoryReply(0, "bad\nreceipt", "测试"),
        ):
            with self.assertRaises(ValueError) as raised:
                make()
            self.assertRegex(str(raised.exception), r"^[a-z_]+$")


if __name__ == "__main__":
    unittest.main()
