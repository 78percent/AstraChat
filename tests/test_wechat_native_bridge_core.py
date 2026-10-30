"""Offline policy tests; synthetic identities only, no upstream imports."""
from contextlib import closing
import json
import ctypes
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from scripts.wechat_native_bridge_core import (
    BridgeCore, BridgeError, BridgeJournal, BridgeSettings, COMMIT, PLATFORM, URL,
    decode_frame, handshake_ack, native_event, self_mentions,
)

ROOT = Path(__file__).resolve().parents[1]
SESSION = "11111111222243338444555555555555"
CLIENT = "aaaaaaaa222243338444555555555555"


def settings(**changes):
    value = {"enabled": True, "expected_self": "wxid_robot", "allowed_room": "12345@chatroom",
             "token": "a" * 64, "platform_id": PLATFORM, "url": URL, "upstream_commit": COMMIT}
    value.update(changes)
    return BridgeSettings.parse(value)


def incoming(**changes):
    value = {"type": 1, "receiver": b"wxid_robot", "sender": b"12345@chatroom",
             "native_message_id": 33, "sequence": 1,
             "text": "wxid_member:\n三叶 synthetic input".encode(), "mention_source": b""}
    value.update(changes)
    return SimpleNamespace(**value)


def send_frame(**changes):
    value = {"type": "send", "session": SESSION, "request_id": uuid.uuid4().hex,
             "reply_to": "33", "room": "12345@chatroom", "text": "synthetic reply",
             "expires_at": 108.0}
    value.update(changes)
    return value


def completion(prepared, **changes):
    value = {"pid": 31, "code": 0, "cleanup_code": 0, "success": True,
             "report": {"completion_success": True, "target": prepared.room, "text": prepared.text}}
    value.update(changes)
    return value


class ConfigWireTests(unittest.TestCase):
    def test_config_is_exact_and_redacted(self):
        config = settings()
        self.assertNotIn("wxid_robot", repr(config))
        self.assertNotIn("a" * 64, repr(config))
        for changed in ({"enabled": 1}, {"expected_self": "filehelper"}, {"allowed_room": "friend"},
                        {"url": "ws://192.168.1.1:6198/wechat-native/ws"}, {"token": "x" * 64},
                        {"upstream_commit": "different"}, {"unexpected": True}):
            with self.subTest(changed=tuple(changed)):
                with self.assertRaises(BridgeError):
                    settings(**changed)

    def test_wire_rejects_duplicates_nonobjects_and_invalid_numbers(self):
        for raw in ('{"type":"send","type":"event"}', "[]", "null", "bad", b"{}", " " * 16385):
            with self.assertRaises(BridgeError):
                decode_frame(raw)

    def test_handshake_requires_both_sessions_and_exact_version(self):
        frame = {"type": "hello_ack", "version": 1, "session": SESSION, "client_session": CLIENT}
        self.assertEqual(handshake_ack(frame, CLIENT), SESSION)
        for change in ({"version": True}, {"client_session": SESSION}, {"session": "bad"}, {"extra": 1}):
            with self.assertRaises(BridgeError):
                handshake_ack({**frame, **change}, CLIENT)


class NativeMappingTests(unittest.TestCase):
    def test_native_char_array_mention_source_and_cdata_are_preserved(self):
        # Native V5 inherits this c_char array field from ReceivedTextMessage.
        class MentionFields(ctypes.Structure):
            _fields_ = [("mention_source", ctypes.c_char * 4096)]
        fields = MentionFields()
        fields.mention_source = b'<msgsource><atuserlist><![CDATA[wxid_other,wxid_robot]]></atuserlist></msgsource>'
        raw = incoming(mention_source=fields.mention_source, text=b"wxid_member:\n@SyntheticLabel hello")
        self.assertEqual(native_event(raw, settings(), SESSION, 100)["mentions"], ["wxid_robot"])
        # Visible @ labels and unrelated callback aliases confer no authority.
        raw = incoming(mention_source=b"", at_members=["wxid_robot"], is_at_self=True,
                       text=b"wxid_member:\n@SyntheticLabel hello")
        self.assertEqual(native_event(raw, settings(), SESSION, 100)["mentions"], [])

    def test_rejected_metadata_never_reads_text(self):
        class PrivatePayload:
            type, receiver, sender = 1, b"wxid_robot", b"99999@chatroom"
            @property
            def text(self):
                raise AssertionError("unrelated text accessed")
        self.assertIsNone(native_event(PrivatePayload(), settings(), SESSION, 100))
        private = PrivatePayload()
        private.sender, private.type = b"12345@chatroom", 37
        self.assertIsNone(native_event(private, settings(), SESSION, 100))

    def test_real_native_id_exact_text_sender_and_mentions(self):
        raw = incoming(native_message_id=2**64 - 1, mention_source=b"<msgsource><atuserlist>wxid_robot</atuserlist></msgsource>")
        event = native_event(raw, settings(), SESSION, 100)
        self.assertEqual(event["id"], "18446744073709551615")
        self.assertEqual(event["sender"], "wxid_member")
        self.assertEqual(event["text"], "三叶 synthetic input")
        self.assertEqual(event["mentions"], ["wxid_robot"])
        self.assertEqual(event["received_at"], 100.0)

    def test_invalid_native_inputs_fail_closed(self):
        for changed in ({"native_message_id": 0}, {"native_message_id": True}, {"sequence": 0},
                        {"receiver": b"wxid_other"}, {"text": b"wxid_robot:\nself"},
                        {"text": b"wxid_member:\n\xff"}, {"text": b"no member prefix"},
                        {"text": b"filehelper:\nnot a member"}, {"text": b"wxid_member:\n\0evil"},
                        {"text": b"wxid_member:\n  "}, {"text": b"x" * 1024}):
            with self.subTest(changed=tuple(changed)):
                self.assertIsNone(native_event(incoming(**changed), settings(), SESSION, 100))

    def test_mentions_do_not_trust_visible_name_or_unsafe_xml(self):
        for source in (b"@wxid_robot", b"<msgsource><atuserlist>wxid_other</atuserlist></msgsource>",
                       b'<!DOCTYPE x [<!ENTITY a "wxid_robot">]><atuserlist>&a;</atuserlist>',
                       b"<msgsource><atuserlist>wxid_robot</atuserlist><atuserlist>wxid_other</atuserlist></msgsource>",
                       b"<evil><atuserlist>wxid_robot</atuserlist></evil>", b"x" * 4096):
            self.assertEqual(self_mentions(source, "wxid_robot"), [])
        self.assertEqual(self_mentions(b"<atuserlist><![CDATA[wxid_other,wxid_robot]]></atuserlist>", "wxid_robot"), ["wxid_robot"])


class JournalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-core-", dir=ROOT / "work")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.journal = BridgeJournal(self.path)
        self.addCleanup(lambda: self.journal.close())
        self.core = BridgeCore(settings(), self.journal, SESSION)
        self.event = self.core.observe(incoming(), 100)
        self.core.mark_forwarded(self.event)

    def test_inbound_is_durable_deduplicated(self):
        self.assertIsNone(self.core.observe(incoming(sequence=2), 101))
        another = BridgeCore(settings(), self.journal, CLIENT)
        self.assertIsNone(another.observe(incoming(), 102))
        self.assertEqual(self.core.counts["duplicate_events"], 1)

    def test_unforwarded_input_does_not_authorize_send(self):
        core = BridgeCore(settings(), self.journal, SESSION)
        prepared, result = core.prepare(send_frame(), 100)
        self.assertIsNone(prepared)
        self.assertEqual(result["status"], "rejected")

    def test_intent_exists_before_native_and_matching_completion(self):
        frame = send_frame()
        prepared, immediate = self.core.prepare(frame, 100)
        self.assertIsNone(immediate)
        with closing(sqlite3.connect(self.path)) as peer:
            self.assertEqual(peer.execute("SELECT status FROM outbound").fetchone()[0], "in_flight")
        result = self.core.complete(prepared, completion(prepared), 31)
        self.assertEqual(result["status"], "native_completed")
        self.assertEqual(set(result), {"type", "session", "request_id", "status"})
        duplicate, cached = self.core.prepare(frame, 101)
        self.assertIsNone(duplicate)
        self.assertEqual(cached, result)

    def test_request_mutation_cannot_reuse_id(self):
        frame = send_frame()
        prepared, _ = self.core.prepare(frame, 100)
        self.core.complete(prepared, completion(prepared), 31)
        for change in ({"text": "different"}, {"expires_at": 107.0}):
            attempted, result = self.core.prepare({**frame, **change}, 101)
            self.assertIsNone(attempted)
            self.assertEqual(result["status"], "rejected")

    def test_group_session_reply_and_deadline_bounds(self):
        for changed, now in (({"room": "999@chatroom"}, 100), ({"reply_to": "999"}, 100),
                             ({"expires_at": 108.001}, 100), ({"expires_at": 100}, 100),
                             ({"expires_at": 131}, 125), ({"expires_at": float("nan")}, 100),
                             ({"expires_at": True}, 100), ({}, 99)):
            with self.subTest(changed=tuple(changed), now=now):
                self.assertEqual(self.core.prepare(send_frame(**changed), now)[1]["status"], "rejected")
        with self.assertRaisesRegex(BridgeError, "session"):
            self.core.prepare(send_frame(session=CLIENT), 100)

    def test_four_segments_per_input(self):
        for _ in range(4):
            prepared, _ = self.core.prepare(send_frame(text="短句"), 100)
            self.core.complete(prepared, completion(prepared), 31)
        self.assertEqual(self.core.prepare(send_frame(text="短句"), 100)[1]["status"], "rejected")

    def test_total_125_characters_per_input(self):
        prepared, _ = self.core.prepare(send_frame(text="字" * 125), 100)
        self.core.complete(prepared, completion(prepared), 31)
        self.assertEqual(self.core.prepare(send_frame(text="字"), 100)[1]["status"], "rejected")

    def test_send_content_boundaries(self):
        for text in ("", "  ", "x\0x", "字" * 342, "x" * 126):
            self.assertEqual(self.core.prepare(send_frame(text=text), 100)[1]["status"], "rejected")

    def test_pid_target_text_and_callback_must_match(self):
        for change in ({"pid": 32}, {"pid": True}, {"code": True}, {"cleanup_code": 1},
                       {"success": False}, {"report": {"completion_success": True, "target": "wrong", "text": "x"}}):
            # Different incoming events keep the per-event quota independent.
            next_id = 40 + self.core.counts["unknown"]
            event = self.core.observe(incoming(native_message_id=next_id), 100)
            self.core.mark_forwarded(event)
            prepared, _ = self.core.prepare(send_frame(reply_to=str(next_id)), 100)
            self.assertEqual(self.core.complete(prepared, completion(prepared, **change), 31)["status"], "unknown")

    def test_restart_preserves_unknown_and_wont_authorize_old_input(self):
        frame = send_frame()
        prepared, _ = self.core.prepare(frame, 100)
        self.journal.close()
        self.journal = BridgeJournal(self.path)
        self.assertEqual(self.journal.existing(prepared.request_id)["status"], "unknown")
        core = BridgeCore(settings(), self.journal, CLIENT)
        self.assertIsNone(core.observe(incoming(), 101))
        self.assertEqual(core.prepare({**frame, "session": CLIENT}, 101)[1]["status"], "rejected")

    def test_durable_storage_does_not_contain_original_text(self):
        prepared, _ = self.core.prepare(send_frame(text="秘密合成正文xyz987"), 100)
        self.core.complete(prepared, completion(prepared), 31)
        for path in Path(self.tmp.name).glob("ledger.sqlite3*"):
            content = path.read_bytes()
            self.assertNotIn("秘密合成正文xyz987".encode(), content)
            self.assertNotIn("三叶 synthetic input".encode(), content)

    def test_expired_before_dispatch_cancels_without_native_evidence(self):
        prepared, _ = self.core.prepare(send_frame(), 100)
        result = self.core.cancel_before_dispatch(prepared)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.journal.existing(prepared.request_id)["status"], "rejected")

    def test_ack_session_and_known_id_required(self):
        frame = {"type": "event_ack", "session": SESSION, "id": "33", "result": "accepted"}
        self.core.acknowledge(frame)
        self.assertEqual(self.core.counts["acked"], 1)
        for change in ({"id": "999"}, {"session": CLIENT}, {"extra": "x"}):
            with self.assertRaises(BridgeError):
                self.core.acknowledge({**frame, **change})

    def test_ack_diagnostics_keep_results_but_redact_untrusted_values(self):
        frame = {"type": "event_ack", "session": SESSION, "id": "33", "result": "IGNORED",
                 "reason": "wake_not_matched"}
        self.core.acknowledge(frame)
        self.assertEqual(self.core.ack_results, {"IGNORED": 1})
        self.assertEqual(self.core.last_ack, {"result": "IGNORED", "reason": "wake_not_matched"})
        self.core.acknowledge({**frame, "result": "private wxid_robot", "reason": {"token": "secret"}})
        self.assertEqual(self.core.last_ack, {"result": "OTHER", "reason": "unspecified"})
        diagnostics = json.dumps([self.core.ack_results, self.core.last_ack, self.core.last_event])
        for private in ("wxid_robot", "secret", "synthetic input", "12345@chatroom"):
            self.assertNotIn(private, diagnostics)
        self.assertEqual(self.core.last_event, {"mention_source_present": False, "self_mentioned": False})

    def research_event(self, mid=34):
        event = self.core.observe(incoming(native_message_id=mid,
            text="wxid_member:\n三叶，帮我查一下武汉天气".encode()), 100)
        self.core.mark_forwarded(event)
        return event

    def test_research_requires_local_original_input_eligibility(self):
        attempted, result = self.core.prepare(send_frame(profile="research", text="x" * 400), 100)
        self.assertIsNone(attempted)
        self.assertEqual(result["status"], "rejected")
        # Same native ID cannot acquire a larger profile by changing text.
        self.assertIsNone(self.core.observe(incoming(text="wxid_member:\n/查 天气".encode()), 101))
        self.assertEqual(self.journal.db.execute("SELECT research_eligible FROM inbound WHERE id='33'").fetchone()[0], 0)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM outbound").fetchone()[0], 0)

    def test_research_allows_two_bubbles_total_800_characters(self):
        self.research_event()
        for _ in range(2):
            prepared, immediate = self.core.prepare(send_frame(reply_to="34", profile="research", text="x" * 400), 100)
            self.assertIsNone(immediate)
            self.core.complete(prepared, completion(prepared), 31)
        self.assertEqual(self.core.prepare(send_frame(reply_to="34", profile="research", text="x"), 100)[1]["status"], "rejected")
        row = self.journal.db.execute("SELECT delivery_profile FROM inbound WHERE id='34'").fetchone()
        self.assertEqual(row[0], "research")

    def test_research_total_and_native_utf8_limits_are_independent(self):
        self.research_event()
        self.assertEqual(self.core.prepare(send_frame(reply_to="34", profile="research", text="字" * 342), 100)[1]["status"], "rejected")
        prepared, result = self.core.prepare(send_frame(reply_to="34", profile="research", text="x" * 700), 100)
        self.assertIsNone(result)
        self.core.complete(prepared, completion(prepared), 31)
        self.assertEqual(self.core.prepare(send_frame(reply_to="34", profile="research", text="x" * 101), 100)[1]["status"], "rejected")
        prepared, result = self.core.prepare(send_frame(reply_to="34", profile="research", text="x" * 100), 100)
        self.assertIsNone(result)
        self.core.complete(prepared, completion(prepared), 31)

    def test_first_intent_locks_profile_in_both_directions(self):
        self.research_event()
        prepared, _ = self.core.prepare(send_frame(reply_to="34", text="short"), 100)
        self.core.complete(prepared, completion(prepared), 31)
        self.assertEqual(self.core.prepare(send_frame(reply_to="34", profile="research", text="long"), 100)[1]["status"], "rejected")
        self.research_event(35)
        prepared, _ = self.core.prepare(send_frame(reply_to="35", profile="research", text="long"), 100)
        self.core.complete(prepared, completion(prepared), 31)
        self.assertEqual(self.core.prepare(send_frame(reply_to="35", text="short"), 100)[1]["status"], "rejected")

    def test_research_profile_is_part_of_dedup_digest(self):
        self.research_event()
        frame = send_frame(reply_to="34", profile="research", text="short")
        prepared, _ = self.core.prepare(frame, 100)
        self.core.complete(prepared, completion(prepared), 31)
        self.assertEqual(self.core.prepare(frame, 101)[1]["status"], "native_completed")
        mutated = {key:value for key,value in frame.items() if key != "profile"}
        self.assertEqual(self.core.prepare(mutated, 101)[1]["status"], "rejected")

    def test_research_restart_cannot_refresh_quota_or_authorization(self):
        event = self.research_event()
        for _ in range(2):
            prepared, _ = self.core.prepare(send_frame(reply_to="34", profile="research", text="x" * 350), 100)
            self.core.complete(prepared, completion(prepared), 31)
        self.journal.close()
        self.journal = BridgeJournal(self.path)
        core = BridgeCore(settings(), self.journal, CLIENT)
        self.assertIsNone(core.observe(incoming(native_message_id=34), 101))
        self.assertEqual(core.prepare(send_frame(session=CLIENT, reply_to="34", profile="research"), 101)[1]["status"], "rejected")
        # Even restoring the original session's volatile binding cannot reset
        # the persisted count or switch to the larger chat message count.
        core = BridgeCore(settings(), self.journal, SESSION)
        core.mark_forwarded(event)
        self.assertEqual(core.prepare(send_frame(reply_to="34", profile="research"), 101)[1]["status"], "rejected")
        self.assertEqual(core.prepare(send_frame(reply_to="34"), 101)[1]["status"], "rejected")

    def test_only_known_optional_research_profile_is_accepted(self):
        self.research_event()
        for value in ("chat", "unlimited", None, True, 1, {}):
            with self.subTest(value=value), self.assertRaises(BridgeError):
                self.core.prepare(send_frame(reply_to="34", profile=value), 100)
        with self.assertRaises(BridgeError):
            self.core.prepare(send_frame(reply_to="34", profile="research", extra=True), 100)

    def test_research_never_extends_original_expiry(self):
        self.research_event()
        for now, expiry in ((100, 109), (125, 131), (130, 131)):
            self.assertEqual(self.core.prepare(send_frame(reply_to="34", profile="research", expires_at=expiry), now)[1]["status"], "rejected")

    def test_legacy_schema_migration_keeps_rows_chat_only_and_body_free(self):
        old = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old)) as db:
            db.executescript("""
                CREATE TABLE inbound(account TEXT,room TEXT,id TEXT,session TEXT,observed REAL,
                    PRIMARY KEY(account,room,id));
                CREATE TABLE outbound(request_id TEXT PRIMARY KEY,session TEXT,account TEXT,room TEXT,
                    reply_to TEXT,digest TEXT,characters INTEGER,created REAL,expires REAL,status TEXT);
            """)
            db.execute("INSERT INTO inbound VALUES(?,?,?,?,?)", ("wxid_robot","12345@chatroom","90",SESSION,100))
            db.execute("INSERT INTO outbound VALUES(?,?,?,?,?,?,?,?,?,?)", ("e"*32,SESSION,"wxid_robot","12345@chatroom","90","digest",100,100,108,"native_completed"))
            db.commit()
        migrated = BridgeJournal(old)
        try:
            row = migrated.db.execute("SELECT * FROM inbound").fetchone()
            self.assertEqual((row["research_eligible"], row["delivery_profile"]), (0,None))
            self.assertEqual(migrated.db.execute("SELECT profile,status FROM outbound").fetchone()[:], ("chat","native_completed"))
            core = BridgeCore(settings(), migrated, SESSION)
            core.mark_forwarded({"id":"90", "received_at":100})
            self.assertEqual(core.prepare(send_frame(reply_to="90", profile="research"), 100)[1]["status"], "rejected")
            self.assertEqual(core.prepare(send_frame(reply_to="90", text="x"*26), 100)[1]["status"], "rejected")
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
