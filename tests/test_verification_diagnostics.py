from __future__ import annotations

import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import CacheLease
from universal_skill_finder.cli import _persist_report_snapshot
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Coverage
from universal_skill_finder.runtime import Deadline, NetworkPolicy, PermitPool, RequestBudget
from universal_skill_finder.snapshot import load_snapshot
from universal_skill_finder.validation import github_repository_destination
from test_cli_v2 import frozen_report
from test_universal_skill_finder import candidate, finder_config, fixture_finder, registry_source, StaticAdapter
from test_presentation import result
from universal_skill_finder.cache import Cache
from types import SimpleNamespace


class VerificationDiagnosticsTests(unittest.TestCase):
    def test_dns_failure_keeps_candidates_unverified_and_reports_the_cause(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = registry_source("one", "skills-sh", base_url="https://catalog.example")
            finder = fixture_finder(finder_config(root, [source]), Cache(root / "cache"))
            finder.adapter_map = {"skills-sh": StaticAdapter([candidate("one", "acme/skills", "humanizer", rank=1)])}
            with patch.object(finder, "validation_resolver", side_effect=ValueError("destination resolution failed")):
                report = finder.search("humanizer")
            self.assertEqual(report.unique_count, 1)
            self.assertEqual(report.eligible_count, 0)
            self.assertEqual(report.results, [])
            self.assertTrue(any("resolver could not complete checks for 1 candidate." in note for note in report.notes))
            self.assertEqual(report.to_dict()["notes"], report.notes)

    def test_permission_denied_proof_lock_sends_no_request_and_keeps_reason(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            finder = fixture_finder(finder_config(root, []), Cache(root / "cache"))

            @contextmanager
            def denied(*_args, **_kwargs):
                yield CacheLease("permission_denied")

            with patch.object(finder.cache, "exclusive_lease", denied), \
                 patch.object(finder.validation_transport, "request") as request:
                proof = finder._cached_destination_proof(
                    github_repository_destination("one", repository="acme/skills"), phase="final",
                    budget=RequestBudget(), permits=PermitPool(NetworkPolicy.for_mode()),
                    deadline=Deadline.start(NetworkPolicy.for_mode()),
                )
            request.assert_not_called()
            self.assertEqual(proof.status, "not_checked")
            self.assertEqual(proof.detail, "cache access denied; cannot acquire destination proof lock")

    def test_cache_notes_retain_fallback_cause_without_copying_arbitrary_errors(self):
        coverage = [Coverage("one", "cached", health_status="health_state_unavailable",
                             detail="cached fallback; cache access denied; cannot acquire health-state lock")]
        row = result(validation_status="inconclusive", target_proof={
            "status": "inconclusive", "detail": "private exception https://private.example?secret=fixture-only",
        })
        notes = UniversalSkillFinder._verification_notes(coverage, [row])
        self.assertEqual(len(notes), 1)
        self.assertIn("Cache access denied", notes[0])
        self.assertNotIn("fixture-only", str(notes))
        self.assertNotIn("private.example", str(notes))

    def test_snapshot_preserves_diagnostics_as_original_search_metadata(self):
        with TemporaryDirectory() as temporary:
            report = frozen_report()
            report.notes = ["Cache access denied."]
            path = Path(temporary) / "snapshot.json"
            args = SimpleNamespace(limit=10, source=[], exclude=[], thorough=False, count=2, page_size=1)
            _persist_report_snapshot(report, path, args)
            self.assertEqual(list(load_snapshot(path).report_metadata["notes"]), report.notes)


if __name__ == "__main__":
    unittest.main()
