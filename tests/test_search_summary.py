from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.presentation import _summary_notes, render_report


def report(*, continuation: bool = False, failed: int = 0, verification_total: int = 75):
    coverage = [SimpleNamespace(source_id=f"live-{index}", status="ok", result_count=15, shown=2)
                for index in range(5)]
    coverage.extend(SimpleNamespace(source_id=f"cached-{index}", status="cached", result_count=10, shown=1)
                    for index in range(4))
    coverage.extend(SimpleNamespace(source_id=f"failed-{index}", status="timeout", result_count=0, shown=0)
                    for index in range(failed))
    return SimpleNamespace(
        report_format_version=2,
        mode="online",
        continuation_page=continuation,
        query="anti-slop",
        results=[],
        coverage=coverage,
        accepted_occurrences=81,
        unique_count=75,
        eligible_count=10 if verification_total else 0,
        unavailable_count=2 if verification_total else 0,
        inconclusive_count=63 if verification_total else 0,
        not_checked_count=0,
        page_shown=10 if not continuation else 2,
        materialized_total=10 if not continuation else 12,
        page_start=1 if not continuation else 11,
        requested_count=10,
        page_size=10,
        installed_scan={},
        notes=[],
        continuation=SimpleNamespace(available=True),
        footer_link_proof={},
    )


class SearchSummaryTests(unittest.TestCase):
    def test_summary_notes_rejects_malformed_destination_check_mapping_explicitly(self):
        with self.assertRaisesRegex(ValueError, "summary destination checks must be a mapping"):
            _summary_notes(report(), [], {"checks": []})

    def test_singular_header_wording_in_markdown_plain_and_html(self):
        single = report()
        single.coverage = single.coverage[:1]
        single.unique_count = 1
        for format in ("markdown", "plain"):
            text = render_report(single, format=format)
            self.assertIn("1 candidate", text)
            self.assertIn("Sources:", text)
            self.assertIn("1 searched", text)
            single.continuation_page = True
            text = render_report(single, format=format)
            self.assertIn("1 saved skill", text)
            self.assertNotIn("1 saved skills", text)
            single.continuation_page = False
        html = render_report(single, format="html")
        self.assertIn("1 source (1 searched, 0 cached)", html)
        self.assertNotIn("1 sources", html)

    def test_online_header_is_scan_first_and_preserves_detail_in_notes(self):
        text = render_report(report())
        before_notes, notes = text.split("## Notes", 1)

        self.assertIn("Search: **anti-slop**", before_notes)
        self.assertIn("**75 candidates** · **10 shown**", before_notes)
        self.assertIn("Sources: **5 searched** · **4 cached**", before_notes)
        self.assertNotIn("81 candidates →", before_notes)
        self.assertNotIn("10 with verified destinations", before_notes)
        self.assertIn("- Candidates: 81 accepted · 75 unique skills · 6 duplicate entries merged.", notes)
        self.assertIn("- Destination checks: 10 with verified destinations (not necessarily install-ready) · 2 unavailable · 63 inconclusive · 0 not checked.", notes)
        self.assertIn("- Page: 1–10 · 10 on this page · 10 materialized so far.", notes)
        self.assertIn("- Requested up to 10 results · Page size: up to 10 verified results.", notes)
        self.assertIn("- Sources: 5 searched · 4 cached · 0 failed · 0 not searched.", notes)

    def test_partial_suffix_exposes_failed_or_unverified_work_without_inflating_source_total(self):
        text = render_report(report(failed=2, verification_total=0))
        before_notes, notes = text.split("## Notes", 1)

        self.assertIn("Sources: **5 searched** · **4 cached**", before_notes)
        self.assertIn("**partial**", before_notes)
        self.assertNotIn("11 sources", before_notes)
        self.assertIn("- Sources: 5 searched · 4 cached · 2 failed · 0 not searched.", notes)
        self.assertIn("- Status: partial coverage or destination verification; counts above are not a confirmed zero-match result.", notes)

    def test_partial_suffix_marks_a_zero_card_page_with_pending_validation(self):
        found = report()
        found.results = []
        found.unique_count = 39
        found.page_shown = 0
        found.eligible_count = 0
        found.unavailable_count = 0
        found.inconclusive_count = 30
        found.not_checked_count = 9
        text = render_report(found)

        self.assertIn("**partial**", text.split("## Notes", 1)[0])

    def test_deadline_underfill_is_explicit_in_every_human_format(self):
        found = report()
        found.unique_count = 52
        found.page_shown = 6
        found.materialized_total = 6
        found.eligible_count = 6
        found.unavailable_count = 0
        found.inconclusive_count = 2
        found.not_checked_count = 44
        found.page_incomplete = True
        found.validation_checked_count = 8
        found.validation_deferred_count = 44
        found.validation_stop_reason = "deadline_reached"
        found.validation_stopped_reason = (
            "8-second destination-verification deadline reached after final validation "
            "completed for 8 of 52 candidates"
        )

        for format in ("markdown", "plain", "html"):
            with self.subTest(format=format):
                text = render_report(found, format=format)
                self.assertIn("incomplete page", text)
                self.assertIn(found.validation_stopped_reason, text)
                self.assertIn("44 candidates were deferred", text)

    def test_small_exhausted_pool_does_not_claim_an_incomplete_page(self):
        found = report()
        found.unique_count = 6
        found.page_shown = 6
        found.materialized_total = 6
        found.eligible_count = 6
        found.unavailable_count = found.inconclusive_count = found.not_checked_count = 0
        found.page_incomplete = False
        found.validation_stop_reason = "pool_exhausted"

        self.assertNotIn("incomplete page", render_report(found))

    def test_completed_not_checked_work_is_separate_from_deferred_work(self):
        found = report()
        found.unique_count = 53
        found.page_shown = 6
        found.materialized_total = 6
        found.eligible_count = 6
        found.inconclusive_count = 2
        found.not_checked_count = 45
        found.page_incomplete = True
        found.validation_deferred_count = 43
        found.validation_stopped_reason = "destination-verification deadline reached"

        text = render_report(found)
        self.assertIn("43 candidates were deferred", text)
        self.assertIn("Completed without a checked destination: 2 candidates", text)

    def test_coverage_columns_distinguish_pool_depth_from_global_display(self):
        found = report()
        found.coverage = [SimpleNamespace(
            source_id="skills-sh", status="ok", result_count=10, shown=1,
            requested_limit=10, source_total=None,
        )]
        for format in ("markdown", "plain", "html"):
            with self.subTest(format=format):
                text = render_report(found, format=format)
                self.assertIn("Candidates in pool", text)
                self.assertIn("Globally shown", text)
                self.assertIn("not the source's total matches", text)

    def test_header_counts_searched_and_complete_aliases_as_completed_sources(self):
        found = report()
        found.coverage = [
            SimpleNamespace(source_id="searched", status="searched", result_count=1, shown=1),
            SimpleNamespace(source_id="complete", status="complete", result_count=1, shown=1),
            SimpleNamespace(source_id="cached", status="cached", result_count=1, shown=1),
        ]
        text = render_report(found)

        self.assertIn("Sources: **2 searched** · **1 cached**", text.split("## Notes", 1)[0])
        self.assertIn("- Sources: 2 searched · 1 cached · 0 failed · 0 not searched.", text)

    def test_continuation_header_uses_saved_pool_and_never_claims_a_new_search(self):
        text = render_report(report(continuation=True))
        before_notes = text.split("## Notes", 1)[0]

        self.assertIn("Search: **anti-slop**", before_notes)
        self.assertIn("**75 saved skills** · **2 shown** · **no new search**", before_notes)
        self.assertNotIn("Sources:", before_notes)

    def test_offline_preview_remains_distinct(self):
        offline = report()
        offline.mode = "offline_preview"
        offline.results = []
        offline.candidate_previews = []
        text = render_report(offline)

        self.assertIn("Offline preview", text)
        self.assertNotIn("no new search", text)
        self.assertNotIn("sources (5 searched", text)

    def test_html_uses_the_same_compact_header_and_detail_notes(self):
        html = render_report(report(), format="html")

        self.assertIn("<strong>anti-slop</strong> · <strong>9 sources (5 searched, 4 cached)</strong> · <strong>75 candidates</strong> · <strong>showing 10</strong>", html)
        self.assertIn("Candidates: 81 accepted · 75 unique skills · 6 duplicate entries merged.", html)
        self.assertIn("Sources: 5 searched · 4 cached · 0 failed · 0 not searched.", html)


if __name__ == "__main__":
    unittest.main()
