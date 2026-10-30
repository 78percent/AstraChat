"""Network-free contracts for the one-shot official API probe."""

import importlib.util
import io
import json
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/probe_deepseek.py"
SPEC = importlib.util.spec_from_file_location("probe_deepseek", SCRIPT)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeDeepseekTests(unittest.TestCase):
    def test_sse_transport_splits_and_final_done_without_newline(self):
        raw = ('data: {"model":"deepseek-flash","choices":[{"delta":{"content":"连接正常"}}]}\r\n\r\n'
               'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
               'data: [DONE]').encode("utf-8")
        events = probe.iter_sse_data(raw[i:i + 3] for i in range(0, len(raw), 3))
        ticks = iter([11.25, 11.5])
        result = probe.measure_stream(events, 10, lambda: next(ticks))
        self.assertTrue(result["success"])
        self.assertTrue(result["stream_done"])
        self.assertEqual(result["first_content_seconds"], 1.25)
        self.assertEqual(result["nonempty_content_chunks"], 1)
        self.assertEqual(result["content_characters"], 4)
        self.assertNotIn("连接正常", json.dumps(result, ensure_ascii=False))

    def test_eof_data_is_processed_but_no_done_is_incomplete(self):
        events = probe.iter_sse_data([b'data: {"choices":[{"delta":{"content":"hello"}}]}'])
        result = probe.measure_stream(events, 0, lambda: 1)
        self.assertEqual(result["nonempty_content_chunks"], 1)
        self.assertFalse(result["stream_done"])
        self.assertFalse(result["success"])

    def test_http_and_stream_error_payloads_never_enter_report(self):
        secret = "secret-test-sentinel"
        exc = urllib.error.HTTPError("https://api.deepseek.com/?key=" + secret, 401, secret,
                                     {}, io.BytesIO(secret.encode()))
        result = probe.safe_error(exc)
        self.assertEqual(result["http_status"], 401)
        self.assertNotIn(secret, json.dumps(result))
        result = probe.measure_stream([json.dumps({"error": {"message": secret}})], 0, lambda: 1)
        self.assertFalse(result["success"])
        self.assertNotIn(secret, json.dumps(result))

    def test_only_official_https_paths_are_allowed(self):
        self.assertEqual(probe.official_base_url("https://api.deepseek.com/v1/"), "https://api.deepseek.com/v1")
        for url in ("http://api.deepseek.com", "https://api.deepseek.com.evil.invalid",
                    "https://example.com", "https://x:secret@api.deepseek.com",
                    "https://api.deepseek.com?api_key=secret", "https://api.deepseek.com:444",
                    "https://api.deepseek.com/other"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                probe.official_base_url(url)

    def test_redirect_does_not_forward_authorization(self):
        handler = probe.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://example.com"))

    def test_direct_transport_disables_system_proxy_without_mutating_settings(self):
        config = {"deepseek_official": {"api_key": "not-a-real-key", "base_url": "https://api.deepseek.com",
                                         "model_id": "deepseek-flash"}}
        with patch.object(probe.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = urllib.error.URLError("offline fixture")
            result = probe.probe_config(config, models_only=True, transport="direct")
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(result["transport"], "direct")
        self.assertEqual(result["request_stage"], "models")
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
