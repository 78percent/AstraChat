"""Read-only local NapCat status. Never refresh QR codes, log into QQ, send, or restart."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

BASE_URL = "http://127.0.0.1:6099"
TIMEOUT_SECONDS = 5
ALLOWED_ENDPOINTS = frozenset({"/api/auth/login", "/api/QQLogin/CheckLoginStatus",
                               "/api/QQLogin/GetQQLoginInfo"})


class StatusError(Exception):
    """Static, non-sensitive error codes only."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise StatusError("local_file_outside_project")
    return path


def post(opener, endpoint: str, payload: dict, credential: str | None = None) -> tuple[int, dict]:
    if endpoint not in ALLOWED_ENDPOINTS:
        raise StatusError("endpoint_not_allowed")
    headers = {"Content-Type": "application/json"}
    if credential is not None:
        headers["Authorization"] = "Bearer " + credential
    request = urllib.request.Request(BASE_URL + endpoint, headers=headers, method="POST",
                                     data=json.dumps(payload).encode("utf-8"))
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        value = json.loads(response.read(1024 * 1024))
        if not isinstance(value, dict):
            raise StatusError("unexpected_response_shape")
        return response.status, value


def response_code(value: dict) -> int | None:
    code = value.get("code")
    return code if type(code) is int else None


def check_status(root: Path) -> dict:
    result = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
              "scope": "napcat_backend_status_only", "success": False,
              "webui_authenticated": False, "loginPhase": None, "coreReady": None,
              "isLogin": None, "isOffline": None, "online": None,
              "account_matches_expected": None, "backend_reports_expected_account_online": False,
              "qq_delivery_verified": False, "credentials_recorded": False,
              "qq_login_or_send_or_restart_performed": False}
    stage = "local_inputs"
    try:
        settings = json.loads(local_file(root, "runtime/napcat/config/webui.json").read_text("utf-8-sig"))
        token = settings.get("token")
        inputs = json.loads(local_file(root, "config/connection-inputs.local.json").read_text("utf-8-sig"))
        expected = inputs["qq"]["bot_account_id"]
        if not isinstance(token, str) or not token:
            raise StatusError("missing_webui_token")
        if not isinstance(expected, str) or not re.fullmatch(r"[1-9][0-9]{4,11}", expected):
            raise StatusError("invalid_expected_account")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        stage = "webui_authentication"
        status, auth = post(opener, "/api/auth/login", {
            "hash": hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()})
        data = auth.get("data") or {}
        credential = data.get("Credential") if isinstance(data, dict) else None
        result["authentication_http_status"] = status
        result["authentication_response_code"] = response_code(auth)
        result["webui_authenticated"] = (status == 200 and response_code(auth) == 0
                                           and isinstance(credential, str) and bool(credential))
        if not result["webui_authenticated"]:
            raise StatusError("webui_authentication_failed")
        stage = "qq_login_status"
        status, payload = post(opener, "/api/QQLogin/CheckLoginStatus", {}, credential)
        result["login_status_http_status"] = status
        result["login_status_response_code"] = response_code(payload)
        if status != 200 or response_code(payload) != 0:
            raise StatusError("qq_login_status_query_failed")
        data = payload.get("data") or {}
        for field in ("coreReady", "isLogin", "isOffline"):
            result[field] = data.get(field) if isinstance(data.get(field), bool) else None
        phase = data.get("loginPhase")
        result["loginPhase"] = (phase if isinstance(phase, str) and re.fullmatch(r"[a-z_]{1,40}", phase)
                                else "other_or_missing")
        result["login_error_present"] = bool(data.get("loginError"))
        stage = "qq_login_info"
        status, payload = post(opener, "/api/QQLogin/GetQQLoginInfo", {}, credential)
        result["login_info_http_status"] = status
        result["login_info_response_code"] = response_code(payload)
        if status != 200 or response_code(payload) != 0:
            raise StatusError("qq_login_info_query_failed")
        data = payload.get("data") or {}
        result["online"] = data.get("online") if isinstance(data.get("online"), bool) else None
        account = data.get("uin")
        result["account_matches_expected"] = str(account) == expected if account is not None else None
        result["backend_reports_expected_account_online"] = all(
            result[field] is True for field in ("isLogin", "coreReady", "online", "account_matches_expected"))
        result["success"] = True
    except Exception as exc:
        result["failed_stage"] = stage
        result["error_type"] = type(exc).__name__
        if isinstance(exc, StatusError):
            result["error_code"] = str(exc)
        elif isinstance(exc, urllib.error.HTTPError):
            result["error_http_status"] = exc.code
        elif isinstance(exc, urllib.error.URLError):
            result["cause_type"] = type(exc.reason).__name__
    return result


def report_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    reports = local_file(root, "reports").resolve()
    resolved = path.resolve()
    if (resolved.parent != reports or path.suffix.lower() != ".json"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json", path.name)):
        raise StatusError("report_must_be_directly_under_reports_as_json")
    return path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", metavar="reports/local-status.json",
                        help="Optionally save sanitized JSON directly under this project's reports directory.")
    args = parser.parse_args()
    try:
        destination = report_path(root, args.report) if args.report else None
        result = check_status(root)
        if destination is not None:
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        result = {"success": False, "error_type": type(exc).__name__}
        if isinstance(exc, StatusError):
            result["error_code"] = str(exc)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
