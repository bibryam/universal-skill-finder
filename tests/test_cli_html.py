from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cli import _parser, main
from universal_skill_finder.models import Coverage, Result, SearchReport
from universal_skill_finder.presentation import render_report
from universal_skill_finder.snapshot import create_snapshot, save_exclusive_path


def _frozen_record(row):
    """Supply a JSON-shaped record without granting portable proof authority."""
    record = row.to_dict()
    for proof in record.get("link_proofs", []):
        proof.update(
            checked_at=datetime.now(timezone.utc).isoformat(),
            http_status=200,
            identity_basis="github-owner-repository-path-v1",
        )
    return record


def _result(*, name: str, description: str) -> Result:
    repository, ref, skill_path = "anthropics/skills", "main", "skills/pdf"
    destination = f"https://github.com/{repository}/tree/{quote(ref, safe='')}/{skill_path}"
    return Result(
        id="skill:html", name=name, description=description, canonical_url=None,
        repository=repository, skill_path=skill_path, ref=ref, publisher="anthropics",
        content_sha256=None, source_ids=["fixture"], trust=["publisher-owned"],
        text_match_percent=100, rank_fusion_score=0.03, metrics_by_source={},
        install={"kind": "github", "repository": repository, "ref": ref,
                 "skill_path": skill_path, "requires_approval": True},
        warnings=[], occurrences=[], validation_status="eligible", result_number=1,
        link_proofs=[{"role": "skill_destination", "url": destination, "status": "eligible"}],
        target_proof={},
    )


def _report(*, query: str = "humanize", name: str = "Humanizer Ω") -> SearchReport:
    row = _result(name=name, description="Rewrite café prose safely")
    report = SearchReport(
        query=query,
        results=[row],
        coverage=[Coverage("fixture", "ok", result_count=1, shown=1)],
        generated_at="2026-09-10T00:00:00+00:00",
        configuration_path="fixture.json",
        accepted_occurrences=1,
        unique_count=1,
        eligible_count=1,
        requested_count=1,
        page_size=1,
        page_shown=1,
        materialized_total=1,
    )
    report.provenance["effective_configuration_revision"] = "html-fixture"
    report.snapshot = {
        "status": "not_persisted",
        "ordered_pool": [row.id],
        "result_records": {row.id: _frozen_record(row)},
        "ranking_traces": {row.id: {"algorithm_version": "fixture"}},
    }
    return report


class CliHtmlTests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_search_html_is_self_contained_unicode_and_matches_report_file(self):
        with TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "search.html"
            finder = SimpleNamespace(search=Mock(return_value=_report(query="café <humanize>")))
            with patch("universal_skill_finder.cli._load", return_value=(object(), object())) as load, \
                 patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder):
                code, output, error = self.invoke([
                    "search", "café", "--html", "--report-file", str(artifact), "--progress", "off",
                ])
            self.assertEqual((code, error), (0, ""))
            saved = artifact.read_text(encoding="utf-8")

        self.assertEqual(output, saved)
        self.assertIn("<html", output.lower())
        self.assertIn("</html>", output.lower())
        self.assertIn("Humanizer Ω", output)
        self.assertIn("café", output)
        self.assertNotIn("<humanize>", output)
        finder.search.assert_called_once()
        load.assert_called_once()

    def test_page_html_uses_only_frozen_snapshot_and_matches_report_file(self):
        with TemporaryDirectory() as temporary:
            snapshot_path = Path(temporary) / "snapshot.json"
            artifact = Path(temporary) / "page.html"
            report = _report()
            row = report.results[0]
            snapshot = create_snapshot(
                query=report.query,
                options={"thorough": False},
                config_revision="html-fixture",
                ordered_pool=[row.id],
                result_records={row.id: _frozen_record(row)},
                requested_cap=1,
                page_size=1,
                report_metadata={"coverage": [row.to_dict() for row in report.coverage], "mode": "online"},
            )
            save_exclusive_path(snapshot_path, snapshot)
            checked = {
                "role": "repository",
                "url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
                "final_url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
                "status": "eligible",
            }
            with patch("universal_skill_finder.cli._load") as load, \
                 patch("universal_skill_finder.cli.UniversalSkillFinder") as finder, \
                 patch("universal_skill_finder.cli.validate_frozen_result_record",
                       return_value={"status": "eligible", "link_proofs": [checked]}) as validate:
                code, output, error = self.invoke([
                    "page", "--report", str(snapshot_path), "--html", "--report-file", str(artifact),
                    "--progress", "off",
                ])
            self.assertEqual((code, error), (0, ""))
            saved = artifact.read_text(encoding="utf-8")

        self.assertEqual(output, saved)
        self.assertIn("<html", output.lower())
        self.assertIn("Humanizer Ω", output)
        load.assert_not_called()
        finder.assert_not_called()
        validate.assert_called_once()

    def test_html_is_mutually_exclusive_with_json_and_markdown(self):
        for arguments in (
            ["search", "humanize", "--html", "--json"],
            ["search", "humanize", "--html", "--markdown"],
            ["page", "--report", "frozen.json", "--html", "--json"],
            ["page", "--report", "frozen.json", "--html", "--markdown"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as raised, \
                 patch("universal_skill_finder.cli._load") as load:
                with redirect_stderr(io.StringIO()):
                    _parser().parse_args(arguments)
            self.assertEqual(raised.exception.code, 2)
            load.assert_not_called()

    def test_empty_html_query_returns_local_help_without_loading_configuration(self):
        with patch("universal_skill_finder.cli._load") as load:
            code, output, error = self.invoke(["search", "--html"])
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Search enabled sources", output)
        load.assert_not_called()

    def test_existing_html_artifact_is_preserved_before_search_starts(self):
        with TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "existing.html"
            artifact.write_text("preserve this artifact", encoding="utf-8")
            with patch("universal_skill_finder.cli._load") as load, \
                 patch("universal_skill_finder.cli.UniversalSkillFinder") as finder:
                code, _output, error = self.invoke([
                    "search", "humanize", "--html", "--report-file", str(artifact), "--progress", "off",
                ])
            contents = artifact.read_text(encoding="utf-8")
        self.assertEqual(code, 3)
        self.assertIn("file or network error", error)
        self.assertEqual(contents, "preserve this artifact")
        load.assert_not_called()
        finder.assert_not_called()

    def test_json_and_markdown_search_outputs_remain_available(self):
        for flag, expected in (("--json", "Humanizer Ω"), ("--markdown", "## Universal Skill Finder")):
            with self.subTest(flag=flag):
                finder = SimpleNamespace(search=Mock(return_value=_report()))
                with patch("universal_skill_finder.cli._load", return_value=(object(), object())), \
                     patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder):
                    code, output, error = self.invoke(["search", "humanize", flag, "--progress", "off"])
                self.assertEqual((code, error), (0, ""))
                if flag == "--json":
                    self.assertEqual(json.loads(output)["results"][0]["name"], expected)
                else:
                    self.assertIn(expected, output)
                finder.search.assert_called_once()

    def test_html_retains_the_tail_of_a_normalized_1900_character_description(self):
        marker = " DESCRIPTION-TAIL-MARKER"
        description = "d" * (1900 - len(marker)) + marker
        # Replace the fixture's row rather than relying on a renderer-specific
        # description normalizer. Registry evidence is bounded at 2000 chars.
        report = _report(name="Long description")
        report.results[0].description = description
        rendered = render_report(report, format="html")
        details = rendered.split("<summary>Description</summary>", 1)[1].split("</details>", 1)[0]
        self.assertIn(marker, details)
        self.assertIn(description[:100], details)

    def test_html_metrics_keep_the_last_source_after_long_concatenation(self):
        report = _report(name="Many source metrics")
        row = report.results[0]
        row.source_ids = [f"source-{index:03d}" for index in range(100)]
        row.metrics_by_source = {
            source_id: {"stars": index}
            for index, source_id in enumerate(row.source_ids)
        }
        rendered = render_report(report, format="html")
        self.assertIn("source-000: stars: 0", rendered)
        self.assertIn("source-099: stars: 99", rendered)

    def test_html_exact_target_disclosure_keeps_long_path_and_ref(self):
        report = _report(name="Long exact target")
        row = report.results[0]
        path = "skills/" + "nested-" * 35 + "humanizer"
        ref = "release-" + "r" * 180
        row.target_proof = {
            "kind": "github",
            "status": "eligible",
            "method": "anonymous_exact_skill_md_get",
            "identity_basis": "github-exact-skill-md-v1",
            "url": "https://raw.githubusercontent.com/anthropics/skills/main/skills/pdf/SKILL.md",
            "content_sha256": "a" * 64,
            "actual_name": row.name,
            "reported": {"repository": row.repository, "ref": ref, "skill_path": path, "name": row.name},
            "resolved": {"repository": row.repository, "ref": ref, "skill_path": path},
        }
        rendered = render_report(report, format="html")
        disclosure = rendered.split("<summary>Exact location and installation command</summary>", 1)[1].split("</details>", 1)[0]
        self.assertIn(path, disclosure)
        self.assertIn(ref, disclosure)

    def test_html_notes_keep_complete_warning_groups_after_canonical_preparation(self):
        report = _report(name="Complete notes")
        warning_tail = " WARNING-GROUP-TAIL"
        inventory_tail = " INVENTORY-GROUP-TAIL"
        warning = "w" * (250 - len(warning_tail)) + warning_tail
        report.results = [
            replace(report.results[0], id=f"skill:warning-{number}", result_number=number, warnings=[warning])
            for number in range(1, 121)
        ]
        report.installed_scan = {
            "status": "complete",
            "limitations": ["i" * (250 - len(inventory_tail)) + inventory_tail],
        }
        rendered = render_report(report, format="html")
        notes = rendered.split("<h2>Notes</h2>", 1)[1].split("<h2>Next actions</h2>", 1)[0]
        self.assertIn(warning_tail, notes)
        self.assertIn(inventory_tail, notes)
        self.assertIn("#120", notes)


if __name__ == "__main__":
    unittest.main()
