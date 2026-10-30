"""Authenticated local transport for the pinned Windows native WeChat bridge.

The bridge owns native calls; this endpoint only admits bounded group events
to the existing roleplay service. A native completion is API acceptance, never
a fabricated WeChat message ID or a recipient delivery receipt.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hmac
import json
import math
import re
import time
import uuid

from .gate import ApiAccepted, ConfigurationError, DefiniteSendFailure, Envelope
from .lookup_intent import research_eligible


PLATFORM_ID = "wechat-native-sakura"
PATH = "/wechat-native/ws"
MAX_FRAME_BYTES = 16384
MAX_EVENT_AGE = 30.0
VOICE_EVENT_AGE = 300.0
_ROOM = re.compile(r"[1-9][0-9]{0,117}@chatroom")
_PERSON = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}")
_SYSTEM = frozenset({"filehelper", "weixin", "fmessage", "newsapp", "floatbottle"})
_ACK_RESULTS = frozenset({"ACCEPTED", "IGNORED", "DUPLICATE", "INVALID", "REJECTED", "ABORTED", "SKIPPED"})
_ACK_REASONS = frozenset({"closing", "disabled", "wrong_identity", "not_message", "self_message",
                          "invalid_sender_or_kind", "private_not_allowed", "group_not_allowed",
                          "wake_not_matched"})


def _person(value):
    return (isinstance(value, str) and _PERSON.fullmatch(value) is not None
            and value not in _SYSTEM and not value.startswith("gh_"))


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        return value in (parsed.hex, str(parsed))
    except ValueError:
        return False


def _message_id(value):
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value) is not None
            and int(value) <= 2**64 - 1)


def _object(text):
    """Reject duplicate keys and oversized text before interpreting a frame."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ValueError("native_frame_invalid")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("native_duplicate_key")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("native_object_required")
    return value


@dataclass(eq=False)
class _Binding:
    event: Envelope
    expires_at: float
    server_received_at: float
    research: bool = False
    admitted: bool = False
    prepared: dict = field(default_factory=dict)
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    def estimated_host_now(self, server_now):
        # Keep wire deadlines in the host clock domain. Ignoring inbound transit
        # makes this estimate conservative; the host enforces the final deadline.
        return self.event.received_at + max(0.0, server_now - self.server_received_at)


@dataclass(eq=False)
class _Connection:
    ws: object
    session: str
    client_session: str
    closed: bool = False
    scheduled: bool = False
    events: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    admissions: set = field(default_factory=set)
    writes: asyncio.Lock = field(default_factory=asyncio.Lock)


class NativeBridgeServer:
    def __init__(self, settings, token: str, accept, emit=lambda record: None, *,
                 host: str = "0.0.0.0", port: int = 6198, clock=time.time,
                 research_enabled: bool = False, voice_enabled: bool = False):
        if (settings.platform_id != PLATFORM_ID or settings.id_kind != "wechat_native"
                or not _person(settings.bot_self_id) or not settings.allowed_group_ids
                or any(not isinstance(room, str) or _ROOM.fullmatch(room) is None
                       for room in settings.allowed_group_ids)):
            raise ConfigurationError("invalid_native_bridge_identity")
        if not isinstance(token, str) or re.fullmatch(r"[a-f0-9]{64}", token) is None:
            raise ConfigurationError("invalid_native_bridge_token")
        if host not in {"0.0.0.0", "127.0.0.1"} or type(port) is not int or not 0 <= port <= 65535:
            raise ConfigurationError("invalid_native_bridge_address")
        if not callable(accept) or not callable(emit):
            raise ConfigurationError("invalid_native_bridge_callbacks")
        if type(research_enabled) is not bool:
            raise ConfigurationError("invalid_native_research_enabled")
        self.settings, self._token = settings, token
        self._accept, self._emit, self._clock = accept, emit, clock
        self.research_enabled = research_enabled
        if type(voice_enabled) is not bool:
            raise ConfigurationError("invalid_native_voice_enabled")
        self.voice_enabled = voice_enabled
        self.host, self.port = host, port
        self._runner = self._site = None
        self._connection = None
        self._connecting = False
        self._closing = False
        self._handlers = set()
        self._send_tasks = set()

    @property
    def connected(self):
        return bool(self._connection and not self._connection.closed and not self._closing)

    def _log(self, code):
        self._emit({"component": "wechat_native_bridge", "code": code})

    def authorized(self, header):
        return (isinstance(header, str) and len(header) == 71
                and hmac.compare_digest(header, "Bearer " + self._token))

    async def start(self, *, host=None, port=None):
        if self._runner is not None or self._closing:
            raise RuntimeError("native_bridge_not_startable")
        host = self.host if host is None else host
        port = self.port if port is None else port
        if host not in {"0.0.0.0", "127.0.0.1"} or type(port) is not int or not 0 <= port <= 65535:
            raise ConfigurationError("invalid_native_bridge_address")
        # AstrBot already supplies aiohttp; pure protocol tests need no server SDK.
        from aiohttp import web
        app = web.Application(client_max_size=MAX_FRAME_BYTES)
        app.router.add_get(PATH, self._handle)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner, self._site = runner, site
        self._log("listening")

    def _hello(self, value):
        fields = {"type", "version", "platform_id", "self_id", "session"}
        if (set(value) not in (fields, fields | {'capabilities'})
                or value.get('capabilities', []) not in ([], ['scheduled_text_v1'])
                or value.get("type") != "hello" or type(value.get("version")) is not int
                or value["version"] != 1 or value.get("platform_id") != PLATFORM_ID
                or value.get("self_id") != self.settings.bot_self_id
                or not _uuid(value.get("session"))):
            raise ValueError("native_hello_invalid")
        return value["session"]

    async def _handle(self, request):
        from aiohttp import WSMsgType, web
        if not self.authorized(request.headers.get("Authorization")):
            raise web.HTTPUnauthorized(text="unauthorized")
        if self._closing or self._connecting or self._connection is not None:
            raise web.HTTPConflict(text="unavailable")
        self._connecting = True
        task = asyncio.current_task()
        self._handlers.add(task)
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=20, receive_timeout=45)
        connection = None
        try:
            await ws.prepare(request)
            message = await asyncio.wait_for(ws.receive(), timeout=5)
            if message.type != WSMsgType.TEXT:
                raise ValueError("native_hello_required")
            hello = _object(message.data)
            client_session = self._hello(hello)
            connection = _Connection(ws, uuid.uuid4().hex, client_session)
            connection.scheduled = hello.get('capabilities') == ['scheduled_text_v1']
            self._connection = connection
            self._connecting = False
            await self._write(connection, {"type": "hello_ack", "version": 1,
                                          "client_session": client_session})
            self._log("connected")
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._frame(connection, _object(message.data))
                elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
                else:
                    raise ValueError("native_text_frame_required")
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, TimeoutError):
            self._log("protocol_rejected")
        except Exception:
            self._log("connection_failed")
        finally:
            self._connecting = False
            if connection is not None:
                await self._disconnect(connection)
            elif not ws.closed:
                await ws.close(code=1008, message=b"invalid handshake")
            self._handlers.discard(task)
        return ws

    async def _write(self, connection, value):
        if self._closing or connection.closed or self._connection is not connection:
            raise RuntimeError("native_connection_closed")
        packet = {**value, "session": connection.session}
        async with connection.writes:
            if self._closing or connection.closed or self._connection is not connection:
                raise RuntimeError("native_connection_closed")
            await connection.ws.send_json(packet)

    def _event(self, value):
        expected = {"type", "session", "id", "room", "sender", "text", "mentions", "received_at"}
        if set(value) not in (expected, expected | {"kind", "input_modality"}) or value.get("type") != "event" or not _message_id(value.get("id")):
            raise ValueError("native_event_invalid")
        room, sender = value.get("room"), value.get("sender")
        kind, modality = value.get("kind", "group"), value.get("input_modality", "text")
        if (kind not in ("group", "private") or modality not in ("text", "voice")
                or (modality == "voice" and not self.voice_enabled)
                or (kind == "group" and (not isinstance(room, str) or _ROOM.fullmatch(room) is None
                                         or room not in self.settings.allowed_group_ids))
                or (kind == "private" and (room != sender or sender not in self.settings.admin_private_ids))
                or not _person(sender) or sender == self.settings.bot_self_id):
            raise ValueError("native_event_identity_invalid")
        text, mentions = value.get("text"), value.get("mentions")
        if (not isinstance(text, str) or not text.strip() or "\0" in text
                or len(text.encode("utf-8")) > 1023
                or not isinstance(mentions, list) or len(mentions) > 32
                or any(not _person(member) for member in mentions) or len(set(mentions)) != len(mentions)):
            raise ValueError("native_event_content_invalid")
        received = value.get("received_at")
        if (isinstance(received, bool) or not isinstance(received, (int, float))
                or not math.isfinite(received) or received <= 0
                or not -2 <= self._clock() - received <= MAX_EVENT_AGE):
            raise ValueError("native_event_expired")
        return Envelope(platform_id=PLATFORM_ID, bot_self_id=self.settings.bot_self_id,
                        sender_id=sender, incoming_message_id=value["id"], kind=kind,
                        group_id=room if kind == "group" else None, text=text, mentions=tuple(mentions),
                        received_at=float(received), input_modality=modality)

    async def _frame(self, connection, value):
        if connection.closed or self._connection is not connection or value.get("session") != connection.session:
            raise ValueError("native_session_mismatch")
        if value.get("type") == "voice_prepared":
            if (set(value) != {"type", "session", "request_id", "status"}
                    or not _uuid(value.get("request_id")) or value.get("status") not in {"ready", "fallback"}):
                raise ValueError("native_voice_result_invalid")
            pending = connection.pending.get("voice:" + value["request_id"])
            if pending is not None and not pending.done():
                pending.set_result(value["status"])
            return
        if value.get("type") == "send_result":
            if (set(value) != {"type", "session", "request_id", "status"}
                    or not _uuid(value.get("request_id"))
                    or value.get("status") not in {"native_completed", "unknown", "rejected"}):
                raise ValueError("native_send_result_invalid")
            pending = connection.pending.get(value["request_id"])
            if pending is None or pending.done():
                self._log("late_result_ignored")
            else:
                pending.set_result(value["status"])
            return
        event = self._event(value)
        now = self._clock()
        connection.events = {key: binding for key, binding in connection.events.items()
                             if binding.expires_at > binding.estimated_host_now(now)
                             or not binding.ready.is_set()}
        if event.incoming_message_id in connection.events:
            await self._write(connection, {"type": "event_ack", "id": event.incoming_message_id,
                                          "result": "DUPLICATE"})
            return
        if len(connection.admissions) >= 16 or len(connection.events) >= 256:
            await self._write(connection, {"type": "event_ack", "id": event.incoming_message_id,
                                          "result": "REJECTED"})
            return
        binding = _Binding(event, event.received_at + (VOICE_EVENT_AGE if self.voice_enabled else MAX_EVENT_AGE), now,
                           research=self.research_enabled and research_eligible(event.text))
        connection.events[event.incoming_message_id] = binding
        task = asyncio.create_task(self._admit(connection, binding), name="native-bridge-admission")
        connection.admissions.add(task)
        task.add_done_callback(connection.admissions.discard)

    async def _admit(self, connection, binding):
        async def bound_send(item, text):
            # The closure cannot switch to a later transport session.
            await binding.ready.wait()
            return await self._send(connection, binding, item, text)
        if self.voice_enabled:
            async def prepare(item, text):
                await binding.ready.wait()
                await self._prepare_voice(connection, binding, item, text)
            bound_send.prepare_voice = prepare
        result = "REJECTED"
        try:
            if not connection.closed and not self._closing:
                result = await self._accept(binding.event, bound_send)
                if result not in _ACK_RESULTS:
                    result = "REJECTED"
                binding.admitted = result == "ACCEPTED"
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log("admission_failed")
        finally:
            binding.ready.set()
        if not connection.closed and self._connection is connection and not self._closing:
            try:
                packet = {"type": "event_ack", "id": binding.event.incoming_message_id,
                          "result": str(result)}
                # An arbitrary callback may attach arbitrary data. Only these
                # fixed codes can cross into the persisted local diagnostics.
                reason = getattr(result, "reason", None)
                if isinstance(reason, str) and reason in _ACK_REASONS:
                    packet["reason"] = reason
                await self._write(connection, packet)
            except Exception:
                self._log("admission_ack_failed")

    def _validate_reply(self, connection, binding, item, text, *, voice=False):
        original = binding.event
        identity = ("platform_id", "bot_self_id", "sender_id", "incoming_message_id", "kind", "group_id", "received_at")
        if (not binding.admitted or not isinstance(item, Envelope)
                or any(getattr(item, key) != getattr(original, key) for key in identity)
                or (item.kind == "group" and item.group_id not in self.settings.allowed_group_ids)
                or (item.kind == "private" and item.sender_id not in self.settings.admin_private_ids)
                or not isinstance(text, str) or not text.strip() or "\0" in text
                or len(text.encode("utf-8")) > 1023):
            raise DefiniteSendFailure("native_send_invalid")
        estimated_host_now = binding.estimated_host_now(self._clock())
        deadline = original.received_at + (VOICE_EVENT_AGE if voice else MAX_EVENT_AGE)
        if estimated_host_now >= deadline:
            raise DefiniteSendFailure("native_reply_expired")
        if self._closing or connection.closed or self._connection is not connection:
            raise DefiniteSendFailure("native_reply_disconnected_before_dispatch")
        if len(connection.pending) >= 16:
            raise DefiniteSendFailure("native_send_busy")
        return estimated_host_now, deadline

    async def _prepare_voice(self, connection, binding, item, text):
        self._validate_reply(connection, binding, item, text, voice=True)
        request_id = uuid.uuid4().hex
        pending = asyncio.get_running_loop().create_future()
        key = "voice:" + request_id
        connection.pending[key] = pending
        packet = {"type": "prepare_voice", "request_id": request_id,
                  "reply_to": item.incoming_message_id,
                  "room": item.group_id if item.kind == "group" else item.sender_id, "text": text}
        if binding.research:
            packet["profile"] = "research"
        try:
            await self._write(connection, packet)
            await asyncio.wait_for(asyncio.shield(pending), timeout=180)
            binding.prepared[text] = request_id
        except TimeoutError:
            await self._write(connection, {"type": "cancel_voice", "request_id": request_id})
            binding.prepared[text] = None  # No commit was submitted; text fallback is safe.
        except asyncio.CancelledError:
            if not connection.closed and self._connection is connection and not self._closing:
                await self._write(connection, {"type": "cancel_voice", "request_id": request_id})
            raise
        finally:
            connection.pending.pop(key, None)
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()

    async def _send(self, connection, binding, item, text):
        voice = text in binding.prepared
        estimated_host_now, deadline = self._validate_reply(connection, binding, item, text, voice=voice)
        request_id = uuid.uuid4().hex
        pending = asyncio.get_running_loop().create_future()
        connection.pending[request_id] = pending
        task = asyncio.current_task()
        self._send_tasks.add(task)
        expires_at = min(deadline, estimated_host_now + self.settings.send_timeout_seconds)
        try:
            packet = {"type": "send", "request_id": request_id,
                      "reply_to": item.incoming_message_id,
                      "room": item.group_id if item.kind == "group" else item.sender_id,
                      "text": text, "expires_at": expires_at}
            if voice:
                packet.update(type="commit_voice", handle=binding.prepared.pop(text))
            # This is a snapshot of the authenticated original inbound text,
            # never an instruction carried by the generated reply or item.text.
            if binding.research:
                packet["profile"] = "research"
            await self._write(connection, packet)
            remaining = expires_at - binding.estimated_host_now(self._clock())
            status = await asyncio.wait_for(asyncio.shield(pending),
                                            timeout=max(0.001, min(self.settings.send_timeout_seconds,
                                                                   remaining)))
            if status == "native_completed":
                return ApiAccepted()
            if status == "rejected":
                raise DefiniteSendFailure("native_send_rejected")
            await self._disconnect(connection)
            raise RuntimeError("native_send_unknown")
        except (TimeoutError, asyncio.CancelledError):
            # A timed-out transport request may still be executing natively.
            # Retire this connection so another reply cannot pass it.
            await self._disconnect(connection)
            raise
        finally:
            connection.pending.pop(request_id, None)
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()  # Consume a simultaneous disconnect exception.
            self._send_tasks.discard(task)

    async def send_scheduled(self, kind, peer, text, run_id, task_id):
        connection = self._connection
        if (self._closing or not self.settings.enabled or connection is None or connection.closed
                or not connection.scheduled or not _uuid(run_id) or not _uuid(task_id)
                or run_id in connection.pending or not isinstance(text, str) or not text.strip()
                or len(text) > 125 or '\0' in text
                or kind not in ('group', 'private')
                or peer not in (self.settings.allowed_group_ids if kind == 'group' else self.settings.admin_private_ids)):
            raise DefiniteSendFailure('scheduled_send_unavailable')
        pending = asyncio.get_running_loop().create_future()
        connection.pending[run_id] = pending
        task = asyncio.current_task()
        self._send_tasks.add(task)
        try:
            await self._write(connection, {'type': 'scheduled_send', 'request_id': run_id, 'task_id': task_id,
                'room': peer, 'text': text, 'expires_at': time.time() + 8})
            status = await asyncio.wait_for(asyncio.shield(pending), timeout=8)
            if status == 'native_completed':
                return ApiAccepted()
            if status == 'rejected':
                raise DefiniteSendFailure('scheduled_send_rejected')
            await self._disconnect(connection)
            raise RuntimeError('scheduled_send_unknown')
        except (TimeoutError, asyncio.CancelledError):
            await self._disconnect(connection)
            raise
        finally:
            connection.pending.pop(run_id, None)
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()
            self._send_tasks.discard(task)

    async def _disconnect(self, connection):
        connection.closed = True
        if self._connection is connection:
            self._connection = None
        for pending in tuple(connection.pending.values()):
            if not pending.done():
                pending.set_exception(RuntimeError("native_connection_lost"))
        for task in tuple(connection.admissions):
            task.cancel()
        if connection.admissions:
            await asyncio.gather(*tuple(connection.admissions), return_exceptions=True)
        connection.events.clear()
        if not connection.ws.closed:
            await connection.ws.close()
        self._log("disconnected")

    async def close(self):
        self._closing = True
        connection = self._connection
        if connection is not None:
            await self._disconnect(connection)
        if self._send_tasks:
            await asyncio.gather(*tuple(self._send_tasks), return_exceptions=True)
        current = asyncio.current_task()
        for task in tuple(self._handlers):
            if task is not current:
                task.cancel()
        if self._handlers:
            await asyncio.gather(*(task for task in tuple(self._handlers) if task is not current),
                                 return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = self._site = None
        self._log("closed")
