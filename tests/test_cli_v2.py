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

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cli import _persist_report_snapshot, _snapshot_destinations, main
from universal_skill_finder.models import Coverage, SearchReport
from universal_skill_finder.snapshot import create_snapshot, encode_cursor, load_snapshot, materialize_page, save_exclusive_path
from test_presentation import result


def frozen_record(row):
    """Use the same reviewed anonymous GitHub proof shape snapshots persist."""
    record = row.to_dict()
    for proof in record.get("link_proofs", []):
        if proof.get("url", "").startswith("https://github.com/"):
            proof.update(
                identity_basis="github-owner-repository-path-v1",
                http_status=200,
                checked_at=datetime.now(timezone.utc).isoformat(),
            )
    return record


def frozen_report() -> SearchReport:
    rows = [
        result(id="skill:one", name="PDF one", validation_status="eligible", result_number=1),
        result(id="skill:two", name="PDF two", validation_status="eligible", result_number=2),
    ]
    report = SearchReport(
        query="pdf", results=rows, coverage=[Coverage("fixture", "ok", result_count=2)],
        generated_at="2026-09-10T00:00:00+00:00", configuration_path="fixture.json",
        accepted_occurrences=2, unique_count=2, eligible_count=2, requested_count=2,
        page_size=1,
    )
    report.provenance["effective_configuration_revision"] = "fixture-revision"
    report.snapshot = {
        "status": "not_persisted",
        "ordered_pool": [row.id for row in rows],
        "result_records": {row.id: frozen_record(row) for row in rows},
        "ranking_traces": {row.id: {"algorithm_version": "fixture"} for row in rows},
    }
    return report


class CliV2Tests(unittest.TestCase):
    def invoke(self, arguments: list[str]):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_empty_and_whitespace_queries_render_local_help_without_io(self):
        for arguments in ([], ["search"], ["search", "   "]):
            with self.subTest(arguments=arguments), patch("universal_skill_finder.cli._load") as load:
                code, output, error = self.invoke(arguments)
            self.assertEqual((code, error), (0, ""))
            self.assertIn("Search enabled sources", output)
            load.assert_not_called()

    def test_alias_conflicts_and_live_mode_conflicts_fail_before_io(self):
        cases = [
            ["pdf", "--limit", "2", "--per-source-limit", "3"],
            ["pdf", "--count", "2", "--max-results", "3"],
            ["pdf", "--offline", "--preview"],
            ["pdf", "--dry-run", "--thorough"],
            ["pdf", "--count", "101"],
            ["pdf", "--page-size", "101"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), patch("universal_skill_finder.cli._load") as load:
                code, _output, error = self.invoke(arguments)
            self.assertEqual(code, 3)
            self.assertTrue(error)
            load.assert_not_called()

    def test_public_and_legacy_caps_resolve_once(self):
        for arguments, expected in ((["pdf", "-n", "100"], 100),
                                    (["pdf", "--max-results", "500"], 500)):
            with self.subTest(arguments=arguments), patch("universal_skill_finder.cli._search", return_value=0) as search:
                self.assertEqual(main(arguments), 0)
            args = search.call_args.args[0]
            self.assertEqual(args.count, expected)
            self.assertEqual(args.max_results, expected)
        with patch("universal_skill_finder.cli._load") as load:
            code, _out, _err = self.invoke(["pdf", "--max-results", "501"])
        self.assertEqual(code, 3)
        load.assert_not_called()

    def test_saved_pages_and_explanations_use_only_the_frozen_snapshot(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "search-report.json"
            finder = SimpleNamespace(search=Mock(return_value=frozen_report()))
            with patch("universal_skill_finder.cli._load", return_value=(object(), object())), \
                 patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder):
                code, output, error = self.invoke([
                    "search", "pdf", "--json", "--count", "2", "--page-size", "1",
                    "--report-json", str(path), "--progress", "off",
                ])
            self.assertEqual((code, error), (0, ""))
            document = json.loads(output)
            self.assertEqual([row["result_number"] for row in document["results"]], [1])
            self.assertTrue(document["continuation"]["available"])
            self.assertTrue(path.is_file())
            finder.search.assert_called_once()

            cursor = document["continuation"]["cursor"]
            proof = {"role": "repository", "url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
                     "status": "eligible", "final_url": "https://github.com/anthropics/skills/tree/main/skills/pdf"}
            with patch("universal_skill_finder.cli._load") as load, patch("universal_skill_finder.cli.UniversalSkillFinder") as finder_class, \
                 patch("universal_skill_finder.cli.validate_frozen_result_record",
                       return_value={"status": "eligible", "link_proofs": [proof]}):
                code, output, error = self.invoke([
                    "page", "--report", str(path), "--cursor", cursor, "--json",
                ])
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["results"][0]["result_number"], 2)
            load.assert_not_called()
            finder_class.assert_not_called()

            with patch("universal_skill_finder.cli._load") as load:
                code, explanation, error = self.invoke([
                    "explain", "--report", str(path), "--result", "2",
                ])
            self.assertEqual((code, error), (0, ""))
            self.assertIn("PDF two", explanation)
            load.assert_not_called()

    def test_explicit_artifacts_are_exclusive(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "existing.json"
            path.write_text("preserve", encoding="utf-8")
            finder = SimpleNamespace(search=Mock(return_value=frozen_report()))
            with patch("universal_skill_finder.cli._load", return_value=(object(), object())), \
                 patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder):
                code, _output, error = self.invoke(["search", "pdf", "--report-json", str(path)])
            self.assertEqual(code, 3)
            self.assertIn("file or network error", error)
            self.assertEqual(path.read_text(encoding="utf-8"), "preserve")

    def test_later_page_validates_only_frozen_record_and_reuses_persisted_page(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            first = result(id="skill:one", name="One", validation_status="eligible")
            second = result(id="skill:two", name="Two", validation_status="not_checked")
            second.occurrences = [{
                "source_id": "repo", "adapter": "github-repo",
                "repository": "owner/repo", "ref": "main", "skill_path": "skills/two",
            }]
            records = {row.id: frozen_record(row) for row in (first, second)}
            snapshot = create_snapshot(
                query="pdf", options={"thorough": False}, config_revision="fixture",
                ordered_pool=[first.id, second.id], result_records=records,
                requested_cap=2, page_size=1,
            )
            page_one = materialize_page(
                snapshot, None,
                validate=lambda identity: {"status": "eligible" if identity == first.id else "not_checked"},
            )
            save_exclusive_path(path, page_one.snapshot)
            proof = {"role": "repository", "url": "https://github.com/owner/repo/tree/main/skills/two",
                     "status": "eligible", "final_url": "https://github.com/owner/repo/tree/main/skills/two"}
            with patch("universal_skill_finder.cli.validate_frozen_result_record",
                       return_value={"status": "eligible", "link_proofs": [proof]}) as validate:
                code, output, error = self.invoke([
                    "page", "--report", str(path), "--cursor", page_one.next_cursor, "--json",
                ])
                repeat_code, repeat_output, repeat_error = self.invoke([
                    "page", "--report", str(path), "--cursor", page_one.next_cursor, "--json",
                ])
            self.assertEqual((code, error, repeat_code, repeat_error), (0, "", 0, ""))
            self.assertEqual(json.loads(output)["results"][0]["result_number"], 2)
            self.assertEqual(json.loads(repeat_output)["results"][0]["result_number"], 2)
            validate.assert_called_once()

    def test_frozen_eligible_flag_without_link_proof_cannot_bypass_validation(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            row = result(id="skill:one", name="One", validation_status="eligible")
            record = frozen_record(row)
            record["link_proofs"] = []
            snapshot = create_snapshot(
                query="pdf", options={"thorough": False}, config_revision="fixture",
                ordered_pool=[row.id], result_records={row.id: record},
                requested_cap=1, page_size=1,
            )
            snapshot = replace(snapshot, validation_ledger={row.id: {"status": "eligible"}})
            save_exclusive_path(path, snapshot)
            cursor = encode_cursor(snapshot.snapshot_id, 0, 0)
            with patch("universal_skill_finder.cli.validate_frozen_result_record",
                       return_value={"status": "unavailable", "detail": "fixture missing"}) as validate:
                code, output, error = self.invoke([
                    "page", "--report", str(path), "--cursor", cursor, "--json",
                ])
            self.assertEqual((code, error), (0, ""))
            self.assertEqual(json.loads(output)["results"], [])
            validate.assert_called_once()

    def test_extend_count_uses_only_saved_snapshot_and_preserves_numbers(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            rows = [result(id="skill:one", name="One", validation_status="eligible"),
                    result(id="skill:two", name="Two", validation_status="eligible")]
            snapshot = create_snapshot(
                query="pdf", options={"thorough": False}, config_revision="fixture",
                ordered_pool=[row.id for row in rows], result_records={row.id: frozen_record(row) for row in rows},
                requested_cap=1, page_size=1,
            )
            first = materialize_page(snapshot, None, validate=lambda _: {"status": "eligible"})
            save_exclusive_path(path, first.snapshot)
            proof = {"role": "repository", "url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
                     "status": "eligible", "final_url": "https://github.com/anthropics/skills/tree/main/skills/pdf"}
            with patch("universal_skill_finder.cli._load") as load, patch("universal_skill_finder.cli.UniversalSkillFinder") as finder, \
                 patch("universal_skill_finder.cli.validate_frozen_result_record",
                       return_value={"status": "eligible", "link_proofs": [proof]}):
                code, output, error = self.invoke([
                    "page", "--report", str(path), "--cursor", first.resume_cursor,
                    "--extend-count", "2", "--json",
                ])
            document = json.loads(output)
            self.assertEqual((code, error), (0, ""))
            self.assertEqual([row["result_number"] for row in document["results"]], [2])
            self.assertEqual(document["requested_count"], 2)
            self.assertFalse(document["show_more_available"])
            load.assert_not_called()
            finder.assert_not_called()

    def test_snapshot_persistence_keeps_current_installed_annotations(self):
        report = frozen_report()
        report.results[0].installed = {"status": "matching_instructions", "evidence": ["skill_md_sha256"], "scopes": ["user"]}
        report.installed_scan = {"status": "complete", "skills_read": 1}
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            _persist_report_snapshot(report, path, SimpleNamespace(
                limit=10, source=[], exclude=[], thorough=False, count=2, page_size=1,
            ))
            saved = load_snapshot(path)
        self.assertEqual(saved.result_records["skill:one"]["installed"]["status"], "matching_instructions")
        self.assertEqual(saved.inventory_evidence["status"], "complete")

    def test_roundtrip_immutable_occurrences_rebuild_reviewed_destination(self):
        row = result(id="skill:tuple", name="PDF forms", repository="owner/repo",
                     ref="main", skill_path="skills/pdf")
        row.occurrences = [{"adapter": "skills-sh", "source_id": "skills-sh",
                            "listing_url": "https://skills.sh/owner/repo/pdf",
                            "listing_role": "listing",
                            "source_evidence": {"expected_identity": {"id": "owner/repo/pdf"}}}]
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            saved = create_snapshot(query="pdf", options={}, config_revision="test",
                                    ordered_pool=[row.id], result_records={row.id: row.to_dict()})
            save_exclusive_path(path, saved)
            loaded = load_snapshot(path)
        record = loaded.result_records[row.id]
        self.assertIsInstance(record["occurrences"], tuple)
        destinations = _snapshot_destinations(row.id, record)
        self.assertEqual(len(destinations), 2)
        self.assertTrue(all(item.profile is not None for item in destinations))

    def test_more_without_cursor_preserves_coverage_and_saves_exact_page(self):
        with TemporaryDirectory() as temporary:
            snapshot_path = Path(temporary) / "snapshot.json"
            page_path = Path(temporary) / "page.md"
            report = frozen_report()
            report.accepted_occurrences = 3
            report.coverage = [Coverage("fixture", "ok", result_count=3),
                               Coverage("disabled", "disabled", enabled=False)]
            _persist_report_snapshot(report, snapshot_path, SimpleNamespace(
                limit=10, source=[], exclude=[], thorough=False, count=1, page_size=1,
            ))
            with patch("universal_skill_finder.cli._load") as load, patch("universal_skill_finder.cli.UniversalSkillFinder") as finder, \
                 patch("universal_skill_finder.cli.validate_frozen_result_record", return_value={"status": "eligible"}):
                code, output, error = self.invoke([
                    "page", "--report", str(snapshot_path), "--more", "--markdown",
                    "--progress", "plain", "--report-file", str(page_path),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(output, page_path.read_text(encoding="utf-8"))
            self.assertIn("Candidates: 3 accepted", output)
            self.assertIn("1 duplicate", output)
            self.assertIn("fixture", output)
            self.assertIn("disabled", output)
            self.assertNotIn("No sources completed", output)
            self.assertIn("2. PDF two", output)
            self.assertTrue(error)
            load.assert_not_called()
            finder.assert_not_called()
            with patch("universal_skill_finder.cli.validate_frozen_result_record") as validator:
                code, output, error = self.invoke(["page", "--report", str(snapshot_path), "--more", "--json"])
            self.assertEqual(code, 0)
            exhausted = json.loads(output)
            self.assertTrue(exhausted["pool_exhausted"])
            self.assertEqual(exhausted["results"], [])
            self.assertEqual(exhausted["requested_count"], 2)
            validator.assert_not_called()

    def test_page_existing_artifact_fails_before_advancing_snapshot(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "exists.md"
            path.write_text("keep", encoding="utf-8")
            with patch("universal_skill_finder.cli.load_snapshot") as load:
                code, _output, _error = self.invoke([
                    "page", "--report", "not-read.json", "--more", "--report-file", str(path),
                ])
            self.assertEqual(code, 3)
            load.assert_not_called()
            self.assertEqual(path.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
