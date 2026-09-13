from __future__ import annotations

import io
import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cli import main
from universal_skill_finder.models import Coverage, SearchReport
from universal_skill_finder.presentation import install_command, installed_label, render_markdown
from test_presentation import result


def integration_report(*, status="ok", partial=False, empty=False) -> SearchReport:
    coverage = [Coverage("github-search", status, result_count=0 if empty else 1,
                         cache_age_seconds=42 if status == "cached" else None,
                         public_url="https://github.com/search", host="api.github.com",
                         incomplete_results=partial,
                         detail="Some repository files were unavailable." if partial else None)]
    return SearchReport("pdf forms", [] if empty else [result()], coverage, "2026-09-05", "sanitized-config.json")


class SearchIntegrationTests(unittest.TestCase):
    def invoke(self, report: SearchReport, arguments: list[str], annotate=None):
        output = io.StringIO()
        finder = SimpleNamespace(search=Mock(return_value=report))
        with patch("universal_skill_finder.cli._load", return_value=(object(), object())), \
             patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder), \
             patch("universal_skill_finder.cli.annotate_installed", side_effect=annotate) as scanner, \
             redirect_stdout(output):
            status = main(["pdf", "forms", *arguments])
        return status, output.getvalue(), scanner, finder.search

    def test_live_and_cached_partial_markdown_are_red_and_explicit(self):
        for status, label in (("ok", "Partial search"), ("cached", "Partial cached (not contacted; age 42s)")):
            with self.subTest(status=status):
                code, text, scanner, _ = self.invoke(integration_report(status=status, partial=True), ["--markdown"])
                self.assertEqual(code, 0)
                self.assertIn(label, text)
                self.assertIn("Some repository files were unavailable.", text)
                self.assertNotIn("Searched", text)
                self.assertNotIn("Cached", text)
                scanner.assert_not_called()

    def test_empty_partial_markdown_never_claims_complete_no_matches(self):
        for status in ("ok", "cached"):
            with self.subTest(status=status):
                code, text, _, _ = self.invoke(integration_report(status=status, partial=True, empty=True), ["--markdown"])
                self.assertEqual(code, 0)
                self.assertIn("No verified matches were returned; search coverage is incomplete.", text)
                self.assertNotIn("No matches were found", text)
                self.assertNotIn("| # | Skill |", text)
                self.assertIn("[https://github.com/bibryam/universal-skill-finder](https://github.com/bibryam/universal-skill-finder)", text)

    def test_strict_partial_is_code_two_normal_partial_is_code_zero(self):
        for status in ("ok", "cached"):
            for empty in (False, True):
                with self.subTest(status=status, empty=empty):
                    report = integration_report(status=status, partial=True, empty=empty)
                    self.assertEqual(self.invoke(deepcopy(report), ["--json"])[0], 0)
                    self.assertEqual(self.invoke(deepcopy(report), ["--json", "--strict"])[0], 2)
        self.assertEqual(self.invoke(integration_report(empty=True), ["--strict"])[0], 0)

    def test_strict_ignores_unselected_disabled_and_excluded_sources(self):
        report = integration_report()
        report.coverage.extend([Coverage("not_selected", "not_selected"), Coverage("disabled", "disabled", enabled=False),
                                Coverage("excluded", "excluded")])
        self.assertEqual(self.invoke(report, ["--strict", "--source", "github-search"])[0], 0)
        report.coverage.append(Coverage("selected-timeout", "timeout"))
        self.assertEqual(self.invoke(deepcopy(report), ["--strict"])[0], 2)
        self.assertEqual(self.invoke(deepcopy(report), ["--strict", "--source", "github-search"])[0], 0)

    def test_v2_cards_keep_required_fields_and_canonical_footer(self):
        report = integration_report(partial=True)
        report.results = [result(id=f"skill:{index}", name=f"pdf-{index}") for index in range(1, 11)]
        code, text, _, search = self.invoke(report, ["--markdown", "--assistant", "codex"])
        self.assertEqual(code, 0)
        self.assertEqual(len(re.findall(r"^### \d+\. ", text, re.MULTILINE)), 10)
        self.assertEqual(len(re.findall(r"^\*\*Location:\*\* ", text, re.MULTILINE)), 10)
        self.assertEqual(len(re.findall(r"^\*\*Found on:\*\* ", text, re.MULTILINE)), 10)
        self.assertEqual(len(re.findall(r"^\*\*Signals:\*\* ", text, re.MULTILINE)), 10)
        cards = text.split("## Source coverage", 1)[0]
        for field in ("Read and fill PDF forms", "Inspect and install:", "**Inspect #", "**Install #"):
            self.assertEqual(cards.count(field), 10)
        self.assertNotIn("npx skills@", text)
        self.assertIn("## Source coverage", text)
        self.assertIn("[https://github.com/bibryam/universal-skill-finder](https://github.com/bibryam/universal-skill-finder)", text)
        self.assertEqual(search.call_args.kwargs["max_results"], 100)
        self.assertEqual(search.call_args.kwargs["count"], 100)
        self.assertEqual(search.call_args.kwargs["page_size"], 25)
        self.assertFalse(search.call_args.kwargs["verify_results"])
        self.assertEqual(search.call_args.kwargs["source_ids"], [])

    def test_installed_labels_are_fixed_keep_commands_and_do_not_render_paths(self):
        cases = [
            ({"status": "exact_local", "evidence": ["canonical_local_directory"]}, "Installed locally"),
            ({"status": "matching_instructions", "evidence": ["skill_md_sha256"]}, "Matching instructions found"),
            ({"status": "name_collision", "evidence": ["same_name_different_instructions"]}, "Name collision; different instructions"),
            ({"status": "unknown", "evidence": ["name_only"]}, "Name match; repository unverified"),
        ]
        for annotation, label in cases:
            with self.subTest(status=annotation["status"]):
                report = integration_report()
                before_command, _ = install_command(report.results[0], "claude-code")
                report.results[0].installed = {**annotation, "path": "/Users/private/.claude/skills/pdf", "label": "<script>untrusted</script>"}
                report.installed_scan = {"status": "complete", "roots": [{"path": "/Users/private/.claude/skills"}]}
                text = render_markdown(report, assistant="claude-code")
                self.assertEqual(installed_label(report.results[0]), label)
                self.assertIn("**Local:** " + label, text)
                self.assertIn("Inspect and install: type **Inspect #1**  **Install #1**", text)
                self.assertNotIn(before_command, text)
                self.assertNotIn("/Users/private", text)
                self.assertNotIn("<script>", text)
                self.assertIn("not the same repository or complete bundle", text)
                self.assertEqual(install_command(report.results[0], "claude-code")[0], before_command)

    def test_unknown_and_absent_installed_states_do_not_claim_installation(self):
        for annotation in ({}, {"status": "not_found", "evidence": []}, {"status": "unknown", "evidence": ["scan_incomplete"]},
                           {"status": "<script>installed</script>", "evidence": []}):
            with self.subTest(annotation=annotation):
                row = result(installed=annotation)
                self.assertEqual(installed_label(row), "")

    def test_positive_local_match_also_displays_conflicting_version_without_suppressing_command(self):
        for status, primary in (("exact_local", "Installed locally"),
                                ("matching_instructions", "Matching instructions found")):
            with self.subTest(status=status):
                report = integration_report()
                command, _ = install_command(report.results[0], "codex")
                report.results[0].installed = {
                    "status": status, "evidence": ["skill_md_sha256", "same_name_different_instructions"],
                    "scopes": ["project"], "collision_scopes": ["user"],
                }
                label = primary + "; conflicting local version also found"
                self.assertEqual(installed_label(report.results[0]), label)
                markdown = render_markdown(report, assistant="codex")
                self.assertIn("**Local:** " + label, markdown)
                self.assertIn("Inspect and install: type **Inspect #1**  **Install #1**", markdown)
                self.assertNotIn(command, markdown)
                self.assertEqual(install_command(report.results[0], "codex")[0], command)

    def test_partial_local_inventory_disclaimer_does_not_change_search_coverage(self):
        report = integration_report()
        report.installed_scan = {"status": "partial", "roots": [{"scope": "user", "status": "partial"}]}
        text = render_markdown(report, assistant="codex")
        self.assertIn("Searched", text)
        self.assertIn("Local skill inventory is incomplete", text)
        self.assertIn("not proof that a skill is absent", text)
        self.assertFalse(report.coverage[0].incomplete_results)

    def test_scanner_runs_once_for_each_known_assistant_after_search(self):
        for assistant in ("codex", "claude-code"):
            with self.subTest(assistant=assistant):
                report = integration_report()

                def annotate(found, host):
                    self.assertIs(found, report)
                    self.assertEqual(host, assistant)
                    found.results[0].installed = {"status": "matching_instructions", "evidence": ["skill_md_sha256"], "scopes": ["user"]}
                    found.installed_scan = {"status": "complete", "assistant": host, "scopes": ["user"]}

                code, text, scanner, _ = self.invoke(report, ["--json", "--assistant", assistant], annotate)
                self.assertEqual(code, 0)
                scanner.assert_called_once_with(report, assistant)
                document = json.loads(text)
                self.assertEqual(document["results"][0]["installed"]["status"], "matching_instructions")
                self.assertEqual(document["installed_scan"]["scopes"], ["user"])

    def test_scanner_is_not_called_for_preview_optout_or_unknown_host(self):
        cases = [([], "assistant_unknown"), (["--assistant", "codex", "--no-installed-check"], "opted_out"),
                 (["--assistant", "claude-code", "--dry-run"], "preview"),
                 (["--assistant", "codex", "--dry-run", "--no-installed-check"], "preview")]
        for arguments, reason in cases:
            with self.subTest(arguments=arguments):
                code, text, scanner, _ = self.invoke(integration_report(), ["--json", *arguments])
                self.assertEqual(code, 0)
                scanner.assert_not_called()
                self.assertEqual(json.loads(text)["installed_scan"]["reason"], reason)

    def test_unsupported_host_is_rejected_before_finder_or_scanner(self):
        with patch("universal_skill_finder.cli._load") as load, patch("universal_skill_finder.cli.annotate_installed") as scanner, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["pdf", "--assistant", "unsupported-host"])
        self.assertEqual(raised.exception.code, 2)
        load.assert_not_called()
        scanner.assert_not_called()

    def test_plain_output_keeps_partial_and_local_evidence_visible(self):
        report = integration_report(partial=True)
        report.results[0].installed = {"status": "matching_instructions", "evidence": ["skill_md_sha256"], "path": "/Users/private/skill"}
        code, text, _, _ = self.invoke(report, [])
        self.assertEqual(code, 0)
        self.assertIn("Partial search", text)
        self.assertIn("Matching instructions found", text)
        self.assertNotIn("/Users/private", text)


if __name__ == "__main__":
    unittest.main()
