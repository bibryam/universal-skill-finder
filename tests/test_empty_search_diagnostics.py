from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.presentation import render_progress, render_report


def blocked_report() -> dict[str, object]:
    return {
        "report_format_version": 2,
        "mode": "online",
        "query": "humanize",
        "results": [],
        "accepted_candidates": 39,
        "unique_skills": 39,
        "duplicates_merged": 0,
        "eligible": 0,
        "unavailable": 0,
        "inconclusive": 30,
        "not_checked": 9,
        "page_shown_count": 0,
        "materialized_count": 0,
        "page_start": 0,
        "requested_count": 10,
        "page_size": 10,
        "coverage": [
            {"source_id": "catalog", "status": "cached", "result_count": 39, "shown": 0, "enabled": True},
            {"source_id": "remote", "status": "health_state_unavailable", "detail": "health-state lock budget exhausted", "shown": 0, "enabled": True},
        ],
        "notes": ["Destination validation could not complete for the checked candidates."],
    }


class EmptySearchDiagnosticsTests(unittest.TestCase):
    def test_nonempty_unverified_pool_is_not_rendered_as_zero_matches_or_a_zero_range(self):
        for format in ("markdown", "plain", "html"):
            text = render_report(blocked_report(), format=format)
            self.assertNotIn("0 · 0 on this page", text)
            self.assertIn("No verified matches were materialized; destination verification is inconclusive for one or more candidates.", text)
            self.assertIn("Retry this checked search after resolving the reported access or destination-verification errors.", text)
            self.assertNotIn("No matches were found", text)

    def test_genuine_zero_match_search_keeps_its_distinct_message(self):
        report = blocked_report()
        report.update({"accepted_candidates": 0, "unique_skills": 0, "inconclusive": 0, "not_checked": 0,
                       "coverage": [{"source_id": "catalog", "status": "ok", "result_count": 0, "shown": 0, "enabled": True}]})
        text = render_report(report)
        self.assertIn("No matches were found in the sources that completed.", text)
        self.assertNotIn("Retry this checked search", text)

    def test_coverage_header_has_five_explicit_columns(self):
        text = render_report(blocked_report())
        self.assertIn("| Source | Search status | Candidates in pool | Globally shown | Enabled |", text)
        self.assertNotIn("| Source | Search status | Candidates in pool | Globally shown | Enabled | |", text)

    def test_validation_progress_counts_verified_destinations_not_attempts(self):
        event = SimpleNamespace(type="validation_finished", completed=0, total=39)
        self.assertEqual(render_progress(event), "[0/39] verified destinations")


if __name__ == "__main__":
    unittest.main()
