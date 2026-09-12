from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.config import EffectiveConfig, load_config, set_source_enabled, validate_effective
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate
from universal_skill_finder.presentation import install_command, metrics_text, render_markdown
from universal_skill_finder.source_presentation import render_sources_markdown, source_rows
from universal_skill_finder.text import safe_install_reference, safe_skill_path
from test_presentation import result, report
from test_universal_skill_finder import FakeHttp, fixture_finder


class FixtureAdapter:
    def __init__(self, *, partial=False, fail=False):
        self.calls = []
        self.partial = partial
        self.fail = fail

    def search(self, source, query, limit, context):
        self.calls.append(source["id"])
        if self.fail and source["id"] == "tessl":
            raise TimeoutError("fixture timeout")
        if source["id"] == "tessl" and self.partial:
            context.incomplete_results = True
            context.detail = "Skipped invalid Tessl result"
        url = "https://github.com/example/skills/tree/main/skills/pdf"
        return [Candidate(
            native_id="pdf", name="pdf", description="Read and fill PDF forms",
            source_id=source["id"], source_kind=source["kind"], adapter=source["adapter"], native_rank=1,
            canonical_url=url, repository="example/skills",
            skill_path="skills/pdf", ref="main", trust="community-index",
            target_proof={"kind": "fixture", "status": "eligible", "skill_path": "skills/pdf",
                          "content_sha256": "0" * 64},
            link_proofs=[{"role": "skill_destination", "url": url, "status": "eligible",
                          "method": "fixture", "identity_basis": "fixture_exact_skill"}],
        )]


class TesslIntegrationTests(unittest.TestCase):
    def test_default_list_and_preview_include_tessl_without_credentials(self):
        with TemporaryDirectory() as directory:
            config = load_config(str(Path(directory) / "sources.json"))
            rows = source_rows(config, environ={"TESSL_TOKEN": "synthetic-value"})
            row = next(row for row in rows if row["id"] == "tessl")
            self.assertTrue(row["enabled"])
            self.assertEqual(row["public_url"], "https://tessl.io/registry")
            self.assertEqual(row["credentials"], [])
            self.assertNotIn("synthetic-value", json.dumps(rows))
            self.assertEqual(len(rows), 13)
            self.assertEqual(sum(row["enabled"] for row in rows), 9)
            markdown = render_sources_markdown(config, environ={})
            self.assertIn("[tessl](https://tessl.io/registry)", markdown)
            self.assertIn("Disable tessl", markdown)
            finder = fixture_finder(config, Cache(Path(directory) / "cache"), http=FakeHttp())
            with patch.object(finder.http, "request", side_effect=AssertionError("no network in preview"), create=True):
                preview = finder.search("pdf forms", dry_run=True)
            coverage = next(row for row in preview.coverage if row.source_id == "tessl")
            self.assertEqual(coverage.status, "planned")
            self.assertEqual(coverage.host, "api.tessl.io")

    def test_normal_search_includes_tessl_and_disable_persists(self):
        with TemporaryDirectory() as directory:
            overlay = Path(directory) / "sources.json"
            config = load_config(str(overlay))
            finder = fixture_finder(config, Cache(Path(directory) / "cache"), http=FakeHttp())
            adapter = FixtureAdapter()
            finder.adapter_map = {name: adapter for name in finder.adapter_map}
            report = finder.search("pdf forms")
            self.assertIn("tessl", adapter.calls)
            self.assertEqual(len(adapter.calls), 9)
            self.assertIn("tessl", report.results[0].source_ids)
            set_source_enabled(config, "tessl", False)
            disabled = load_config(str(overlay))
            self.assertFalse(disabled.source("tessl")["effective_enabled"])
            finder = fixture_finder(disabled, Cache(Path(directory) / "other-cache"), http=FakeHttp())
            adapter = FixtureAdapter()
            finder.adapter_map = {name: adapter for name in finder.adapter_map}
            report = finder.search("pdf forms", source_ids=["tessl"])
            self.assertEqual(adapter.calls, [])
            self.assertEqual(next(row for row in report.coverage if row.source_id == "tessl").status, "disabled")

    def test_partial_cache_and_link_fallback_keep_output_contract(self):
        with TemporaryDirectory() as directory:
            config = load_config(str(Path(directory) / "sources.json"))
            finder = fixture_finder(config, Cache(Path(directory) / "cache"), http=FakeHttp())
            adapter = FixtureAdapter(partial=True)
            finder.adapter_map["tessl"] = adapter
            fresh = finder.search("pdf forms", source_ids=["tessl"])
            cached = finder.search("pdf forms", source_ids=["tessl"], offline=True)
            self.assertEqual(adapter.calls, ["tessl"])
            self.assertEqual(fresh.results[0].repository, "example/skills")
            self.assertEqual(cached.results, [])
            self.assertEqual(len(cached.candidate_previews), 1)
            coverage = next(row for row in cached.coverage if row.source_id == "tessl")
            self.assertEqual(coverage.status, "cached")
            self.assertTrue(coverage.incomplete_results)
            output = render_markdown(cached, assistant="codex")
            self.assertIn("Partial cached (not contacted", output)
            self.assertNotIn("https://github.com/example/skills", output)
            self.assertNotIn("npx ", output)
            self.assertNotIn("| # | Skill |", output)
            self.assertNotIn("⭐ Star Universal Skill Finder on GitHub", output)

    def test_failure_does_not_discard_other_sources(self):
        with TemporaryDirectory() as directory:
            config = load_config(str(Path(directory) / "sources.json"))
            finder = fixture_finder(config, Cache(Path(directory) / "cache"), http=FakeHttp())
            adapter = FixtureAdapter(fail=True)
            finder.adapter_map = {name: adapter for name in finder.adapter_map}
            report = finder.search("pdf forms", source_ids=["tessl", "skills-sh"])
            self.assertEqual(len(report.results), 1)
            self.assertEqual(report.results[0].source_ids, ["skills-sh"])
            self.assertNotEqual(next(row for row in report.coverage if row.source_id == "tessl").status, "ok")

    def test_tessl_source_configuration_cannot_change_host_or_send_credentials(self):
        with TemporaryDirectory() as directory:
            original = load_config(str(Path(directory) / "sources.json")).source("tessl")
            original = {key: value for key, value in original.items() if key != "pack"}
            cases = [
                {"base_url": "https://attacker.example"}, {"base_url": "https://api.tessl.io/private"},
                {"base_url": "https://api.tessl.io?token=hidden"}, {"endpoint": "https://api.tessl.io/experimental/search"},
                {"auth_env": "TESSL_TOKEN"}, {"headers": {}}, {"auth_optional": True},
                {"allow_insecure_local": True},
            ]
            self.assertEqual(validate_effective(EffectiveConfig({}, [], [original], Path(directory) / "unused.json", {})), [])
            for change in cases:
                with self.subTest(change=change):
                    config = EffectiveConfig({}, [], [{**original, **change}], Path(directory) / "unused.json", {})
                    self.assertTrue(validate_effective(config))


class TesslAssessmentTests(unittest.TestCase):
    def assessment(self, **metrics):
        return result(source_ids=["tessl"], metrics_by_source={"tessl": {"tessl_metric_scope": "skill", **metrics}},
                      occurrences=[{"adapter": "tessl", "source_id": "tessl"}])

    def test_assessments_are_source_labelled_with_no_safety_endorsement(self):
        row = self.assessment(tessl_quality=0.7875000000000001, tessl_security_level="HIGH",
                              tessl_scored_at="2026-09-05T00:00:00Z")
        text = render_markdown(report(results=[row]), assistant="codex")
        self.assertIn("tessl: Tessl quality (raw): 0.788", text)
        self.assertIn("Tessl security level: HIGH", text)
        self.assertIn("scored 2026-09-05T00:00:00Z", text)
        self.assertIn("not this finder's safety verdict", text)
        self.assertIn("Scores do not change federation ranking", text)
        self.assertIn("### 1. ", text)
        self.assertNotIn("https://github.com/bibryam/universal-skill-finder", text)

    def test_zero_and_none_level_are_distinct_from_missing_assessments(self):
        self.assertIn("quality (raw): 0", metrics_text(self.assessment(tessl_quality=0)))
        self.assertIn("security level: NONE", metrics_text(self.assessment(tessl_security_level="NONE")))
        self.assertEqual(metrics_text(self.assessment(tessl_quality=None, tessl_security_level=None)), "Not available")

    def test_malformed_assessments_and_deprecated_grade_are_not_rendered(self):
        for value in (None, True, False, -0.1, 1.1, float("nan"), float("inf"), "0.8", {}, []):
            with self.subTest(value=value):
                self.assertEqual(metrics_text(self.assessment(tessl_quality=value)), "Not available")
        for value in (None, True, 0, {}, [], "SAFE", "HIGH|[click](https://evil.example)"):
            with self.subTest(value=value):
                self.assertEqual(metrics_text(self.assessment(tessl_security_level=value)), "Not available")
        self.assertEqual(metrics_text(self.assessment(tessl_security="LOW")), "Not available")

    def test_other_adapters_cannot_label_their_metrics_as_tessl_assessments(self):
        row = self.assessment(tessl_quality=0.8, tessl_security_level="NONE")
        row.occurrences = [{"adapter": "skills-sh", "source_id": "tessl"}]
        self.assertEqual(metrics_text(row), "Not available")
        row.occurrences = [{"adapter": "tessl", "source_id": "other"}]
        self.assertEqual(metrics_text(row), "Not available")

    def test_bundle_assessments_cannot_be_presented_as_individual_skill_scores(self):
        for scope in (None, "bundle", "unknown"):
            row = self.assessment(tessl_metric_scope=scope, tessl_quality=0.9, tessl_security_level="NONE")
            self.assertEqual(metrics_text(row), "Not available")


class HiddenSkillPathTests(unittest.TestCase):
    def test_leading_hidden_skill_roots_are_safe_without_widening_reference_syntax(self):
        for path in (".agents/skills/pdf", ".claude/skills/pdf", ".codex/skills/pdf", "skills/.curated/pdf"):
            with self.subTest(path=path):
                self.assertEqual(safe_skill_path(path), path)
                self.assertIn("/tree/main/" + path, install_command(result(skill_path=path), "codex")[0])
        self.assertIsNone(safe_install_reference(".agents/skills/pdf"))

    def test_hidden_path_support_keeps_traversal_flags_and_shell_tokens_rejected(self):
        for path in ("./skills/pdf", "../pdf", ".agents/../private", ".agents/./pdf", ".agents//pdf",
                     ".agents/--all/pdf", ".agents/skills/pdf/", "/.agents/pdf", ".agents/skills;id",
                     ".agents/skills$(id)", ".agents/skills%2f..", ".agents/skills\\pdf", "@scope/pdf"):
            with self.subTest(path=path):
                self.assertIsNone(safe_skill_path(path))


if __name__ == "__main__":
    unittest.main()
