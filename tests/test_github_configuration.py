from __future__ import annotations

import json
import os
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters import ADAPTER_SPECS
from universal_skill_finder.cache import Cache
from universal_skill_finder.config import EffectiveConfig, load_config, set_source_enabled, validate_effective
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.source_presentation import public_source_url, source_rows


class EmptyAdapter:
    def __init__(self):
        self.calls = []

    def search(self, source, query, limit, context):
        self.calls.append(source["id"])
        return []


class GitHubConfigurationTests(unittest.TestCase):
    def test_bundled_source_is_optional_and_dedicated(self):
        with TemporaryDirectory() as directory:
            config = load_config(str(Path(directory) / "overlay.json"))
            source = config.source("github-code-search")
            self.assertFalse(source["effective_enabled"])
            self.assertEqual(source["auth_env"], "UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN")
            self.assertEqual(source["base_url"], "https://api.github.com")
            self.assertIs(source["public_only"], True)
            self.assertEqual(sum(s["effective_enabled"] for s in config.sources), 9)
            self.assertEqual(len(config.sources), 13)
            self.assertEqual(ADAPTER_SPECS[source["adapter"]].cache_policy, "query")

    def test_enable_joins_default_search_and_missing_token_is_isolated(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=False):
            # Remove only the credential under test. A spawned Windows Python
            # requires OS variables such as SystemRoot; clearing the whole
            # environment tests a broken process fixture, not missing auth.
            os.environ.pop("UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN", None)
            overlay = Path(directory) / "overlay.json"
            config = load_config(str(overlay))
            set_source_enabled(config, "github-code-search", True)
            config = load_config(str(overlay))
            finder = UniversalSkillFinder(config, cache=Cache(Path(directory) / "cache"))
            stub = EmptyAdapter()
            finder.adapter_map = {name: adapter if name == "github-code-search" else stub
                                  for name, adapter in finder.adapter_map.items()}
            with patch.object(finder.http, "request", side_effect=AssertionError("must not contact network")) as request:
                report = finder.search("pdf")
            request.assert_not_called()
            coverage = {row.source_id: row for row in report.coverage}
            self.assertEqual(coverage["github-code-search"].status, "auth_missing")
            self.assertEqual(len(stub.calls), 9)
            self.assertTrue(all(coverage[source].status == "ok" for source in stub.calls))
            self.assertEqual(len(report.coverage), 13)
            set_source_enabled(config, "github-code-search", False)
            self.assertFalse(load_config(str(overlay)).source("github-code-search")["effective_enabled"])

    def test_fixed_origin_auth_and_public_configuration_cannot_be_weakened(self):
        with TemporaryDirectory() as directory:
            source = load_config(str(Path(directory) / "overlay.json")).source("github-code-search")
            source = {key: value for key, value in source.items() if key != "pack"}
            changes = (
                {"base_url": "https://attacker.example"}, {"base_url": "https://api.github.com/other"},
                {"base_url": "https://api.github.com?token=hidden"}, {"endpoint": "https://api.github.com/search/code"},
                {"headers": {}}, {"auth_env": None}, {"auth_env": "invalid-name"},
                {"public_only": False}, {"public_only": 1}, {"auth_optional": True},
                {"allow_insecure_local": True},
            )
            for change in changes:
                candidate = {**deepcopy(source), **change}
                config = EffectiveConfig({}, [], [candidate], Path(directory) / "unused.json", {})
                with self.subTest(change=change):
                    self.assertTrue(validate_effective(config))

    def test_source_listing_is_public_and_never_serializes_token_values(self):
        with TemporaryDirectory() as directory:
            config = load_config(str(Path(directory) / "overlay.json"))
            source = config.source("github-code-search")
            self.assertEqual(public_source_url(source), "https://github.com/search?type=code")
            rows = source_rows(config, environ={"UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN": "test-secret-not-a-real-token"})
            row = next(row for row in rows if row["id"] == "github-code-search")
            self.assertEqual(row["credentials"], [{"environment_variable": "UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN", "required": True, "present": True}])
            self.assertNotIn("test-secret-not-a-real-token", json.dumps(rows))

    def test_unrelated_token_name_is_not_automatically_adopted(self):
        with TemporaryDirectory() as directory, patch.dict(
            os.environ, {"TEAM_GITHUB_TOKEN": "synthetic-fixture"}, clear=False,
        ):
            os.environ.pop("UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN", None)
            config = load_config(str(Path(directory) / "overlay.json"))
            set_source_enabled(config, "github-code-search", True)
            config = load_config(str(config.overlay_path))
            finder = UniversalSkillFinder(config, cache=Cache(Path(directory) / "cache"))
            with patch.object(finder.http, "request", side_effect=AssertionError("must not contact network")) as request:
                report = finder.search("pdf", source_ids=["github-code-search"])
            request.assert_not_called()
            coverage = next(row for row in report.coverage if row.source_id == "github-code-search")
            self.assertEqual(coverage.status, "auth_missing")

    def test_custom_source_can_explicitly_reference_an_environment_variable(self):
        with TemporaryDirectory() as directory:
            config = load_config(str(Path(directory) / "overlay.json"))
            source = config.source("github-code-search")
            source["auth_env"] = "TEAM_GITHUB_TOKEN"
            self.assertEqual(validate_effective(config), [])
            row = next(row for row in source_rows(config, environ={"TEAM_GITHUB_TOKEN": "synthetic-fixture"})
                       if row["id"] == "github-code-search")
            self.assertEqual(row["credentials"], [{"environment_variable": "TEAM_GITHUB_TOKEN", "required": True, "present": True}])


if __name__ == "__main__":
    unittest.main()
