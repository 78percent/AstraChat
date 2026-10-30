import unittest

from scripts.check_release import inspect


class ReleaseBoundaryTests(unittest.TestCase):
    def test_private_data_is_rejected_even_without_a_secret_pattern(self):
        for path in ("runtime/chat.json", "config/model-access.local.json", "personas/review/private.txt", "console-ui/node_modules/a.js"):
            self.assertIn("private_or_generated_path", inspect(path, b"{}"))

    def test_credentials_are_flagged_without_returning_their_value(self):
        key = b"sk-" + b"A" * 40
        self.assertEqual(inspect("config/example.json", key), ["credential_pattern"])

    def test_source_and_empty_configuration_examples_are_allowed(self):
        self.assertEqual(inspect("config/model-access.example.json", b'{"api_key":""}'), [])
        self.assertEqual(inspect("scripts/main.py", b"print('hello')"), [])
