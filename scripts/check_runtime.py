"""Read-only preflight for an existing deployment's reviewed gateway path.

Neutral modes never open persona files. Explicit roleplay mode reads only its
declared runtime copy to verify the configured hash, not the uploaded source.
Never creates runtime files, changes/binds cards, invokes Docker or calls APIs.
This is not content-safety validation. Credentials are compared only in memory.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import re
import sys
from pathlib import Path

PLATFORM_ID = "qq-sakura"
WECHAT_PLATFORM_ID = "wechat-sakura"
WECHAT_PLATFORM_TYPE = "weixin_oc"
GATEWAY_PLUGIN = "astrbot_plugin_roleplay_gateway"
DEEPSEEK_ENV_NAME = "ROLEPLAY_DEEPSEEK_API_KEY"
STREAM_PROVIDER_ID = "deepseek-flash-official"
STREAM_SOURCE_ID = "deepseek-official"
STREAM_MODEL = "deepseek-flash"
ROLEPLAY_MODE = "roleplay_stream"
MAX_PERSONA_BYTES = 2 * 1024 * 1024
# Load the checkout-local validator without depending on the caller's cwd or
# importing any runtime/configuration data. The source module has no side effects.
_base_spec = importlib.util.spec_from_file_location(
    "_roleplay_official_base", Path(__file__).with_name("configure_deepseek.py"))
assert _base_spec and _base_spec.loader
_base_module = importlib.util.module_from_spec(_base_spec)
_base_spec.loader.exec_module(_base_module)
# Keep aligned with configure_deepseek.set_env_value; no general dotenv syntax.
DEEPSEEK_KEY_PATTERN = r"[A-Za-z0-9_-]{16,256}"
# Reviewed Compose manifests only, never persona hashes. Changed manifests need
# another explicit review; do not silently add their hashes to this allowlist.
APPROVED_COMPOSE = {
    "f380f7a52183fa3e85075f976effb96011d80db7f34b64c299663751ab64beae": "upstream-pinned",
    "b44f12a9a5c7d4d4d91efcb513dce9f146e54d8c2267daf62cd5b9bb7037a6f5": "regex-derived-gate1",
    # Reviewed 2026-10-25: the only addition is 127.0.0.1:6198:6198 on AstrBot.
    "8209ab18a5cd32d5186144adffeb6ee03dca10eb578ca3efbf77be83e1e63e61": "regex-derived-gate1",
}
QQ_FIELDS = ("bot_account_id", "admin_account_ids", "private_contact_allowlist", "test_group_ids")
NETWORK_FIELDS = ("httpServers", "httpSseServers", "httpClients", "websocketServers", "websocketClients", "plugins")


class RuntimeCheckError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise RuntimeCheckError(code, message)


def local_path(root: Path, relative: str, *, directory: bool = False) -> Path:
    try:
        path = (root / relative).resolve(strict=True)
        require(path.is_relative_to(root), "PATH_OUTSIDE_PROJECT", "部署文件或挂载目录越出项目边界。")
        require(path.is_dir() if directory else path.is_file(), "REQUIRED_PATH_INVALID", "缺少所需部署文件或挂载目录。")
        return path
    except (OSError, RuntimeError):
        raise RuntimeCheckError("REQUIRED_PATH_MISSING", "缺少所需部署文件或挂载目录；不自动创建或重置。") from None


def read_json(root: Path, relative: str) -> dict:
    try:
        value = json.loads(local_path(root, relative).read_text("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeCheckError("CONFIG_INVALID", "部署 JSON 无法读取或格式错误；原始内容未输出。") from None
    require(isinstance(value, dict), "CONFIG_INVALID", "部署 JSON 必须是对象。")
    return value


def read_env(root: Path, relative: str, expected_key: str, *, allow_deepseek_key: bool = False,
             require_deepseek_key: bool = False) -> str:
    try:
        lines = local_path(root, relative).read_text("utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise RuntimeCheckError("ENV_INVALID", "部署环境文件无法读取；原始内容未输出。") from None
    allowed_keys = {expected_key}
    if allow_deepseek_key:
        allowed_keys.add(DEEPSEEK_ENV_NAME)
    entries = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        require(bool(separator) and key in allowed_keys and key not in entries,
                "ENV_INVALID", "部署环境文件含未知、重复或无效字段。")
        require(bool(value.strip()), "SECRET_MISSING", "部署环境文件缺少必要值；不自动生成。")
        if key == DEEPSEEK_ENV_NAME:
            require(re.fullmatch(DEEPSEEK_KEY_PATTERN, value) is not None,
                    "ENV_INVALID", "可选 DeepSeek 密钥格式无效；原始值未被输出。")
        entries[key] = value
    require(expected_key in entries, "ENV_INVALID", "部署环境文件缺少必要字段。")
    if require_deepseek_key:
        require(DEEPSEEK_ENV_NAME in entries, "STREAM_KEY_REQUIRED", "中性流式模式缺少已配置的模型密钥环境变量。")
    return entries[expected_key]


def qq_identity(value: dict) -> dict:
    qq = value.get("qq")
    require(isinstance(qq, dict), "ACCOUNT_CONFIG_INVALID", "部署账号清单无效。")
    selected = {key: qq.get(key) for key in QQ_FIELDS}
    valid = lambda v: isinstance(v, str) and re.fullmatch(r"[1-9][0-9]{4,11}", v) is not None
    account = selected["bot_account_id"]
    require(valid(account), "ACCOUNT_CONFIG_INVALID", "部署机器人账号格式无效。")
    for key in QQ_FIELDS[1:]:
        members = selected[key]
        require(isinstance(members, list) and bool(members) and all(valid(v) for v in members),
                "ACCOUNT_CONFIG_INVALID", "部署管理员或会话清单无效。")
    require(account not in selected["admin_account_ids"] and account not in selected["private_contact_allowlist"],
            "ACCOUNT_CONFIG_INVALID", "机器人账号不能同时充当管理员或测试联系人。")
    return selected


def nonempty_secret(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def same_secret(left: object, right: object) -> bool:
    if not nonempty_secret(left) or not nonempty_secret(right):
        return False
    return hmac.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))


def active_endpoints(config: dict) -> dict[str, list[dict]]:
    network = config.get("network")
    require(isinstance(network, dict), "ONEBOT_CONFIG_INVALID", "OneBot 网络配置无效。")
    active = {}
    for field in NETWORK_FIELDS:
        entries = network.get(field)
        require(isinstance(entries, list), "ONEBOT_CONFIG_INVALID", "OneBot 网络配置缺少有效列表。")
        require(all(isinstance(entry, dict) and isinstance(entry.get("enable"), bool) for entry in entries),
                "ONEBOT_CONFIG_INVALID", "OneBot 网络配置含无效端点。")
        active[field] = [entry for entry in entries if entry["enable"]]
    return active


def validate_stream_provider(root: Path, config: dict, gate: dict) -> None:
    """Validate the single official route without opening model-access/persona files.

    Unknown provider/source knobs are rejected, including retry, header, tools,
    alternate endpoint and payload overrides. The registered model/source is not
    rewritten, and unrelated providers are not selected or changed.
    """
    require(gate.get("provider_id") == STREAM_PROVIDER_ID,
            "STREAM_PROVIDER_ID_CHANGED", "中性流式模式只能使用已审查的模型配置。")
    providers, sources = config.get("provider"), config.get("provider_sources")
    require(isinstance(providers, list) and all(isinstance(item, dict) for item in providers)
            and isinstance(sources, list) and all(isinstance(item, dict) for item in sources),
            "STREAM_PROVIDER_INVALID", "模型与来源配置列表无效。")
    selected = [item for item in providers if item.get("id") == STREAM_PROVIDER_ID]
    selected_sources = [item for item in sources if item.get("id") == STREAM_SOURCE_ID]
    require(len(selected) == 1 and len(selected_sources) == 1,
            "STREAM_PROVIDER_INVALID", "已审查的模型或来源缺失、重复。")
    provider, source = selected[0], selected_sources[0]
    require(set(provider) == {"id", "provider_source_id", "enable", "model", "custom_extra_body"}
            and provider.get("provider_source_id") == STREAM_SOURCE_ID
            and provider.get("enable") is True and provider.get("model") == STREAM_MODEL
            and provider.get("custom_extra_body") == {"thinking": {"type": "disabled"}},
            "STREAM_PROVIDER_INVALID", "中性流式模型配置偏离已审查的单路非思考配置。")
    require(set(source) == {"id", "provider", "type", "provider_type", "enable", "key",
                            "api_base", "timeout", "proxy", "custom_headers"}
            and source.get("provider") == "deepseek"
            and source.get("type") == "openai_chat_completion"
            and source.get("provider_type") == "chat_completion"
            and source.get("enable") is True
            and source.get("key") == ["$" + DEEPSEEK_ENV_NAME]
            and type(source.get("timeout")) is int and source["timeout"] == 30
            and source.get("proxy") == "" and source.get("custom_headers") == {},
            "STREAM_SOURCE_INVALID", "中性流式来源配置或认证引用偏离已审查范围。")
    try:
        _base_module.official_base(source.get("api_base"))
    except _base_module.ConfigurationError:
        raise RuntimeCheckError("STREAM_ENDPOINT_INVALID", "中性流式模式必须使用官方 HTTPS 模型接口。") from None
    read_env(root, "runtime/astrbot.env", "ASTRBOT_DASHBOARD_INITIAL_PASSWORD",
             allow_deepseek_key=True, require_deepseek_key=True)


def validate_persona_fields(gate: dict) -> None:
    identity, name = gate.get("persona_id"), gate.get("persona_name")
    filename, digest = gate.get("persona_filename"), gate.get("persona_sha256")
    require(isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identity) is not None,
            "PERSONA_ID_INVALID", "角色标识必须为允许的 ASCII 标识。")
    require(isinstance(name, str) and bool(name.strip()) and len(name) <= 100
            and not any(ord(char) < 32 or char in "\u0085\u2028\u2029" for char in name),
            "PERSONA_NAME_INVALID", "角色名称必须非空且不含换行或控制字符。")
    require(isinstance(filename, str) and 0 < len(filename) <= 200
            and filename not in {".", ".."} and filename == Path(filename).name
            and not any(char in filename for char in '/\\:<>"|?*')
            and not any(ord(char) < 32 for char in filename)
            and Path(filename).suffix.lower() in {".txt", ".md", ".json", ".png"}
            and not filename.endswith((" ", ".")),
            "PERSONA_FILENAME_INVALID", "运行人设必须使用受支持扩展名的单个文件名。")
    require(isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) is not None,
            "PERSONA_HASH_INVALID", "运行人设需要明确的小写 SHA256。")


def confirmed_memory_admins(config: dict, qq: dict) -> list[str]:
    """Use explicit administrator declarations, never the private chat allowlist."""
    admins = config.get("admins_id")
    require(isinstance(admins, list) and bool(admins)
            and all(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{4,11}", value) is not None
                    for value in admins)
            and len(set(admins)) == len(admins)
            and admins == qq.get("admin_account_ids")
            and qq.get("bot_account_id") not in admins,
            "MEMORY_ADMIN_SOURCE_INVALID", "记忆管理员必须来自与确认清单一致的明确管理员列表。")
    return list(admins)


def validate_memory_admins(gate: dict, config: dict, qq: dict) -> None:
    expected = confirmed_memory_admins(config, qq)
    require(gate.get("memory_admin_ids") == expected,
            "MEMORY_ADMIN_SCOPE_CHANGED", "角色模式的记忆管理员必须与明确管理员清单一致，不能使用私聊白名单。")


def validate_runtime_persona(root: Path, gate: dict) -> None:
    """Check an explicitly selected runtime copy; never follow source metadata."""
    validate_persona_fields(gate)
    directory = local_path(root, "runtime/roleplay/personas", directory=True)
    path = local_path(root, "runtime/roleplay/personas/" + gate["persona_filename"])
    require(path.parent == directory, "PERSONA_PATH_OUTSIDE_DIRECTORY", "运行人设越出专属目录。")
    try:
        require(path.stat().st_size <= MAX_PERSONA_BYTES, "PERSONA_TOO_LARGE", "运行人设超过 2 MB 限制。")
        with path.open("rb") as stream:
            data = stream.read(MAX_PERSONA_BYTES + 1)
    except OSError:
        raise RuntimeCheckError("PERSONA_UNREADABLE", "无法读取运行人设；正文未输出。") from None
    require(0 < len(data) <= MAX_PERSONA_BYTES, "PERSONA_TOO_LARGE", "运行人设为空或超过 2 MB 限制。")
    require(hashlib.sha256(data).hexdigest() == gate["persona_sha256"],
            "PERSONA_HASH_MISMATCH", "运行人设与已选择的 SHA256 不一致；不会自动接受新版本。")


def validate_platforms(config: dict) -> tuple[dict, dict | None]:
    """Preserve the QQ node; permit only one explicitly named native WeChat node."""
    platforms = config.get("platform")
    require(isinstance(platforms, list) and 1 <= len(platforms) <= 2
            and all(isinstance(item, dict) for item in platforms),
            "PLATFORM_CONFIG_INVALID", "部署只能保留既有 QQ 与一个明确的个人微信节点。")
    qq_nodes = [item for item in platforms if item.get("id") == PLATFORM_ID]
    wechat_nodes = [item for item in platforms if item.get("id") == WECHAT_PLATFORM_ID]
    require(len(qq_nodes) == 1 and len(wechat_nodes) <= 1
            and len(qq_nodes) + len(wechat_nodes) == len(platforms),
            "PLATFORM_CONFIG_INVALID", "平台标识缺失、重复或存在未审查的额外节点。")
    qq_node = qq_nodes[0]
    require(qq_node.get("type") == "aiocqhttp" and qq_node.get("enable") is True
            and qq_node.get("ws_reverse_host") == "0.0.0.0" and qq_node.get("ws_reverse_port") == 6199,
            "PLATFORM_CONFIG_INVALID", "部署 OneBot 接入端配置不一致。")
    wechat_node = wechat_nodes[0] if wechat_nodes else None
    if wechat_node is not None:
        require(wechat_node.get("type") == WECHAT_PLATFORM_TYPE
                and type(wechat_node.get("enable")) is bool,
                "PLATFORM_CONFIG_INVALID", "个人微信节点类型或启用字段无效。")
    return qq_node, wechat_node


def validate_wechat_scope(config: dict, gate: dict | None) -> list[str]:
    """Return allowed UMO entries; credential presence is checked only in memory."""
    _, node = validate_platforms(config)
    gate = {} if gate is None else gate
    require(isinstance(gate, dict), "WECHAT_SCOPE_INVALID", "微信插件配置必须是对象。")
    enabled = gate.get("wechat_enabled", False)
    require(type(enabled) is bool and gate.get("wechat_platform_id", WECHAT_PLATFORM_ID) == WECHAT_PLATFORM_ID,
            "WECHAT_SCOPE_INVALID", "微信启用字段或平台标识偏离明确配置。")
    values = {}
    for key in ("wechat_allowed_private_ids", "wechat_admin_ids"):
        members = gate.get(key, [])
        require(isinstance(members, list) and len(members) <= 32
                and all(isinstance(value, str) and 0 < len(value) <= 128
                        and value.isprintable() and ":" not in value
                        and not any(char.isspace() for char in value) for value in members)
                and len(set(members)) == len(members),
                "WECHAT_SCOPE_INVALID", "微信身份列表必须为有界、无空白和分隔符的不重复字符串。")
        values[key] = members
    allowed, admins = values["wechat_allowed_private_ids"], values["wechat_admin_ids"]
    require(set(admins) <= set(allowed), "WECHAT_SCOPE_INVALID", "微信管理员必须属于明确允许的微信私聊身份。")
    if not enabled:
        return []
    require(node is not None and node.get("enable") is True and gate.get("mode") == ROLEPLAY_MODE,
            "WECHAT_NOT_READY", "启用微信角色回复需要指定原生节点已启用且角色模式有效。")
    require(bool(allowed) and bool(admins), "WECHAT_SCOPE_INVALID", "启用微信需要明确的私聊身份与管理员，不能扩大 QQ 范围。")
    require(nonempty_secret(node.get("weixin_oc_token"))
            and nonempty_secret(node.get("weixin_oc_account_id")),
            "WECHAT_ACCOUNT_NOT_CONFIGURED", "原生微信登录资料尚未配置；此检查不代表当前登录有效。")
    return [f"{WECHAT_PLATFORM_ID}:FriendMessage:{value}" for value in allowed]


def validate_access_scope(config: dict, qq: dict, wechat_entries: list[str]) -> None:
    settings = config.get("platform_settings", {})
    require(isinstance(settings, dict), "WHITELIST_INVALID", "会话白名单配置无效。")
    expected = [f"{PLATFORM_ID}:GroupMessage:{item}" for item in qq["test_group_ids"]]
    expected += [f"{PLATFORM_ID}:FriendMessage:{item}" for item in qq["private_contact_allowlist"]]
    expected += wechat_entries
    require(config.get("admins_id") == qq["admin_account_ids"] and settings.get("enable_id_white_list") is True
            and settings.get("id_whitelist") == expected and settings.get("wl_ignore_admin_on_group") is False
            and settings.get("wl_ignore_admin_on_friend") is False,
            "WHITELIST_CHANGED", "部署管理员或会话访问边界与确认清单不一致。")


def check_neutral_mode(root: Path, config: dict, qq: dict, compose_profile: str,
                       *, gateway_override: dict | None = None) -> str:
    """Validate preparation or a reviewed neutral/roleplay gateway mode.

    gateway_override is used only by the local transactional configurator to
    validate its proposed gateway bytes before replacing them; CLI callers
    always validate the real file. All account/credential boundaries still run.
    """
    provider = config.get("provider_settings")
    require(isinstance(provider, dict) and provider.get("enable") is False,
            "NATIVE_PROVIDER_ENABLED", "中性接线阶段必须关闭原生模型回复；不自动修改。")
    require(config.get("disable_builtin_commands") is True,
            "BUILTIN_COMMANDS_ENABLED", "中性接线阶段必须关闭内置指令；不自动修改。")
    plugins = config.get("plugin_set")
    require(plugins in ([], [GATEWAY_PLUGIN]),
            "PLUGIN_BOUNDARY_CHANGED", "插件名单偏离已审查的中性接线范围。")
    if not plugins:
        validate_access_scope(config, qq, validate_wechat_scope(config, None))
        return "prepared-inert"
    require(compose_profile == "regex-derived-gate1",
            "PLUGIN_IMAGE_MISMATCH", "中性插件需要已审查的依赖镜像与源码挂载。")
    rate_limit = config.get("platform_settings", {}).get("rate_limit")
    require(isinstance(rate_limit, dict) and type(rate_limit.get("count")) is int
            and rate_limit["count"] == 0,
            "NATIVE_RATE_LIMIT_CHANGED", "中性插件必须独立处理准入；原生限流配置已改变。")
    stored_gate = read_json(root, f"runtime/astrbot/config/{GATEWAY_PLUGIN}_config.json")
    gate = stored_gate if gateway_override is None else gateway_override
    require(isinstance(gate, dict) and isinstance(gate.get("enabled"), bool)
            and gate.get("mode") in ("neutral_receipt", "neutral_stream", ROLEPLAY_MODE),
            "GATEWAY_MODE_CHANGED", "插件不再是已审查的运行模式。")
    require(gate.get("provider_id", STREAM_PROVIDER_ID) == STREAM_PROVIDER_ID,
            "STREAM_PROVIDER_ID_CHANGED", "中性模式的模型选择偏离已审查配置。")
    require(gate.get("platform_id") == PLATFORM_ID and gate.get("bot_self_id") == qq["bot_account_id"]
            and gate.get("admin_private_ids") == qq["private_contact_allowlist"]
            and gate.get("allowed_group_ids") == qq["test_group_ids"],
            "GATEWAY_SCOPE_CHANGED", "插件账号或会话范围与已确认的部署清单不一致。")
    validate_access_scope(config, qq, validate_wechat_scope(config, gate))
    if gate["mode"] == ROLEPLAY_MODE:
        validate_memory_admins(gate, config, qq)
    if gate["mode"] in ("neutral_stream", ROLEPLAY_MODE):
        validate_stream_provider(root, config, gate)
    if gate["mode"] == ROLEPLAY_MODE:
        validate_runtime_persona(root, gate)
    prefix = {"neutral_stream": "neutral-stream", "neutral_receipt": "neutral-receipt",
              ROLEPLAY_MODE: "roleplay-stream"}[gate["mode"]]
    return prefix + ("-enabled" if gate["enabled"] else "-disabled")


def check_runtime(root: Path, *, gateway_override: dict | None = None,
                  config_override: dict | None = None) -> dict:
    """Validate stored files or transactional in-memory proposals without writing.

    Existing files are still required and parsed with either override. The CLI
    never supplies overrides; configurators own compare-before-replace guards.
    """
    root = root.resolve()
    local_path(root, "runtime", directory=True)
    current = read_json(root, "config/connection-inputs.local.json")
    snapshot = read_json(root, "runtime/inputs.snapshot.json")
    qq = qq_identity(current)
    require(qq == qq_identity(snapshot), "DEPLOYMENT_ACCOUNT_CHANGED", "账号或会话清单与既有部署不一致；请人工核对。")
    compose = local_path(root, "compose.yaml").read_text("utf-8-sig").replace("\r\n", "\n")
    profile = APPROVED_COMPOSE.get(hashlib.sha256(compose.encode("utf-8")).hexdigest())
    require(profile is not None, "COMPOSE_NEEDS_REVIEW", "Compose 已改变，需要单独审查端口、挂载和运行边界；不自动放行。")
    for directory in ("runtime/astrbot", "runtime/roleplay", "runtime/roleplay/state", "runtime/napcat/qq",
                      "runtime/napcat/config", "runtime/napcat/plugins"):
        local_path(root, directory, directory=True)
    if profile == "regex-derived-gate1":
        local_path(root, "plugins/astrbot_plugin_roleplay_gateway", directory=True)
        local_path(root, "Dockerfile.astrbot")
        local_path(root, "requirements-runtime.txt")

    stored_config = read_json(root, "runtime/astrbot/cmd_config.json")
    config = stored_config if config_override is None else config_override
    require(isinstance(config, dict), "CONFIG_INVALID", "部署 JSON 必须是对象。")
    access = read_json(root, "runtime/access.local.json")
    webui = read_json(root, "runtime/napcat/config/webui.json")
    generic = read_json(root, "runtime/napcat/config/onebot11.json")
    account_config = read_json(root, f"runtime/napcat/config/onebot11_{qq['bot_account_id']}.json")
    account_env = read_env(root, "runtime/napcat.env", "ACCOUNT")
    initial_password = read_env(root, "runtime/astrbot.env", "ASTRBOT_DASHBOARD_INITIAL_PASSWORD",
                                allow_deepseek_key=True)
    require(account_env == qq["bot_account_id"], "DEPLOYMENT_ACCOUNT_CHANGED", "现有部署启动账号与确认清单不一致。")
    require(access.get("astrbot_url") == "http://127.0.0.1:6185" and access.get("napcat_url") == "http://127.0.0.1:6099",
            "MANAGEMENT_BOUNDARY_CHANGED", "管理入口必须保持已审查的本机地址。")
    dashboard = config.get("dashboard", {})
    require(isinstance(dashboard, dict) and dashboard.get("enable") is True and dashboard.get("host") == "0.0.0.0"
            and dashboard.get("port") == 6185 and nonempty_secret(access.get("astrbot_username"))
            and dashboard.get("username") == access.get("astrbot_username"),
            "MANAGEMENT_CONFIG_INVALID", "AstrBot 管理入口配置不一致。")
    require(same_secret(initial_password, access.get("astrbot_initial_password")),
            "SECRET_MISMATCH", "现有首次启动凭据文件不一致；不重置密码。")
    require(webui.get("host") == "0.0.0.0" and webui.get("port") == 6099 and webui.get("prefix") == "",
            "MANAGEMENT_CONFIG_INVALID", "NapCat 管理入口配置偏离已审查边界。")
    require(same_secret(webui.get("token"), access.get("napcat_webui_token")),
            "SECRET_MISMATCH", "现有 NapCat 管理凭据缺失或不一致；不输出或重置。")

    settings = config.get("platform_settings", {})
    require(isinstance(settings, dict), "WHITELIST_INVALID", "会话白名单配置无效。")
    require(settings.get("ignore_bot_self_message") is True and settings.get("ignore_at_all") is True,
            "MESSAGE_BOUNDARY_CHANGED", "自发消息或全体提及过滤边界已改变。")
    neutral_profile = check_neutral_mode(root, config, qq, profile, gateway_override=gateway_override)
    platform, wechat_platform = validate_platforms(config)
    require(not any(active_endpoints(generic).values()), "GENERIC_ACCOUNT_GUARD_DISABLED", "通用账号配置不得激活网络端点。")
    active = active_endpoints(account_config)
    require(len(active["websocketClients"]) == 1 and not any(active[key] for key in NETWORK_FIELDS if key != "websocketClients"),
            "ONEBOT_BOUNDARY_CHANGED", "账号配置必须仅启用已审查的反向 WebSocket。")
    client = active["websocketClients"][0]
    require(client.get("url") == "ws://astrbot:6199/ws" and client.get("messagePostFormat") == "array"
            and client.get("reportSelfMessage") is False and client.get("debug") is False,
            "ONEBOT_BOUNDARY_CHANGED", "OneBot 目标、消息格式或日志边界已改变。")
    require(same_secret(platform.get("ws_reverse_token"), client.get("token")),
            "SECRET_MISMATCH", "OneBot 两端凭据缺失或不一致；不输出或重置。")

    # Metadata only: no source_file resolution, byte reads, imports or DB access.
    now, before = current.get("persona", {}), snapshot.get("persona", {})
    changed = not (isinstance(now, dict) and isinstance(before, dict)) or any(
        now.get(key) != before.get(key) for key in ("source_file", "sha256"))
    roleplay = neutral_profile.startswith("roleplay-stream-")
    warnings = [{"code": "PERSONA_SOURCE_NOT_VERIFIED", "message": "本检查不读取原始上传人设；角色模式仅核验显式运行副本的大小与哈希，不进行内容安全判断。"}]
    if changed:
        warnings.append({"code": "PERSONA_DECLARATION_CHANGED", "message": "人设路径或声明哈希与快照不同；独立记录，不影响既有中性接线检查。"})
    return {"ok": True, "mode": "existing-roleplay-integration" if roleplay else "existing-neutral-integration", "compose_profile": profile,
            "neutral_runtime_profile": neutral_profile,
            "runtime_profile": neutral_profile,
            "configured_account_matches_inputs": True, "runtime_paths_within_project": True,
            "credentials_consistent": True, "runtime_modified": False, "persona_source_read": False,
            "persona_hash_accepted": False, "persona_declaration_changed": changed,
            "runtime_persona_read": roleplay, "runtime_persona_hash_verified": roleplay,
            "memory_admin_scope_verified": roleplay,
            "wechat_platform_configured": wechat_platform is not None,
            "wechat_private_scope_verified": True, "live_wechat_login_verified": False,
            "neutral_prompt_verified": False, "live_qq_login_verified": False, "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="只输出脱敏检查结果。")
    args = parser.parse_args(argv)
    try:
        report = check_runtime(args.root)
    except RuntimeCheckError as exc:
        report = {"ok": False, "code": exc.code, "message": str(exc)}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        report = {"ok": False, "code": "RUNTIME_INVALID", "message": "已有部署配置无法安全核对；未输出原始内容或重建资料。"}
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    elif report["ok"]:
        print("已有部署账户、挂载与连接配置检查通过；未更改配置或凭据。")
        for warning in report["warnings"]:
            print(f"警告 [{warning['code']}]：{warning['message']}")
        print("此结果不代表人设已认可、中性提示已绑定或 QQ 实连通过。")
    else:
        print(f"检查失败 [{report['code']}]：{report['message']}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
