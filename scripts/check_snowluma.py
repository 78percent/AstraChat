"""Read-only, redacted SnowLuma configuration preflight; never starts services.

Renders Compose JSON in memory and compares credentials without printing them.
The launcher separately validates retained AstrBot/NapCat configuration;
this helper does not call that checker or prove live login or message delivery.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location(
    "_snowluma_retained_runtime", Path(__file__).with_name("check_runtime.py"))
assert _spec and _spec.loader
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)

IMAGE = "motricseven7/snowluma:v2.2.0@sha256:34b4ec03738a698231cbad39c5650747330f5a96d953494fb6c12e1273974c0b"
PROJECT_NAME = "astrbot-roleplay-snowluma"
NETWORKS = {"httpServers", "httpClients", "wsServers", "wsClients"}
ENVIRONMENT = {
    "TZ": "Asia/Shanghai", "SNOWLUMA_UID": "1000", "SNOWLUMA_GID": "1000",
    "SNOWLUMA_WEBUI_HOST": "0.0.0.0", "SNOWLUMA_WEBUI_PORT": "5099",
    "SNOWLUMA_LOG_LEVEL": "info", "SNOWLUMA_SCREEN": "1920x1080x24",
    "SNOWLUMA_HOOK_AUTOLOAD": "1", "SNOWLUMA_TELEMETRY": "0",
    "SNOWLUMA_QQ_FLAGS": "--disable-gpu --disable-software-rasterizer --disable-gpu-compositing",
}


class PreflightError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def require(ok: bool, code: str, message: str) -> None:
    if not ok:
        raise PreflightError(code, message)


def render_compose(root: Path) -> dict:
    manifest = legacy.local_path(root, "compose.snowluma.yaml")
    try:
        result = subprocess.run(
            ["docker", "compose", "--project-directory", str(root), "-f", str(manifest),
             "config", "--format", "json"],
            cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise PreflightError("COMPOSE_UNAVAILABLE", "无法读取 Docker Compose 配置；未输出命令详情或环境内容。") from None
    require(result.returncode == 0, "COMPOSE_RENDER_FAILED", "Compose 配置解析失败；原始输出已隐藏。")
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise PreflightError("COMPOSE_JSON_INVALID", "Compose 未返回有效 JSON；原始输出已隐藏。") from None
    require(isinstance(value, dict), "COMPOSE_JSON_INVALID", "Compose 解析结果必须为对象。")
    return value


def validate_compose(root: Path, compose: dict, access: dict) -> None:
    require(compose.get("name") == PROJECT_NAME and set(compose.get("services", {})) == {"snowluma"},
            "COMPOSE_SCOPE_CHANGED", "SnowLuma 必须使用独立 Compose 项目和唯一服务。")
    service = compose["services"]["snowluma"]
    allowed = {"image", "platform", "restart", "shm_size", "cap_add", "security_opt", "ulimits",
               "environment", "ports", "volumes", "networks", "logging", "command", "entrypoint"}
    require(isinstance(service, dict) and set(service) <= allowed
            and service.get("command") in (None, []) and service.get("entrypoint") in (None, []),
            "SERVICE_BOUNDARY_CHANGED", "容器运行权限、入口或服务字段偏离官方部署边界。")
    require(service.get("image") == IMAGE and service.get("platform") == "linux/amd64",
            "IMAGE_CHANGED", "SnowLuma 镜像版本、摘要或平台发生变化。")
    require(service.get("restart") == "unless-stopped" and str(service.get("shm_size")) == "1073741824"
            and service.get("cap_add") == ["SYS_PTRACE"]
            and service.get("security_opt") == ["seccomp=unconfined"]
            and service.get("ulimits") == {"nofile": {"soft": 65536, "hard": 1048576}},
            "SERVICE_BOUNDARY_CHANGED", "容器运行参数偏离已审查的官方要求。")
    ports = service.get("ports")
    require(isinstance(ports, list) and len(ports) == 2, "PORT_BOUNDARY_CHANGED", "只能发布两个本机管理端口。")
    seen = set()
    for item in ports:
        require(isinstance(item, dict) and item.get("host_ip") == "127.0.0.1"
                and item.get("protocol", "tcp") == "tcp"
                and item.get("mode", "ingress") == "ingress",
                "PORT_BOUNDARY_CHANGED", "管理端口必须只绑定本机 TCP。")
        seen.add((str(item.get("target")), str(item.get("published"))))
    require(seen == {("5099", "5099"), ("6081", "6081")},
            "PORT_BOUNDARY_CHANGED", "管理端口必须为本机 5099 和 6081。")
    require(service.get("networks") == {"existing-bot": None}
            or service.get("networks") == {"existing-bot": {}},
            "NETWORK_BOUNDARY_CHANGED", "SnowLuma 只能加入既有机器人内部网络。")
    networks = compose.get("networks")
    require(isinstance(networks, dict) and set(networks) == {"existing-bot"},
            "NETWORK_BOUNDARY_CHANGED", "SnowLuma 的网络定义发生变化。")
    network = networks["existing-bot"]
    require(isinstance(network, dict) and network.get("external") is True
            and network.get("name") == "astrbot-roleplay_bot"
            and set(network) <= {"name", "external", "ipam"} and not network.get("ipam"),
            "NETWORK_BOUNDARY_CHANGED", "必须复用既有外部机器人网络。")

    mounts = service.get("volumes")
    require(isinstance(mounts, list) and len(mounts) == 3 and all(isinstance(m, dict) for m in mounts),
            "MOUNT_BOUNDARY_CHANGED", "必须使用三个独立 SnowLuma 数据挂载。")
    by_target = {m.get("target"): m for m in mounts}
    require(set(by_target) == {"/app/data", "/app/.config", "/app/.local/share"},
            "MOUNT_BOUNDARY_CHANGED", "数据挂载目标缺失、重复或新增。")
    for mount in mounts:
        require(set(mount) <= {"type", "source", "target", "read_only", "bind", "volume"}
                and mount.get("read_only", False) is False
                and not mount.get("volume") and not mount.get("bind"),
                "MOUNT_BOUNDARY_CHANGED", "数据挂载选项偏离独立可写数据目录。")
    data_path = legacy.local_path(root, "runtime/snowluma/data", directory=True)
    require(data_path == root / "runtime/snowluma/data", "MOUNT_BOUNDARY_CHANGED",
            "独立数据目录不能通过链接改为其他路径或 NapCat 数据。")
    bind = by_target["/app/data"]
    require(bind.get("type") == "bind" and isinstance(bind.get("source"), str)
            and Path(bind["source"]).resolve(strict=True) == data_path,
            "MOUNT_BOUNDARY_CHANGED", "SnowLuma data 必须绑定项目内独立目录。")
    volumes = compose.get("volumes")
    require(isinstance(volumes, dict) and set(volumes) == {"snowluma-qq-config", "snowluma-qq-data"},
            "MOUNT_BOUNDARY_CHANGED", "QQ 配置与登录卷必须独立，不能复用 NapCat。")
    for target, source in (("/app/.config", "snowluma-qq-config"), ("/app/.local/share", "snowluma-qq-data")):
        mount = by_target[target]
        require(mount.get("type") == "volume" and mount.get("source") == source
                and volumes[source] == {"name": PROJECT_NAME + "_" + source},
                "MOUNT_BOUNDARY_CHANGED", "QQ 卷名称或来源变化；不得移用 NapCat 数据。")
    environment = service.get("environment")
    require(isinstance(environment, dict)
            and set(environment) == set(ENVIRONMENT) | {"VNC_PASSWD", "SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD"}
            and all(environment.get(k) == v for k, v in ENVIRONMENT.items()),
            "ENVIRONMENT_CHANGED", "运行环境偏离已审查范围；不自动接受许可或启用额外功能。")
    require(legacy.same_secret(environment.get("VNC_PASSWD"), access.get("vnc_password"))
            and legacy.same_secret(environment.get("SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD"), access.get("webui_password")),
            "SECRET_MISMATCH", "SnowLuma 本地管理凭据缺失或不一致；不输出或重置。")
    require(service.get("logging") == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
            "LOG_BOUNDARY_CHANGED", "容器日志应保持有界本地轮转。")


def validate_onebot(config: dict, *, account: bool, token: object = None) -> None:
    expected_keys = {"networks", "statusCommand", "historySync", "notifications"}
    if account:
        expected_keys.add("mode")
    require(set(config) == expected_keys and (not account or config.get("mode") == "snapshot"),
            "ONEBOT_SCHEMA_CHANGED", "账号必须使用 snapshot；全局及账号配置含未审查字段。")
    networks = config.get("networks")
    require(isinstance(networks, dict) and set(networks) == NETWORKS
            and all(isinstance(v, list) for v in networks.values()),
            "ONEBOT_SCHEMA_CHANGED", "OneBot 网络列表无效。")
    require(all(not networks[k] for k in NETWORKS - {"wsClients"}),
            "ONEBOT_BOUNDARY_CHANGED", "不得开启额外 OneBot 监听或上报端点。")
    clients = networks["wsClients"]
    require(len(clients) == (1 if account else 0), "ONEBOT_BOUNDARY_CHANGED", "全局必须无端点，账号仅可有一个反向 WS。")
    require(config.get("statusCommand") == {"enabled": False, "swallow": False, "cooldownSeconds": 5, "trigger": "#sl"}
            and config.get("historySync") == {"enabled": False}
            and config.get("notifications") == {"channelIds": []},
            "NATIVE_REPLY_BOUNDARY_CHANGED", "原生状态回复、历史补全或通知边界已改变。")
    if account:
        client = clients[0]
        expected = {"name": "astrbot-reverse", "enabled": True, "url": "ws://astrbot:6199/ws",
                    "role": "Universal", "messageFormat": "array", "reportSelfMessage": False,
                    "reconnectIntervalMs": 5000}
        require(isinstance(client, dict) and set(client) == set(expected) | {"accessToken"}
                and all(type(client.get(k)) is type(v) and client.get(k) == v for k, v in expected.items()),
                "ONEBOT_BOUNDARY_CHANGED", "反向 WS 地址、角色、格式或自发消息设置已改变。")
        require(legacy.same_secret(client.get("accessToken"), token),
                "ONEBOT_SECRET_MISMATCH", "SnowLuma 与 AstrBot 的反向 WS 认证不一致；凭据未输出。")


def check_snowluma(root: Path) -> dict:
    root = root.resolve(strict=True)
    access = legacy.read_json(root, "runtime/snowluma/access.local.json")
    compose = render_compose(root)
    validate_compose(root, compose, access)
    inputs = legacy.read_json(root, "config/connection-inputs.local.json")
    qq = legacy.qq_identity(inputs)
    astrbot = legacy.read_json(root, "runtime/astrbot/cmd_config.json")
    platforms = [p for p in astrbot.get("platform", []) if isinstance(p, dict) and p.get("id") == legacy.PLATFORM_ID]
    require(len(platforms) == 1, "ASTRBOT_PLATFORM_INVALID", "找不到唯一的既有 AstrBot QQ 平台。")
    require(platforms[0].get("type") == "aiocqhttp" and platforms[0].get("enable") is True
            and platforms[0].get("ws_reverse_host") == "0.0.0.0" and platforms[0].get("ws_reverse_port") == 6199,
            "ASTRBOT_PLATFORM_INVALID", "既有 AstrBot 反向 WS 端配置不匹配。")
    config_dir = "runtime/snowluma/data/config/"
    validate_onebot(legacy.read_json(root, config_dir + "onebot.json"), account=False)
    validate_onebot(legacy.read_json(root, config_dir + f"onebot_{qq['bot_account_id']}.json"),
                    account=True, token=platforms[0].get("ws_reverse_token"))
    return {"ok": True, "mode": "snowluma-static-preflight", "runtime_modified": False,
            "compose_boundary_verified": True, "separate_qq_storage_configured": True,
            "credentials_consistent": True, "legacy_configuration_check_performed": False,
            "retained_validation_scope": "handled-separately-by-start-local-wrapper",
            "qq_login_verified": False, "reverse_ws_connection_verified": False,
            "message_delivery_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = check_snowluma(args.root)
    except PreflightError as error:
        report = {"ok": False, "code": error.code, "message": str(error), "runtime_modified": False}
    except Exception:
        report = {"ok": False, "code": "PREFLIGHT_INVALID", "message": "配置无法安全核对；原始内容已隐藏，未修改任何配置。", "runtime_modified": False}
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    elif report["ok"]:
        print("SnowLuma 静态配置检查通过；未修改配置。保留的 AstrBot/NapCat 配置由启动入口另行检查。")
        print("此结果不代表 QQ 已登录、反向 WS 已连接或消息已送达。")
    else:
        print(f"检查失败 [{report['code']}]：{report['message']}", file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
