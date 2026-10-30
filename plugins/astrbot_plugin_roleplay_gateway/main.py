"""AstrBot v4.29.1 adapter for receipts, neutral streams and scoped roleplay."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import time
import uuid
from collections.abc import Mapping

from aiocqhttp.exceptions import ActionFailed
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .gate import (
    DefiniteSendFailure,
    Envelope,
    GateService,
    GateSettings,
    ReceiptStore,
    ConfigurationError,
    STREAM_MODE,
    ROLEPLAY_MODE,
    STREAM_PROVIDER_ID,
    message_id,
)
from .qq_text import format_qq_bubble
from .memory_commands import is_memory_command


PLUGIN_NAME = "astrbot_plugin_roleplay_gateway"


def snapshot_event(event: AstrMessageEvent) -> Envelope:
    """Take original OneBot text, not the prefix-stripped waking_check text."""
    raw = event.message_obj.raw_message
    if not isinstance(raw, Mapping):
        raise ValueError("missing_onebot_event")
    parts, mentions = [], []
    segments = raw.get("message", [])
    if not isinstance(segments, list):
        raise ValueError("onebot_message_not_array")
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        data = segment.get("data", {})
        if not isinstance(data, Mapping):
            continue
        if segment.get("type") == "text" and isinstance(data.get("text"), str):
            parts.append(data["text"])
        elif segment.get("type") == "at":
            mentions.append(str(data.get("qq", "")))
        # reply and at-all do not become aliases or implicit wake conditions.
    kind = raw.get("message_type", "other")
    received_at = getattr(event, "created_at", None)
    return Envelope(
        platform_id=str(event.get_platform_id()),
        bot_self_id=str(event.get_self_id()),
        sender_id=str(event.get_sender_id()),
        incoming_message_id=message_id(raw.get("message_id")),
        kind=kind if kind in {"private", "group"} else "other",
        group_id=str(event.get_group_id()) if kind == "group" else None,
        text="".join(parts),
        mentions=tuple(mentions),
        received_at=float(received_at) if isinstance(received_at, (int, float)) else time.time(),
        post_type=str(raw.get("post_type", "")),
    )


@register(PLUGIN_NAME, "Local", "QQ 与微信角色回复、隔离记忆及资料查询", "0.8.0")
class RoleplayGateway(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.settings = GateSettings.from_mapping(config)
        from .online_lookup import LookupSettings
        self.lookup_settings = LookupSettings.from_mapping(config.get("online_lookup", {}))
        self.service: GateService | None = None
        self.wechat_settings: GateSettings | None = None
        self.wechat_service: GateService | None = None
        self.native_settings: GateSettings | None = None
        self.native_service: GateService | None = None
        self.native_server = None
        self._native_token = None
        self._shared_parallel = getattr(context, "_roleplay_shared_parallel", None)
        if self._shared_parallel is None:
            self._shared_parallel = asyncio.Semaphore(3)
            try:
                setattr(context, "_roleplay_shared_parallel", self._shared_parallel)
            except AttributeError:
                # Minimal fixed-receipt test contexts can be immutable.
                pass
        self._startup_lock = asyncio.Lock()
        self._terminated = False
        self._status_instance_id = uuid.uuid4().hex
        self._status_started_at = time.time()
        self.console_admin = None
        self._console_busy = False

    def _write_runtime_status(self, phase: str) -> None:
        """Publish service readiness independently of the configured log level.

        This is a startup/termination snapshot, not a platform-login heartbeat.
        No identity, message, persona, key, or exception text is included.
        """
        def state(service, enabled, *, socket_ready=True):
            ready = (service is not None and not self._terminated and socket_ready
                     and not getattr(service, "blocked", False)
                     and not getattr(service, "_closing", False))
            try:
                active = enabled() is True and not self._terminated
            except Exception:
                active = False
            lookup = getattr(service, "lookup", None)
            return {"ready": ready, "enabled": active,
                    "lookup_enabled": bool(ready and lookup is not None
                                           and getattr(lookup, "reusable", False))}

        record = {
            "schema_version": 1, "instance_id": self._status_instance_id,
            "pid": os.getpid(), "started_at_unix": self._status_started_at,
            "updated_at_unix": time.time(), "phase": phase,
            "terminated": self._terminated,
            "lookup_enabled": self.lookup_settings.enabled,
            "services": {
                "qq": state(self.service, self._enabled),
                "wechat": state(self.wechat_service, self._wechat_enabled),
                "native": state(self.native_service, self._native_enabled,
                                socket_ready=self.native_server is not None),
            },
        }
        temporary = None
        try:
            directory = StarTools.get_data_dir(PLUGIN_NAME)
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / "runtime-status.json"
            # A late close from an older plugin must not erase a newer reload.
            if destination.exists():
                try:
                    previous = json.loads(destination.read_text(encoding="utf-8"))
                    if (previous.get("instance_id") != self._status_instance_id
                            and previous.get("started_at_unix", 0) > self._status_started_at):
                        return
                except (ValueError, TypeError, AttributeError):
                    pass
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".runtime-status-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
                json.dump(record, stream, ensure_ascii=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            temporary = None
        except OSError:
            self._emit({"component": "roleplay_gateway", "code": "runtime_status_write_failed"})
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @staticmethod
    def _emit(record: dict) -> None:
        # Records contain identifiers/status only, never message text or exceptions.
        logger.info(f"roleplay_gateway {json.dumps(record, ensure_ascii=True)}")

    def _enabled(self) -> bool:
        if self._console_busy:
            return False
        try:
            pacing_unchanged = (self.settings.reply_pacing == type(self.settings.reply_pacing).from_mapping(
                self.config.get("reply_pacing", {})))
        except (ValueError, TypeError):
            pacing_unchanged = False
        persona_unchanged = (self.settings.mode != ROLEPLAY_MODE or all(
            self.config.get(key, "") == getattr(self.settings, key)
            for key in ("persona_id", "persona_name", "persona_filename", "persona_sha256")))
        current_admins = self.config.get("memory_admin_ids", [])
        admins_unchanged = (self.settings.mode != ROLEPLAY_MODE or (
            isinstance(current_admins, list)
            and all(isinstance(item, str) for item in current_admins)
            and frozenset(item.strip() for item in current_admins) == self.settings.memory_admin_ids))
        return (self._lookup_unchanged() and persona_unchanged and admins_unchanged and pacing_unchanged and self.config.get("enabled", False) is True
                and self.config.get("mode", "neutral_receipt") == self.settings.mode
                and self.config.get("provider_id", STREAM_PROVIDER_ID) == self.settings.provider_id)

    def _lookup_unchanged(self) -> bool:
        try:
            return self.lookup_settings == type(self.lookup_settings).from_mapping(
                self.config.get("online_lookup", {}))
        except (TypeError, ValueError):
            return False

    async def _stream_provider(self, *, allow_missing: bool = False):
        """Resolve the one configured provider without starting a model request."""
        getter = getattr(self.context, "get_provider_by_id", None)
        if not callable(getter):
            raise ConfigurationError("provider_lookup_unavailable")
        from .model_profiles import selected, profile_models
        provider = getter(self.settings.provider_id)
        if inspect.isawaitable(provider):
            provider = await provider
        if provider is None:
            if allow_missing:
                return None
            raise ConfigurationError("neutral_stream_provider_missing")
        router = self._profiles()
        router._deepseek_provider = provider
        provider = router.resolve()
        profile = selected(self.config)
        self._check_stream_provider(provider, profile_models(self.config)[profile], profile)
        return provider

    def _profiles(self):
        from .model_profiles import ModelProfiles
        router = getattr(self, '_model_profiles', None)
        if router is None:
            directory = StarTools.get_data_dir(PLUGIN_NAME)
            router = self._model_profiles = ModelProfiles(self, directory.parents[1].resolve())
        return router

    @staticmethod
    def _check_stream_provider(provider, expected_model='deepseek-flash', profile='deepseek') -> None:
        from .model_profiles import check_provider
        try:
            check_provider(provider, profile, expected_model)
        except ValueError:
            raise ConfigurationError('neutral_stream_provider_boundary_changed') from None

    async def initialize(self) -> None:
        async with self._startup_lock:
            self._write_runtime_status("initializing")
            try:
                await self._initialize_service(allow_missing=True)
                await self._initialize_wechat_service(allow_missing=True)
                await self._initialize_native_service(allow_missing=True)
            finally:
                self._write_runtime_status("initialized")
        await self._initialize_console()

    async def _initialize_console(self):
        if self.console_admin is not None or self._terminated:
            return
        from .native_settings import bridge_token
        try:
            bridge_token(self.config)
        except (TypeError, ValueError):
            return
        from .console_admin import ConsoleAdmin
        directory = StarTools.get_data_dir(PLUGIN_NAME)
        admin = ConsoleAdmin(self, directory.parents[1].resolve())
        try:
            await admin.start()
            self.console_admin = admin
        except Exception:
            self._emit({'component': 'roleplay_gateway', 'code': 'console_unavailable'})

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        # Cold boot loads plugins before providers; hot reload can start directly.
        async with self._startup_lock:
            self._write_runtime_status("loading")
            try:
                await self._initialize_service(allow_missing=False)
                await self._initialize_wechat_service(allow_missing=False)
                await self._initialize_native_service(allow_missing=False)
            finally:
                self._write_runtime_status("loaded")

    def _native_enabled(self) -> bool:
        if self._console_busy:
            return False
        from .native_settings import settings_from_config, bridge_token
        try:
            current = settings_from_config(self.config)
            return (self._lookup_unchanged() and not self._terminated and current.enabled and current == self.native_settings
                    and self.config.get("native_voice_enabled", False) == getattr(self, "_native_voice_enabled", False)
                    and self.config.get("native_group_voice_ai_wake", False) == getattr(self, "_native_group_voice_ai_wake", False)
                    and bridge_token(self.config) == self._native_token)
        except (TypeError, ValueError):
            return False

    async def _initialize_native_service(self, *, allow_missing: bool) -> None:
        if self._terminated or self.native_service is not None:
            return
        from .native_settings import settings_from_config, bridge_token
        service = server = None
        try:
            settings = settings_from_config(self.config)
            self.native_settings = settings
            self._native_voice_enabled = self.config.get("native_voice_enabled", False)
            if type(self._native_voice_enabled) is not bool:
                raise ValueError("invalid_native_voice_enabled")
            self._native_group_voice_ai_wake = self.config.get("native_group_voice_ai_wake", False)
            if (type(self._native_group_voice_ai_wake) is not bool
                    or (self._native_group_voice_ai_wake and not self._native_voice_enabled)):
                raise ValueError("invalid_native_group_voice_ai_wake")
            if not settings.enabled:
                return
            provider = await self._stream_provider(allow_missing=allow_missing)
            if provider is None:
                self._emit({"component": "roleplay_gateway", "code": "native_waiting_for_providers"})
                return
            from .native_bridge import NativeBridgeServer
            self._native_token = bridge_token(self.config)
            directory = StarTools.get_data_dir(PLUGIN_NAME)
            service = await self._create_service(
                settings, directory / "native", directory.parents[1].resolve(), provider,
                self._native_enabled, "_roleplay_native_safety_guard",
            )
            server_options = ({"research_enabled": True} if getattr(service, "lookup", None) is not None else {})
            if self._native_voice_enabled:
                server_options["voice_enabled"] = True
            server = NativeBridgeServer(settings, self._native_token,
                                        accept=service.accept, emit=self._emit, **server_options)
            await server.start(host="0.0.0.0", port=6198)
            self.native_service, self.native_server = service, server
            self._emit({"component": "roleplay_gateway", "code": "native_initialized",
                        "probability": settings.reply_pacing.probability})
        except BaseException as error:
            cleanup_failed = False
            try:
                if server is not None:
                    await server.close()
            except Exception:
                cleanup_failed = True
            finally:
                try:
                    if service is not None:
                        await service.close()
                except Exception:
                    cleanup_failed = True
            self._emit({"component": "roleplay_gateway", "code": "native_initialization_failed",
                        "error_type": type(error).__name__, "cleanup_failed": cleanup_failed})
            if isinstance(error, asyncio.CancelledError):
                raise

    def _wechat_enabled(self) -> bool:
        if self._console_busy:
            return False
        from .weixin_adapter import settings_from_config
        try:
            current = settings_from_config(self.config)
            return self._lookup_unchanged() and current.enabled and current == self.wechat_settings
        except (TypeError, ValueError):
            return False

    async def _initialize_wechat_service(self, *, allow_missing: bool) -> None:
        if self._terminated or self.wechat_service is not None:
            return
        from .weixin_adapter import check_runtime_contract, settings_from_config
        try:
            settings = settings_from_config(self.config)
            self.wechat_settings = settings
            if not settings.enabled:
                return
            check_runtime_contract()
            provider = None
            if settings.mode in (STREAM_MODE, ROLEPLAY_MODE):
                provider = await self._stream_provider(allow_missing=allow_missing)
                if provider is None:
                    self._emit({"component": "roleplay_gateway", "code": "wechat_waiting_for_providers"})
                    return
            directory = StarTools.get_data_dir(PLUGIN_NAME)
            self.wechat_service = await self._create_service(
                settings, directory / "weixin", directory.parents[1].resolve(),
                provider, self._wechat_enabled, "_roleplay_wechat_safety_guard",
            )
            self._emit({"component": "roleplay_gateway", "code": "wechat_initialized",
                        "mode": settings.mode})
        except Exception as error:
            # One platform failing startup must not close the working QQ stores.
            self._emit({"component": "roleplay_gateway", "code": "wechat_initialization_failed",
                        "error_type": type(error).__name__})

    async def _initialize_service(self, *, allow_missing: bool) -> None:
        if self._terminated or self.service is not None:
            return
        existing_guard = getattr(self.context, "_roleplay_stream_safety_guard", None)
        if existing_guard is not None:
            # Switching to fixed receipts must not bypass unresolved old sends.
            existing_guard.ensure_available()
        provider = None
        if self.settings.mode in (STREAM_MODE, ROLEPLAY_MODE):
            provider = await self._stream_provider(allow_missing=allow_missing)
            if provider is None:
                self._emit({"component": "roleplay_gateway", "code": "waiting_for_providers",
                            "mode": self.settings.mode})
                return
        directory = StarTools.get_data_dir(PLUGIN_NAME)
        self.service = await self._create_service(
            self.settings, directory, directory.parents[1].resolve(), provider,
            self._enabled, "_roleplay_stream_safety_guard",
        )
        self._emit({"component": "roleplay_gateway", "code": "initialized", "mode": self.settings.mode})

    async def _create_service(self, settings, directory, data_root, provider, enabled, guard_attr):
        existing_guard = getattr(self.context, guard_attr, None)
        if existing_guard is not None:
            existing_guard.ensure_available()
        store = ReceiptStore(directory / "receipts.sqlite3")
        segment_store = None
        memory = None
        lookup = None
        try:
            if settings.mode in (STREAM_MODE, ROLEPLAY_MODE):
                # Lazy imports preserve the existing fixed-receipt rollback path.
                from .neutral_stream import NeutralStreamService, StreamSafetyGuard
                from .provider_gate import FailFastOpenAI
                from .segment_delivery import SegmentLedger
                FailFastOpenAI(provider)  # Version/SDK/config check only; no request.
                guard = getattr(self.context, guard_attr, None)
                if guard is None:
                    guard = StreamSafetyGuard()
                    setattr(self.context, guard_attr, guard)
                guard.ensure_available()
                segment_store = SegmentLedger(directory / "segments.sqlite3")
                from .model_profiles import selected, profile_models
                check_provider = lambda value: self._check_stream_provider(
                    value, profile_models(self.config)[selected(self.config)], selected(self.config))
                class OfficialNeutralAdapter(FailFastOpenAI):
                    async def consume(self, parts, on_delta, *, timeout_seconds=25.0, json_output=False):
                        # A pooled adapter revalidates the route on every request.
                        check_provider(self.provider)
                        return await super().consume(parts, on_delta, timeout_seconds=timeout_seconds,
                                                     json_output=json_output)
                def factory():
                    current_provider = self._profiles().resolve()
                    check_provider(current_provider)
                    return OfficialNeutralAdapter(current_provider)
                if settings.mode == ROLEPLAY_MODE:
                    from .memory_store import MemoryStore
                    from .persona import load_persona
                    from .roleplay_stream import RoleplayStreamService
                    from .stream_reply import ReplyLimits
                    persona_root = data_root / "roleplay" / "personas"
                    persona_path = persona_root / settings.persona_filename
                    if (not persona_path.resolve().is_relative_to(persona_root.resolve())
                            or not persona_path.resolve().is_relative_to(data_root)):
                        raise ConfigurationError("persona_path_outside_runtime")
                    persona = load_persona(persona_path, persona_id=settings.persona_id,
                                           name=settings.persona_name)
                    if persona.revision != settings.persona_sha256:
                        raise ConfigurationError("persona_revision_changed_reimport_required")
                    memory = MemoryStore(directory / "memory.sqlite3")
                    await memory.open()
                    from .online_tools import create_lookup_service
                    lookup = create_lookup_service(self.context, self._profiles().deepseek(), self.config.get("online_lookup", {}))
                    lookup_options = {"lookup": lookup} if lookup is not None else {}
                    if settings.id_kind == "wechat_native" and self.config.get("native_voice_enabled", False):
                        lookup_options["voice_enabled"] = True
                        lookup_options["group_voice_ai_wake"] = self.config.get("native_group_voice_ai_wake", False)
                    # Kouri's Gemini route can exceed the short DeepSeek budget
                    # before its first streamed byte. Keep this bounded and
                    # profile-specific so the existing DeepSeek path is unchanged.
                    if selected(self.config) == "kouri":
                        lookup_options["limits"] = ReplyLimits(
                            first_receipt_seconds=40, generation_seconds=45,
                            total_seconds=50)
                    service = RoleplayStreamService(
                        settings, store, segment_store, factory, persona=persona, memory=memory,
                        emit=self._emit, enabled=enabled, guard=guard,
                        shared_parallel=self._shared_parallel,
                        **lookup_options,
                    )
                    from dataclasses import replace
                    from .context_budget import ContextBudget
                    service.context_budget = replace(ContextBudget(), recent_turns=self.config.get('console_context_limit', 12))
                    service.worldbook_source = lambda identity: (self.console_admin.features.worlds(identity)
                        if self.console_admin is not None and self.console_admin.features is not None else [])
                else:
                    service = NeutralStreamService(
                        settings, store, segment_store, factory,
                        emit=self._emit, enabled=enabled, guard=guard,
                        shared_parallel=self._shared_parallel,
                    )
                await segment_store.open()
            else:
                service = GateService(settings, store, emit=self._emit, enabled=enabled,
                                      shared_parallel=self._shared_parallel)
            await store.open()
            return service
        except BaseException:
            try:
                if lookup is not None:
                    await lookup.close()
            finally:
                await store.close()
                if segment_store is not None:
                    await segment_store.close()
                if memory is not None:
                    await memory.close()
            raise

    # The inner decorator creates handler metadata and therefore sets priority.
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def on_message(self, event: AstrMessageEvent) -> None:
        """Own the configured OneBot instance; return promptly after admission."""
        if (str(event.get_platform_id()) != self.settings.platform_id
                or str(event.get_self_id()) != self.settings.bot_self_id):
            return
        # Even disabled/denied input on this instance cannot fall through to
        # native LLM or a later command handler. Other instances are untouched.
        event.stop_event()
        if self.service is None:
            return
        try:
            envelope = snapshot_event(event)
        except (ValueError, TypeError, AttributeError):
            self._emit({"component": "roleplay_gateway", "code": "invalid_onebot_event"})
            return

        if self.settings.mode != ROLEPLAY_MODE and is_memory_command(envelope.text):
            self._emit({"component": "roleplay_gateway", "code": "memory_command_requires_roleplay"})
            return

        bot = event.bot

        async def send(item: Envelope, text: str):
            params = {
                "self_id": int(item.bot_self_id),
                "message": [{"type": "text", "data": {"text": format_qq_bubble(text)}}],
            }
            if item.kind == "group":
                action = "send_group_msg"
                params["group_id"] = int(item.group_id)
            else:
                action = "send_private_msg"
                params["user_id"] = int(item.sender_id)
            try:
                # aiocqhttp returns result.data; event.send() discards that result.
                return await bot.call_action(action, **params)
            except ActionFailed:
                raise DefiniteSendFailure() from None

        try:
            await self.service.accept(envelope, send)
        except Exception as error:
            self._emit({"component": "roleplay_gateway", "code": "admission_error",
                        "error_type": type(error).__name__})

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def on_wechat_message(self, event: AstrMessageEvent) -> None:
        """The native weixin_oc type has no PlatformAdapterType enum in 4.29.1."""
        platform_id = (self.wechat_settings.platform_id if self.wechat_settings is not None
                       else self.config.get("wechat_platform_id", "wechat-sakura"))
        if (event.get_platform_name() != "weixin_oc"
                or event.get_platform_id() != platform_id):
            return
        # Own disabled/unbound/invalid messages too: native LLM must not take over.
        event.stop_event()
        if self.wechat_service is None or not self._wechat_enabled():
            return
        from .weixin_adapter import send_weixin_text, snapshot_weixin_event
        try:
            envelope = snapshot_weixin_event(event, platform_id=platform_id)
        except (ValueError, TypeError, AttributeError, ImportError):
            self._emit({"component": "roleplay_gateway", "code": "invalid_wechat_event"})
            return
        if self.wechat_settings.mode != ROLEPLAY_MODE and is_memory_command(envelope.text):
            self._emit({"component": "roleplay_gateway", "code": "memory_command_requires_roleplay"})
            return

        async def send(item: Envelope, text: str):
            return await send_weixin_text(event.platform, item, text)

        try:
            await self.wechat_service.accept(envelope, send)
        except Exception as error:
            self._emit({"component": "roleplay_gateway", "code": "wechat_admission_error",
                        "error_type": type(error).__name__})

    async def terminate(self) -> None:
        if self.console_admin is not None:
            await self.console_admin.close()
            self.console_admin = None
        async with self._startup_lock:
            self._terminated = True
            self._write_runtime_status("terminating")
            try:
                await self._terminate_services()
            finally:
                if getattr(self, '_model_profiles', None) is not None:
                    await self._model_profiles.close()
                self._write_runtime_status("terminated")

    async def _terminate_services(self) -> None:
        # Always settle all owners even when one close raises.
        try:
            if self.service is not None:
                await self.service.close()
                self.service = None
        finally:
            try:
                if self.wechat_service is not None:
                    await self.wechat_service.close()
                    self.wechat_service = None
            finally:
                try:
                    if self.native_server is not None:
                        await self.native_server.close()
                        self.native_server = None
                finally:
                    if self.native_service is not None:
                        await self.native_service.close()
                        self.native_service = None
