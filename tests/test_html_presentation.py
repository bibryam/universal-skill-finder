from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from universal_skill_finder.presentation import render_report
from test_presentation_v2 import proof, report


REPOSITORY = "https://github.com/bibryam/universal-skill-finder"


class HtmlPresentationTests(unittest.TestCase):
    def test_compact_card_retains_full_description_exact_target_provenance_metrics_and_command(self):
        found = report()
        row = found.results[0]
        row.number = 3
        row.name = "humanizer"
        row.repository = "davila7/claude-code-templates"
        row.skill_path = "cli-tool/components/skills/productivity/humanizer"
        row.ref = "main"
        row.description = "Retained description. " * 55 + "Full retained description marker."
        row.source_ids = ["skillsmp"]
        row.found_on = [SimpleNamespace(source_id="skillsmp", label="SkillsMP", role="listing", status="not_checked",
                                        url="https://example.test/unverified-listing", reason="listing not verified")]
        row.metrics_by_source = {"skillsmp": {"stars": 30563}}
        row.metrics_text = None
        row.occurrences = []
        row.installed = {"status": "matching_instructions", "evidence": ["skill_md_sha256"]}
        row.target_proof = SimpleNamespace(
            kind="github", status="eligible", actual_name="humanizer", content_sha256="a" * 64,
            url="https://raw.githubusercontent.com/davila7/claude-code-templates/main/cli-tool/components/skills/productivity/humanizer/SKILL.md",
            method="anonymous_exact_skill_md_get", identity_basis="github-exact-skill-md-v1",
            reported={"repository": row.repository, "ref": "main", "skill_path": row.skill_path},
            resolved={"repository": row.repository, "ref": "main", "skill_path": row.skill_path},
        )
        text = render_report(found, assistant="claude-code", format="html")
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertIn("<style>", text)
        self.assertIn("Content-Security-Policy", text)
        self.assertIn("default-src 'none'", text)
        self.assertIn("name=\"viewport\"", text)
        self.assertNotIn("<script", text.lower())
        self.assertIn("<h2>3. ", text)
        self.assertIn("davila7/claude-code-templates", text)
        self.assertIn("skillsmp: stars: 30,563", text)
        self.assertIn("SkillsMP (listing not verified)", text)
        self.assertNotIn("https://example.test/unverified-listing", text)
        self.assertIn("<summary>Description</summary>", text)
        self.assertIn("Full retained description marker.", text)
        self.assertIn("<summary>Exact location and installation command</summary>", text)
        self.assertIn("cli-tool/components/skills/productivity/humanizer @ main", text)
        self.assertIn("<strong>Reported location:</strong>", text)
        self.assertIn("npx skills@1.5.23 add https://github.com/davila7/claude-code-templates/tree/main/cli-tool/components/skills/productivity/humanizer --skill humanizer --agent claude-code --copy", text)
        self.assertIn("Local check:", text)
        self.assertNotIn("<details open", text)
        closed_header = text.split('<article class="card">', 1)[1].split("<details>", 1)[0]
        self.assertIn("skillsmp: stars: 30,563", closed_header)
        self.assertNotIn(row.skill_path, closed_header)
        self.assertIn("Requested up to 3 results · Page size 2", text)
        self.assertIn("with verified destinations (not necessarily install-ready)", text)

    def test_html_escapes_remote_content_and_does_not_turn_unchecked_targets_into_links_or_commands(self):
        found = report()
        row = found.results[0]
        row.name = "<img src=x onerror=alert(1)>"
        row.description = "</details><script>alert(1)</script>"
        row.link_proofs = [proof("listing", "not_checked", "https://evil.example/listing")]
        row.target_proof = {"kind": "github_archive", "status": "eligible", "resolved": {"repository": "evil/repo"}}
        row.install = {}
        text = render_report(found, assistant="codex", format="html")
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", text)
        self.assertIn("&lt;/details&gt;&lt;script&gt;alert(1)&lt;/script&gt;", text)
        self.assertNotIn("https://evil.example/listing", text)
        self.assertNotIn("npx skills@", text)
        self.assertNotIn("<script>alert", text)

    def test_dry_run_and_offline_preview_have_no_cards_commands_or_footer(self):
        found = report(mode="offline_preview")
        found.results = []
        found.candidate_previews = [SimpleNamespace(name="preview", description="Cached candidate", sources=["skillsmp"])]
        offline = render_report(found, format="html")
        self.assertIn("Offline preview", offline)
        self.assertNotIn("<article", offline)
        self.assertNotIn("<details", offline)
        self.assertNotIn("npx skills@", offline)
        self.assertNotIn("<footer", offline)
        self.assertNotIn("⭐ Star Universal Skill Finder on GitHub", offline)
        found.coverage[0].link_proof = proof("source_page", "eligible", "https://example.test/coverage")
        self.assertNotIn("https://example.test/coverage", render_report(found, format="html"))
        dry = render_report(report(), format="html", dry_run=True)
        self.assertIn("Destination preview", dry)
        self.assertNotIn("<article", dry)
        self.assertNotIn("npx skills@", dry)
        self.assertNotIn("<footer", dry)
        self.assertNotIn("⭐ Star Universal Skill Finder on GitHub", dry)

    def test_html_footer_is_a_star_link_only_with_eligible_proof(self):
        found = report()
        found.footer_link_proof = proof("repository", "eligible", REPOSITORY)
        eligible = render_report(found, format="html")
        self.assertEqual(eligible.count("⭐ Star Universal Skill Finder on GitHub"), 1)
        self.assertIn(f'href="{REPOSITORY}"', eligible)
        self.assertNotIn("+-- * Universal Skill Finder", eligible)

        found.footer_link_proof = proof("repository", "inconclusive", REPOSITORY)
        unproved = render_report(found, format="html")
        self.assertEqual(unproved.count("⭐ Star Universal Skill Finder on GitHub"), 1)
        self.assertIn("repository link not verified for this report", unproved)
        self.assertNotIn(REPOSITORY, unproved)

    def test_failed_html_search_omits_star_footer_even_with_stored_proof(self):
        found = report()
        found.results = []
        found.coverage = [SimpleNamespace(source_id="failed", status="timeout", enabled=True)]
        found.footer_link_proof = proof("repository", "eligible", REPOSITORY)
        text = render_report(found, format="html")
        self.assertNotIn("<footer", text)
        self.assertNotIn("⭐ Star Universal Skill Finder on GitHub", text)

    def test_empty_html_keeps_pending_exhausted_and_coverage_explanations(self):
        found = report()
        found.results = []
        found.continuation_page = True
        found.has_pending = True
        found.validation_stopped_reason = "validation budget reached"
        pending = render_report(found, format="html")
        self.assertIn("validation budget reached", pending)
        found.has_pending = False
        found.pool_exhausted = True
        exhausted = render_report(found, format="html")
        self.assertIn("saved snapshot is exhausted", exhausted)
        found.continuation_page = False
        found.pool_exhausted = False
        found.coverage[0].incomplete_results = True
        partial = render_report(found, format="html")
        self.assertIn("search coverage is incomplete", partial)

    def test_legacy_schema_fails_closed_without_remote_fields_or_actions(self):
        legacy = report()
        legacy.report_format_version = 1
        legacy.results[0].name = "legacy <unsafe>"
        text = render_report(legacy, assistant="codex", format="html")
        self.assertIn("Legacy report cannot be rendered as verified HTML.", text)
        self.assertNotIn("legacy", text.lower().replace("legacy report", ""))
        self.assertNotIn("<article", text)
        self.assertNotIn("npx skills@", text)
        self.assertNotIn("href=", text)


if __name__ == "__main__":
    unittest.main()
