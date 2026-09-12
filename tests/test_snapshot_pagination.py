from __future__ import annotations

import sys
import unittest
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.snapshot import (
    SnapshotError,
    create_snapshot,
    decode_cursor,
    encode_cursor,
    extend_snapshot,
    load_snapshot,
    materialize_page,
    next_safe_cursor,
    resolve_materialized_result,
    save_exclusive,
    save_exclusive_path,
    seed_initial_page,
    snapshot_dict,
    snapshot_writer_lock,
    update_snapshot,
    validate_frozen_result_record,
)
from universal_skill_finder.validation import AnonymousResponse, Destination, ProviderProfile, TargetResolutionCache


class _ProofLease:
    def release(self):
        pass


class _ProofBudget:
    def reserve(self, _kind, _amount, _deadline):
        return _ProofLease()


class _ProofPermits:
    def acquire(self, _origin, _phase, _deadline):
        return _ProofLease()


class _ScriptedProofTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _public_resolver(_host, _port):
    return ["8.8.8.8"]


class SnapshotPaginationTests(unittest.TestCase):
    def test_seeded_short_or_empty_first_page_keeps_a_safe_pending_cursor(self):
        short = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b", "c"],
                                requested_cap=3, page_size=2)
        seeded = seed_initial_page(short, ["c"])
        self.assertEqual(seeded.ids, ("c",))
        self.assertEqual(dict(seeded.snapshot.result_numbers), {"c": 1})
        self.assertIsNotNone(seeded.next_cursor)
        self.assertEqual(seeded.next_cursor, next_safe_cursor(seeded.snapshot))
        self.assertFalse(seeded.is_exhausted)

        empty = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"],
                                requested_cap=2, page_size=1)
        pending = seed_initial_page(empty, [])
        self.assertEqual(pending.ids, ())
        self.assertIsNotNone(pending.next_cursor)
        self.assertEqual(pending.next_cursor, next_safe_cursor(pending.snapshot))
        self.assertFalse(pending.is_exhausted)
        self.assertNotIn(encode_cursor(empty.snapshot_id, 0, 0), pending.snapshot.cursor_history)

    def test_seeded_root_replays_stably_after_loaded_proofs_are_demoted(self):
        row = {
            "id": "c", "validation_status": "eligible",
            "link_proofs": [{"role": "listing", "url": "https://example.test/c", "status": "eligible"}],
            "target_proof": {"status": "eligible", "reported": {"name": "c"}},
        }
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b", "c"],
                                   result_records={"a": {"id": "a"}, "b": {"id": "b"}, "c": row},
                                   requested_cap=2, page_size=1)
        seeded = seed_initial_page(snapshot, ["c"])
        root = encode_cursor(seeded.snapshot.snapshot_id, 0, 0)
        with TemporaryDirectory() as temporary:
            path = save_exclusive_path(Path(temporary) / "report.json", seeded.snapshot)
            loaded = load_snapshot(path)
        self.assertEqual(loaded.validation_ledger["c"]["status"], "not_checked")
        self.assertEqual(loaded.result_records["c"]["link_proofs"][0]["status"], "not_checked")
        self.assertEqual(loaded.result_records["c"]["target_proof"]["status"], "not_checked")
        replay = materialize_page(loaded, root, validate=lambda _: self.fail("root replay must not validate"))
        self.assertTrue(replay.reused)
        self.assertEqual((replay.ids, dict(replay.snapshot.result_numbers)), (("c",), {"c": 1}))

    def test_seed_initial_page_rejects_invalid_or_reseeded_ids(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b"],
                                   requested_cap=2, page_size=1)
        for ids in (["a", "a"], ["outside"], ["a", "b"]):
            with self.subTest(ids=ids), self.assertRaises(SnapshotError):
                seed_initial_page(snapshot, ids)
        seeded = seed_initial_page(snapshot, ["a"])
        with self.assertRaises(SnapshotError):
            seed_initial_page(seeded.snapshot, ["b"])

    def test_page_scan_is_bounded_and_continues_after_terminal_deferrals(self):
        pool = [f"candidate-{index}" for index in range(40)]
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=pool,
                                   requested_cap=10, page_size=10)
        calls = []
        first = materialize_page(
            snapshot, None,
            validate=lambda candidate_id: calls.append(candidate_id) or {"status": "unavailable"},
        )
        self.assertEqual((first.ids, first.scanned_count), ((), 30))
        self.assertFalse(first.is_exhausted)
        self.assertFalse(first.has_pending)
        self.assertEqual(calls, pool[:30])
        self.assertEqual(first.next_cursor, next_safe_cursor(first.snapshot))
        second = materialize_page(
            first.snapshot, first.next_cursor,
            validate=lambda candidate_id: calls.append(candidate_id) or {"status": "unavailable"},
        )
        self.assertTrue(second.is_exhausted)
        self.assertEqual(calls, pool)

    def test_budget_stop_does_not_consume_tail_or_persist_empty_history(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b"],
                                   requested_cap=2, page_size=2)
        calls = []
        stopped = materialize_page(
            snapshot, None, validate=lambda item: calls.append(item) or {"status": "eligible"},
            can_validate=lambda *_: False,
        )
        root = encode_cursor(snapshot.snapshot_id, 0, 0)
        self.assertEqual((stopped.ids, calls), ((), []))
        self.assertTrue(stopped.has_pending)
        self.assertFalse(stopped.is_exhausted)
        self.assertEqual(stopped.resume_cursor, root)
        self.assertNotIn(root, stopped.snapshot.cursor_history)
        recovered = materialize_page(
            stopped.snapshot, next_safe_cursor(stopped.snapshot),
            validate=lambda item: calls.append(item) or {"status": "eligible"},
        )
        self.assertEqual(recovered.ids, ("a", "b"))
        self.assertEqual(calls, ["a", "b"])

    def test_unreviewed_not_checked_is_deferred_and_does_not_block_later_candidate(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["unreviewed", "healthy"],
                                   requested_cap=1, page_size=1)

        def validate(candidate_id):
            return ({"status": "not_checked", "detail": "no reviewed public destination"}
                    if candidate_id == "unreviewed" else {"status": "eligible"})

        page = materialize_page(snapshot, None, validate=validate)
        self.assertEqual(page.ids, ("healthy",))
        self.assertTrue(page.snapshot.validation_ledger["unreviewed"]["deferred"])
        self.assertFalse(page.has_pending)

    def test_load_recovers_legacy_unscheduled_empty_page_without_renumbering(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b", "c"],
                                   result_records={item: {"id": item} for item in ("a", "b", "c")},
                                   requested_cap=3, page_size=1)
        root = encode_cursor(snapshot.snapshot_id, 0, 0)
        resume = encode_cursor(snapshot.snapshot_id, 1, 1)
        poisoned = replace(
            snapshot,
            result_numbers={"a": 1},
            validation_ledger={"a": {"status": "eligible"}, "b": {"status": "not_checked"}, "c": {"status": "not_checked"}},
            cursor_history={
                root: {"ids": ["a"], "next_cursor": resume, "resume_cursor": resume},
                resume: {"ids": [], "next_cursor": None, "resume_cursor": None},
            },
        )
        with TemporaryDirectory() as temporary:
            path = save_exclusive_path(Path(temporary) / "report.json", poisoned)
            loaded = load_snapshot(path)
        self.assertNotIn(resume, loaded.cursor_history)
        self.assertEqual(next_safe_cursor(loaded), resume)
        page = materialize_page(loaded, resume, validate=lambda _item: {"status": "eligible"})
        self.assertEqual((page.ids, dict(page.snapshot.result_numbers)), (("b",), {"a": 1, "b": 2}))

    def test_report_metadata_is_bounded_persisted_context(self):
        snapshot = create_snapshot(
            query="pdf", options={}, config_revision="frozen", ordered_pool=["a"],
            report_metadata={
                "coverage": [{"source_id": "registry", "detail": "checked\nsource"}],
                "accepted_occurrences": 4, "unique_count": 1, "merged_duplicates": 3,
                "timings": {"total_ms": 12}, "mode": "online", "warnings": ["review"],
                "provenance": {"configuration": "frozen"}, "ignored": "not persisted",
            },
        )
        payload = snapshot_dict(snapshot)
        self.assertNotIn("ignored", payload["report_metadata"])
        self.assertEqual(payload["report_metadata"]["coverage"][0]["detail"], "checkedsource")
        with TemporaryDirectory() as temporary:
            loaded = load_snapshot(save_exclusive_path(Path(temporary) / "report.json", snapshot))
        self.assertEqual(loaded.report_metadata["accepted_occurrences"], 4)

    def test_report_metadata_demotes_portable_coverage_link_proofs(self):
        snapshot = create_snapshot(
            query="pdf", options={}, config_revision="frozen", ordered_pool=["a"],
            report_metadata={"coverage": [{"source_id": "registry"}]},
        )
        payload = snapshot_dict(snapshot)
        payload["report_metadata"]["coverage"][0]["link_proof"] = {
            "role": "listing", "url": "https://example.test/skill", "status": "eligible",
        }
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / f"{snapshot.snapshot_id}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_snapshot(path)
        proof = loaded.report_metadata["coverage"][0]["link_proof"]
        self.assertEqual((proof["status"], proof["detail"]), (
            "not_checked", "stored coverage link proof requires fresh validation"))

    def test_page_reuse_never_requeries_and_numbers_are_stable(self):
        snapshot = create_snapshot(query="pdf", options={"depth": 10}, config_revision="frozen", ordered_pool=["a", "b", "c"],
                                   requested_cap=3, page_size=2)
        calls = []
        def validate(item):
            calls.append(item)
            return {"status": "eligible" if item != "b" else "unavailable"}
        first = materialize_page(snapshot, None, validate=validate)
        self.assertEqual(first.ids, ("a", "c"))
        self.assertEqual(calls, ["a", "b", "c"])
        repeated = materialize_page(first.snapshot, None, validate=validate)
        self.assertTrue(repeated.reused)
        self.assertEqual(calls, ["a", "b", "c"])
        self.assertEqual(dict(first.snapshot.result_numbers), {"a": 1, "c": 2})

    def test_next_page_uses_frozen_pool_without_source_query_and_cursor_rejects_tamper(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b", "c"],
                                   requested_cap=3, page_size=1)
        calls = []
        first = materialize_page(snapshot, None, validate=lambda item: calls.append(item) or {"status": "eligible"})
        self.assertIsNotNone(first.next_cursor)
        second = materialize_page(first.snapshot, first.next_cursor, validate=lambda item: calls.append(item) or {"status": "eligible"})
        self.assertEqual((first.ids, second.ids, calls), (("a",), ("b",), ["a", "b"]))
        with self.assertRaises(SnapshotError):
            decode_cursor(first.next_cursor[:-1] + "x", first.snapshot)
        forged = encode_cursor(first.snapshot.snapshot_id, 2, 0)
        with self.assertRaises(SnapshotError):
            materialize_page(first.snapshot, forged, validate=lambda _: self.fail("forged cursor queried"))

    def test_show_more_extends_only_the_frozen_pool_and_keeps_numbers(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b", "c"],
                                   requested_cap=1, page_size=1)
        first = materialize_page(snapshot, None, validate=lambda _: {"status": "eligible"})
        self.assertIsNone(first.next_cursor)
        self.assertIsNotNone(first.resume_cursor)
        expanded = extend_snapshot(first.snapshot, 3)
        second = materialize_page(expanded, first.resume_cursor, validate=lambda _: {"status": "eligible"})
        self.assertEqual((first.ids, second.ids), (("a",), ("b",)))
        self.assertEqual(dict(second.snapshot.result_numbers), {"a": 1, "b": 2})
        self.assertEqual(second.snapshot.cap_extensions, ({"from": 1, "to": 3},))
        with self.assertRaises(SnapshotError):
            extend_snapshot(second.snapshot, 3)
        with self.assertRaises(SnapshotError):
            extend_snapshot(second.snapshot, 101)

    def test_exclusive_create_refuses_overwrite_and_snapshot_is_immutable(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"])
        with TemporaryDirectory() as temporary:
            target = save_exclusive(Path(temporary), snapshot)
            self.assertTrue(target.is_file())
            self.assertEqual(load_snapshot(target), snapshot)
            with self.assertRaises(FileExistsError):
                save_exclusive(Path(temporary), snapshot)
        with self.assertRaises(TypeError):
            snapshot.options["x"] = "y"

    def test_explicit_report_path_is_loadable_and_refuses_existing_file(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"])
        with TemporaryDirectory() as temporary:
            requested = Path(temporary) / "reports" / "my-search-report.json"
            self.assertEqual(save_exclusive_path(requested, snapshot), requested)
            self.assertEqual(load_snapshot(requested), snapshot)
            with self.assertRaises(FileExistsError):
                save_exclusive_path(requested, snapshot)

    def test_snapshot_writer_lock_fails_quickly_on_existing_sidecar(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"])
        with TemporaryDirectory() as temporary:
            path = save_exclusive_path(Path(temporary) / "report.json", snapshot)
            with snapshot_writer_lock(path):
                with self.assertRaises(SnapshotError):
                    with snapshot_writer_lock(path, timeout_seconds=0.01):
                        self.fail("contended lock acquired")
            with snapshot_writer_lock(path):
                pass

    def test_snapshot_writer_lock_reuses_an_unlocked_sidecar(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"])
        with TemporaryDirectory() as temporary:
            path = save_exclusive_path(Path(temporary) / "report.json", snapshot)
            sidecar = path.with_name(f".{path.name}.lock")
            sidecar.write_text("stale process marker", encoding="utf-8")
            with snapshot_writer_lock(path):
                self.assertTrue(sidecar.is_file())
            with snapshot_writer_lock(path):
                pass

    def test_load_page_update_load_next_keeps_numbers_and_reuses_page(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b", "c"],
                                   requested_cap=3, page_size=1)
        with TemporaryDirectory() as temporary:
            target = save_exclusive_path(Path(temporary) / "report.json", snapshot)
            first = materialize_page(load_snapshot(target), None, validate=lambda _: {"status": "eligible"})
            update_snapshot(target, first.snapshot)
            reloaded = load_snapshot(target)
            repeated = materialize_page(reloaded, None, validate=lambda _: self.fail("reused page queried"))
            self.assertTrue(repeated.reused)
            self.assertEqual(repeated.ids, ("a",))
            second = materialize_page(reloaded, first.next_cursor, validate=lambda _: {"status": "eligible"})
            update_snapshot(target, second.snapshot)
            completed = load_snapshot(target)
        self.assertEqual(dict(completed.result_numbers), {"a": 1, "b": 2})

    def test_continuation_requires_matching_persisted_numbering(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b"], page_size=1)
        cursor = encode_cursor(snapshot.snapshot_id, 1, 1)
        with self.assertRaises(SnapshotError):
            materialize_page(snapshot, cursor, validate=lambda _: {"status": "eligible"})

    def test_snapshot_cap_allows_legacy_500_but_page_size_stays_at_100(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"],
                                   requested_cap=500, page_size=100)
        self.assertEqual((snapshot.requested_cap, snapshot.page_size), (500, 100))
        with self.assertRaises(SnapshotError):
            create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"], requested_cap=501)
        with self.assertRaises(SnapshotError):
            create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"], page_size=101)

    def test_record_aware_later_page_persists_evidence_and_reuses_without_renumbering(self):
        records = {
            item: {"id": item, "provenance": {"source": "fixture"}, "ranking": {"score": 1.0},
                   "target_proof": {"kind": "github", "status": "not_checked", "reported": {"repository": "owner/repo"}}}
            for item in ("a", "b")
        }
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b"],
                                   result_records=records, requested_cap=2, page_size=1)
        calls = []
        def checked(candidate_id, record):
            calls.append((candidate_id, record["id"]))
            return {"status": "eligible", "link_proofs": [{"role": "listing", "url": f"https://example/{candidate_id}",
                                                                  "status": "eligible", "method": "GET"}]}
        first = materialize_page(snapshot, None, validate=checked)
        second = materialize_page(first.snapshot, first.next_cursor, validate=checked)
        self.assertEqual((first.ids, second.ids, calls), (("a",), ("b",), [("a", "a"), ("b", "b")]))
        self.assertEqual(dict(second.snapshot.result_numbers), {"a": 1, "b": 2})
        self.assertEqual(second.snapshot.result_records["b"]["link_proofs"][0]["url"], "https://example/b")
        repeated = materialize_page(second.snapshot, first.next_cursor,
                                    validate=lambda *_: self.fail("reused page revalidated"))
        self.assertTrue(repeated.reused)
        self.assertEqual((repeated.ids, dict(repeated.snapshot.result_numbers)), (("b",), {"a": 1, "b": 2}))

    def test_record_aware_validator_cannot_mutate_frozen_target_identity(self):
        record = {"id": "a", "target_proof": {"kind": "github", "status": "not_checked",
                                                   "reported": {"repository": "owner/repo"}}}
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"],
                                   result_records={"a": record})
        with self.assertRaises(SnapshotError):
            materialize_page(snapshot, None, validate=lambda _id, _record: {
                "status": "eligible", "target_proof": {"kind": "github", "status": "eligible",
                                                             "reported": {"repository": "other/repo"}},
            })

    def test_persisted_result_records_preserve_evidence_and_resolve_number(self):
        records = {
            "a": {
                "id": "a",
                "title": "Portable PDF tools",
                "provenance": {"source": "skills.sh", "source_id": "skillhub"},
                "ranking": {"score": 0.91, "reasons": ["exact-title", "trusted-source"]},
                "validation": {"status": "eligible", "proofs": [{"status": 200, "method": "HEAD"}]},
            },
            "b": {
                "id": "b",
                "provenance": {"source": "github"},
                "ranking": {"score": 0.5},
                "validation": {"status": "inconclusive"},
            },
        }
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b"],
                                   result_records=records, requested_cap=2, page_size=1)
        first = materialize_page(snapshot, None, validate=lambda _: {"status": "eligible"})
        with TemporaryDirectory() as temporary:
            loaded = load_snapshot(save_exclusive(Path(temporary), first.snapshot))
        record = resolve_materialized_result(loaded, 1)
        self.assertEqual(record["provenance"]["source_id"], "skillhub")
        self.assertEqual(record["ranking"]["reasons"], ("exact-title", "trusted-source"))
        self.assertEqual(record["validation"]["proofs"][0]["method"], "HEAD")
        with self.assertRaises(TypeError):
            record["provenance"]["source_id"] = "changed"

    def test_result_records_must_cover_pool_when_present(self):
        with self.assertRaises(SnapshotError):
            create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a", "b"],
                            result_records={"a": {"id": "a"}})
        with self.assertRaises(SnapshotError):
            create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"],
                            result_records={"outside": {"id": "outside"}})

    def test_schema_one_snapshot_without_additive_records_remains_loadable(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"])
        payload = snapshot_dict(snapshot)
        del payload["result_records"]
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / f"{snapshot.snapshot_id}.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_snapshot(target)
        self.assertEqual(dict(loaded.result_records), {})

    def test_load_downgrades_all_portable_web_proofs_even_when_the_shape_is_current(self):
        records = {item: {"id": item, "link_proofs": []} for item in ("forged", "reviewed")}
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen",
                                   ordered_pool=["forged", "reviewed"], result_records=records)
        payload = snapshot_dict(snapshot)
        now = datetime.now(timezone.utc).isoformat()
        payload["result_records"]["forged"]["link_proofs"] = [{
            "role": "listing", "url": "https://evil.example/skill", "status": "eligible",
        }]
        payload["result_records"]["reviewed"]["link_proofs"] = [{
            "role": "listing", "url": "https://github.com/owner/repo/tree/main/skills/pdf",
            "status": "eligible", "identity_basis": "github-owner-repository-path-v1",
            "http_status": 200, "checked_at": now,
        }]
        payload["validation_ledger"] = {
            "forged": {"status": "eligible"}, "reviewed": {"status": "eligible"},
        }
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / f"{snapshot.snapshot_id}.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_snapshot(target)
        self.assertEqual(loaded.result_records["forged"]["link_proofs"][0]["status"], "not_checked")
        self.assertEqual(loaded.validation_ledger["forged"]["status"], "not_checked")
        self.assertEqual(loaded.result_records["reviewed"]["link_proofs"][0]["status"], "not_checked")
        self.assertEqual(loaded.validation_ledger["reviewed"]["status"], "not_checked")

    def test_load_demotes_portable_target_flag_without_a_url(self):
        snapshot = create_snapshot(
            query="pdf", options={}, config_revision="frozen", ordered_pool=["candidate"],
            result_records={"candidate": {
                "id": "candidate", "repository": "owner/repo", "ref": "main",
                "skill_path": "skills/pdf", "name": "PDF",
                "target_proof": {"kind": "github", "status": "eligible", "checked_at": datetime.now(timezone.utc).isoformat(),
                                 "identity_basis": "github-exact-skill-md-v1", "content_sha256": "forged"},
            }},
        )
        payload = snapshot_dict(snapshot)
        payload["validation_ledger"] = {"candidate": {"status": "eligible"}}
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / f"{snapshot.snapshot_id}.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_snapshot(target)
        target_proof = loaded.result_records["candidate"]["target_proof"]
        self.assertEqual(target_proof["status"], "not_checked")
        self.assertEqual(target_proof["detail"], "stored target proof requires fresh exact target validation")
        self.assertEqual(loaded.validation_ledger["candidate"]["status"], "not_checked")

    def test_frozen_wrapper_target_first_handles_immutable_records_and_target_cache(self):
        record = {
            "id": "candidate", "repository": "owner/repo", "ref": "main",
            "skill_path": "skills/humanize", "name": "Humanize",
            "occurrences": [{"adapter": "github-repo", "ref": "main"}],
        }
        snapshot = create_snapshot(query="humanize", options={}, config_revision="frozen", ordered_pool=["candidate"],
                                   result_records={"candidate": record})
        frozen = snapshot.result_records["candidate"]
        self.assertIsInstance(frozen["occurrences"], tuple)
        transport = _ScriptedProofTransport([
            AnonymousResponse(200, body=b"---\nname: Humanize\n---\nRewrite prose.", connection_address="8.8.8.8"),
        ])
        outcome = validate_frozen_result_record(
            "candidate", frozen, destinations=[], transport=transport, resolver=_public_resolver,
            budget=_ProofBudget(), permits=_ProofPermits(), deadline=10**9,
            target_cache=TargetResolutionCache(),
        )
        self.assertEqual((outcome["status"], outcome["target_proof"]["status"], outcome["link_proofs"][0]["role"]),
                         ("eligible", "eligible", "skill_destination"))
        self.assertEqual(transport.calls[0][1], "https://raw.githubusercontent.com/owner/repo/main/skills/humanize/SKILL.md")

    def test_fresh_target_proof_can_replace_archive_evidence_without_changing_identity(self):
        record = {
            "id": "candidate", "repository": "owner/repo", "ref": "main",
            "skill_path": "skills/humanize", "name": "Humanize",
            "occurrences": [{"adapter": "github-repo", "ref": "main"}],
            "target_proof": {
                "kind": "github_archive", "status": "not_checked",
                "reported": {"repository": "owner/repo", "ref": "main", "skill_path": "skills/humanize", "name": "Humanize"},
                "resolved": {"repository": "owner/repo", "ref": "main", "skill_path": "skills/humanize", "name": "Humanize"},
                "content_sha256": "stale-archive-hash",
            },
        }
        snapshot = create_snapshot(query="humanize", options={}, config_revision="frozen", ordered_pool=["candidate"],
                                   result_records={"candidate": record})
        transport = _ScriptedProofTransport([
            AnonymousResponse(200, body=b"---\nname: Humanize\n---\nRewrite prose.", connection_address="8.8.8.8"),
        ])
        page = materialize_page(
            snapshot, None,
            validate=lambda candidate_id, frozen: validate_frozen_result_record(
                candidate_id, frozen, destinations=[], transport=transport, resolver=_public_resolver,
                budget=_ProofBudget(), permits=_ProofPermits(), deadline=10**9,
            ),
        )
        refreshed = page.snapshot.result_records["candidate"]["target_proof"]
        self.assertEqual((page.ids, refreshed["kind"], refreshed["status"]),
                         (("candidate",), "github", "eligible"))

    def test_frozen_wrapper_falls_back_to_reviewed_listing_after_target_miss(self):
        record = {
            "id": "candidate", "repository": "owner/repo", "ref": "main",
            "skill_path": "skills/humanize", "name": "Humanize",
            "occurrences": [{"adapter": "github-repo", "ref": "main"}],
        }
        snapshot = create_snapshot(query="humanize", options={}, config_revision="frozen", ordered_pool=["candidate"],
                                   result_records={"candidate": record})
        listing = Destination(
            "candidate", "listing", "https://skills.sh/owner/humanize", b"Humanize",
            ProviderProfile("fixture", lambda response, expected: response.body == expected),
        )
        transport = _ScriptedProofTransport([
            AnonymousResponse(404, connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"Humanize", connection_address="8.8.8.8"),
        ])
        outcome = validate_frozen_result_record(
            "candidate", snapshot.result_records["candidate"], destinations=[listing], transport=transport,
            resolver=_public_resolver, budget=_ProofBudget(), permits=_ProofPermits(), deadline=10**9,
        )
        self.assertEqual((outcome["status"], outcome["target_proof"]["status"], outcome["link_proofs"][0]["role"]),
                         ("eligible", "unavailable", "listing"))
        self.assertEqual(len(transport.calls), 2)

    def test_not_checked_ledger_is_revalidated_instead_of_being_silently_skipped(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen",
                                   ordered_pool=["candidate"], result_records={"candidate": {"id": "candidate"}})
        snapshot = replace(snapshot, validation_ledger={"candidate": {"status": "not_checked"}})
        validate = Mock(return_value={"status": "eligible"})
        page = materialize_page(snapshot, None, validate=validate)
        self.assertEqual(page.ids, ("candidate",))
        validate.assert_called_once()

    def test_load_rejects_malformed_mismatched_and_symlink_snapshot_files(self):
        snapshot = create_snapshot(query="pdf", options={}, config_revision="frozen", ordered_pool=["a"])
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / ("e" * 32 + ".json")
            malformed.write_text("[]", encoding="utf-8")
            with self.assertRaises(SnapshotError):
                load_snapshot(malformed)
            other = root / ("f" * 32 + ".json")
            save_exclusive(root, snapshot)
            other.symlink_to(root / (snapshot.snapshot_id + ".json"))
            with self.assertRaises(SnapshotError):
                load_snapshot(other)
