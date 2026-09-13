from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.cli import main
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate, Coverage, SearchReport
from universal_skill_finder.presentation import render_report
from universal_skill_finder.snapshot import (
    apply_validation_result,
    create_snapshot,
    load_snapshot,
    materialize_ranked_all,
    paginate_ranked_page,
    save_exclusive_path,
)


class RecordingAdapter:
    def __init__(self, rows: list[Candidate]):
        self.rows = rows
        self.calls: list[tuple[str, int]] = []

    def search(self, _source, query, limit, _context):
        self.calls.append((query, limit))
        return [Candidate.from_dict(row.to_dict()) for row in self.rows[:limit]]


class NoDestinationRequests:
    def request(self, *_args, **_kwargs):
        raise AssertionError("discovery search must not validate destinations")


def row(
    source: str,
    adapter: str,
    path: str,
    *,
    name: str,
    description: str,
    rank: int,
) -> Candidate:
    return Candidate(
        native_id=f"{source}:{path}", name=name, description=description,
        source_id=source, source_kind="registry", adapter=adapter,
        native_rank=rank, repository="owner/skills", skill_path=path,
        ref="main", slug=path.rsplit("/", 1)[-1], publisher="owner",
        canonical_url=f"https://github.com/owner/skills/tree/main/{path}",
    )


def registry(source_id: str, adapter: str) -> dict:
    return {
        "id": source_id, "kind": "registry", "adapter": adapter,
        "enabled": True, "effective_enabled": True,
        "base_url": "https://example.test", "trust": "community-index",
    }


def repository(source_id: str) -> dict:
    return {
        "id": source_id, "kind": "repository", "adapter": "github-repo",
        "enabled": True, "effective_enabled": True,
        "repository": "owner/catalogue", "ref": "main", "trust": "publisher-owned",
    }


def finder(root: Path, sources: list[dict], adapters: dict[str, RecordingAdapter]) -> UniversalSkillFinder:
    config = EffectiveConfig(
        settings={"cache_ttl_seconds": 300, "max_workers": 4},
        packs=[], sources=sources, overlay_path=root / "sources.json", overlay={},
    )
    instance = UniversalSkillFinder(
        config, cache=Cache(root / "cache"),
        validation_transport=NoDestinationRequests(),
        validation_resolver=lambda *_args: (_ for _ in ()).throw(
            AssertionError("discovery search must not resolve destinations")
        ),
    )
    instance.adapter_map = adapters
    return instance


class DiscoveryFirstTests(unittest.TestCase):
    def test_provider_results_are_not_deleted_by_local_lexical_filter(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = RecordingAdapter([
                row("remote", "skills-sh", "skills/general", name="Popular toolkit",
                    description="General utility", rank=1),
                row("remote", "skills-sh", "skills/video", name="Video editor",
                    description="Edit video projects", rank=20),
            ])
            local = RecordingAdapter([
                row("local", "github-repo", "skills/general", name="Popular toolkit",
                    description="General utility", rank=1),
            ])
            found = finder(
                root,
                [registry("remote", "skills-sh"), repository("local")],
                {"skills-sh": remote, "github-repo": local},
            ).search(
                "video editor", limit=20, count=100, page_size=25,
                verify_results=False,
            )

        self.assertEqual([item.name for item in found.results], ["Video editor", "Popular toolkit"])
        self.assertEqual([item.result_count for item in found.coverage], [2, 0])
        self.assertEqual(found.validation_checked_count, 0)
        self.assertEqual(found.validation_deferred_count, 2)
        self.assertEqual(found.validation_stop_reason, "deferred_to_inspect")
        self.assertTrue(found.discovery_mode)

    def test_default_depth_is_cached_per_source_and_recorded_for_ranking(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = RecordingAdapter([
                row("remote", "skills-sh", "skills/video", name="Video editor",
                    description="Edit video projects", rank=1),
            ])
            instance = finder(root, [registry("remote", "skills-sh")], {"skills-sh": adapter})
            first = instance.search("video editor", limit=20, count=100, page_size=25, verify_results=False)
            second = instance.search("video editor", limit=20, count=100, page_size=25, verify_results=False)

        self.assertEqual(adapter.calls, [("video editor", 20)])
        self.assertEqual(second.coverage[0].status, "cached")
        for found in (first, second):
            native = found.results[0].occurrences[0]["source_evidence"]["native"]
            self.assertEqual(native["requested_depth"], 20)

    def test_ranking_uses_exact_70_20_10_components_and_ignores_destination_state(self):
        with TemporaryDirectory() as temporary:
            instance = finder(Path(temporary), [], {})
            candidate = row(
                "one", "skills-sh", "skills/video-editor", name="Video editor",
                description="Video editor for projects", rank=1,
            )
            candidate.source_evidence = {"native": {"requested_depth": 20}}
            before = instance._merge([candidate], "video editor")[0]
            candidate.target_proof = {"status": "unavailable", "detail": "missing"}
            candidate.link_proofs = [{"role": "skill_destination", "status": "unavailable"}]
            after = instance._merge([candidate], "video editor")[0]

        self.assertEqual(before.ranking["score"], after.ranking["score"])
        self.assertEqual(before.ranking["components"], {
            "query_relevance": 1.0,
            "source_signal": 0.85,
            "corroboration": 0.0,
        })
        self.assertEqual(before.ranking["evidence"]["weights"], {
            "query_relevance": 0.7,
            "source_signal": 0.2,
            "corroboration": 0.1,
        })
        self.assertAlmostEqual(before.ranking["score"], 0.87)

    def test_native_rank_uses_requested_depth_and_corroboration_uses_adapter_families(self):
        with TemporaryDirectory() as temporary:
            instance = finder(Path(temporary), [], {})
            best = row("one", "skills-sh", "skills/best", name="Video editor",
                       description="Edit video projects", rank=1)
            last = row("one", "skills-sh", "skills/last", name="Video editor",
                       description="Edit video projects", rank=20)
            for candidate in (best, last):
                candidate.source_evidence = {"native": {"requested_depth": 20}}
            ranked = instance._merge([last, best], "video editor")
            signals = {item.skill_path: item.ranking["components"]["source_signal"] for item in ranked}

            shared = [
                row("one", "skills-sh", "skills/shared", name="Video editor",
                    description="Edit video projects", rank=1),
                row("two", "skillsmp", "skills/shared", name="Video editor",
                    description="Edit video projects", rank=1),
                row("three", "clawhub", "skills/shared", name="Video editor",
                    description="Edit video projects", rank=1),
            ]
            one = instance._merge(shared[:1], "video editor")[0]
            two = instance._merge(shared[:2], "video editor")[0]
            three = instance._merge(shared, "video editor")[0]

        self.assertEqual(signals, {"skills/best": 0.85, "skills/last": 0.15})
        self.assertEqual([
            item.ranking["components"]["corroboration"] for item in (one, two, three)
        ], [0.0, 0.5, 1.0])

    def test_compact_output_is_two_lines_with_one_safe_link_and_one_metric(self):
        with TemporaryDirectory() as temporary:
            instance = finder(Path(temporary), [], {})
            candidate = row(
                "skills-sh", "skills-sh", "skills/video", name="Video editor",
                description="Edit video projects", rank=1,
            )
            candidate.metrics = {"installs": 1_400}
            result = instance._merge([candidate], "video editor")[0]
        result.result_number = 1
        report = SearchReport(
            query="video editor", results=[result], coverage=[Coverage("skills-sh", "ok", 1)],
            generated_at="2026-09-13T00:00:00Z", configuration_path="fixture",
            discovery_mode=True, accepted_occurrences=1, unique_count=1,
            requested_count=100, page_size=25, page_start=1, page_shown=1,
            materialized_total=1,
        )
        rendered = render_report(report)

        self.assertIn(
            "1. [Video editor](https://github.com/owner/skills/tree/main/skills/video) · owner/skills/skills/video",
            rendered,
        )
        self.assertIn("   skills-sh · 1.4K installs · Edit video projects", rendered)
        self.assertNotIn("skills-sh: 1.4K", rendered)
        self.assertIn("1 unique candidate** · **1 source completed", rendered)
        self.assertIn("Save with `--report-json`", rendered)
        self.assertNotIn("Next: **Inspect #N**", rendered)

        result.browse_links = [{
            "source_id": "evil", "adapter": "unknown", "role": "listing",
            "url": "https://evil.example/steal", "status": "discoverable",
            "method": "connector_reviewed_route",
        }]
        inert = render_report(report)
        self.assertNotIn("evil.example", inert)
        self.assertNotIn("[Video editor]", inert)

    def test_frozen_next_page_and_show_all_never_touch_validation_ledger(self):
        records = {f"skill:{index}": {"id": f"skill:{index}", "name": f"Skill {index}"}
                   for index in range(1, 37)}
        snapshot = create_snapshot(
            query="video", options={"mode": "discovery"}, config_revision="fixture",
            ordered_pool=list(records), result_records=records,
            requested_cap=36, page_size=25,
        )
        first = paginate_ranked_page(snapshot, None)
        second = paginate_ranked_page(first.snapshot, first.next_cursor)
        all_results = materialize_ranked_all(second.snapshot)

        self.assertEqual((len(first.ids), len(second.ids), len(all_results.ids)), (25, 11, 36))
        self.assertEqual(all_results.snapshot.result_numbers["skill:36"], 36)
        self.assertEqual(dict(all_results.snapshot.validation_ledger), {})
        self.assertEqual(all_results.snapshot.ordered_pool, snapshot.ordered_pool)

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            save_exclusive_path(path, first.snapshot)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("universal_skill_finder.cli.validate_frozen_result_record") as validator, \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["page", "--report", str(path), "--all", "--json", "--progress", "off"])
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual(len(json.loads(stdout.getvalue())["results"]), 36)
        validator.assert_not_called()

    def test_show_all_cannot_silently_extend_the_saved_cap(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["page", "--report", "/tmp/not-read.json", "--all", "--more"])
        self.assertEqual(error.exception.code, 2)

    def test_inspection_updates_only_evidence_not_rank_or_number(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            instance = finder(root, [], {})
            result = instance._merge([
                row("skills-sh", "skills-sh", "skills/video", name="Video editor",
                    description="Edit video projects", rank=1),
            ], "video editor")[0]
            snapshot = create_snapshot(
                query="video editor", options={"mode": "discovery"}, config_revision="fixture",
                ordered_pool=[result.id], result_records={result.id: result.to_dict()},
                ranking_traces={result.id: result.ranking}, requested_cap=100, page_size=25,
            )
            snapshot = paginate_ranked_page(snapshot, None).snapshot
            before_order = snapshot.ordered_pool
            before_numbers = dict(snapshot.result_numbers)
            before_ranking = dict(snapshot.ranking_traces)
            updated = apply_validation_result(snapshot, result.id, {
                "status": "unavailable", "detail": "destination missing",
            })
            self.assertEqual(updated.ordered_pool, before_order)
            self.assertEqual(dict(updated.result_numbers), before_numbers)
            self.assertEqual(dict(updated.ranking_traces), before_ranking)
            self.assertEqual(updated.validation_ledger[result.id]["status"], "unavailable")
            self.assertEqual(updated.result_records[result.id]["validation_status"], "unavailable")

            path = root / "snapshot.json"
            save_exclusive_path(path, snapshot)
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch(
                "universal_skill_finder.cli.validate_frozen_result_record",
                return_value={"status": "unavailable", "detail": "destination missing"},
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["inspect", "--report", str(path), "--result", "1", "--json"])

            reloaded = load_snapshot(path)
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        self.assertEqual(json.loads(stdout.getvalue())["inspection"]["status"], "unavailable")
        self.assertEqual(reloaded.ordered_pool, before_order)
        self.assertEqual(dict(reloaded.result_numbers), before_numbers)


if __name__ == "__main__":
    unittest.main()
