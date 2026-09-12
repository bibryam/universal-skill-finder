from __future__ import annotations

import sys
import threading
import time
import unittest
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.http import FinderHttpError
from universal_skill_finder.models import Candidate, LinkProof
from universal_skill_finder.presentation import render_report
from universal_skill_finder.runtime import Deadline, NetworkPolicy, PermitPool, RequestBudget, SourceJob, WorkerSupervisor
from universal_skill_finder.validation import AnonymousPublicTransport, AnonymousResponse, reviewed_destination


def registry_source(source_id: str, base_url: str) -> dict[str, object]:
    return {
        "id": source_id, "adapter": "skills-sh", "kind": "registry", "base_url": base_url,
        "effective_enabled": True, "enabled": True, "trust": "community-index",
    }


class FairFederationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        config = EffectiveConfig(settings={}, packs=[], sources=[], overlay_path=root / "sources.json", overlay={})
        self.finder = UniversalSkillFinder(config, cache=Cache(root / "cache"))

    def test_initial_rounds_start_each_origin_before_a_repeat(self):
        first = registry_source("a-one", "https://one.example")
        second = registry_source("a-two", "https://one.example")
        third = registry_source("b-one", "https://two.example")
        fourth = registry_source("b-two", "https://two.example")
        order = self.finder._fair_admission_order(
            [second, fourth, first, third], "pdf forms", 10, offline=False, refresh=False
        )
        self.assertEqual([source["id"] for source in order], ["a-one", "b-one", "a-two", "b-two"])

    def test_query_cache_partitions_nonsecret_effective_auth_mode(self):
        source = registry_source("auth-cache", "https://skills.sh")
        source["auth_env"] = "UNIVERSAL_SKILL_FINDER_TEST_OPTIONAL_TOKEN"
        with patch.dict("os.environ", {"UNIVERSAL_SKILL_FINDER_TEST_OPTIONAL_TOKEN": ""}, clear=False):
            anonymous = self.finder._cache_key(source, "pdf", 10)
        with patch.dict("os.environ", {"UNIVERSAL_SKILL_FINDER_TEST_OPTIONAL_TOKEN": "synthetic"}, clear=False):
            authenticated = self.finder._cache_key(source, "pdf", 10)
        self.assertNotEqual(anonymous, authenticated)

    def test_github_api_health_scope_shares_quota_cooldown_across_source_aliases(self):
        first = registry_source("github-one", "https://api.github.com")
        second = registry_source("github-two", "https://api.github.com")
        first["adapter"] = second["adapter"] = "github-code-search"
        first_scope = self.finder._health_scope(first)
        second_scope = self.finder._health_scope(second)
        self.assertIsNotNone(first_scope)
        self.assertEqual(first_scope.key(), second_scope.key())
        self.finder.health.record_rate_limit(first_scope, time.time() + 60)
        self.assertEqual(self.finder.health.before_request(second_scope).status, "rate_cooldown")

    def test_destination_proof_cache_uses_shorter_negative_ttl(self):
        class Transport:
            def __init__(self):
                self.calls = 0

            def request(self, _method, url, **_kwargs):
                self.calls += 1
                if url.endswith("/missing"):
                    return AnonymousResponse(404, body=b"{}", connection_address="8.8.8.8")
                return AnonymousResponse(200, body=b'{"id":"one"}', connection_address="8.8.8.8")

        root = Path(self.temporary.name)
        transport = Transport()
        finder = UniversalSkillFinder(
            EffectiveConfig(settings={}, packs=[], sources=[], overlay_path=root / "sources.json", overlay={}),
            cache=Cache(root / "cache"), validation_transport=transport,
            validation_resolver=lambda _host, _port: ("8.8.8.8",),
        )
        policy = NetworkPolicy.for_mode()
        deadline = Deadline.start(policy)
        budget = RequestBudget(requests=10, validation_requests=10)
        permits = PermitPool(policy)

        def proof(destination):
            return finder._cached_destination_proof(
                destination, phase="final", budget=budget, permits=permits, deadline=deadline,
            )

        positive = reviewed_destination("one", role="listing", url="https://skills.sh/one",
                                        adapter="skills-sh", expected_identity={"id": "one"})
        self.assertEqual(proof(positive).status, "eligible")
        positive_key = finder.cache.key("proof-v1", positive.role, positive.url, positive.profile.name,
                                        json.dumps(positive.expected_identity, sort_keys=True, default=str))
        positive_path = finder.cache._path("proofs", positive_key)
        old = time.time() - 61
        os.utime(positive_path, (old, old))
        self.assertEqual(proof(positive).status, "eligible")
        self.assertEqual(transport.calls, 1)

        negative = reviewed_destination("missing", role="listing", url="https://skills.sh/missing",
                                        adapter="skills-sh", expected_identity={"id": "missing"})
        self.assertEqual(proof(negative).status, "unavailable")
        negative_key = finder.cache.key("proof-v1", negative.role, negative.url, negative.profile.name,
                                        json.dumps(negative.expected_identity, sort_keys=True, default=str))
        negative_path = finder.cache._path("proofs", negative_key)
        os.utime(negative_path, (old, old))
        self.assertEqual(proof(negative).status, "unavailable")
        self.assertEqual(transport.calls, 3)

    def test_rate_cooldown_accepts_http_date_and_github_reset(self):
        date_error = FinderHttpError("rate limited", status=429,
                                     headers={"Retry-After": "Wed, 21 Oct 2037 07:28:00 GMT"})
        reset_error = FinderHttpError("rate limited", status=429,
                                      headers={"X-RateLimit-Reset": "2147483647"})
        self.assertIsNotNone(self.finder._retry_at(date_error))
        self.assertIsNotNone(self.finder._retry_at(reset_error))

    def test_partial_rate_limit_retains_candidates_and_reports_cooldown(self):
        class Adapter:
            def search(self, source, query, limit, context):
                context.incomplete_results = True
                context.detail = "fixture returned a useful prefix before rate limiting"
                context.rate_limit_headers = {}
                row = Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh")
                row.link_proofs = [{"role": "listing", "status": "eligible", "url": "https://skills.sh/one"}]
                return [row]

        root = Path(self.temporary.name)
        source = registry_source("limited", "https://skills.sh")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"), http=object())
        finder.adapter_map = {"skills-sh": Adapter()}
        report = finder.search("pdf forms")
        self.assertEqual(len(report.results), 0)
        self.assertEqual(report.unique_count, 1)
        self.assertEqual(report.preview_count, 1)
        self.assertTrue(report.coverage[0].incomplete_results)
        self.assertEqual(report.coverage[0].health_status, "rate_cooldown")
        self.assertIsNotNone(report.coverage[0].cooldown_until)

    def test_round_plan_caps_each_origin_at_two_starts(self):
        shared = [registry_source(f"shared-{index}", "https://shared.example") for index in range(4)]
        other = [registry_source(f"other-{index}", f"https://other-{index}.example") for index in range(4)]
        rounds = self.finder._initial_admission_rounds(shared + other, "pdf", 10, offline=False, refresh=False, workers=6)
        first_shared = [source for source in rounds[0] if source["base_url"] == "https://shared.example"]
        self.assertEqual(len(first_shared), 2)
        self.assertEqual(sum(len(round_) for round_ in rounds), 8)

    def test_frozen_pool_rejects_late_publication(self):
        deadline = Deadline.start(NetworkPolicy.for_mode(), now=0.0)
        pool = WorkerSupervisor(deadline)
        pool.submit(SourceJob("source", "https://source.example"), now=0.0)
        pool.started("source", now=0.0)
        self.assertTrue(pool.publish("source", [], now=0.0))
        accepted, _outcomes = pool.freeze()
        self.assertEqual(accepted, {"source": []})
        self.assertFalse(pool.publish("source", [], now=0.0))

    def test_schema2_count_progress_and_offline_previews_are_honest(self):
        class Adapter:
            def __init__(self):
                self.calls = 0

            def search(self, source, query, limit, context):
                self.calls += 1
                first = Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh", skill_path="one")
                first.canonical_url = "https://unverified.example/skills/pdf-forms"
                return [
                    first,
                    Candidate("two", "PDF reader", "Read PDF forms", source["id"], "registry", "skills-sh", skill_path="two"),
                ]

        root = Path(self.temporary.name)
        source = registry_source("cacheable", "https://one.example")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        adapter = Adapter()
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"))
        finder.adapter_map = {"skills-sh": adapter}
        events = []
        online = finder.search("pdf forms", count=2, page_size=1, thorough=True, progress_callback=events.append)
        self.assertEqual((online.requested_count, online.page_size, len(online.results)), (2, 1, 0))
        self.assertEqual(online.not_checked_count, 2)
        self.assertEqual(online.eligible_count, 0)
        self.assertEqual(online.preview_count, 2)
        self.assertTrue(all("url" not in preview for preview in online.candidate_previews))
        self.assertEqual(online.snapshot["status"], "available_to_persist")
        self.assertEqual(len(online.snapshot["ranking_pool"]), 2)
        self.assertEqual(online.snapshot["ordered_pool"], list(online.snapshot["result_records"]))
        self.assertEqual(set(online.snapshot["ranking_traces"]), set(online.snapshot["ordered_pool"]))
        self.assertEqual(online.timings["collection_cutoff_seconds"], 20.0)
        self.assertEqual([event.type for event in events], [
            "search_started", "source_finished", "ranking_started", "validation_started", "validation_finished", "search_finished",
        ])

        offline = finder.search("pdf forms", count=2, page_size=2, offline=True)
        self.assertEqual(offline.mode, "offline_preview")
        self.assertEqual(offline.results, [])
        self.assertEqual(offline.preview_count, 2)
        self.assertTrue(all("result_number" not in preview for preview in offline.candidate_previews))
        self.assertEqual(adapter.calls, 1)

    def test_hung_injected_source_returns_at_cutoff_without_late_cache_publication(self):
        class HungAdapter:
            def search(self, source, query, limit, context):
                time.sleep(0.20)
                return [Candidate("late", "Late PDF", "Late result", source["id"], "registry", "skills-sh")]

        root = Path(self.temporary.name)
        source = registry_source("hung", "https://hung.example")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        cache = Cache(root / "cache")
        finder = UniversalSkillFinder(config, cache=cache)
        finder.adapter_map = {"skills-sh": HungAdapter()}
        short_policy = NetworkPolicy(0.05, 0.10, 0.05)
        started = time.monotonic()
        with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=short_policy):
            report = finder.search("pdf forms")
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertEqual(report.results, [])
        self.assertEqual(report.coverage[0].status, "deadline_exceeded")
        self.assertEqual(cache.metadata("queries"), [])
        time.sleep(0.22)
        self.assertEqual(cache.metadata("queries"), [])
        self.assertEqual(report.snapshot["ordered_pool"], [])

    def test_final_and_footer_proofs_share_one_freeze_anchored_deadline(self):
        class Adapter:
            def search(self, source, query, limit, context):
                return [Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh")]

        root = Path(self.temporary.name)
        source = registry_source("deadline", "https://skills.sh")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"), http=object(), validation_transport=AnonymousPublicTransport())
        finder.adapter_map = {"skills-sh": Adapter()}
        observed = {}

        def validate(_results, _deadline, _policy, **kwargs):
            observed["final"] = kwargs["validation_deadline"]

        def footer(_destination, **kwargs):
            observed["footer"] = kwargs["deadline"]
            return LinkProof("repository", "https://github.com/bibryam/universal-skill-finder", "eligible")

        finder._validate_ranked_pool = validate  # type: ignore[method-assign]
        finder._cached_destination_proof = footer  # type: ignore[method-assign]
        finder.search("pdf forms")

        self.assertIs(observed["final"], observed["footer"])

    def test_final_validation_cap_is_not_restarted_after_local_ranking(self):
        class Adapter:
            def search(self, source, query, limit, context):
                return [Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh")]

        root = Path(self.temporary.name)
        source = registry_source("ranking-delay", "https://skills.sh")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"), http=object())
        finder.adapter_map = {"skills-sh": Adapter()}
        original_merge = finder._merge

        def slow_merge(candidates, query):
            time.sleep(0.06)
            return original_merge(candidates, query)

        observed = {}
        finder._merge = slow_merge  # type: ignore[method-assign]
        finder._validate_ranked_pool = lambda *_args, **kwargs: observed.setdefault(
            "deadline", kwargs["validation_deadline"]
        )  # type: ignore[method-assign]
        policy = NetworkPolicy(0.20, 0.20, 0.03)

        with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=policy):
            finder.search("pdf forms")

        self.assertEqual(observed["deadline"].remaining(), 0.0)

    def test_preview_executor_joins_before_search_returns(self):
        class Adapter:
            def search(self, source, query, limit, context):
                row = Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh")
                row.listing_url = "https://skills.sh/one"
                row.listing_role = "listing"
                row.source_evidence = {"expected_identity": {"id": "one"}}
                return [row]

        class SlowTransport:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.finished = threading.Event()
                self.calls = 0

            def request(self, _method, _url, **_kwargs):
                self.calls += 1
                self.entered.set()
                if not self.release.wait(5.0):
                    raise RuntimeError("fixture release timed out")
                self.finished.set()
                return AnonymousResponse(200, body=b'{"id":"one"}', connection_address="8.8.8.8")

        root = Path(self.temporary.name)
        source = registry_source("preview", "https://skills.sh")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        transport = SlowTransport()
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"), http=object(), validation_transport=transport,
                            validation_resolver=lambda _host, _port: ("8.8.8.8",))
        finder.adapter_map = {"skills-sh": Adapter()}
        finder._validate_ranked_pool = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        policy = NetworkPolicy(1.0, 2.0, 1.0)
        outcome = {}
        done = threading.Event()

        def run_search():
            try:
                outcome["report"] = finder.search("pdf forms", preview=True)
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                done.set()

        with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=policy):
            worker = threading.Thread(target=run_search, name="preview-join-fixture")
            worker.start()
            try:
                self.assertTrue(transport.entered.wait(2.0), "preview validation did not start")
                self.assertFalse(done.is_set(), "search returned while preview validation was active")
            finally:
                transport.release.set()
            self.assertTrue(done.wait(2.0), "search did not return after preview validation finished")
            worker.join(timeout=0.1)

        self.assertFalse(worker.is_alive())
        if "error" in outcome:
            raise outcome["error"]

        self.assertTrue(transport.finished.is_set())
        self.assertEqual(transport.calls, 1)

    def test_remote_and_repository_cache_targets_cannot_authorize_commands(self):
        for adapter, kind in (("skills-sh", "registry"), ("github-repo", "repository")):
            with self.subTest(adapter=adapter):
                row = Candidate("one", "PDF forms", "Fill PDF forms", "forged", kind, adapter)
                row.target_proof = {
                    "kind": "github", "status": "eligible", "content_sha256": "a" * 64,
                    "method": "anonymous_exact_skill_md_get", "identity_basis": "github-exact-skill-md-v1",
                    "url": "https://raw.githubusercontent.com/example/skills/main/pdf/SKILL.md",
                    "resolved": {"repository": "example/skills", "ref": "main", "skill_path": "pdf"},
                    "actual_name": "pdf",
                }
                source = {"id": "real-source", "adapter": adapter, "kind": kind}
                checked = UniversalSkillFinder._validated_candidates([row.to_dict()], source)[0]
                self.assertEqual(checked.target_proof["status"], "not_checked")
                self.assertEqual(checked.source_id, "real-source")

    def test_builtin_local_source_uses_process_boundary_and_preserves_content_proof(self):
        root = Path(self.temporary.name)
        skill = root / "local" / "humanizer"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: Humanizer\ndescription: Humanize prose\n---\n", encoding="utf-8")
        source = {
            "id": "local", "adapter": "local-directory", "kind": "repository", "path": str(root / "local"),
            "effective_enabled": True, "enabled": True, "trust": "user-configured",
        }
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"))
        self.assertTrue(finder._uses_builtin_adapter(source))
        report = finder.search("humanize")
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].target_proof["status"], "eligible")
        self.assertEqual(report.coverage[0].status, "ok")
        rendered = render_report(report, assistant="codex")
        self.assertIn("**Location:** Local directory › humanizer", rendered)
        self.assertIn("**Found on:** local (local directory)", rendered)
        self.assertIn("Installation unavailable: local directory installation requires manual review", rendered)
        self.assertNotIn("file://", rendered)

    def test_preempted_injected_threads_do_not_exceed_physical_worker_cap(self):
        class Adapter:
            def __init__(self):
                self.calls = 0
                self.active = 0
                self.peak_active = 0
                self.lock = threading.Lock()

            def search(self, source, query, limit, context):
                with self.lock:
                    self.calls += 1
                    self.active += 1
                    self.peak_active = max(self.peak_active, self.active)
                try:
                    if source["id"].startswith("hang"):
                        time.sleep(0.50)
                        return []
                    return [Candidate(source["id"], "Healthy PDF", "Fill PDF forms", source["id"], "registry", "skills-sh")]
                finally:
                    with self.lock:
                        self.active -= 1

        root = Path(self.temporary.name)
        sources = [registry_source(f"hang-{index}", f"https://{chr(96 + index)}.example") for index in range(1, 7)]
        sources.extend(registry_source(f"healthy-{index}", f"https://{chr(96 + index)}.example") for index in range(7, 10))
        config = EffectiveConfig(settings={"max_workers": 6}, packs=[], sources=sources, overlay_path=root / "sources.json", overlay={})
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"))
        adapter = Adapter()
        finder.adapter_map = {"skills-sh": adapter}
        policy = NetworkPolicy(0.20, 0.30, 0.05)
        with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=policy):
            report = finder.search("pdf forms")
        # Preemption is logical for injected adapters. The uncancellable first
        # wave remains physically running, so later sources are honestly left
        # unstarted instead of being queued behind it.
        self.assertEqual(adapter.calls, 6)
        self.assertLessEqual(adapter.peak_active, 6)
        self.assertTrue(any(row.status == "preempted" for row in report.coverage[:6]))
        self.assertTrue(all(row.status != "ok" for row in report.coverage if row.source_id.startswith("healthy-")))
        self.assertLess(report.timings["collection_ms"], 350)
        time.sleep(0.55)
        self.assertEqual(adapter.calls, 6)

    def test_reviewed_listing_validation_promotes_only_exact_identity(self):
        class Adapter:
            def search(self, source, query, limit, context):
                row = Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh")
                row.listing_url = "https://skills.sh/one"
                row.listing_role = "listing"
                row.source_evidence = {"expected_identity": {"id": "one"}}
                return [row]

        class Transport:
            calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                return AnonymousResponse(200, body=b'{"id":"one"}', connection_address="8.8.8.8")

        root = Path(self.temporary.name)
        source = registry_source("reviewed", "https://skills.sh")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"))
        finder.adapter_map = {"skills-sh": Adapter()}
        transport = Transport()
        finder.validation_transport = transport
        finder.validation_resolver = lambda _host, _port: ("8.8.8.8",)
        report = finder.search("pdf forms")
        self.assertEqual(transport.calls, 1)
        self.assertEqual((report.eligible_count, len(report.results)), (1, 1))
        self.assertNotEqual(report.results[0].target_proof.get("status"), "eligible")
        self.assertEqual(report.results[0].link_proofs[-1]["status"], "eligible")
        self.assertEqual(report.results[0].attributions[0]["role"], "listing")
        cached = finder.search("pdf forms")
        self.assertEqual(cached.results[0].validation_status, "eligible")
        self.assertEqual(cached.results[0].link_proofs[-1]["cache_age_seconds"], 0)
        self.assertEqual(transport.calls, 1)

    def test_unavailable_listing_falls_through_to_exact_github_destination(self):
        class Adapter:
            def search(self, source, query, limit, context):
                row = Candidate(
                    "one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh",
                    repository="owner/repo", ref="main", skill_path="skills/pdf-forms",
                )
                row.listing_url = "https://skills.sh/missing-pdf-forms"
                row.listing_role = "listing"
                row.source_evidence = {"expected_identity": {"id": "missing-pdf-forms"}}
                return [row]

        class Transport:
            def __init__(self):
                self.urls = []

            def request(self, _method, url, **_kwargs):
                self.urls.append(url)
                if url.startswith("https://skills.sh/"):
                    return AnonymousResponse(404, body=b"{}", connection_address="8.8.8.8")
                return AnonymousResponse(
                    200,
                    body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">',
                    connection_address="8.8.8.8",
                )

        root = Path(self.temporary.name)
        source = registry_source("fallback", "https://skills.sh")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        transport = Transport()
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"), validation_transport=transport,
                            validation_resolver=lambda _host, _port: ("8.8.8.8",))
        finder.adapter_map = {"skills-sh": Adapter()}

        report = finder.search("pdf forms")

        self.assertEqual(transport.urls, [
            "https://skills.sh/missing-pdf-forms",
            "https://raw.githubusercontent.com/owner/repo/main/skills/pdf-forms/SKILL.md",
            "https://github.com/owner/repo/tree/main/skills/pdf-forms",
        ])
        self.assertEqual(report.results[0].validation_status, "eligible")
        self.assertEqual(report.results[0].link_proofs[-1]["role"], "skill_destination")
        self.assertEqual(report.results[0].link_proofs[-1]["status"], "eligible")

    def test_exact_target_keeps_all_source_listing_proofs_and_attributions(self):
        class Adapter:
            def search(self, source, query, limit, context):
                source_id = source["id"]
                return [Candidate(
                    f"{source_id}-native", "PDF forms", "Fill PDF forms", source_id, "registry", "skills-sh",
                    repository="owner/repo", ref="main", skill_path="skills/pdf-forms",
                    listing_url=f"https://skills.sh/owner/repo/{source_id}", listing_role="listing",
                    listing_derivation="source_provided",
                )]

        class Transport:
            def __init__(self):
                self.urls = []

            def request(self, _method, url, **_kwargs):
                self.urls.append(url)
                if url.startswith("https://raw.githubusercontent.com/"):
                    return AnonymousResponse(
                        200, body=b"---\nname: PDF forms\n---\nFill PDF forms.", connection_address="8.8.8.8",
                    )
                if url.startswith("https://github.com/"):
                    return AnonymousResponse(
                        200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">',
                        connection_address="8.8.8.8",
                    )
                terminal = url.rsplit("/", 1)[-1]
                return AnonymousResponse(
                    200, body=f"<nav>owner/repo</nav><h1>{terminal}</h1>".encode(),
                    connection_address="8.8.8.8",
                )

        root = Path(self.temporary.name)
        sources = [registry_source("first", "https://skills.sh"), registry_source("second", "https://skills.sh")]
        config = EffectiveConfig(settings={}, packs=[], sources=sources, overlay_path=root / "sources.json", overlay={})
        transport = Transport()
        finder = UniversalSkillFinder(
            config, cache=Cache(root / "multi-source-cache"), validation_transport=transport,
            validation_resolver=lambda _host, _port: ("8.8.8.8",),
        )
        finder.adapter_map = {"skills-sh": Adapter()}

        report = finder.search("pdf forms")

        self.assertEqual(len(report.results), 1)
        result = report.results[0]
        self.assertEqual(result.validation_status, "eligible")
        self.assertEqual(
            {(proof["role"], proof["url"]) for proof in result.link_proofs if proof["status"] == "eligible"},
            {
                ("skill_destination", "https://github.com/owner/repo/tree/main/skills/pdf-forms"),
                ("repository", "https://github.com/owner/repo"),
                ("listing", "https://skills.sh/owner/repo/first"),
                ("listing", "https://skills.sh/owner/repo/second"),
            },
        )
        self.assertEqual(
            {(row["source_id"], row["url"]) for row in result.attributions},
            {
                ("first", "https://skills.sh/owner/repo/first"),
                ("second", "https://skills.sh/owner/repo/second"),
            },
        )
        self.assertFalse(any(url.startswith("https://raw.githubusercontent.com/")
                             for _role, url in {(proof["role"], proof["url"] or "") for proof in result.link_proofs}))

    def test_preview_emits_only_early_verified_unnumbered_evidence_without_changing_pool(self):
        class Adapter:
            def search(self, source, query, limit, context):
                if source["id"] == "slow":
                    time.sleep(0.08)
                row = Candidate(source["id"], f"{source['id']} PDF", "Fill PDF forms", source["id"], "registry", "skills-sh")
                row.listing_url = f"https://skills.sh/{source['id']}"
                row.listing_role = "listing"
                row.source_evidence = {"expected_identity": {"id": source["id"]}}
                return [row]

        class Transport:
            def __init__(self):
                self.calls = 0

            def request(self, _method, url, **_kwargs):
                self.calls += 1
                native_id = url.rsplit("/", 1)[-1]
                return AnonymousResponse(200, body=(f'{{"id":"{native_id}"}}').encode(), connection_address="8.8.8.8")

        root = Path(self.temporary.name)
        sources = [registry_source("fast", "https://skills.sh"), registry_source("slow", "https://skills.sh")]
        config = EffectiveConfig(settings={}, packs=[], sources=sources, overlay_path=root / "sources.json", overlay={})
        preview_transport = Transport()
        finder = UniversalSkillFinder(config, cache=Cache(root / "preview-cache"), validation_transport=preview_transport,
                            validation_resolver=lambda _host, _port: ("8.8.8.8",))
        finder.adapter_map = {"skills-sh": Adapter()}
        events = []
        report = finder.search("pdf forms", preview=True, progress_callback=events.append)
        previews = [event for event in events if event.type == "early_verified"]
        self.assertTrue(previews)
        self.assertLessEqual(len(previews), 3)
        self.assertTrue(all(event.link_proof["status"] == "eligible" for event in previews))
        self.assertTrue(all(not hasattr(event, "result_number") for event in previews))
        self.assertEqual(set(report.snapshot["ordered_pool"]), set(report.snapshot["result_records"]))
        quiet_transport = Transport()
        quiet = UniversalSkillFinder(config, cache=Cache(root / "quiet-cache"), validation_transport=quiet_transport,
                           validation_resolver=lambda _host, _port: ("8.8.8.8",))
        quiet.adapter_map = {"skills-sh": Adapter()}
        quiet_events = []
        quiet_report = quiet.search("pdf forms", preview=False, progress_callback=quiet_events.append)
        self.assertFalse(any(event.type == "early_verified" for event in quiet_events))
        self.assertEqual(report.snapshot["ordered_pool"], quiet_report.snapshot["ordered_pool"])
        self.assertEqual(preview_transport.calls, quiet_transport.calls)

    def test_verified_github_tree_is_inspection_only_without_exact_skill_content(self):
        class Adapter:
            def search(self, source, query, limit, context):
                return [Candidate(
                    "one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh",
                    repository="owner/repo", ref="main", skill_path="skills/pdf-forms",
                )]

        class Transport:
            def request(self, *args, **kwargs):
                return AnonymousResponse(
                    200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">',
                    connection_address="8.8.8.8",
                )

        root = Path(self.temporary.name)
        source = registry_source("registry", "https://skills.sh")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"))
        finder.adapter_map = {"skills-sh": Adapter()}
        finder.validation_transport = Transport()
        finder.validation_resolver = lambda _host, _port: ("8.8.8.8",)
        report = finder.search("pdf forms")
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].validation_status, "eligible")
        self.assertNotEqual(report.results[0].target_proof.get("status"), "eligible")
        self.assertEqual(report.results[0].install["kind"], "github")
        self.assertIn("Installation unavailable", render_report(report))

    def test_custom_discovery_http_does_not_enable_live_destination_networking(self):
        class Adapter:
            def search(self, source, query, limit, context):
                return [Candidate(
                    "one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh",
                    repository="owner/repo", ref="main", skill_path="skills/pdf-forms",
                )]

        root = Path(self.temporary.name)
        source = registry_source("fixture", "https://skills.sh")
        config = EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={})
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"), http=object())
        finder.adapter_map = {"skills-sh": Adapter()}
        self.assertIsNone(finder.validation_transport)
        report = finder.search("pdf forms")
        self.assertEqual(report.results, [])
        self.assertEqual(report.not_checked_count, 1)


if __name__ == "__main__":
    unittest.main()
