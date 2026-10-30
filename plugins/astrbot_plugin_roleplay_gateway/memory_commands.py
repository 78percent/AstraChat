"""Static, scoped memory commands; no model calls or roleplay history writes.

The existing receipt ledger provides persistent at-most-once admission. Its
transaction is separate from memory mutations: a crash may leave a mutation
committed without its acknowledgement. A repeated event is never re-executed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import re

from .context_budget import ContextBudget, estimate_tokens
from .gate import (
    DefiniteSendFailure, Envelope, Sender, incoming_message_id, native_group_id,
    native_person_id, opaque_id, send_evidence,
)
from .memory_store import Namespace


PREFIX = "/记忆"
MAX_COMMAND_CHARACTERS = 4096
MAX_VIEW_CHARACTERS = 4000
USAGE = "用法：/记忆 查看、/记忆 编辑 <正文>、/记忆 清空摘要、/记忆 清空全部。"
DENIED = "没有权限管理该会话的记忆。"
FAILED = "记忆操作失败，未确认完成；请稍后查看当前状态。"


class CommandError(ValueError):
    """Only a static diagnostic code, never command text."""


@dataclass(frozen=True)
class MemoryCommand:
    action: str
    kind: str | None = None
    peer_id: str | None = None
    text: str = ""


def is_memory_command(text: object) -> bool:
    # Malformed continuations such as /记忆查看 are still owned by this handler.
    return isinstance(text, str) and text.lstrip().startswith(PREFIX)


def parse_command(text: str) -> MemoryCommand:
    if not is_memory_command(text) or len(text) > MAX_COMMAND_CHARACTERS or "\0" in text:
        raise CommandError("invalid_command")
    body = text.lstrip()[len(PREFIX):]
    if body and not body[0].isspace():
        raise CommandError("invalid_command")
    body = body.strip()
    first = body.split(None, 1)
    kind = peer_id = None
    if first and first[0] in ("群", "私聊"):
        fields = body.split(None, 2)
        if len(fields) != 3 or not re.fullmatch(r"[1-9][0-9]{0,31}", fields[1]):
            raise CommandError("invalid_target")
        kind = "group" if fields[0] == "群" else "private"
        peer_id, body = fields[1], fields[2]
    fields = body.split(None, 1)
    if not fields:
        raise CommandError("invalid_command")
    actions = {"查看": "view", "编辑": "edit", "清空摘要": "clear_summary", "清空全部": "clear_all"}
    action = actions.get(fields[0])
    if action is None or (action != "edit" and len(fields) != 1):
        raise CommandError("invalid_command")
    content = fields[1].strip() if len(fields) == 2 else ""
    if action == "edit":
        if not content:
            raise CommandError("summary_empty")
        if estimate_tokens(content) > ContextBudget().summary_tokens:
            raise CommandError("summary_budget_exceeded")
    return MemoryCommand(action, kind, peer_id, content)


def command_namespace(command: MemoryCommand, event: Envelope, settings, persona_id: str) -> Namespace:
    """Authorize before any memory lookup; selectors never change reply routing."""
    current_peer = event.group_id if event.kind == "group" else event.sender_id
    id_kind = getattr(settings, "id_kind", "onebot")
    if id_kind == "weixin" and command.kind is not None:
        raise CommandError("permission_denied")
    if id_kind == "wechat_native" and (
            command.kind is not None or event.kind not in ("group", "private")
            or (event.kind == "group" and native_group_id(event.group_id) is None)
            or native_person_id(event.sender_id) is None):
        raise CommandError("permission_denied")
    kind = command.kind or event.kind
    peer_id = command.peer_id or current_peer
    administrators = getattr(settings, "memory_admin_ids", frozenset())
    administrator = event.sender_id in administrators
    if event.kind == "group":
        if not administrator or command.kind is not None:
            raise CommandError("permission_denied")
    elif event.kind != "private":
        raise CommandError("permission_denied")
    elif (kind != "private" or peer_id != event.sender_id) and not administrator:
        raise CommandError("permission_denied")
    permitted = settings.allowed_group_ids if kind == "group" else settings.admin_private_ids
    if kind not in ("private", "group") or peer_id not in permitted:
        raise CommandError("permission_denied")
    return Namespace(settings.platform_id, kind, peer_id, persona_id)


class MemoryCommandService:
    """Independent bounded workers; same-target commands remain ordered.

    Target locks intentionally do not use the roleplay generation lock. A clear
    can invalidate an active response immediately. Its lock includes command
    delivery so a preceding view cannot be sent after a later clear confirms.
    """

    def __init__(self, service, *, max_pending: int = 8):
        self.service = service
        self.max_pending = max_pending
        self._admission_lock = asyncio.Lock()
        self._workers: set[asyncio.Task] = set()
        self._sends: set[asyncio.Task] = set()
        self._targets: dict[object, asyncio.Lock] = {}
        self._closing = False

    def _live(self) -> bool:
        return (not self._closing and not self.service._closing and not self.service.blocked
                and self.service.enabled() is True)

    def _eligible(self, event: Envelope) -> bool:
        settings = self.service.settings
        if (not self._live() or not self.service.owns(event) or event.post_type != "message"
                or event.sender_id == event.bot_self_id):
            return False
        if settings.id_kind == "weixin" and (event.kind != "private" or opaque_id(event.sender_id) is None):
            return False
        if settings.id_kind == "wechat_native" and (
                event.kind not in ("group", "private") or (event.kind == "group" and native_group_id(event.group_id) is None)
                or native_person_id(event.sender_id) is None):
            return False
        if event.kind == "private":
            return event.sender_id in settings.admin_private_ids
        return (event.kind == "group" and event.group_id in settings.allowed_group_ids
                and event.bot_self_id in event.mentions)

    def _log(self, event: Envelope, code: str, **values) -> None:
        self.service._log("memory_command_" + code, event, **values)

    def _done(self, task: asyncio.Task, owned: set[asyncio.Task]) -> None:
        owned.discard(task)
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self.service._log("memory_command_background_error", error_type=type(error).__name__)

    async def accept(self, event: Envelope, sender: Sender) -> str:
        if not is_memory_command(event.text) or not self._eligible(event):
            return "IGNORED"
        incoming_id = incoming_message_id(event.incoming_message_id, self.service.settings.id_kind)
        if incoming_id is None:
            self._log(event, "missing_message_id")
            return "INVALID"
        event = replace(event, incoming_message_id=incoming_id)
        async with self._admission_lock:
            if not self._live():
                return "IGNORED"
            if not await self.service.store.claim(event, purpose="management"):
                self._log(event, "duplicate")
                return "DUPLICATE"
            if not self._live():
                await self._transition(event, "ACCEPTED", "ABORTED", "disabled")
                return "ABORTED"
            if len(self._workers) >= self.max_pending:
                await self._transition(event, "ACCEPTED", "REJECTED", "pending_limit")
                return "REJECTED"
            # Parse and authorize before creating or reading any namespace.
            rejection = None
            command = namespace = None
            try:
                command = parse_command(event.text)
                namespace = command_namespace(command, event, self.service.settings,
                                              self.service.persona.persona_id)
            except CommandError as error:
                rejection = str(error)
            target = namespace if namespace is not None else ("rejected", event.session)
            lock = self._targets.setdefault(target, asyncio.Lock())
            task = asyncio.create_task(self._run(event, sender, command, namespace, rejection, lock),
                                       name="roleplay-memory-command")
            self._workers.add(task)
            task.add_done_callback(lambda done: self._done(done, self._workers))
            return "ACCEPTED"

    async def _transition(self, event, expected, status, reason, receipt=None):
        changed = await self.service.store.transition(event, expected, status,
                                                     "memory_command_" + reason, receipt)
        if changed:
            self._log(event, "result", status=status, reason=reason, outgoing_message_id=receipt)
        return changed

    @staticmethod
    def _scope(namespace: Namespace) -> str:
        kind = "群" if namespace.kind == "group" else "私聊"
        return f"{kind} {namespace.peer_id} · 角色 {namespace.persona_id}"

    async def _execute(self, command: MemoryCommand, namespace: Namespace) -> str:
        memory = self.service.memory
        if command.action == "view":
            snapshot = await memory.snapshot(namespace, limit=0)
            body = snapshot.summary or "（摘要为空；近期对话可能仍有记录。）"
            if len(body) > MAX_VIEW_CHARACTERS:
                body = body[:MAX_VIEW_CHARACTERS] + "\n（仅显示前 4000 字符；现有摘要超出当前预算，请重新编辑。）"
            return f"{self._scope(namespace)}\n摘要版本 {snapshot.summary_revision}：\n{body}"
        if command.action == "edit":
            snapshot = await memory.edit_summary(namespace, command.text)
            result = "摘要已更新；近期对话保留。"
        elif command.action == "clear_summary":
            snapshot = await memory.clear_summary(namespace)
            result = "摘要已清空；近期对话保留，正在进行的回复不取消。"
        elif command.action == "clear_all":
            snapshot = await self.service.clear_memory(namespace)
            result = "摘要与近期对话已清空；旧任务已失效，已提交平台的消息可能仍到达。"
        else:
            raise CommandError("invalid_command")
        return f"{self._scope(namespace)}\n{result}\n摘要版本 {snapshot.summary_revision}。"

    async def _settle_send(self, call: asyncio.Task) -> bool:
        if not call.done():
            if call.cancelling() == 0:
                call.cancel()
            _, pending = await asyncio.wait({call}, timeout=self.service.limits.cleanup_seconds)
            if pending:
                self.service._block("memory_command_send_unsettled")
                return False
        return True

    async def _send(self, event, sender, text, outcome):
        if not self._live():
            await self._transition(event, "IN_FLIGHT", "ABORTED", "before_send")
            return
        call = asyncio.create_task(sender(event, text), name="roleplay-memory-command-send")
        self._sends.add(call)
        call.add_done_callback(lambda done: self._done(done, self._sends))
        try:
            done, _ = await asyncio.wait({call}, timeout=self.service.settings.send_timeout_seconds)
            if not done:
                await self._transition(event, "IN_FLIGHT", "UNKNOWN", outcome + "_send_timeout")
                await self._settle_send(call)
                return
            if not self._live():
                await self._transition(event, "IN_FLIGHT", "UNKNOWN", outcome + "_disabled_after_send")
                return
            try:
                result = call.result()
            except DefiniteSendFailure:
                await self._transition(event, "IN_FLIGHT", "FAILED", outcome + "_platform_rejected")
                return
            except (asyncio.CancelledError, Exception):
                await self._transition(event, "IN_FLIGHT", "UNKNOWN", outcome + "_send_error")
                return
            status, receipt = send_evidence(result, self.service.settings.id_kind)
            suffix = {"SENT": "_acknowledged", "API_ACCEPTED": "_api_accepted", "UNKNOWN": "_missing_receipt"}[status]
            await self._transition(event, "IN_FLIGHT", status, outcome + suffix, receipt)
        finally:
            if not call.done():
                await self._settle_send(call)

    async def _run(self, event, sender, command, namespace, rejection, lock):
        started = False
        try:
            async with lock:
                if not self._live():
                    await self._transition(event, "ACCEPTED", "ABORTED", "disabled")
                    return
                if not await self._transition(event, "ACCEPTED", "IN_FLIGHT", "started"):
                    return
                started = True
                if not self._live():
                    await self._transition(event, "IN_FLIGHT", "ABORTED", "disabled")
                    return
                outcome = "denied" if rejection == "permission_denied" else "invalid"
                if rejection is not None:
                    if rejection == "permission_denied":
                        text = DENIED
                    elif rejection == "summary_budget_exceeded":
                        text = "摘要超过 1200 估算 token，未修改；请缩短后重试。"
                    else:
                        text = USAGE
                else:
                    try:
                        text = await self._execute(command, namespace)
                        outcome = command.action + "_completed"
                    except Exception as error:
                        self._log(event, "operation_failed", error_type=type(error).__name__)
                        text, outcome = FAILED, "operation_failed"
                await self._send(event, sender, text, outcome)
        except asyncio.CancelledError:
            await self._transition(event, "IN_FLIGHT" if started else "ACCEPTED",
                                   "UNKNOWN" if started else "ABORTED", "cancelled")
            raise
        except Exception as error:
            await self._transition(event, "IN_FLIGHT" if started else "ACCEPTED",
                                   "UNKNOWN" if started else "ABORTED", "worker_failed")
            self._log(event, "worker_failed", error_type=type(error).__name__)

    async def wait_idle(self):
        while self._workers:
            await asyncio.gather(*tuple(self._workers), return_exceptions=True)

    async def close(self):
        self._closing = True
        async with self._admission_lock:
            pass
        for task in tuple(self._workers):
            if task.cancelling() == 0:
                task.cancel()
        if self._workers:
            _, pending = await asyncio.wait(tuple(self._workers), timeout=self.service.limits.cleanup_seconds)
            if pending:
                self.service._block("memory_command_worker_unsettled")
                raise RuntimeError("memory_command_worker_unsettled")
        for call in tuple(self._sends):
            if call.cancelling() == 0:
                call.cancel()
        if self._sends:
            _, pending = await asyncio.wait(tuple(self._sends), timeout=self.service.limits.cleanup_seconds)
            if pending:
                self.service._block("memory_command_send_unsettled")
                raise RuntimeError("memory_command_send_unsettled")
