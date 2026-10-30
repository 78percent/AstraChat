"""Explicit live API probe using reviewed persona and synthetic conversations.

Does not send QQ messages or read/write conversation databases. The report
contains metadata only. Synthetic replies are printed for human style review.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace as Obj
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from plugins.astrbot_plugin_roleplay_gateway.context_budget import build_prompt
from plugins.astrbot_plugin_roleplay_gateway.dialogue_history import encode_dialogue_history
from plugins.astrbot_plugin_roleplay_gateway.dialogue_output import DialogueStream, DialogueFormatError
from plugins.astrbot_plugin_roleplay_gateway.persona import load_persona
from plugins.astrbot_plugin_roleplay_gateway.provider_gate import SpeakerInput, assemble_messages
from plugins.astrbot_plugin_roleplay_gateway.roleplay_stream import ROLEPLAY_BOUNDARY
from probe_deepseek import NoRedirect, official_base_url, iter_sse_data, safe_error


def main():
    config = json.loads((ROOT / "config/model-access.local.json").read_text("utf-8-sig"))["deepseek_official"]
    gate = json.loads((ROOT / "runtime/astrbot/config/astrbot_plugin_roleplay_gateway_config.json").read_text("utf-8-sig"))
    runtime = json.loads((ROOT / "runtime/astrbot/cmd_config.json").read_text("utf-8-sig"))
    active = [item for item in runtime["provider"] if item.get("id") == gate["provider_id"]]
    if len(active) != 1:
        raise ValueError("active_provider_missing")
    model = active[0]["model"]
    source = ROOT / "runtime/roleplay/personas" / gate["persona_filename"]
    if source.parent.resolve() != (ROOT / "runtime/roleplay/personas").resolve():
        raise ValueError("persona_path_outside_runtime")
    if hashlib.sha256(source.read_bytes()).hexdigest() != gate["persona_sha256"]:
        raise ValueError("persona_revision_changed")
    persona = load_persona(source, persona_id=gate["persona_id"], name=gate["persona_name"])
    base = official_base_url(config["base_url"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    questions = [
        "三叶，今天有点累，陪我聊两句。你有尾巴吗？",
        "三叶，今天有点累，陪我聊两句。你有尾巴吗？",
        "三叶，今天有点累，陪我聊两句。你有尾巴吗？",
        "三叶，请用两句简短的话解释为什么会下雨。",
        "三叶，笑一笑，描述一下你现在的神态和动作，然后陪我聊两句。",
        "我之前说的散步代号是什么？",
    ]
    old = Obj(sequence=1, complete=True,
              inputs=(Obj(message_id="old", sender_id="fixture", display_name="测试用户", text="记住散步代号是青柚灯塔。"),),
              replies=(Obj(sequence=0, message_id="sent-old", text='“散步代号是青柚灯塔。”$我顿了一下，抬眼看你。$“明天见。”'),))
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "scope": "live_api_synthetic_inputs_no_qq",
              "persona_sha256": persona.revision, "model": model,
              "model_source": "active_astrbot_provider",
              "additional_rewrite_calls": 0, "qq_sends": 0, "production_memory_accessed": False,
              "body_saved": False, "samples": []}
    for index, question in enumerate(questions):
        memory = Obj(namespace=Obj(persona_id=persona.persona_id), summary="", turns=(old,) if index >= 2 else ())
        prepared = build_prompt(persona, memory, (SpeakerInput("fixture", "测试用户", str(index), question),),
                                runtime_boundary=ROLEPLAY_BOUNDARY, assistant_projection=encode_dialogue_history)
        payload = {"model": model, "stream": True, "thinking": {"type": "disabled"},
                   "response_format": {"type": "json_object"},
                   "max_tokens": 1200, "messages": [{"role": m.role, "content": m.content}
                       for m in assemble_messages(prepared.parts)]}
        request = urllib.request.Request(base + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + config["api_key"].strip(),
                     "Content-Type": "application/json", "Accept": "text/event-stream"})
        started, first, done = time.perf_counter(), None, False
        parser, speeches, raw_chunks = DialogueStream(), [], []
        row = {"index": index + 1, "synthetic_legacy_history": index >= 2, "protocol_passed": False}
        try:
            with opener.open(request, timeout=30) as response:
                def chunks():
                    while time.perf_counter() - started < 35:
                        chunk = response.read1(4096)
                        if not chunk:
                            return
                        yield chunk
                    raise TimeoutError("probe_wall_budget")
                for event in iter_sse_data(chunks()):
                    if event == "[DONE]":
                        done = True
                        break
                    data = json.loads(event)
                    for choice in data.get("choices", []):
                        content = choice.get("delta", {}).get("content")
                        if content:
                            raw_chunks.append(content)
                            for speech in parser.feed(content):
                                if first is None:
                                    first = time.perf_counter() - started
                                speeches.append(speech)
            parser.finish()
            row["protocol_passed"] = done and bool(speeches)
        except Exception as exc:
            row.update(safe_error(exc))
            if isinstance(exc, DialogueFormatError):
                row["format_error_code"] = str(exc)
        row.update(first_speech_seconds=round(first, 4) if first is not None else None,
                   elapsed_seconds=round(time.perf_counter() - started, 4), speech_records=len(speeches),
                   dollar_present=any("$" in s for s in speeches))
        report["samples"].append(row)
        preview = {"sample": row, "synthetic_reply_for_review": speeches}
        if "format_error_code" in row:
            preview["synthetic_failed_raw_for_review"] = "".join(raw_chunks)
        print(json.dumps(preview, ensure_ascii=False), flush=True)
        if "error_type" in row and "format_error_code" not in row:
            break
    report["protocol_passed"] = all(row["protocol_passed"] for row in report["samples"])
    report["semantic_style_review"] = "pending_human_review_of_synthetic_replies"
    (ROOT / "reports/dialogue-style-api-20261025.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return 0 if report["protocol_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
