"""Protocol/lifecycle tests without an AstrBot runtime or native WeChat calls."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.gate import AdmissionResult, ApiAccepted, DefiniteSendFailure, GateSettings
from plugins.astrbot_plugin_roleplay_gateway.native_bridge import (
    NativeBridgeServer, PLATFORM_ID, _Connection, _object,
)


class FakeSocket:
    def __init__(self):
        self.messages = []
        self.closed = False

    async def send_json(self, value):
        if self.closed:
            raise RuntimeError("closed")
        self.messages.append(value)

    async def close(self, **kwargs):
        self.closed = True


class NativeBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1_000_000.0
        self.calls, self.logs = [], []
        self.result = "ACCEPTED"
        async def accept(event, sender):
            self.calls.append((event, sender))
            return self.result
        self.settings = GateSettings(platform_id=PLATFORM_ID, bot_self_id="wxid_bot",
                                     allowed_group_ids=frozenset({"123@chatroom"}),
                                     enabled=True, id_kind="wechat_native")
        self.server = NativeBridgeServer(self.settings, "a" * 64, accept,
                                         self.logs.append, clock=lambda: self.now)
        self.socket = FakeSocket()
        self.connection = _Connection(self.socket, "1" * 32, "2" * 32)
        self.server._connection = self.connection

    async def asyncTearDown(self):
        await self.server.close()

    def event(self, **changes):
        value = {"type": "event", "session": self.connection.session, "id": "123456789",
                 "room": "123@chatroom", "sender": "wxid_member", "text": "三叶你好",
                 "mentions": ["wxid_bot"], "received_at": self.now}
        value.update(changes)
        return value

    async def admit(self, **changes):
        await self.server._frame(self.connection, self.event(**changes))
        if self.connection.admissions:
            await asyncio.gather(*tuple(self.connection.admissions))
        return self.calls[-1]

    async def begin_send(self):
        event, sender = await self.admit()
        task = asyncio.create_task(sender(event, "你好"))
        await asyncio.sleep(0)
        return task, self.socket.messages[-1]

    async def complete(self, packet, status="native_completed", **changes):
        value = {"type": "send_result", "session": self.connection.session,
                 "request_id": packet["request_id"], "status": status}
        value.update(changes)
        await self.server._frame(self.connection, value)

    async def test_scheduled_send_requires_negotiated_capability_and_bound_target(self):
        with self.assertRaises(DefiniteSendFailure):
            await self.server.send_scheduled('group', '123@chatroom', '提醒', 'a'*32, 'b'*32)
        self.connection.scheduled = True
        with self.assertRaises(DefiniteSendFailure):
            await self.server.send_scheduled('group', '456@chatroom', '提醒', 'a'*32, 'b'*32)
        task = asyncio.create_task(self.server.send_scheduled('group', '123@chatroom', '提醒', 'a'*32, 'b'*32))
        await asyncio.sleep(0)
        packet = self.socket.messages[-1]
        self.assertEqual(packet['type'], 'scheduled_send')
        self.assertEqual(self.connection.events, {})
        await self.complete(packet)
        self.assertIsInstance(await task, ApiAccepted)

    def test_exact_bearer_authentication(self):
        self.assertTrue(self.server.authorized("Bearer " + "a" * 64))
        for header in (None, "a" * 64, "bearer " + "a" * 64, "Bearer " + "b" * 64,
                       "Bearer " + "a" * 64 + " "):
            self.assertFalse(self.server.authorized(header))

    async def test_ignored_ack_transports_only_whitelisted_reason(self):
        self.result = AdmissionResult("IGNORED", "wake_not_matched")
        await self.admit()
        packet = self.socket.messages[-1]
        self.assertEqual(packet["result"], "IGNORED")
        self.assertEqual(packet["reason"], "wake_not_matched")
        self.assertNotIn("text", packet)
        self.result = AdmissionResult("IGNORED", "private wxid_member payload")
        await self.admit(id="123456790")
        self.assertNotIn("reason", self.socket.messages[-1])

    def test_pinned_handshake_and_sessions(self):
        hello = {"type": "hello", "version": 1, "platform_id": PLATFORM_ID,
                 "self_id": "wxid_bot", "session": "2" * 32}
        self.assertEqual(self.server._hello(hello), "2" * 32)
        for changes in ({"version": True}, {"version": 2}, {"platform_id": "qq"},
                        {"self_id": "wxid_other"}, {"session": "bad"}, {"extra": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.server._hello({**hello, **changes})

    def test_json_duplicate_keys_and_size_rejected(self):
        self.assertEqual(_object('{"type":"event"}'), {"type": "event"})
        for raw in ('{"type":"event","type":"send_result"}', "[]", '"text"', "中" * 6000):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                _object(raw)

    async def test_valid_event_keeps_identity_mentions_and_receipt_time(self):
        event, _ = await self.admit()
        self.assertEqual((event.platform_id, event.bot_self_id, event.group_id, event.sender_id),
                         (PLATFORM_ID, "wxid_bot", "123@chatroom", "wxid_member"))
        self.assertEqual(event.incoming_message_id, "123456789")
        self.assertEqual(event.mentions, ("wxid_bot",))
        self.assertEqual(event.received_at, self.now)
        self.assertEqual(self.socket.messages[-1]["result"], "ACCEPTED")

    async def test_voice_prepare_then_commit_keeps_private_target_and_deadline(self):
        self.server.voice_enabled = True
        self.server.settings = replace(self.settings, admin_private_ids=frozenset({"wxid_peer"}))
        event, sender = await self.admit(kind="private", input_modality="voice", room="wxid_peer", sender="wxid_peer")
        self.assertEqual(event.session, "wechat-native-sakura:private:wxid_peer")
        preparation = asyncio.create_task(sender.prepare_voice(event, "你好"))
        await asyncio.sleep(0)
        packet = self.socket.messages[-1]
        self.assertEqual(packet["type"], "prepare_voice")
        self.assertEqual(packet["room"], "wxid_peer")
        self.assertFalse(any(item["type"] in {"send", "commit_voice"} for item in self.socket.messages))
        self.now += 50
        await self.server._frame(self.connection, dict(type="voice_prepared", session=self.connection.session,
                                request_id=packet["request_id"], status="ready"))
        await preparation
        sending = asyncio.create_task(sender(event, "你好")); await asyncio.sleep(0)
        commit = self.socket.messages[-1]
        self.assertEqual(commit["type"], "commit_voice")
        self.assertEqual(commit["handle"], packet["request_id"])
        self.assertLessEqual(commit["expires_at"], self.now + 8)
        await self.complete(commit)
        self.assertIsInstance(await sending, ApiAccepted)

    async def test_cancelled_voice_preparation_cannot_dispatch(self):
        self.server.voice_enabled = True
        event, sender = await self.admit(input_modality="text", kind="group")
        task = asyncio.create_task(sender.prepare_voice(event, "你好"));await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.socket.messages[-1]["type"], "cancel_voice")
        self.assertFalse(self.connection.pending)

    async def test_non_bound_private_never_reaches_service(self):
        self.server.voice_enabled = True
        self.server.settings = replace(self.settings, admin_private_ids=frozenset({"wxid_peer"}))
        with self.assertRaises(ValueError):
            await self.server._frame(self.connection, self.event(kind="private", input_modality="voice",
                                                                room="wxid_other", sender="wxid_other"))
        self.assertFalse(self.calls)

    async def test_untrusted_event_shapes_never_reach_service(self):
        for changes in ({"room": "456@chatroom"}, {"sender": "wxid_bot"},
                        {"sender": "filehelper"}, {"sender": "gh_public"},
                        {"id": "0"}, {"id": str(2**64)}, {"id": 123}, {"id": "01"},
                        {"received_at": self.now - 30.01}, {"received_at": self.now + 2.01},
                        {"received_at": True}, {"received_at": float("nan")},
                        {"mentions": ["wxid_bot", "wxid_bot"]}, {"mentions": ["@all"]},
                        {"text": "a\0b"}, {"text": "中" * 342}, {"extra": "x"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.server._frame(self.connection, self.event(**changes))
        self.assertEqual(self.calls, [])

    async def test_duplicate_frame_does_not_start_second_admission(self):
        await self.admit()
        await self.server._frame(self.connection, self.event())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.socket.messages[-1]["result"], "DUPLICATE")

    async def test_ignored_event_cannot_send(self):
        self.result = "IGNORED"
        event, sender = await self.admit()
        with self.assertRaises(DefiniteSendFailure):
            await sender(event, "不应发送")
        self.assertFalse(any(value["type"] == "send" for value in self.socket.messages))

    async def test_sender_closure_rejects_changed_target_or_identity(self):
        event, sender = await self.admit()
        for changed in (replace(event, group_id="456@chatroom"), replace(event, sender_id="wxid_other"),
                        replace(event, incoming_message_id="99"), replace(event, received_at=self.now - 1)):
            with self.assertRaises(DefiniteSendFailure):
                await sender(changed, "不应发送")

    async def test_native_completion_is_only_api_evidence(self):
        task, packet = await self.begin_send()
        self.assertEqual(packet["session"], self.connection.session)
        self.assertEqual(packet["reply_to"], "123456789")
        self.assertEqual(packet["room"], "123@chatroom")
        self.assertEqual(packet["expires_at"], self.now + 8)
        await self.complete(packet)
        result = await task
        self.assertIs(type(result), ApiAccepted)
        self.assertEqual(self.connection.pending, {})

    async def test_rejection_is_definite_and_unknown_retires_connection(self):
        task, packet = await self.begin_send()
        await self.complete(packet, "rejected")
        with self.assertRaises(DefiniteSendFailure):
            await task
        self.assertTrue(self.server.connected)
        event, sender = self.calls[-1]
        task = asyncio.create_task(sender(event, "下一段"))
        await asyncio.sleep(0)
        await self.complete(self.socket.messages[-1], "unknown")
        with self.assertRaisesRegex(RuntimeError, "native_send_unknown"):
            await task
        self.assertFalse(self.server.connected)

    async def test_unsolicited_completion_cannot_confirm_actual_request(self):
        task, packet = await self.begin_send()
        await self.complete(packet, request_id="f" * 32)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        await self.complete(packet)
        self.assertIs(type(await task), ApiAccepted)

    async def test_wrong_session_frame_cannot_confirm_send(self):
        task, packet = await self.begin_send()
        with self.assertRaises(ValueError):
            await self.complete(packet, session="f" * 32)
        self.assertFalse(task.done())
        await self.complete(packet)
        await task

    async def test_disconnect_settles_pending_and_old_callback_cannot_use_new_socket(self):
        task, _ = await self.begin_send()
        event, sender = self.calls[-1]
        await self.server._disconnect(self.connection)
        with self.assertRaisesRegex(RuntimeError, "native_connection_lost"):
            await task
        replacement = _Connection(FakeSocket(), "3" * 32, "4" * 32)
        self.server._connection = replacement
        with self.assertRaises(DefiniteSendFailure):
            await sender(event, "旧会话回复")
        self.assertEqual(replacement.ws.messages, [])

    async def test_timeout_retires_connection_and_never_retries(self):
        self.server.settings = replace(self.settings, send_timeout_seconds=0.01)
        task, _ = await self.begin_send()
        with self.assertRaises(TimeoutError):
            await task
        self.assertFalse(self.server.connected)
        self.assertEqual(sum(value["type"] == "send" for value in self.socket.messages), 1)
        self.assertEqual(self.connection.pending, {})

    async def test_expired_reply_is_not_dispatched(self):
        event, sender = await self.admit()
        self.now += 30
        with self.assertRaises(DefiniteSendFailure):
            await sender(event, "过期回复")
        self.assertFalse(any(value["type"] == "send" for value in self.socket.messages))

    async def test_reply_deadline_never_outlives_original_ingress_window(self):
        event, sender = await self.admit()
        self.now += 28
        task = asyncio.create_task(sender(event, "最后一段"))
        await asyncio.sleep(0)
        packet = self.socket.messages[-1]
        self.assertEqual(packet["expires_at"], event.received_at + 30)
        await self.complete(packet)
        await task

    async def test_clock_offsets_preserve_host_deadline_and_wait_window(self):
        host_ingress = 1_000_000.0
        for index, offset in enumerate((-1.0, 1.0), start=1):
            with self.subTest(server_offset=offset):
                self.now = host_ingress + offset
                event, sender = await self.admit(id=str(index), received_at=host_ingress)
                self.now += 5
                task = asyncio.create_task(sender(event, "不同系统时间的回复"))
                await asyncio.sleep(0)
                packet = self.socket.messages[-1]
                self.assertEqual(packet["expires_at"], host_ingress + 5 + 8)
                self.assertFalse(task.done())
                await self.complete(packet)
                self.assertIs(type(await task), ApiAccepted)

    async def test_clock_offsets_preserve_expiry_and_pruning(self):
        host_ingress = 1_000_000.0
        for index, offset in enumerate((-1.0, 1.0), start=1):
            with self.subTest(server_offset=offset):
                self.connection.events.clear()
                self.now = host_ingress + offset
                event, sender = await self.admit(id=str(index), received_at=host_ingress)
                self.now += 30
                before = sum(value["type"] == "send" for value in self.socket.messages)
                with self.assertRaises(DefiniteSendFailure):
                    await sender(event, "已过期")
                self.assertEqual(sum(value["type"] == "send" for value in self.socket.messages), before)
                await self.admit(id=str(index + 10), received_at=host_ingress + 30)
                self.assertNotIn(str(index), self.connection.events)

    async def test_close_settles_send_and_rejects_later_sends(self):
        task, _ = await self.begin_send()
        await self.server.close()
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(self.server._send_tasks, set())
        self.assertTrue(self.socket.closed)

    async def test_send_limit_never_places_an_extra_request(self):
        event, sender = await self.admit()
        futures = [asyncio.get_running_loop().create_future() for _ in range(16)]
        self.connection.pending.update({str(i): future for i, future in enumerate(futures)})
        try:
            with self.assertRaises(DefiniteSendFailure):
                await sender(event, "超过并发")
            self.assertFalse(any(value["type"] == "send" for value in self.socket.messages))
        finally:
            self.connection.pending.clear()
            for future in futures:
                future.cancel()

    async def test_cancelled_send_retires_connection_without_second_dispatch(self):
        task, _ = await self.begin_send()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.server.connected)
        self.assertEqual(self.connection.pending, {})
        self.assertEqual(sum(value["type"] == "send" for value in self.socket.messages), 1)

    async def test_admission_capacity_rejects_extra_event_and_close_settles_workers(self):
        blocked = asyncio.Event()
        async def slow_accept(event, sender):
            await blocked.wait()
            return "ACCEPTED"
        self.server._accept = slow_accept
        for number in range(1, 18):
            await self.server._frame(self.connection, self.event(id=str(number)))
        self.assertEqual(len(self.connection.admissions), 16)
        self.assertEqual(self.socket.messages[-1]["id"], "17")
        self.assertEqual(self.socket.messages[-1]["result"], "REJECTED")
        await self.server.close()
        self.assertFalse(self.connection.admissions)

    async def test_native_payload_size_rejected_before_dispatch(self):
        event, sender = await self.admit()
        for text in ("", " \n", "中" * 342, "测试\0旁路"):
            with self.subTest(length=len(text)), self.assertRaises(DefiniteSendFailure):
                await sender(event, text)
        self.assertFalse(any(value["type"] == "send" for value in self.socket.messages))

    async def test_research_profile_requires_enabled_original_query(self):
        self.server.research_enabled = True
        event, sender = await self.admit(text="三叶，帮我查一下武汉天气")
        task = asyncio.create_task(sender(event, "a" * 400))
        await asyncio.sleep(0)
        packet = self.socket.messages[-1]
        self.assertEqual(packet["profile"], "research")
        self.assertEqual(packet["expires_at"], self.now + 8)
        await self.complete(packet)
        self.assertIs(type(await task), ApiAccepted)

    async def test_research_disabled_keeps_legacy_wire_shape(self):
        event, sender = await self.admit(text="/查 武汉天气")
        task = asyncio.create_task(sender(event, "短句"))
        await asyncio.sleep(0)
        packet = self.socket.messages[-1]
        self.assertNotIn("profile", packet)
        await self.complete(packet)
        await task

    async def test_changed_event_text_cannot_upgrade_original_chat_profile(self):
        self.server.research_enabled = True
        event, sender = await self.admit(text="三叶你好")
        task = asyncio.create_task(sender(replace(event, text="/查 武汉天气"), "短句"))
        await asyncio.sleep(0)
        packet = self.socket.messages[-1]
        self.assertNotIn("profile", packet)
        await self.complete(packet)
        await task

    async def test_research_retains_native_bytes_and_ingress_expiry(self):
        self.server.research_enabled = True
        event, sender = await self.admit(text="/查 武汉天气")
        with self.assertRaises(DefiniteSendFailure):
            await sender(event, "字" * 342)
        self.now += 28
        task = asyncio.create_task(sender(event, "a" * 400))
        await asyncio.sleep(0)
        packet = self.socket.messages[-1]
        self.assertEqual(packet["profile"], "research")
        self.assertEqual(packet["expires_at"], event.received_at + 30)
        await self.complete(packet)
        await task

    def test_research_enable_requires_boolean(self):
        for value in (1, "true", None):
            with self.assertRaises(ValueError):
                NativeBridgeServer(self.settings, "a" * 64, lambda *args: None,
                                   research_enabled=value)

    async def test_unexpired_event_capacity_is_bounded_and_expired_entries_pruned(self):
        self.result = "IGNORED"
        for number in range(1, 257):
            await self.admit(id=str(number))
        self.assertEqual(len(self.connection.events), 256)
        await self.server._frame(self.connection, self.event(id="257"))
        self.assertEqual(self.socket.messages[-1]["result"], "REJECTED")
        self.assertEqual(len(self.calls), 256)
        self.now += 31
        await self.admit(id="258")
        self.assertEqual(len(self.connection.events), 1)


if __name__ == "__main__":
    unittest.main()
