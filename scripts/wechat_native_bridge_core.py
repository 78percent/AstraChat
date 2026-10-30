"""Bounded host policy and durable IDs for the native WeChat group bridge.

No native imports or network operations. SQLite stores IDs, digests and evidence,
never message bodies. Unrelated native payloads are filtered before text access.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid
from xml.etree import ElementTree

from plugins.astrbot_plugin_roleplay_gateway.lookup_intent import research_eligible


COMMIT = "a81c495770b397646ba0661118927c83589c41fc"
VOICE_COMMIT = "233c29ca061ac03e9ef4717ade8712dab941d1f7"
VOICE_MAX_AGE = 300.0
PLATFORM = "wechat-native-sakura"
URL = "ws://127.0.0.1:6198/wechat-native/ws"
_ROOM = re.compile(r"[1-9][0-9]{0,117}@chatroom")
_PERSON = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}")
_SYSTEM = frozenset({"filehelper", "weixin", "fmessage", "newsapp", "floatbottle"})
MAX_AGE = 30.0
SEND_WINDOW = 8.0
MAX_PARTS = 4
MAX_CHARACTERS = 125
RESEARCH_MAX_PARTS = 2
RESEARCH_MAX_CHARACTERS = 800


class BridgeError(RuntimeError):
    """Static codes only; do not include private input or native exception text."""


def person(value):
    return (isinstance(value, str) and _PERSON.fullmatch(value) is not None
            and value not in _SYSTEM and not value.startswith("gh_"))


def positive_id(value):
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
            and int(value) <= 2**64 - 1)


def uuid_text(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is None:
        return False
    try:
        return uuid.UUID(value).int != 0
    except ValueError:
        return False


def timestamp(value):
    return (type(value) in (int, float) and math.isfinite(value) and value > 0)


@dataclass(frozen=True)
class BridgeSettings:
    enabled: bool
    expected_self: str = field(repr=False)
    allowed_room: str = field(repr=False)
    token: str = field(repr=False)
    platform_id: str = PLATFORM
    url: str = URL
    upstream_commit: str = COMMIT
    private_peer: str | None = field(default=None, repr=False)

    @classmethod
    def parse(cls, raw):
        expected = {"enabled", "expected_self", "allowed_room", "token", "platform_id", "url", "upstream_commit"}
        if not isinstance(raw, dict) or set(raw) not in (expected, expected | {"private_peer"}):
            raise BridgeError("invalid_config_fields")
        if type(raw["enabled"]) is not bool:
            raise BridgeError("invalid_enabled")
        if not person(raw["expected_self"]):
            raise BridgeError("invalid_account_binding")
        if not isinstance(raw["allowed_room"], str) or _ROOM.fullmatch(raw["allowed_room"]) is None:
            raise BridgeError("invalid_room_binding")
        if not isinstance(raw["token"], str) or re.fullmatch(r"[0-9a-f]{64}", raw["token"]) is None:
            raise BridgeError("invalid_bridge_token")
        if raw["platform_id"] != PLATFORM or raw["url"] != URL or raw["upstream_commit"] not in (COMMIT, VOICE_COMMIT):
            raise BridgeError("unsupported_bridge_binding")
        if raw.get("private_peer") is not None and (raw["upstream_commit"] != VOICE_COMMIT
                or not person(raw["private_peer"]) or raw["private_peer"] == raw["expected_self"]):
            raise BridgeError("invalid_private_binding")
        return cls(**raw)


def decode_frame(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16384:
        raise BridgeError("invalid_wire_frame")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BridgeError("duplicate_wire_key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise BridgeError("invalid_wire_json") from None
    if not isinstance(value, dict):
        raise BridgeError("invalid_wire_object")
    return value


def handshake_ack(frame, client_session):
    if (set(frame) != {"type", "version", "session", "client_session"}
            or frame.get("type") != "hello_ack" or type(frame.get("version")) is not int
            or frame["version"] != 1 or not uuid_text(frame.get("session"))
            or frame.get("client_session") != client_session):
        raise BridgeError("invalid_hello_ack")
    return frame["session"]


def _identifier(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= 127:
        return None
    try:
        return raw.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return None


def self_mentions(source, self_id):
    """Return only the bot's verified mention, never infer from visible labels."""
    if not isinstance(source, bytes) or len(source) > 4095 or b"\0" in source:
        return []
    try:
        text = source.decode("utf-8", errors="strict")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            return []
        root = ElementTree.fromstring(text)
    except (UnicodeDecodeError, ElementTree.ParseError, ValueError):
        return []
    if root.tag not in {"msgsource", "atuserlist"}:
        return []
    nodes = [root] if root.tag == "atuserlist" else list(root.iter("atuserlist"))
    if len(nodes) != 1 or len(nodes[0]) or not nodes[0].text:
        return []
    values = [value.strip() for value in nodes[0].text.split(",")]
    if len(values) > 128 or any(not person(value) for value in values):
        return []
    return [self_id] if self_id in values else []


def native_event(raw, settings, session, now, *, voice=False):
    """Return one allowed text event, or None without exposing rejected data."""
    if not timestamp(now):
        raise BridgeError("invalid_clock")
    if type(getattr(raw, "type", None)) is not int or raw.type != (34 if voice else 1):
        return None
    if _identifier(getattr(raw, "receiver", None)) != settings.expected_self:
        return None
    target = _identifier(getattr(raw, "sender", None))
    if target is None or target not in (settings.allowed_room, settings.private_peer):
        return None
    private = target == settings.private_peer
    mid, sequence = getattr(raw, "native_message_id", None), getattr(raw, "sequence", None)
    if (type(mid) is not int or not 1 <= mid <= 2**64 - 1
            or type(sequence) is not int or not 1 <= sequence <= 2**64 - 1):
        return None
    value = getattr(raw, "image_xml" if voice else "text", None)
    if not isinstance(value, bytes) or not 1 <= len(value) <= (16384 if voice else 1023) or b"\0" in value:
        return None
    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    member, separator, body = (target, True, text) if private else text.partition(":\n")
    if not separator or not person(member) or member == settings.expected_self or not body.strip():
        return None
    mentions = self_mentions(getattr(raw, "mention_source", b""), settings.expected_self)
    event = {"type": "event", "session": session, "id": str(mid),
             "room": target, "sender": member, "text": body,
             "mentions": mentions, "received_at": float(now)}
    if settings.upstream_commit == VOICE_COMMIT:
        event.update(kind="private" if private else "group", input_modality="voice" if voice else "text")
    return event


class BridgeJournal:
    """Durable at-most-once dispatch intent. No original text is written."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS inbound (
                account TEXT NOT NULL, room TEXT NOT NULL, id TEXT NOT NULL,
                session TEXT NOT NULL, observed REAL NOT NULL,
                PRIMARY KEY(account,room,id));
            CREATE TABLE IF NOT EXISTS outbound (
                request_id TEXT PRIMARY KEY, session TEXT NOT NULL,
                account TEXT NOT NULL, room TEXT NOT NULL, reply_to TEXT NOT NULL,
                digest TEXT NOT NULL, characters INTEGER NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('in_flight','native_completed','unknown','rejected')));
        """)
        # Only the locally observed original text may grant a larger reply.
        # Old rows remain chat-only; no historical text is reconstructed/stored.
        inbound_columns = {row[1] for row in self.db.execute("PRAGMA table_info(inbound)")}
        outbound_columns = {row[1] for row in self.db.execute("PRAGMA table_info(outbound)")}
        with self.db:
            if "research_eligible" not in inbound_columns:
                self.db.execute("ALTER TABLE inbound ADD COLUMN research_eligible INTEGER NOT NULL DEFAULT 0 CHECK(research_eligible IN (0,1))")
            if "delivery_profile" not in inbound_columns:
                self.db.execute("ALTER TABLE inbound ADD COLUMN delivery_profile TEXT CHECK(delivery_profile IN ('chat','research'))")
            if "profile" not in outbound_columns:
                self.db.execute("ALTER TABLE outbound ADD COLUMN profile TEXT NOT NULL DEFAULT 'chat' CHECK(profile IN ('chat','research'))")
        # A previous process may have performed the side effect before dying.
        with self.db:
            self.db.execute("UPDATE outbound SET status='unknown' WHERE status='in_flight'")

    def claim_event(self, settings, session, event):
        with self.db:
            cursor = self.db.execute("""INSERT OR IGNORE INTO inbound
                (account,room,id,session,observed,research_eligible) VALUES(?,?,?,?,?,?)""",
                (settings.expected_self, event["room"], event["id"], session,
                 event["received_at"], int(research_eligible(event["text"]))))
        return cursor.rowcount == 1

    def event_time(self, settings, session, incoming, room=None):
        row = self.db.execute("SELECT observed FROM inbound WHERE account=? AND room=? AND id=? AND session=?",
                              (settings.expected_self, room or settings.allowed_room, incoming, session)).fetchone()
        return None if row is None else row[0]

    def existing(self, request_id):
        return self.db.execute("SELECT * FROM outbound WHERE request_id=?", (request_id,)).fetchone()

    def reserve(self, settings, session, frame, digest, now):
        """Serialize quota with dispatch intent; reserve occurs before native send."""
        profile = frame.get("profile", "chat")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            binding = self.db.execute("""SELECT research_eligible,delivery_profile FROM inbound
                WHERE account=? AND room=? AND id=? AND session=?""",
                (settings.expected_self, frame["room"], frame["reply_to"], session)).fetchone()
            if (binding is None or profile not in {"chat", "research"}
                    or (profile == "research" and binding["research_eligible"] != 1)
                    or binding["delivery_profile"] not in (None, profile)):
                return False
            # Include every persisted intent, including unknown/rejected sends.
            # A restart or a profile switch never creates a fresh quota.
            usage = self.db.execute("SELECT COUNT(*), COALESCE(SUM(characters),0) FROM outbound WHERE account=? AND room=? AND reply_to=?",
                                    (settings.expected_self, frame["room"], frame["reply_to"])).fetchone()
            previous_profiles = {row[0] for row in self.db.execute(
                "SELECT DISTINCT profile FROM outbound WHERE account=? AND room=? AND reply_to=?",
                (settings.expected_self, frame["room"], frame["reply_to"]))}
            if previous_profiles - {profile}:
                return False
            max_parts, max_characters = ((RESEARCH_MAX_PARTS, RESEARCH_MAX_CHARACTERS)
                                         if profile == "research" else (MAX_PARTS, MAX_CHARACTERS))
            if usage[0] >= max_parts or usage[1] + len(frame["text"]) > max_characters:
                return False
            self.db.execute("""UPDATE inbound SET delivery_profile=?
                WHERE account=? AND room=? AND id=? AND session=?""",
                (profile, settings.expected_self, frame["room"], frame["reply_to"], session))
            self.db.execute("""INSERT INTO outbound
                (request_id,session,account,room,reply_to,digest,characters,created,expires,status,profile)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (frame["request_id"], session, settings.expected_self, frame["room"],
                             frame["reply_to"], digest, len(frame["text"]), now, frame["expires_at"], "in_flight", profile))
        return True

    def finish(self, request_id, status):
        if status not in {"native_completed", "unknown", "rejected"}:
            raise BridgeError("invalid_delivery_state")
        with self.db:
            updated = self.db.execute("UPDATE outbound SET status=? WHERE request_id=? AND status='in_flight'",
                                      (status, request_id)).rowcount
        if updated != 1:
            raise BridgeError("delivery_intent_lost")

    def close(self):
        self.db.close()


@dataclass(frozen=True)
class PreparedSend:
    request_id: str
    reply_to: str
    room: str = field(repr=False)
    text: str = field(repr=False)
    expires_at: float


class BridgeCore:
    def __init__(self, settings, journal, session):
        if not uuid_text(session):
            raise BridgeError("invalid_server_session")
        self.settings, self.journal, self.session = settings, journal, session
        self.sent_ids = {}
        self.events = {}
        self.counts = {"observed": 0, "filtered": 0, "duplicate_events": 0,
                       "forwarded": 0, "acked": 0, "dispatches": 0,
                       "native_completed": 0, "unknown": 0, "rejected": 0}
        self.ack_results = {}
        self.last_ack = None
        self.last_event = None

    def observe(self, raw, now):
        self.counts["observed"] += 1
        event = native_event(raw, self.settings, self.session, now)
        if event is None:
            self.counts["filtered"] += 1
            return None
        if not self.journal.claim_event(self.settings, self.session, event):
            self.counts["duplicate_events"] += 1
            return None
        self.last_event = {"mention_source_present": bool(getattr(raw, "mention_source", b"")),
                           "self_mentioned": self.settings.expected_self in event["mentions"]}
        return event

    def mark_forwarded(self, event):
        # Called only after WS send returned; authorization is connection-local.
        self.sent_ids = {mid: observed for mid, observed in self.sent_ids.items()
                         if observed + VOICE_MAX_AGE >= event["received_at"]}
        if len(self.sent_ids) >= 4096:
            raise BridgeError("connection_event_capacity")
        self.sent_ids[event["id"]] = event["received_at"]
        self.events = {key: value for key, value in self.events.items() if key in self.sent_ids}
        self.events[event["id"]] = event
        self.counts["forwarded"] += 1

    def _session(self, frame):
        if not isinstance(frame, dict) or frame.get("session") != self.session:
            raise BridgeError("wire_session_mismatch")

    def acknowledge(self, frame):
        self._session(frame)
        if (set(frame) not in ({"type", "session", "id", "result"},
                              {"type", "session", "id", "result", "reason"})
                or frame.get("type") != "event_ack" or not positive_id(frame.get("id"))
                or not isinstance(frame.get("result"), str) or not 1 <= len(frame["result"]) <= 64):
            raise BridgeError("invalid_event_ack")
        if self.journal.event_time(self.settings, self.session, frame["id"], self.events.get(frame["id"], {}).get("room")) is None:
            raise BridgeError("unknown_event_ack")
        result = frame["result"].upper()
        if result not in {"ACCEPTED", "IGNORED", "DUPLICATE", "INVALID", "REJECTED", "ABORTED", "SKIPPED"}:
            result = "OTHER"
        reason = frame.get("reason")
        if not isinstance(reason, str) or reason not in {
                "closing", "disabled", "wrong_identity", "not_message", "self_message",
                "invalid_sender_or_kind", "private_not_allowed", "group_not_allowed", "wake_not_matched"}:
            reason = "unspecified"
        self.counts["acked"] += 1
        self.ack_results[result] = self.ack_results.get(result, 0) + 1
        self.last_ack = {"result": result, "reason": reason}

    def result(self, request_id, status):
        return {"type": "send_result", "session": self.session,
                "request_id": request_id, "status": status}

    def prepare(self, frame, now, *, voice_authorized=False):
        """Return (prepared, immediate_response), reserving before any side effect."""
        self._session(frame)
        base_fields = {"type", "session", "request_id", "reply_to", "room", "text", "expires_at"}
        if (set(frame) not in (base_fields, base_fields | {"profile"})
                or ("profile" in frame and frame["profile"] != "research")
                or frame.get("type") != "send" or not uuid_text(frame.get("request_id"))):
            raise BridgeError("invalid_send_frame")
        request_id = frame["request_id"]
        def rejected():
            self.counts["rejected"] += 1
            return None, self.result(request_id, "rejected")
        if (not timestamp(now) or not positive_id(frame["reply_to"])
                or frame["reply_to"] not in self.sent_ids or frame["room"] != self.events.get(frame["reply_to"], {}).get("room")
                or not isinstance(frame["text"], str) or not frame["text"].strip()
                or "\0" in frame["text"] or len(frame["text"].encode("utf-8")) > 1023
                or not timestamp(frame["expires_at"])):
            return rejected()
        intent = [self.session, frame["reply_to"], frame["room"], frame["text"], frame["expires_at"]]
        if "profile" in frame:
            intent.append(frame["profile"])
        digest = hashlib.sha256(json.dumps(intent, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        existing = self.journal.existing(request_id)
        if existing is not None:
            if existing["digest"] != digest or existing["session"] != self.session:
                return rejected()
            status = "unknown" if existing["status"] == "in_flight" else existing["status"]
            return None, self.result(request_id, status)
        observed = self.journal.event_time(self.settings, self.session, frame["reply_to"], frame["room"])
        if (observed is None or now < observed or now >= frame["expires_at"]
                or frame["expires_at"] > now + SEND_WINDOW
                or frame["expires_at"] > observed + (VOICE_MAX_AGE if voice_authorized else MAX_AGE)):
            return rejected()
        if not self.journal.reserve(self.settings, self.session, frame, digest, now):
            return rejected()
        return PreparedSend(request_id, frame["reply_to"], frame["room"], frame["text"], frame["expires_at"]), None

    def cancel_before_dispatch(self, prepared):
        self.journal.finish(prepared.request_id, "rejected")
        self.counts["rejected"] += 1
        return self.result(prepared.request_id, "rejected")

    def complete(self, prepared, outcome, pid):
        good = False
        if isinstance(outcome, dict):
            report = outcome.get("report")
            good = (type(pid) is int and pid > 0 and type(outcome.get("pid")) is int and outcome["pid"] == pid
                    and type(outcome.get("code")) is int and outcome["code"] == 0
                    and type(outcome.get("cleanup_code")) is int and outcome["cleanup_code"] == 0
                    and outcome.get("success") is True and isinstance(report, dict)
                    and report.get("completion_success") is True
                    and report.get("target") == prepared.room and report.get("text") == prepared.text)
        status = "native_completed" if good else "unknown"
        self.journal.finish(prepared.request_id, status)
        self.counts[status] += 1
        return self.result(prepared.request_id, status)
