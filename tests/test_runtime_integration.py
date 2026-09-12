from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import SourceUnavailable
from universal_skill_finder.cache import Cache
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate
from universal_skill_finder.presentation import render_report
from universal_skill_finder.runtime import NetworkPolicy
from universal_skill_finder.validation import AnonymousResponse


def source(source_id: str, *, enabled: bool = True) -> dict[str, object]:
    return {
        "id": source_id,
        "adapter": "skills-sh",
        "kind": "registry",
        "base_url": "https://skills.sh",
        "effective_enabled": enabled,
        "enabled": enabled,
        "trust": "community-index",
    }


def candidate(source_id: str, native_id: str, name: str, *, repository: str | None = None,
              skill_path: str | None = None, route: bool = True) -> Candidate:
    item = Candidate(
        native_id, name, f"{name} handles PDF workflows", source_id, "registry", "skills-sh",
        repository=repository, skill_path=skill_path, ref="main" if repository else None,
    )
    if route:
        item.listing_url = f"https://skills.sh/{native_id}"
        item.listing_role = "listing"
        item.source_evidence = {"expected_identity": {"id": native_id}}
    return item


class CombinedAdapter:
    def search(self, configured, query, limit, context):
        source_id = configured["id"]
        if source_id == "a-many":
            return [
                candidate(source_id, "pdf-forms", "PDF forms"),
                candidate(source_id, "broken", "PDF shared", repository="acme/shared", skill_path="skills/shared"),
                candidate(source_id, "unknown", "PDF unknown"),
                candidate(source_id, "no-route", "PDF local notes", route=False),
            ]
        if source_id == "b-replacement":
            return [candidate(source_id, "replacement", "PDF shared", repository="acme/shared", skill_path="skills/shared")]
        if source_id == "c-zero":
            return []
        if source_id == "d-failure":
            raise SourceUnavailable("timeout", "controlled source timeout")
        if source_id == "e-late":
            time.sleep(0.15)
            return [candidate(source_id, "late", "PDF late")]
        raise AssertionError(f"unexpected source execution: {source_id}")


class ProofTransport:
    def __init__(self):
        self.calls: list[str] = []

    def request(self, _method, url, **_kwargs):
        self.calls.append(url)
        identity = url.rstrip("/").rsplit("/", 1)[-1]
        if identity == "broken":
            return AnonymousResponse(404, connection_address="8.8.8.8")
        if identity == "unknown":
            return AnonymousResponse(403, connection_address="8.8.8.8")
        return AnonymousResponse(200, body=(f'{{"id":"{identity}"}}').encode(), connection_address="8.8.8.8")


class RuntimeIntegrationTests(unittest.TestCase):
    def test_combined_pool_validation_coverage_and_progress_conserve_evidence(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [source(name) for name in (
                "a-many", "b-replacement", "c-zero", "d-failure", "e-late",
            )] + [source("f-disabled", enabled=False)]
            config = EffectiveConfig(
                settings={}, packs=[], sources=sources,
                overlay_path=root / "sources.json", overlay={},
            )
            transport = ProofTransport()
            finder = UniversalSkillFinder(
                config, cache=Cache(root / "cache"), validation_transport=transport,
                validation_resolver=lambda _host, _port: ("8.8.8.8",),
            )
            finder.adapter_map = {"skills-sh": CombinedAdapter()}
            events = []
            policy = NetworkPolicy(0.05, 0.20, 0.10)
            with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=policy):
                report = finder.search("pdf workflows", count=3, page_size=2, preview=True,
                                       progress_callback=events.append)

        self.assertEqual(report.accepted_occurrences, 5)
        self.assertEqual(report.unique_count, 4)
        self.assertEqual(
            report.eligible_count + report.unavailable_count + report.inconclusive_count + report.not_checked_count,
            report.unique_count,
        )
        self.assertEqual(len(report.results), 2)
        self.assertTrue(all(item.validation_status == "eligible" for item in report.results))
        shared = next(item for item in report.results if item.repository == "acme/shared")
        self.assertEqual(shared.source_ids, ["a-many", "b-replacement"])
        self.assertTrue(any(url.endswith("/broken") for url in transport.calls))
        self.assertTrue(any(url.endswith("/replacement") for url in transport.calls))
        self.assertNotIn("https://skills.sh/broken", render_report(report))
        self.assertEqual(set(report.snapshot["ordered_pool"]), set(report.snapshot["result_records"]))
        self.assertEqual(len(report.snapshot["ordered_pool"]), report.unique_count)

        coverage = {item.source_id: item for item in report.coverage}
        self.assertEqual((coverage["a-many"].status, coverage["a-many"].result_count), ("ok", 4))
        self.assertEqual((coverage["c-zero"].status, coverage["c-zero"].result_count), ("ok", 0))
        self.assertEqual(coverage["d-failure"].status, "timeout")
        self.assertEqual(coverage["f-disabled"].status, "disabled")
        self.assertIn(coverage["e-late"].status, {"deadline_exceeded", "preempted"})
        self.assertTrue(coverage["e-late"].incomplete_results)
        self.assertFalse(any("late" in identity for identity in report.snapshot["ordered_pool"]))

        previews = [event for event in events if event.type == "early_verified"]
        self.assertLessEqual(len(previews), 3)
        self.assertTrue(all(event.status == "eligible" and event.proof_id for event in previews))
        self.assertEqual(events[0].type, "search_started")
        self.assertEqual(events[-1].type, "search_finished")


if __name__ == "__main__":
    unittest.main()
