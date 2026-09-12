from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cli import _persist_report_snapshot
from universal_skill_finder.models import Coverage, SearchReport
from universal_skill_finder.snapshot import load_snapshot
from test_presentation import result


class FirstPageAccountingTests(unittest.TestCase):
    def test_persist_keeps_live_first_page_when_tenth_eligible_is_after_scan_window(self):
        """The saved root page must not re-filter a valid live page to nine cards."""
        pool = []
        for index in range(31):
            source_ids = ["early"] if index < 30 else ["late"]
            status = "eligible" if index < 9 or index == 30 else "not_checked"
            pool.append(result(id=f"skill:{index:02d}", name=f"Skill {index}",
                               source_ids=source_ids, validation_status=status))
        live_rows = [*pool[:9], pool[30]]
        for number, row in enumerate(live_rows, 1):
            row.result_number = number
        report = SearchReport(
            query="fixture", results=live_rows,
            coverage=[Coverage("early", "ok", result_count=30, shown=9),
                      Coverage("late", "ok", result_count=1, shown=1)],
            generated_at="2026-09-11T00:00:00+00:00", configuration_path="fixture.json",
            requested_count=10, page_size=10, accepted_occurrences=31, unique_count=31,
            eligible_count=10, inconclusive_count=21, page_start=1, page_shown=10,
            materialized_total=10,
        )
        report.snapshot = {
            "ordered_pool": [row.id for row in pool],
            "result_records": {row.id: row.to_dict() for row in pool},
            "ranking_traces": {},
        }

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            _persist_report_snapshot(report, path, SimpleNamespace(
                limit=10, source=[], exclude=[], thorough=False, count=10, page_size=10,
            ))
            saved = load_snapshot(path)

        self.assertEqual([row.id for row in report.results], [row.id for row in live_rows])
        self.assertEqual(report.page_shown, 10)
        self.assertEqual(report.coverage[1].shown, 1)
        self.assertEqual(sum("late" in row.source_ids for row in report.results), 1)
        self.assertEqual(dict(saved.result_numbers)[pool[30].id], 10)

    def test_metadata_uses_retained_safe_cards_not_a_pre_filter_coverage_count(self):
        safe = result(id="skill:safe", validation_status="eligible", source_ids=["safe"])
        unchecked = result(id="skill:unchecked", validation_status="not_checked", source_ids=["unchecked"])
        report = SearchReport(
            query="fixture", results=[safe, unchecked],
            coverage=[Coverage("safe", "ok", result_count=1, shown=1),
                      Coverage("unchecked", "ok", result_count=1, shown=1)],
            generated_at="2026-09-11T00:00:00+00:00", configuration_path="fixture.json",
            requested_count=2, page_size=2,
        )
        report.snapshot = {
            "ordered_pool": [safe.id, unchecked.id],
            "result_records": {safe.id: safe.to_dict(), unchecked.id: unchecked.to_dict()},
            "ranking_traces": {},
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            _persist_report_snapshot(report, path, SimpleNamespace(
                limit=10, source=[], exclude=[], thorough=False, count=2, page_size=2,
            ))
            saved = load_snapshot(path)

        self.assertEqual(([row.id for row in report.results], report.page_start, report.page_shown),
                         ([safe.id], 1, 1))
        self.assertEqual([row.shown for row in report.coverage], [1, 0])
        self.assertEqual([row["shown"] for row in saved.report_metadata["coverage"]], [1, 0])


if __name__ == "__main__":
    unittest.main()
