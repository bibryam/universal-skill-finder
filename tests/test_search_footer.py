from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cli import main
from universal_skill_finder.models import Coverage
from universal_skill_finder.presentation import render_help, render_markdown, render_report
from universal_skill_finder.source_presentation import render_sources_markdown
from test_diagnostics import configuration
from test_presentation import report, result


REPOSITORY = "https://github.com/bibryam/universal-skill-finder"
FOOTER = f"[{REPOSITORY}]({REPOSITORY})"
PLAIN_FOOTER = REPOSITORY


def checked_footer(found):
    found.footer_link_proof = {
        "role": "repository", "url": REPOSITORY, "status": "eligible",
        "method": "fixture", "identity_basis": "fixture_exact_repository",
    }
    return found


class SearchFooterTests(unittest.TestCase):
    def assert_footer(self, text: str) -> None:
        lines = text.rstrip("\n").splitlines()
        self.assertEqual(lines[-1], FOOTER)
        self.assertEqual(text.count(FOOTER), 1)
        self.assertNotIn("+-- * Universal Skill Finder", text)
        self.assertNotIn("```text", text)

    def test_footer_is_last_once_for_each_known_host_and_unknown_host(self):
        for assistant in ("codex", "claude-code", None, "unsupported-host"):
            with self.subTest(assistant=assistant):
                self.assert_footer(render_markdown(report(), assistant=assistant))

    def test_canonical_repository_is_linked_once_in_markdown_and_plain(self):
        found = report()
        markdown = render_report(found, format="markdown")
        plain = render_report(found, format="plain")
        self.assertEqual(markdown.rstrip("\n").splitlines()[-1], FOOTER)
        self.assertEqual(plain.rstrip("\n").splitlines()[-1], PLAIN_FOOTER)
        self.assertEqual(markdown.count(FOOTER), 1)
        self.assertEqual(plain.count(REPOSITORY), 1)

    def test_footer_follows_warnings_installation_notes_metrics_and_next_action(self):
        found = report(results=[result(warnings=["Review the requested access"],
                                      metrics_by_source={"skillsmp": {"stars": 12}})])
        text = render_markdown(found, assistant="codex")
        self.assert_footer(text)
        for preceding in ("#1: Review the requested access", "The Inspect and install prompt appears only",
                          "Popularity counts are source-reported", "Say **Inspect #N**"):
            self.assertLess(text.index(preceding), text.index(FOOTER))

    def test_successful_empty_search_has_one_footer_after_no_matches_message(self):
        text = render_markdown(report(results=[], coverage=[Coverage("empty", "ok", 0)]))
        self.assert_footer(text)
        self.assertLess(text.index("No matches were found"), text.index(FOOTER))

    def test_failed_search_omits_footer_even_with_stored_proof(self):
        text = render_markdown(checked_footer(report(results=[], coverage=[Coverage("failed", "timeout")])))
        self.assertIn("No sources completed", text)
        self.assertNotIn(FOOTER, text)

    def test_successful_search_uses_canonical_repository_without_report_proof(self):
        found = report()
        text = render_markdown(found)
        self.assert_footer(text)

    def test_report_proof_cannot_replace_or_suppress_canonical_repository(self):
        for status in ("not_checked", "unavailable", "inconclusive", "reachable"):
            with self.subTest(status=status):
                found = report()
                found.footer_link_proof = {
                    "role": "repository", "url": "https://evil.example/repository", "status": status,
                }
                markdown = render_report(found, format="markdown")
                plain = render_report(found, format="plain")
                self.assertEqual(markdown.rstrip("\n").splitlines()[-1], FOOTER)
                self.assertEqual(plain.rstrip("\n").splitlines()[-1], PLAIN_FOOTER)
                self.assertNotIn("evil.example", markdown)
                self.assertNotIn("evil.example", plain)

    def test_continuation_mapping_keeps_canonical_repository_link(self):
        found = {
            "report_format_version": 2,
            "mode": "online",
            "continuation_page": True,
            "coverage_context": "saved",
            "query": "pdf",
            "results": [],
            "coverage": [{"source_id": "saved", "status": "ok", "candidates_returned": 1, "shown": 0, "enabled": True}],
            "footer_link_proof": {"role": "repository", "status": "not_checked", "url": REPOSITORY},
        }
        for format in ("markdown", "plain"):
            with self.subTest(format=format):
                text = render_report(found, format=format)
                expected = FOOTER if format == "markdown" else PLAIN_FOOTER
                self.assertEqual(text.rstrip("\n").splitlines()[-1], expected)

    def test_no_configured_sources_omits_footer(self):
        text = render_markdown(report(results=[], coverage=[]))
        self.assertNotIn(FOOTER, text)

    def test_dry_run_omits_footer_and_install_command(self):
        text = render_markdown(report(results=[], coverage=[Coverage("planned", "planned")]),
                               assistant="claude-code", dry_run=True)
        self.assertIn("No sources were searched", text)
        self.assertNotIn(FOOTER, text)
        self.assertNotIn(REPOSITORY, text)
        self.assertNotIn("```sh\nnpx", text)

    def test_offline_preview_omits_repository_even_when_a_footer_proof_exists(self):
        found = checked_footer(report())
        found.mode = "offline_preview"
        found.results = []
        found.candidate_previews = []
        text = render_report(found)
        self.assertNotIn(REPOSITORY, text)

    def test_failed_and_help_output_omit_repository_footer(self):
        failed = render_markdown(report(results=[], coverage=[Coverage("failed", "timeout")]))
        self.assertNotIn(REPOSITORY, failed)
        self.assertNotIn(REPOSITORY, render_help())

    def test_cli_json_stays_valid_without_promotional_text_for_every_search_state(self):
        cases = [(report(), [], 0),
                 (report(results=[], coverage=[Coverage("empty", "ok", 0)]), [], 0),
                 (report(results=[], coverage=[Coverage("failed", "timeout")]), [], 2),
                 (report(results=[], coverage=[Coverage("planned", "planned")]), ["--dry-run"], 0)]
        for found, arguments, expected_status in cases:
            with self.subTest(arguments=arguments, status=expected_status):
                finder = SimpleNamespace(search=Mock(return_value=found))
                output = io.StringIO()
                with patch("universal_skill_finder.cli._load", return_value=(None, None)), \
                     patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder), \
                     patch("universal_skill_finder.cli.annotate_installed", side_effect=AssertionError("host scan forbidden")), \
                     patch("socket.socket", side_effect=AssertionError("network forbidden")), redirect_stdout(output):
                    status = main(["search", "pdf", "forms", "--json", "--assistant", "codex", "--no-installed-check", *arguments])
                self.assertEqual(status, expected_status)
                self.assertEqual(json.loads(output.getvalue()), found.to_dict())
                self.assertNotIn("Found this useful", output.getvalue())
                self.assertNotIn("Star this repo", output.getvalue())
                self.assertNotIn(REPOSITORY, output.getvalue())

    def test_source_management_and_diagnostics_have_no_search_footer(self):
        config = configuration(enabled=False)
        text = render_sources_markdown(config, environ={})
        self.assertNotIn("Found this useful", text)
        self.assertNotIn(REPOSITORY, text)
        output = io.StringIO()
        with patch("universal_skill_finder.cli._load", return_value=(config, SimpleNamespace(root=Path("cache")))), \
             patch("socket.socket", side_effect=AssertionError("network forbidden")), redirect_stdout(output):
            self.assertEqual(main(["doctor"]), 0)
        self.assertNotIn("Found this useful", output.getvalue())
        self.assertNotIn("Star this repo", output.getvalue())
        self.assertNotIn(REPOSITORY, output.getvalue())


if __name__ == "__main__":
    unittest.main()
