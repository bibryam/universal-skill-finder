from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))
from universal_skill_finder.cli import main
from universal_skill_finder.config import EffectiveConfig


def configuration(*, enabled=True, optional=False):
    return EffectiveConfig(settings={}, packs=[], overlay={}, overlay_path=Path("overlay.json"), sources=[{
        "id": "example", "adapter": "http-json-v1", "kind": "registry", "enabled": enabled,
        "effective_enabled": enabled, "endpoint": "https://catalog.example/private-route?key=private-query",
        "path": "/private/local-skills", "provenance": "private-provenance",
        "headers": {"Authorization": {"env": "EXAMPLE_CATALOG_TOKEN", "prefix": "Bearer ", "optional": optional},
                    "X-Static": "private-header"},
    }])


class DiagnosticTests(unittest.TestCase):
    def doctor(self, config, environment=None):
        output = io.StringIO()
        with patch("universal_skill_finder.cli._load", return_value=(config, SimpleNamespace(root=Path("cache")))), \
             patch.dict(os.environ, environment or {}, clear=True), redirect_stdout(output), \
             patch("socket.socket", side_effect=AssertionError("network forbidden")):
            code = main(["doctor"])
        return code, output.getvalue()

    def test_doctor_fails_for_missing_generic_header_credential(self):
        code, output = self.doctor(configuration())
        self.assertEqual(code, 2)
        self.assertIn("example:EXAMPLE_CATALOG_TOKEN", output)
        self.assertIn("Code revision (skill): sha256:", output)
        self.assertIn("Contracts: schema=1, adapter=1, cache=1", output)

    def test_doctor_ignores_disabled_and_optional_credentials(self):
        for config in (configuration(enabled=False), configuration(optional=True)):
            with self.subTest(config=config):
                self.assertEqual(self.doctor(config)[0], 0)

    def test_doctor_checks_presence_without_disclosing_values(self):
        code, output = self.doctor(configuration(), {"EXAMPLE_CATALOG_TOKEN": "fixture-only-value"})
        self.assertEqual(code, 0)
        self.assertNotIn("fixture-only-value", output)
        self.assertNotIn("private-route", output)

    def test_explain_is_safe_allowlisted_read_only_view(self):
        output = io.StringIO()
        with patch("universal_skill_finder.cli.load_config", return_value=configuration()), \
             patch.dict(os.environ, {"EXAMPLE_CATALOG_TOKEN": "fixture-only-value"}, clear=True), \
             patch("universal_skill_finder.cli.Cache", side_effect=AssertionError("cache forbidden")), \
             patch("socket.socket", side_effect=AssertionError("network forbidden")), redirect_stdout(output):
            self.assertEqual(main(["sources", "explain", "example"]), 0)
        document = json.loads(output.getvalue())
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["source"]["public_url"], "https://catalog.example")
        self.assertEqual(document["source"]["adapter"], "http-json-v1")
        self.assertTrue(document["source"]["credentials"][0]["present"])
        for value in ("fixture-only-value", "private-route", "private-query", "private-provenance", "private-header", "/private", "Bearer"):
            self.assertNotIn(value, output.getvalue())

    def test_invalid_revision_payload_fails_without_traceback(self):
        output = io.StringIO()
        with patch("universal_skill_finder.cli._doctor", side_effect=ValueError("invalid revision payload")), redirect_stderr(output):
            self.assertEqual(main(["doctor"]), 3)
        self.assertIn("invalid data or plugin payload", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
