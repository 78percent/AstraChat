"""Independent settings for the locally bound real WeChat group bridge."""
from __future__ import annotations

from collections.abc import Mapping
import re

from .gate import ConfigurationError, GateSettings, ROLEPLAY_MODE

PLATFORM_ID = "wechat-native-sakura"


def bridge_token(config: Mapping) -> str:
    token = config.get("native_bridge_token", "")
    if not isinstance(token, str) or re.fullmatch(r"[a-f0-9]{64}", token) is None:
        raise ConfigurationError("invalid_native_bridge_token")
    return token


def settings_from_config(config: Mapping) -> GateSettings:
    enabled = config.get("native_enabled", False)
    if type(enabled) is not bool:
        raise ConfigurationError("invalid_native_enabled")
    platform = config.get("native_platform_id", PLATFORM_ID)
    if platform != PLATFORM_ID or platform in (
            config.get("platform_id"), config.get("wechat_platform_id")):
        raise ConfigurationError("native_platform_identity_conflict")
    pacing = config.get("reply_pacing", {})
    if not isinstance(pacing, Mapping):
        raise ConfigurationError("invalid_reply_pacing")
    pacing = dict(pacing)
    pacing.update(probability=config.get("native_reply_probability", 1.0))
    raw = dict(config)
    raw.update(
        enabled=enabled, platform_id=platform,
        bot_self_id=config.get("native_self_id", ""),
        admin_private_ids=config.get("native_allowed_private_ids", []), allowed_group_ids=config.get("native_allowed_group_ids", []),
        memory_admin_ids=config.get("native_admin_ids", []),
        wake_names=config.get("native_wake_names", config.get("wake_names", [])),
        wake_regex=config.get("native_wake_regex", config.get("wake_regex", [])),
        reply_pacing=pacing,
    )
    settings = GateSettings.from_mapping(raw, id_kind="wechat_native")
    if enabled:
        bridge_token(config)
        if settings.mode != ROLEPLAY_MODE or len(settings.allowed_group_ids) != 1:
            raise ConfigurationError("native_roleplay_group_binding_required")
    return settings
