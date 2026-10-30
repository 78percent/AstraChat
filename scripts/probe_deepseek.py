"""Probe an explicitly configured official DeepSeek endpoint without logging secrets or response text."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from typing import Iterable, Iterator
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def official_base_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_base_url")
    parts = urlsplit(value)
    if not (parts.scheme == "https" and parts.hostname == "api.deepseek.com"
            and parts.port in (None, 443) and parts.username is None and parts.password is None
            and parts.path in ("", "/", "/v1", "/v1/") and not parts.query and not parts.fragment):
        raise ValueError("unapproved_base_url")
    return value.rstrip("/")


def safe_error(exc: Exception) -> dict:
    """Never include exception messages or HTTP response bodies in saved diagnostics."""
    result = {"error_type": type(exc).__name__}
    if isinstance(exc, urllib.error.HTTPError):
        result["http_status"] = exc.code
    elif isinstance(exc, urllib.error.URLError):
        result["cause_type"] = type(exc.reason).__name__
    return result


def iter_sse_data(chunks: Iterable[bytes]) -> Iterator[str]:
    """Handle arbitrary transport boundaries, CRLF, multiline data, and final EOF data."""
    pending = b""
    event_lines: list[str] = []

    def accept(line: bytes) -> str | None:
        line = line.removesuffix(b"\r")
        if not line:
            if not event_lines:
                return None
            event = "\n".join(event_lines)
            event_lines.clear()
            return event
        if line.startswith(b"data:"):
            value = line[5:]
            if value.startswith(b" "):
                value = value[1:]
            event_lines.append(value.decode("utf-8"))
        return None

    for chunk in chunks:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            event = accept(line)
            if event is not None:
                yield event
    if pending:
        event = accept(pending)
        if event is not None:
            yield event
    if event_lines:
        yield "\n".join(event_lines)


def measure_stream(events: Iterable[str], start: float, clock=time.perf_counter) -> dict:
    report = {"actual_model": None, "first_content_seconds": None,
              "nonempty_content_chunks": 0, "content_characters": 0,
              "stream_done": False, "finish_reason": None}
    for data in events:
        if data.strip() == "[DONE]":
            report["stream_done"] = True
            break
        event = json.loads(data)
        if "error" in event:
            report["error_type"] = "stream_error_response"
            break
        model = event.get("model")
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", model):
            report["actual_model"] = model
        for choice in event.get("choices", []):
            content = choice.get("delta", {}).get("content")
            if isinstance(content, str) and content.strip():
                if report["first_content_seconds"] is None:
                    report["first_content_seconds"] = round(clock() - start, 4)
                report["nonempty_content_chunks"] += 1
                report["content_characters"] += len(content)
            finish = choice.get("finish_reason")
            if finish in ("stop", "length", "content_filter", "tool_calls", "insufficient_system_resource"):
                report["finish_reason"] = finish
    report["total_seconds"] = round(clock() - start, 4)
    report["success"] = bool(report["nonempty_content_chunks"] and report["stream_done"]
                             and "error_type" not in report)
    return report


def probe_config(config_value: dict, model_override: str | None = None,
                 models_only: bool = False, transport: str = "system") -> dict:
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "scope": "official_api_only_no_qq_latency_claim", "models_only": models_only,
              "authenticated": False, "models_http_status": None, "success": False,
              "transport": transport, "request_stage": "configuration"}
    try:
        config = config_value["deepseek_official"]
        base = official_base_url(config.get("base_url"))
        key = config.get("api_key")
        if not isinstance(key, str) or not key.strip():
            raise ValueError("missing_api_key")
        model = model_override or config.get("model_id") or "deepseek-flash"
        if not isinstance(model, str) or not re.fullmatch(r"deepseek-[A-Za-z0-9_.-]{1,70}", model):
            raise ValueError("invalid_model_id")
        report["requested_model"] = model
        if transport not in ("system", "direct"):
            raise ValueError("invalid_transport")
        proxy_handler = urllib.request.ProxyHandler({}) if transport == "direct" else urllib.request.ProxyHandler()
        opener = urllib.request.build_opener(proxy_handler, NoRedirect)
        headers = {"Authorization": "Bearer " + key.strip(), "Accept": "application/json"}
        request = urllib.request.Request(base + "/models", headers=headers, method="GET")
        report["request_stage"] = "models"
        try:
            with opener.open(request, timeout=30) as response:
                report["models_http_status"] = response.status
                payload = json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            report["models_http_status"] = exc.code
            raise
        models = [entry["id"] for entry in payload.get("data", [])
                  if isinstance(entry, dict) and isinstance(entry.get("id"), str)
                  and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", entry["id"])]
        report["authenticated"] = report["models_http_status"] == 200
        report["model_ids"] = models
        report["requested_model_available"] = model in models
        if models_only:
            report["success"] = report["authenticated"]
            return report
        if model not in models:
            report["error_type"] = "requested_model_not_available"
            return report
        payload = {"model": model, "stream": True, "max_tokens": 32,
                   "thinking": {"type": "disabled"},
                   "messages": [{"role": "user", "content": "请只回复：连接正常"}]}
        headers = dict(headers, **{"Content-Type": "application/json", "Accept": "text/event-stream"})
        request = urllib.request.Request(base + "/chat/completions", headers=headers,
                                         data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                         method="POST")
        started = time.perf_counter()
        report["generation_http_status"] = None
        report["request_stage"] = "generation"
        try:
            with opener.open(request, timeout=30) as response:
                report["generation_http_status"] = response.status

                def chunks():
                    while True:
                        if time.perf_counter() - started > 45:
                            raise TimeoutError("probe_wall_budget")
                        chunk = response.read1(4096)
                        if not chunk:
                            break
                        yield chunk

                report.update(measure_stream(iter_sse_data(chunks()), started))
        except Exception as exc:
            report.update(safe_error(exc))
            if isinstance(exc, urllib.error.HTTPError):
                report["generation_http_status"] = exc.code
            report["total_seconds"] = round(time.perf_counter() - started, 4)
    except Exception as exc:
        report.update(safe_error(exc))
    return report


def probe(config_path: Path, model_override: str | None = None,
          models_only: bool = False, transport: str = "system") -> dict:
    try:
        config_value = json.loads(config_path.read_text("utf-8-sig"))
    except Exception as exc:
        return {"checked_at": datetime.now(timezone.utc).isoformat(),
                "scope": "official_api_only_no_qq_latency_claim", "models_only": models_only,
                "transport": transport, "request_stage": "configuration", "success": False,
                **safe_error(exc)}
    return probe_config(config_value, model_override, models_only, transport)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=root / "config/model-access.local.json")
    parser.add_argument("--model", help="Explicit model ID; otherwise use configured model_id.")
    parser.add_argument("--models-only", action="store_true", help="Only authenticated GET /models; no generation.")
    parser.add_argument("--transport", choices=("system", "direct"), default="system",
                        help="Use urllib system proxies or explicitly connect directly; changes no OS settings.")
    parser.add_argument("--report", type=Path, default=root / "reports/deepseek-connectivity.json")
    args = parser.parse_args()
    report = probe(args.config, args.model, args.models_only, args.transport)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
