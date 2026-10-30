"""只读汇总角色发言策略和服务端首回执；不读正文、凭据或连接 QQ。"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any


LEDGER = Path("runtime/astrbot/plugin_data/astrbot_plugin_roleplay_gateway/receipts.sqlite3")
WECHAT_LEDGER = LEDGER.parent / "weixin" / "receipts.sqlite3"
NATIVE_LEDGER = LEDGER.parent / "native" / "receipts.sqlite3"
RECEIPT_FIELDS = {
    "platform_id", "incoming_message_id", "purpose", "status", "reason",
    "received_at", "started_at", "first_ack_at", "confirmed_segments",
}
DECISION_FIELDS = {"platform_id", "incoming_message_id", "selected", "reason"}
ACTIVE = frozenset({"ACCEPTED", "IN_FLIGHT"})
FAILURE = frozenset({"FAILED", "UNKNOWN", "ABORTED", "REJECTED", "PARTIAL"})
KNOWN_STATUS = ACTIVE | FAILURE | {"SENT", "API_ACCEPTED", "SKIPPED"}


class ReportError(ValueError):
    """Static error codes only; no paths, identifiers or database contents."""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "ratio": numerator / denominator if denominator else None}


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    low, high = math.floor(index), math.ceil(index)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (index - low), 6)


def _latencies(values: list[float]) -> dict:
    return {"samples": len(values), "p50": _percentile(values, .5),
            "p95": _percentile(values, .95),
            "max": round(max(values), 6) if values else None,
            "percentile_method": "linear_interpolation_of_acknowledged_samples_only"}


def summarize(rows: list[dict]) -> dict:
    """Aggregate metadata already restricted to purpose=roleplay.

    Counts describe separate axes (selection, outcome, timing) and are not all
    additive. Missing receipt timing stays outside quantiles but inside rate
    denominators. No identifiers, arbitrary reasons or stored JSON are emitted.
    """
    counts = {key: 0 for key in (
        "valid_wakes", "selected", "selected_with_policy_record",
        "selected_without_policy_record", "decision_not_recorded",
        "policy_skipped", "probability_skipped", "cooldown_skipped",
        "quota_skipped", "other_policy_skipped", "acknowledged",
        "unanswered", "selected_unanswered", "active", "other_failures",
        "selected_other_failures", "policy_limited", "policy_limited_with_answer",
        "terminal_unanswered", "all_within_ten_seconds", "selected_within_ten_seconds",
    )}
    diagnostics = {key: 0 for key in (
        "invalid_received_at", "invalid_or_negative_ack_latency", "missing_ack_time",
        "ack_time_without_segment_count", "unknown_status", "invalid_decision",
        "selection_outcome_conflict", "recovered_receipts",
    )}
    selected_latencies: list[float] = []
    all_latencies: list[float] = []
    for row in rows:
        counts["valid_wakes"] += 1
        status, reason = row["status"], row["reason"]
        diagnostics["recovered_receipts"] += int(reason == "recovered_from_confirmed_delivery")
        has_decision = row["has_decision"] == 1
        decision, decision_reason = row["selected"], row["decision_reason"]
        is_selected = is_skipped = False
        if has_decision and type(decision) is int and decision == 1:
            is_selected = True
            counts["selected_with_policy_record"] += 1
        elif has_decision and type(decision) is int and decision == 0:
            is_skipped = True
        elif has_decision:
            diagnostics["invalid_decision"] += 1
            counts["decision_not_recorded"] += 1
        elif status == "SKIPPED":
            # Retain a skip visible in receipts even if its decision row is
            # missing. The inconsistency is explicit, never guessed as chosen.
            is_skipped = True
            diagnostics["invalid_decision"] += 1
            decision_reason = (reason.removeprefix("pacing_")
                               if isinstance(reason, str) else None)
        elif _number(row["started_at"]) is not None:
            # Normal policy-disabled roleplay has no pacing row. A started
            # request is selected, without claiming a configuration snapshot.
            is_selected = True
            counts["selected_without_policy_record"] += 1
        else:
            counts["decision_not_recorded"] += 1

        if is_selected:
            counts["selected"] += 1
        if is_skipped:
            counts["policy_skipped"] += 1
            bucket = (decision_reason + "_skipped"
                      if decision_reason in {"probability", "cooldown", "quota"}
                      else "other_policy_skipped")
            counts[bucket] += 1

        received, ack = _number(row["received_at"]), _number(row["first_ack_at"])
        segments = row["confirmed_segments"]
        has_segments = type(segments) is int and segments > 0
        has_ack_time = ack is not None and ack > 0
        acknowledged = has_segments or has_ack_time
        if acknowledged:
            counts["acknowledged"] += 1
        else:
            counts["unanswered"] += 1
            counts["selected_unanswered"] += int(is_selected)
            counts["terminal_unanswered"] += int(status not in ACTIVE)
        if has_segments and not has_ack_time:
            diagnostics["missing_ack_time"] += 1
        if has_ack_time and not has_segments:
            diagnostics["ack_time_without_segment_count"] += 1
        if (is_skipped and acknowledged) or (is_selected and status == "SKIPPED"):
            diagnostics["selection_outcome_conflict"] += 1
        if received is None or received <= 0:
            diagnostics["invalid_received_at"] += 1
        if row["first_ack_at"] is not None:
            if (not has_ack_time or received is None or received <= 0 or ack < received):
                diagnostics["invalid_or_negative_ack_latency"] += 1
            else:
                latency = ack - received
                all_latencies.append(latency)
                counts["all_within_ten_seconds"] += int(latency <= 10)
                if is_selected:
                    selected_latencies.append(latency)
                    counts["selected_within_ten_seconds"] += int(latency <= 10)

        active = status in ACTIVE
        counts["active"] += int(active)
        policy_limited = reason == "policy_limit"
        counts["policy_limited"] += int(policy_limited)
        counts["policy_limited_with_answer"] += int(policy_limited and acknowledged)
        if status not in KNOWN_STATUS:
            diagnostics["unknown_status"] += 1
        failed = ((status in FAILURE or status not in KNOWN_STATUS)
                  and not is_skipped and not (policy_limited and acknowledged))
        counts["other_failures"] += int(failed)
        counts["selected_other_failures"] += int(failed and is_selected)

    total, selected = counts["valid_wakes"], counts["selected"]
    return {
        "counts": counts,
        "rates": {
            "selection": _rate(selected, total),
            "policy_skip": _rate(counts["policy_skipped"], total),
            "unanswered": _rate(counts["unanswered"], total),
            "other_failure": _rate(counts["other_failures"], total),
            "all_valid_within_ten_seconds": _rate(counts["all_within_ten_seconds"], total),
            "selected_within_ten_seconds": _rate(counts["selected_within_ten_seconds"], selected),
            "selected_unanswered": _rate(counts["selected_unanswered"], selected),
            "selected_other_failure": _rate(counts["selected_other_failures"], selected),
        },
        "selected_first_ack_seconds": _latencies(selected_latencies),
        "all_first_ack_seconds": _latencies(all_latencies),
        "diagnostics": diagnostics,
    }


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    # Table names are internal constants, never user input.
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def build_report(root: Path, *, since: float | None = None,
                 until: float | None = None, channel: str = "qq") -> dict:
    if channel not in {"qq", "wechat", "wechat_native"}:
        raise ReportError("invalid_channel")
    for value in (since, until):
        if value is not None and (_number(value) is None or value < 0):
            raise ReportError("invalid_time_window")
    if since is not None and until is not None and since >= until:
        raise ReportError("invalid_time_window")
    root = root.resolve()
    path = (root / {"qq": LEDGER, "wechat": WECHAT_LEDGER, "wechat_native": NATIVE_LEDGER}[channel]).resolve()
    if not path.is_relative_to(root):
        raise ReportError("ledger_outside_project")
    if not path.is_file():
        raise ReportError("ledger_missing")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if (db.execute("PRAGMA user_version").fetchone()[0] not in {2, 3}
                    or not RECEIPT_FIELDS <= _columns(db, "receipts")
                    or not DECISION_FIELDS <= _columns(db, "pacing_decisions")):
                raise ReportError("pacing_schema_unavailable")
            conditions = ["r.purpose = 'roleplay'"]
            parameters = []
            if since is not None:
                conditions.append("r.received_at >= ?")
                parameters.append(since)
            if until is not None:
                conditions.append("r.received_at < ?")
                parameters.append(until)
            rows = db.execute("""SELECT r.status, r.reason, r.received_at, r.started_at,
                    r.first_ack_at, r.confirmed_segments,
                    CASE WHEN p.incoming_message_id IS NULL THEN 0 ELSE 1 END AS has_decision,
                    p.selected, p.reason AS decision_reason
                FROM receipts AS r LEFT JOIN pacing_decisions AS p
                    ON p.platform_id=r.platform_id AND p.incoming_message_id=r.incoming_message_id
                WHERE """ + " AND ".join(conditions), parameters).fetchall()
            # No identifiers or arbitrary purpose strings are exported.
            excluded = db.execute("""SELECT
                SUM(CASE WHEN purpose='management' THEN 1 ELSE 0 END),
                SUM(CASE WHEN purpose='neutral' THEN 1 ELSE 0 END),
                SUM(CASE WHEN purpose='legacy' THEN 1 ELSE 0 END),
                SUM(CASE WHEN purpose NOT IN ('roleplay','management','neutral','legacy')
                         OR purpose IS NULL THEN 1 ELSE 0 END)
                FROM receipts""").fetchone()
            summary = summarize([dict(row) for row in rows])
    except sqlite3.Error:
        raise ReportError("ledger_read_failed") from None
    return {
        "schema_version": 1,
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "read_only_roleplay_receipts_server_ack_metadata",
        "channel": channel,
        "acceptance_evidence": {"qq": "platform_message_receipt",
                                "wechat": "api_accepted_without_platform_message_id",
                                "wechat_native": "native_completion_without_platform_message_id"}[channel],
        "window": {"received_at_from_inclusive": since, "received_at_until_exclusive": until},
        "denominator": "deduplicated_claimed_receipts_with_purpose_roleplay",
        "excluded_all_time_counts": dict(zip(
            ("management", "neutral", "legacy_unclassified", "unknown_purpose"),
            (int(value or 0) for value in excluded))),
        **summary,
        "evidence_limits": {
            "latency": ("original_ingress_to_first_confirmed_platform_ack" if channel == "qq"
                        else "original_ingress_to_first_native_api_acceptance"),
            "ack_time_missing_until_outcome_settlement_or_after_crash": True,
            "active_unanswered_counts_are_provisional": True,
            "no_ack_latency_stays_in_rate_denominator": True,
            "policy_disabled_started_requests_in_selected": True,
            "invalid_input_or_pending_rejection_after_claim_stays_in_all_valid_denominator": True,
            "unclaimed_invalid_ids_unauthorized_and_nonwakes_not_counted": True,
            "percentiles_only_describe_valid_acknowledged_samples": True,
            "client_visible_delivery_verified": False,
            "substantive_answer_semantics_verified": False,
            "formal_95_percent_under_ten_seconds_acceptance": False,
            "historical_policy_and_latency_evidence_incomplete_due_to_recovery": (
                summary["diagnostics"]["recovered_receipts"] > 0),
            "recovered_period_rates_are_not_selection_or_slo_acceptance_evidence": (
                summary["diagnostics"]["recovered_receipts"] > 0),
        },
        "read_only": True,
        "chat_body_read_or_exported": False,
        "credentials_read_or_exported": False,
        "network_or_qq_calls": False,
    }


def parse_time(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        stamp = parsed.timestamp()
        if not math.isfinite(stamp) or stamp < 0:
            raise ValueError
        return stamp
    except (ValueError, OverflowError, OSError):
        raise argparse.ArgumentTypeError("时间必须是带时区的 ISO 8601 日期时间") from None


def _host_requires_runtime() -> bool:
    # Windows SQLite and the Linux writer must not concurrently access a live
    # bind-mounted WAL/SHM. mode=ro does not make that cross-OS access safe.
    return os.name == "nt"


def build_runtime_report(root: Path, *, since: float | None = None,
                         until: float | None = None, channel: str = "qq") -> dict:
    """Run the same metadata-only reader beside the writer, without host SQLite.

    Source travels on stdin; no code or database copies are written. Compose
    exec selects the existing AstrBot service and never starts/restarts it.
    Docker stderr/stdout errors are deliberately not echoed to the user.
    """
    if channel not in {"qq", "wechat", "wechat_native"}:
        raise ReportError("invalid_channel")
    if (any(value is not None and (_number(value) is None or value < 0) for value in (since, until))
            or (since is not None and until is not None and since >= until)):
        raise ReportError("invalid_time_window")
    root = root.resolve()
    source = Path(__file__).read_text(encoding="utf-8")
    options = {"since": since, "until": until, "channel": channel}
    program = (
        "import json\nfrom pathlib import Path\n"
        "scope = {'__name__': '_reply_pacing_runtime', '__file__': '/reply_pacing_runtime.py'}\n"
        "try:\n"
        f"    exec(compile({source!r}, '<reply-pacing-runtime>', 'exec'), scope)\n"
        "    scope['LEDGER'] = Path('plugin_data/astrbot_plugin_roleplay_gateway/receipts.sqlite3')\n"
        "    scope['WECHAT_LEDGER'] = scope['LEDGER'].parent / 'weixin' / 'receipts.sqlite3'\n"
        "    scope['NATIVE_LEDGER'] = scope['LEDGER'].parent / 'native' / 'receipts.sqlite3'\n"
        f"    result = scope['build_report'](Path('/AstrBot/data'), **{options!r})\n"
        "    print(json.dumps(result, ensure_ascii=False, allow_nan=False))\n"
        "except Exception:\n"
        "    print(json.dumps({'ok': False, 'code': 'runtime_report_failed'}))\n"
        "    raise SystemExit(1)\n"
    )
    try:
        completed = subprocess.run(
            ["docker", "compose", "--project-directory", str(root), "-f", str(root / "compose.yaml"),
             "exec", "-T", "astrbot", "python", "-"],
            input=program, cwd=root, capture_output=True, text=True, encoding="utf-8",
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ReportError("runtime_report_unavailable") from None
    if completed.returncode != 0:
        raise ReportError("runtime_report_failed")
    if not isinstance(completed.stdout, str) or len(completed.stdout) > 131072:
        raise ReportError("runtime_report_invalid")
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError):
        raise ReportError("runtime_report_invalid") from None
    expected_fields = {"schema_version", "ok", "generated_at", "scope", "channel", "acceptance_evidence",
                       "window", "denominator", "excluded_all_time_counts", "counts", "rates",
                       "selected_first_ack_seconds", "all_first_ack_seconds", "diagnostics",
                       "evidence_limits", "read_only", "chat_body_read_or_exported",
                       "credentials_read_or_exported", "network_or_qq_calls"}
    if (not isinstance(result, dict) or set(result) != expected_fields
            or result.get("ok") is not True or result.get("channel") != channel
            or result.get("read_only") is not True
            or result.get("chat_body_read_or_exported") is not False
            or result.get("credentials_read_or_exported") is not False):
        raise ReportError("runtime_report_invalid")
    result["execution_location"] = "astrbot_container"
    return result


def _report_destination(root: Path, requested: Path) -> Path:
    root = root.resolve()
    reports = root / "reports"
    # In particular, do not allow a reports junction/symlink to redirect a
    # seemingly harmless report write into runtime or another project.
    if reports.resolve() != reports:
        raise ReportError("report_must_be_inside_reports")
    destination = (root / requested).resolve()
    if (not destination.is_relative_to(reports) or destination == reports
            or destination.suffix.lower() != ".json"):
        raise ReportError("report_must_be_inside_reports")
    return destination


def _write_report(root: Path, requested: Path, encoded: str) -> None:
    destination = _report_destination(root, requested)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after mkdir. Atomic replacement also avoids modifying the linked
    # inode if an existing report file is a hard link to another file.
    if _report_destination(root, requested) != destination:
        raise ReportError("report_must_be_inside_reports")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         prefix=".reply-pacing-", suffix=".tmp",
                                         dir=destination.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(encoded)
        if _report_destination(root, requested) != destination:
            raise ReportError("report_must_be_inside_reports")
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runtime", action="store_true", help="在运行中的 AstrBot 容器内只读统计；Windows 必须使用")
    parser.add_argument("--channel", choices=("qq", "wechat", "wechat_native"), default="qq")
    parser.add_argument("--since", type=parse_time, help="入站起点，含边界；ISO 8601 带时区")
    parser.add_argument("--until", type=parse_time, help="入站终点，不含边界；ISO 8601 带时区")
    parser.add_argument("--report", type=Path, help="可选：只允许保存到本项目 reports 目录内")
    args = parser.parse_args(argv)
    try:
        if not args.runtime and _host_requires_runtime():
            raise ReportError("runtime_required_use_--runtime")
        if args.report is not None:
            _report_destination(args.root, args.report)
        builder = build_runtime_report if args.runtime else build_report
        report = builder(args.root, since=args.since, until=args.until, channel=args.channel)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.report is not None:
            _write_report(args.root, args.report, encoded)
        print(encoded, end="")
        return 0
    except ReportError as error:
        result = {"ok": False, "code": str(error), "runtime_modified": False}
    except (OSError, ValueError, TypeError):
        result = {"ok": False, "code": "report_failed", "runtime_modified": False}
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
