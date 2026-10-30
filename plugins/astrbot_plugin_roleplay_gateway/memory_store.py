"""Persistent, namespace-isolated history with atomic epoch-checked commits.

Only actual SENT or explicit API_ACCEPTED evidence may create MemoryReply values.
Identifiers are validated here, but this store does not contact platforms or certify
receipt authenticity. Unsent/unknown drafts must never reach commit_turn().
Unlike delivery ledgers, this dedicated database intentionally stores dialogue
text. Nothing is logged. Network/model work must happen outside these methods.

Schema 1 persists recent turns and manual summary state. Automatic summary
jobs, source selection, leases and optimistic summary-worker commits are not
implemented. The floor/watermark columns reserve their clear-boundary semantics.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3
import threading
import time
import json
from typing import Callable, TypeVar


_T = TypeVar("_T")
_NS_WHERE = "platform_id=? AND kind=? AND peer_id=? AND persona_id=?"
_NS_COLUMNS = "platform_id,kind,peer_id,persona_id"


def _identifier(value: str) -> None:
    if (not isinstance(value, str) or not value.strip() or len(value) > 256
            or any(ord(char) < 32 for char in value)):
        raise ValueError("invalid_memory_identifier")


def _text(value: str, *, maximum: int = 100_000) -> None:
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or "\0" in value):
        raise ValueError("invalid_memory_text")


def _version(value: int) -> None:
    if type(value) is not int or not 0 <= value < 2**63 - 1:
        raise ValueError("invalid_memory_version")


@dataclass(frozen=True)
class Namespace:
    platform_id: str
    kind: str
    peer_id: str
    persona_id: str

    def __post_init__(self) -> None:
        for value in (self.platform_id, self.peer_id, self.persona_id):
            _identifier(value)
        if self.kind not in ("private", "group"):
            raise ValueError("invalid_memory_kind")

    @property
    def values(self) -> tuple[str, str, str, str]:
        return self.platform_id, self.kind, self.peer_id, self.persona_id


@dataclass(frozen=True)
class MemoryInput:
    message_id: str
    sender_id: str
    display_name: str
    text: str
    edited: bool = False

    def __post_init__(self) -> None:
        for value in (self.message_id, self.sender_id, self.display_name):
            _identifier(value)
        _text(self.text)


@dataclass(frozen=True)
class MemoryReply:
    sequence: int
    message_id: str | None
    text: str
    delivery_status: str = "SENT"
    delivery_ref: str | None = None
    edited: bool = False

    def __post_init__(self) -> None:
        _version(self.sequence)
        if self.delivery_status == "SENT":
            _identifier(self.message_id)
            if self.delivery_ref is not None:
                _identifier(self.delivery_ref)
        elif self.delivery_status == "API_ACCEPTED":
            if self.message_id is not None:
                raise ValueError("api_accepted_has_no_platform_message_id")
            _identifier(self.delivery_ref)
        else:
            raise ValueError("unconfirmed_memory_reply")
        _text(self.text)

    @property
    def evidence_key(self) -> tuple[str, str]:
        return (self.delivery_status,
                self.message_id if self.delivery_status == "SENT" else self.delivery_ref)


@dataclass(frozen=True)
class StoredTurn:
    sequence: int
    batch_id: str
    persona_revision: str
    inputs: tuple[MemoryInput, ...]
    replies: tuple[MemoryReply, ...]
    complete: bool


@dataclass(frozen=True)
class MemorySnapshot:
    namespace: Namespace
    epoch: int
    summary_revision: int
    summary: str
    turns: tuple[StoredTurn, ...]
    last_sequence: int


async def _offload(work: Callable[[], _T]) -> _T:
    """A cancelled await still waits until its SQLite thread has settled."""
    task = asyncio.create_task(asyncio.to_thread(work))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    if cancelled:
        # Retrieve a worker failure without replacing the caller's cancellation.
        try:
            task.result()
        except BaseException:
            pass
        raise asyncio.CancelledError
    return task.result()


@contextmanager
def _transaction(db: sqlite3.Connection):
    # IMMEDIATE serializes check+write across separate store instances too.
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


class MemoryStore:
    _APPLICATION_ID = 0x52504D45
    _SCHEMA_VERSION = 1
    _SCHEMA = {
        "memory_namespaces": (
            "platform_id", "kind", "peer_id", "persona_id", "epoch",
            "summary_revision", "summary", "summary_source_floor",
            "summary_source_watermark", "last_sequence", "created_at", "updated_at",
        ),
        "memory_turns": (
            "platform_id", "kind", "peer_id", "persona_id", "epoch", "sequence",
            "batch_id", "persona_revision", "inputs_json", "replies_json",
            "complete", "created_at",
        ),
    }

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    async def open(self) -> None:
        def work() -> None:
            with self._lock:
                if self._connection is not None:
                    raise RuntimeError("memory_store_already_open")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, timeout=5, check_same_thread=False,
                                     isolation_level=None)
                db.row_factory = sqlite3.Row
                try:
                    db.execute("PRAGMA busy_timeout=5000")
                    db.execute("PRAGMA foreign_keys=ON")
                    with _transaction(db):
                        self._migrate(db)
                    db.execute("PRAGMA journal_mode=WAL")
                    self._connection = db
                except BaseException:
                    db.close()
                    raise
        await _offload(work)

    @classmethod
    def _migrate(cls, db: sqlite3.Connection) -> None:
        app = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if app == 0 and version == 0 and not tables:
            db.execute("""CREATE TABLE memory_namespaces (
                platform_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('private','group')),
                peer_id TEXT NOT NULL, persona_id TEXT NOT NULL,
                epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch>=0),
                summary_revision INTEGER NOT NULL DEFAULT 0 CHECK(summary_revision>=0),
                summary TEXT NOT NULL DEFAULT '',
                summary_source_floor INTEGER NOT NULL DEFAULT 0,
                summary_source_watermark INTEGER NOT NULL DEFAULT 0,
                last_sequence INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY(platform_id,kind,peer_id,persona_id)
            )""")
            db.execute("""CREATE TABLE memory_turns (
                platform_id TEXT NOT NULL, kind TEXT NOT NULL, peer_id TEXT NOT NULL,
                persona_id TEXT NOT NULL, epoch INTEGER NOT NULL, sequence INTEGER NOT NULL,
                batch_id TEXT NOT NULL, persona_revision TEXT NOT NULL,
                inputs_json TEXT NOT NULL, replies_json TEXT NOT NULL,
                complete INTEGER NOT NULL CHECK(complete IN (0,1)), created_at REAL NOT NULL,
                PRIMARY KEY(platform_id,kind,peer_id,persona_id,epoch,sequence),
                UNIQUE(platform_id,kind,peer_id,persona_id,epoch,batch_id),
                FOREIGN KEY(platform_id,kind,peer_id,persona_id)
                    REFERENCES memory_namespaces(platform_id,kind,peer_id,persona_id)
            )""")
            db.execute(f"PRAGMA application_id={cls._APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={cls._SCHEMA_VERSION}")
        elif app != cls._APPLICATION_ID or version != cls._SCHEMA_VERSION:
            raise ValueError("unsupported_memory_schema")
        if {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            } != set(cls._SCHEMA):
            raise ValueError("invalid_memory_schema")
        for table, expected in cls._SCHEMA.items():
            # The table identifiers are fixed module constants, never user input.
            actual = tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})"))
            if actual != expected:
                raise ValueError("invalid_memory_schema")

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("memory_store_closed")
        return self._connection

    @staticmethod
    def _namespace(namespace: Namespace) -> None:
        if not isinstance(namespace, Namespace):
            raise ValueError("invalid_memory_namespace")

    @staticmethod
    def _ensure(db: sqlite3.Connection, namespace: Namespace) -> sqlite3.Row:
        now = time.time()
        db.execute(f"""INSERT OR IGNORE INTO memory_namespaces
            ({_NS_COLUMNS},created_at,updated_at) VALUES (?,?,?,?,?,?)""",
                   (*namespace.values, now, now))
        return db.execute(f"SELECT * FROM memory_namespaces WHERE {_NS_WHERE}",
                          namespace.values).fetchone()

    @staticmethod
    def _snapshot(db: sqlite3.Connection, namespace: Namespace,
                  row: sqlite3.Row, limit: int) -> MemorySnapshot:
        records = db.execute(f"""SELECT sequence,batch_id,persona_revision,inputs_json,
            replies_json,complete FROM memory_turns WHERE {_NS_WHERE} AND epoch=?
            ORDER BY sequence DESC LIMIT ?""", (*namespace.values, row["epoch"], limit)).fetchall()
        try:
            turns = tuple(StoredTurn(
                record["sequence"], record["batch_id"], record["persona_revision"],
                tuple(MemoryInput(**value) for value in json.loads(record["inputs_json"])),
                tuple(MemoryReply(**value) for value in json.loads(record["replies_json"])),
                bool(record["complete"]),
            ) for record in reversed(records))
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid_stored_memory_turn") from None
        return MemorySnapshot(namespace, row["epoch"], row["summary_revision"],
                              row["summary"], turns, row["last_sequence"])

    async def snapshot(self, namespace: Namespace, limit: int = 12) -> MemorySnapshot:
        self._namespace(namespace)
        if type(limit) is not int or not 0 <= limit <= 1000:
            raise ValueError("invalid_memory_limit")
        def work() -> MemorySnapshot:
            with self._lock:
                db = self._db()
                with _transaction(db):
                    row = self._ensure(db, namespace)
                    return self._snapshot(db, namespace, row, limit)
        return await _offload(work)

    async def commit_turn(self, namespace: Namespace, *, expected_epoch: int,
                          batch_id: str, persona_revision: str,
                          inputs: tuple[MemoryInput, ...], replies: tuple[MemoryReply, ...],
                          complete: bool) -> bool:
        self._namespace(namespace)
        _version(expected_epoch)
        _identifier(batch_id)
        _identifier(persona_revision)
        if (not isinstance(inputs, tuple) or not 1 <= len(inputs) <= 32
                or any(not isinstance(value, MemoryInput) for value in inputs)
                or len({value.message_id for value in inputs}) != len(inputs)):
            raise ValueError("invalid_memory_inputs")
        if (not isinstance(replies, tuple) or not 1 <= len(replies) <= 512
                or any(not isinstance(value, MemoryReply) for value in replies)
                or len({value.evidence_key for value in replies}) != len(replies)
                or tuple(value.sequence for value in replies) != tuple(sorted(
                    {value.sequence for value in replies}))):
            raise ValueError("invalid_confirmed_memory_replies")
        if type(complete) is not bool:
            raise ValueError("invalid_memory_completion")
        inputs_json = json.dumps([asdict(value) for value in inputs], ensure_ascii=False)
        # Keep QQ's legacy payload readable by the previous MemoryReply class.
        # Its local dispatch reference remains in the delivery ledger.
        replies_json = json.dumps([
            {"sequence": value.sequence, "message_id": value.message_id, "text": value.text}
            if value.delivery_status == "SENT" else asdict(value)
            for value in replies], ensure_ascii=False)
        if len(inputs_json) + len(replies_json) > 1_000_000:
            raise ValueError("memory_turn_too_large")
        return await _offload(lambda: self._commit_turn_sync(
            namespace, expected_epoch, batch_id, persona_revision, inputs_json, replies_json, complete))

    def _commit_turn_sync(self, namespace: Namespace, expected_epoch: int,
                          batch_id: str, persona_revision: str, inputs_json: str,
                          replies_json: str, complete: bool) -> bool:
        with self._lock:
            db = self._db()
            with _transaction(db):
                row = self._ensure(db, namespace)
                if row["epoch"] != expected_epoch:
                    return False
                if db.execute(f"""SELECT 1 FROM memory_turns WHERE {_NS_WHERE}
                    AND epoch=? AND batch_id=?""", (*namespace.values, expected_epoch, batch_id)).fetchone():
                    return False
                sequence = row["last_sequence"] + 1
                now = time.time()
                db.execute(f"""INSERT INTO memory_turns ({_NS_COLUMNS},epoch,sequence,batch_id,
                    persona_revision,inputs_json,replies_json,complete,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (*namespace.values, expected_epoch, sequence, batch_id,
                            persona_revision, inputs_json, replies_json, int(complete), now))
                db.execute(f"UPDATE memory_namespaces SET last_sequence=?,updated_at=? WHERE {_NS_WHERE}",
                           (sequence, now, *namespace.values))
                return True

    async def edit_summary(self, namespace: Namespace, text: str) -> MemorySnapshot:
        """Manual replacement only; automatic summaries need a future CAS API."""
        self._namespace(namespace)
        _text(text, maximum=64_000)
        return await self._change_summary(namespace, text, clear=False, all_memory=False)

    async def console_change(self, namespace: Namespace, payload: dict):
        """Explicit operator edits with a recovery snapshot and revision check."""
        from .console_memory import change
        return await _offload(lambda: change(self, namespace, payload))

    async def clear_summary(self, namespace: Namespace) -> MemorySnapshot:
        self._namespace(namespace)
        return await self._change_summary(namespace, "", clear=True, all_memory=False)

    async def clear_all(self, namespace: Namespace) -> MemorySnapshot:
        self._namespace(namespace)
        return await self._change_summary(namespace, "", clear=True, all_memory=True)

    async def _change_summary(self, namespace: Namespace, text: str, *,
                              clear: bool, all_memory: bool) -> MemorySnapshot:
        def work() -> MemorySnapshot:
            with self._lock:
                db = self._db()
                with _transaction(db):
                    row = self._ensure(db, namespace)
                    floor = row["last_sequence"] if clear else row["summary_source_floor"]
                    watermark = row["last_sequence"] if clear else row["summary_source_watermark"]
                    if all_memory:
                        db.execute(f"DELETE FROM memory_turns WHERE {_NS_WHERE}", namespace.values)
                    db.execute(f"""UPDATE memory_namespaces SET summary=?,summary_revision=summary_revision+1,
                        epoch=epoch+?,summary_source_floor=?,summary_source_watermark=?,updated_at=?
                        WHERE {_NS_WHERE}""",
                               (text, int(all_memory), floor, watermark, time.time(), *namespace.values))
                    row = db.execute(f"SELECT * FROM memory_namespaces WHERE {_NS_WHERE}",
                                     namespace.values).fetchone()
                    return self._snapshot(db, namespace, row, 12)
        return await _offload(work)

    async def close(self) -> None:
        def work() -> None:
            with self._lock:
                if self._connection is not None:
                    self._connection.close()
                    self._connection = None
        await _offload(work)
