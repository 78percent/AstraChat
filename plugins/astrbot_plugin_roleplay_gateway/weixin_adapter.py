"""Version-bound native Weixin entry; credentials remain owned by AstrBot."""
from __future__ import annotations

import inspect
import math
import time
from collections.abc import Mapping

from .gate import (
    ApiAccepted, ConfigurationError, DefiniteSendFailure, Envelope, GateSettings,
    ROLEPLAY_MODE, incoming_message_id, opaque_id,
)

ASTRBOT_VERSION = "4.29.1"
DEFAULT_PLATFORM_ID = "wechat-sakura"


def settings_from_config(config: Mapping) -> GateSettings:
    """Reuse common model/persona/pacing while explicitly replacing QQ identity."""
    raw = dict(config)
    platform_id = config.get("wechat_platform_id", DEFAULT_PLATFORM_ID)
    enabled = config.get("wechat_enabled", False)
    if type(enabled) is not bool:
        raise ConfigurationError("invalid_wechat_enabled")
    if not isinstance(platform_id, str) or opaque_id(platform_id) is None:
        raise ConfigurationError("invalid_wechat_platform_id")
    lists = {}
    for key in ("wechat_allowed_private_ids", "wechat_admin_ids"):
        values = config.get(key, [])
        if (not isinstance(values, list) or len(values) > 32
                or any(not isinstance(value, str) or opaque_id(value) is None for value in values)
                or len(set(values)) != len(values)):
            raise ConfigurationError(f"invalid_{key}")
        lists[key] = values
    allowed, admins = lists["wechat_allowed_private_ids"], lists["wechat_admin_ids"]
    if not set(admins).issubset(allowed):
        raise ConfigurationError("wechat_admin_not_allowed")
    if enabled and (not allowed or not admins or config.get("mode") != ROLEPLAY_MODE):
        raise ConfigurationError("wechat_roleplay_binding_required")
    raw.update(
        enabled=enabled,
        platform_id=platform_id, bot_self_id=platform_id,
        admin_private_ids=allowed, memory_admin_ids=admins,
        allowed_group_ids=[], wake_names=[], wake_regex=[],
    )
    settings = GateSettings.from_mapping(raw, id_kind="weixin")
    if settings.platform_id == str(config.get("platform_id", "")).strip():
        raise ConfigurationError("wechat_platform_conflicts_with_qq")
    return settings


def _runtime_contract():
    import astrbot
    from astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter import WeixinOCAdapter
    from astrbot.core.platform.sources.weixin_oc.weixin_oc_event import WeixinOCMessageEvent
    from astrbot.api.platform import MessageType
    return astrbot.__version__, WeixinOCAdapter, WeixinOCMessageEvent, MessageType.FRIEND_MESSAGE


def check_runtime_contract():
    """Validate the exact native bool-returning entry; never inspect credentials."""
    version, platform_class, event_class, private_type = _runtime_contract()
    if version != ASTRBOT_VERSION:
        raise ConfigurationError("unsupported_wechat_runtime_version")
    method = getattr(platform_class, "_send_items_to_session", None)
    if not inspect.iscoroutinefunction(method):
        raise ConfigurationError("unsupported_wechat_send_method")
    parameters = tuple(inspect.signature(method).parameters.values())
    expected = (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
        ("user_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
        ("item_list", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
        ("cache_components", inspect.Parameter.KEYWORD_ONLY, None),
        ("cache_message_str", inspect.Parameter.KEYWORD_ONLY, None),
    )
    if tuple((p.name, p.kind, p.default) for p in parameters) != expected:
        raise ConfigurationError("unsupported_wechat_send_signature")
    return platform_class, event_class, private_type


def snapshot_weixin_event(event, *, platform_id: str) -> Envelope:
    """Reject random framework IDs, changed identities and non-private payloads."""
    platform_class, event_class, private_type = check_runtime_contract()
    platform = getattr(event, "platform", None)
    if not isinstance(event, event_class) or not isinstance(platform, platform_class):
        raise ValueError("invalid_wechat_event_class")
    meta = platform.meta()
    if (event.get_platform_name() != "weixin_oc" or meta.name != "weixin_oc"
            or event.get_platform_id() != platform_id or meta.id != platform_id
            or event.get_self_id() != platform_id):
        raise ValueError("wechat_platform_identity_mismatch")
    message = event.message_obj
    raw = message.raw_message
    if (not isinstance(raw, Mapping) or raw.get("message_type") != 1
            or event.get_message_type() != private_type
            or event.session.message_type != private_type
            or event.get_group_id() not in (None, "")
            or any(raw.get(key) for key in ("group_id", "chatroom_id"))):
        raise ValueError("wechat_private_only")
    sender = opaque_id(raw.get("from_user_id"))
    if (sender is None or event.get_sender_id() != sender
            or event.get_session_id() != sender
            or getattr(message, "session_id", None) != sender
            or event.session.platform_name != platform_id):
        raise ValueError("wechat_sender_identity_mismatch")
    raw_id = raw.get("message_id") or raw.get("msg_id")
    stable_id = incoming_message_id(raw_id, "weixin")
    if stable_id is None or str(getattr(message, "message_id", "")) != stable_id:
        raise ValueError("wechat_stable_message_id_required")
    # Native message_str excludes quote/reference text and survives waking_check.
    # Accept actual text items only; media captions/transcriptions are not this phase.
    items = raw.get("item_list")
    if not isinstance(items, list):
        raise ValueError("wechat_text_items_required")
    parts = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != 1:
            continue
        text_item = item.get("text_item")
        if isinstance(text_item, Mapping) and isinstance(text_item.get("text"), str):
            parts.append(text_item["text"])
    text = "\n".join(parts).strip()
    if not text:
        raise ValueError("wechat_empty_text")
    received = getattr(event, "created_at", None)
    received_at = (float(received) if isinstance(received, (int, float))
                   and not isinstance(received, bool) and math.isfinite(received) else time.time())
    return Envelope(platform_id=platform_id, bot_self_id=platform_id, sender_id=sender,
                    incoming_message_id=stable_id, kind="private", text=text,
                    received_at=received_at, post_type="message")


async def send_weixin_text(platform, item: Envelope, text: str) -> ApiAccepted:
    platform_class, _, _ = check_runtime_contract()
    if (not isinstance(platform, platform_class) or platform.meta().name != "weixin_oc"
            or platform.meta().id != item.platform_id or item.bot_self_id != item.platform_id
            or item.kind != "private" or opaque_id(item.sender_id) is None
            or item.group_id is not None):
        raise DefiniteSendFailure("wechat_send_identity_mismatch")
    # Check the instance method too: monkeypatches/changed bindings fail closed.
    method = platform._send_items_to_session
    if getattr(method, "__func__", None) is not platform_class._send_items_to_session:
        raise DefiniteSendFailure("wechat_send_binding_changed")
    if not isinstance(text, str) or not text.strip():
        raise DefiniteSendFailure("wechat_empty_send")
    result = await method(item.sender_id, [{"type": 1, "text_item": {"text": text}}])
    if type(result) is bool and result is True:
        return ApiAccepted()
    if type(result) is bool and result is False:
        raise DefiniteSendFailure("wechat_api_rejected")
    # Ambiguous contract changes remain UNKNOWN at the existing delivery boundary.
    raise RuntimeError("wechat_send_result_unknown")
