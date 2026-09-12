from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.registries import ClawHubAdapter
from universal_skill_finder.models import Coverage
from universal_skill_finder.presentation import installation_fallback, metrics_text, render_markdown
from test_universal_skill_finder import FakeHttp, adapter_context, registry_source
from test_presentation import report, result
from test_search_defaults import default_search_fixture


class DiscoveryDetailsTests(unittest.TestCase):
    def test_uninstallable_result_links_repository_not_just_registry_listing(self):
        row = result(skill_path=None, ref=None, canonical_url="https://registry.example/pdf")
        text = installation_fallback(row, "skill directory unresolved")
        self.assertIn("[Open repository](https://github.com/anthropics/skills)", text)
        self.assertNotIn("registry.example", text)
        self.assertIn("skill directory unresolved", text)

    def test_unsupported_installer_ref_keeps_exact_repository_location_link(self):
        row = result(ref="feature/pdf")
        text = render_markdown(report(results=[row]), assistant="codex")
        self.assertIn("[Open checked destination](https://github.com/anthropics/skills/tree/feature%2Fpdf/skills/pdf)", text)
        self.assertNotIn("```sh\nnpx", text)

    def test_registry_only_result_has_source_link_in_action_column(self):
        row = result(repository=None, skill_path=None, ref=None, install={}, canonical_url="https://registry.example/pdf")
        text = render_markdown(report(results=[row]), assistant="claude-code")
        self.assertIn("[Checked skill destination](https://registry.example/pdf)", text)
        self.assertIn("[Open checked destination](https://registry.example/pdf)", text)
        self.assertNotIn("```sh\nnpx", text)

    def test_no_known_safe_link_is_reported_honestly(self):
        row = result(repository=None, skill_path=None, ref=None, install={}, canonical_url="javascript:alert(1)")
        self.assertIn("Listing link unavailable", installation_fallback(row, "unresolved"))
        self.assertNotIn("javascript:", installation_fallback(row, "unresolved"))

    def test_metrics_preserve_provenance_conflicts_zero_and_repository_scope(self):
        row = result(metrics_by_source={
            "anthropic-skills": {"github_stars": 1234, "github_stars_scope": "repository",
                                 "github_stars_repository": "anthropics/skills", "github_stars_observed_at": "2026-09-05T12:00:00+00:00"},
            "skillsmp": {"stars": 0, "installs": "42", "downloads": 20.0},
        })
        text = metrics_text(row)
        self.assertIn("anthropic-skills: GitHub repo stars: 1,234", text)
        self.assertIn("observed 2026-09-05T12:00:00+00:00", text)
        self.assertIn("skillsmp: stars: 0, installs: 42, downloads: 20", text)
        self.assertNotIn("1,296", text)
        rendered = render_markdown(report(results=[row]), assistant="codex")
        self.assertIn("entire repository, not the individual skill", rendered)
        self.assertIn("not quality or safety scores", rendered)

    def test_unknown_and_invalid_counts_are_not_zero_or_safety_badges(self):
        for value in (None, True, False, -1, 0.5, float("nan"), float("inf"), "1.2k", "1,234", "-1", [], {}, 2**255):
            with self.subTest(value=value):
                row = result(metrics_by_source={"skillsmp": {"stars": value, "ai_score": 100, "security_grade": "safe"}})
                self.assertEqual(metrics_text(row), "Not available")

    def test_metrics_from_unknown_sources_or_other_repositories_are_not_attributed(self):
        row = result(metrics_by_source={"forged": {"github_stars": 100},
                     "anthropic-skills": {"github_stars": 123, "github_stars_repository": "other/skills"}})
        self.assertEqual(metrics_text(row), "Not available")

    def test_rejected_star_timestamp_is_not_attributed_to_other_metrics(self):
        row = result(metrics_by_source={"anthropic-skills": {
            "github_stars": 123, "github_stars_repository": "other/skills",
            "github_stars_observed_at": "2026-09-05T12:00:00+00:00", "downloads": 7,
        }})
        self.assertEqual(metrics_text(row), "anthropic-skills: downloads: 7")

    def test_metric_source_and_timestamp_cannot_inject_markdown_columns(self):
        source = "[evil](https://evil.test)|<script>"
        row = result(source_ids=[source], metrics_by_source={source: {"github_stars": 2,
                     "github_stars_observed_at": "bad|<script>\nnext"}})
        text = metrics_text(row)
        self.assertNotIn("|", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("\n", text)
        self.assertNotIn("[evil](https://evil.test)", text)

    def test_unchecked_coverage_urls_stay_data_and_never_become_links(self):
        with TemporaryDirectory() as temp:
            finder, _ = default_search_fixture(Path(temp), count=1)
            found = finder.search("pdf forms")
        coverage = {item.source_id: item for item in found.coverage}
        self.assertEqual(coverage["registry-one"].public_url, "https://catalog.example")
        self.assertEqual(coverage["publisher-repository"].public_url, "https://github.com/publisher/skills")
        self.assertEqual(coverage["publisher-repository"].metadata_hosts, [])
        self.assertEqual(coverage["disabled-registry"].public_url, "https://example.test")
        self.assertEqual(coverage["limited-registry"].status, "rate_limited")
        text = render_markdown(found)
        self.assertNotIn("[limited-registry](https://example.test)", text)
        self.assertNotIn("[disabled-registry](https://example.test)", text)
        self.assertNotIn("https://example.test", text)

    def test_destination_preview_discloses_secondary_metadata_host(self):
        found = report(coverage=[Coverage("repo", "planned", public_url="https://github.com/acme/skills",
                                        metadata_hosts=["api.github.com"])])
        text = render_markdown(found, dry_run=True)
        self.assertIn("api.github.com", text)
        self.assertIn("No search text", text)
        self.assertNotIn("Searched", text)

    def test_clawhub_top_level_zero_downloads_does_not_fall_back(self):
        payload = {"results": [{"slug": "pdf", "downloads": 0,
                    "native": {"skill": {"stats": {"downloads": 9}}}}]}
        with TemporaryDirectory() as temp:
            found = ClawHubAdapter().search(registry_source("clawhub", "clawhub"), "pdf", 10,
                                           adapter_context(Path(temp), FakeHttp(payload=payload)))
        self.assertEqual(found[0].metrics["downloads"], 0)


if __name__ == "__main__":
    unittest.main()
