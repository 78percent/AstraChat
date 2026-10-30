"""生成首次启动资料；已有运行目录一律保留，不覆盖密码或登录状态。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import sys
from pathlib import Path


PLATFORM_ID = "qq-sakura"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def validated_inputs(root: Path) -> tuple[dict, Path]:
    inputs = json.loads((root / "config/connection-inputs.local.json").read_text("utf-8"))
    qq = inputs["qq"]
    for key in ("admin_account_ids", "private_contact_allowlist", "test_group_ids"):
        values = qq[key]
        if not isinstance(values, list) or not values:
            raise ValueError(f"接入清单的 {key} 必须是非空账号列表。")
        if any(not isinstance(v, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", v) for v in values):
            raise ValueError(f"接入清单的 {key} 存在无效账号。")
    bot = qq["bot_account_id"]
    if not isinstance(bot, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", bot):
        raise ValueError("机器人 QQ 格式错误。")
    if bot in qq["admin_account_ids"] or bot in qq["private_contact_allowlist"]:
        raise ValueError("机器人账号不能同时充当管理员或私聊测试账号。")
    source = (root / inputs["persona"]["source_file"]).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file():
        raise ValueError("人设必须是项目目录内的文件。")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != inputs["persona"]["sha256"].lower():
        raise ValueError("人设与确认的 SHA256 不一致，请先更新接入记录。")
    if not data.decode("utf-8-sig").strip():
        raise ValueError("人设为空。")
    return inputs, source


def deployment_inputs(inputs: dict) -> dict:
    """仅比较会影响生成配置的输入，不把验证进度视为配置变更。"""
    return {
        "qq": {key: inputs["qq"][key] for key in (
            "bot_account_id", "admin_account_ids", "private_contact_allowlist", "test_group_ids")},
        "persona": {key: inputs["persona"][key] for key in ("source_file", "sha256")},
    }


def prepare(root: Path) -> bool:
    root = root.resolve()
    inputs, source = validated_inputs(root)
    runtime = root / "runtime"
    if runtime.exists():
        snapshot = runtime / "inputs.snapshot.json"
        if not snapshot.is_file() or deployment_inputs(json.loads(snapshot.read_text("utf-8"))) != deployment_inputs(inputs):
            raise ValueError("已有运行目录与接入清单不一致；请停机核对，不自动覆盖。")
        required = ["astrbot/cmd_config.json", "astrbot.env", "napcat.env", "access.local.json",
                    "napcat/config/webui.json", "napcat/config/onebot11.json",
                    f"napcat/config/onebot11_{inputs['qq']['bot_account_id']}.json",
                    f"roleplay/personas/{source.name}", "imports/sakura_mitsuha.json"]
        if not all((runtime / name).is_file() for name in required):
            raise ValueError("已有运行目录缺少配置；请核对或从备份恢复，不重置凭据。")
        return False

    qq = inputs["qq"]
    onebot_token = secrets.token_hex(32)
    webui_token = secrets.token_urlsafe(32)
    password = secrets.token_urlsafe(24) + "Aa1!"
    whitelist = [f"{PLATFORM_ID}:GroupMessage:{gid}" for gid in qq["test_group_ids"]]
    whitelist += [f"{PLATFORM_ID}:FriendMessage:{uid}" for uid in qq["private_contact_allowlist"]]
    config = {
        "config_version": 5,
        "admins_id": qq["admin_account_ids"],
        "platform_settings": {
            "unique_session": False,
            "enable_id_white_list": True,
            "id_whitelist": whitelist,
            "id_whitelist_log": False,
            "wl_ignore_admin_on_group": False,
            "wl_ignore_admin_on_friend": False,
            "ignore_bot_self_message": True,
            "ignore_at_all": True,
            "empty_mention_waiting": False,
            "empty_mention_waiting_need_reply": False,
            "segmented_reply": {"enable": False},
        },
        "provider_sources": [],
        "provider": [],
        "provider_settings": {
            "enable": False,
            "streaming_response": True,
            "unsupported_streaming_strategy": "realtime_segmenting",
            "datetime_system_prompt": False,
            "web_search": False,
            "computer_use_runtime": "none",
            "proactive_capability": {"add_cron_tools": False},
        },
        "provider_ltm_settings": {
            "group_icl_enable": False,
            "group_message_history_enable": False,
            "active_reply": {"enable": False},
        },
        "provider_stt_settings": {"enable": False},
        "provider_tts_settings": {"enable": False},
        "dashboard": {"enable": True, "host": "0.0.0.0", "port": 6185, "username": "astrbot"},
        "platform": [{"id": PLATFORM_ID, "type": "aiocqhttp", "enable": True,
                      "ws_reverse_host": "0.0.0.0", "ws_reverse_port": 6199,
                      "ws_reverse_token": onebot_token}],
        "wake_prefix": ["/"],
        "plugin_set": [],
        "disable_builtin_commands": True,
        "disable_metrics": True,
        "timezone": "Asia/Shanghai",
        "log_level": "WARNING",
    }
    client = {"enable": True, "name": "astrbot-roleplay", "url": "ws://astrbot:6199/ws",
              "reportSelfMessage": False, "messagePostFormat": "array", "token": onebot_token,
              "debug": False, "heartInterval": 30000, "reconnectInterval": 5000}
    onebot = {"network": {"httpServers": [], "httpSseServers": [], "httpClients": [],
                          "websocketServers": [], "websocketClients": [client], "plugins": []},
              "musicSignUrl": "", "enableLocalFile2Url": False, "parseMultMsg": False}
    staging = root / (".prepare-" + secrets.token_hex(8))
    staging.mkdir()
    try:
        write_json(staging / "inputs.snapshot.json", inputs)
        write_json(staging / "astrbot/cmd_config.json", config)
        write_json(staging / f"napcat/config/onebot11_{qq['bot_account_id']}.json", onebot)
        # 意外登录其他账号时不自动建立反向连接。
        generic = json.loads(json.dumps(onebot))
        generic["network"]["websocketClients"] = []
        write_json(staging / "napcat/config/onebot11.json", generic)
        write_json(staging / "napcat/config/webui.json", {
            "host": "0.0.0.0", "port": 6099, "prefix": "", "token": webui_token, "loginRate": 3})
        write_json(staging / "access.local.json", {
            "astrbot_url": "http://127.0.0.1:6185", "astrbot_username": "astrbot",
            "astrbot_initial_password": password, "napcat_url": "http://127.0.0.1:6099",
            "napcat_webui_token": webui_token,
            "note": "本地私密登录资料。首次登录 AstrBot 后按界面要求改密码；修改后此初始密码失效。"})
        (staging / "astrbot.env").write_text(f"ASTRBOT_DASHBOARD_INITIAL_PASSWORD={password}\n", "utf-8")
        (staging / "napcat.env").write_text(f"ACCOUNT={qq['bot_account_id']}\n", "utf-8")
        for directory in ("napcat/qq", "napcat/plugins", "roleplay/personas", "roleplay/state"):
            (staging / directory).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, staging / "roleplay/personas" / source.name)
        write_json(staging / "imports/sakura_mitsuha.json", {
            "persona_id": "sakura_mitsuha", "system_prompt": source.read_text("utf-8-sig"),
            "begin_dialogs": [], "tools": [], "skills": []})
        staging.rename(runtime)
    except Exception:
        # staging 是本次创建且位于项目根目录内的随机临时目录。
        if staging.is_dir() and staging.parent == root:
            shutil.rmtree(staging)
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        created = prepare(args.root)
    except (ValueError, KeyError, OSError) as exc:
        print(f"准备失败：{exc}", file=sys.stderr)
        return 1
    print("首次启动资料已生成；模型调用保持关闭。" if created else "保留已有运行资料，未覆盖配置或凭据。")
    print("登录资料位置：runtime/access.local.json（不在终端输出密码）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
