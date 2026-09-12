from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate
from universal_skill_finder.runtime import NetworkPolicy
from universal_skill_finder.validation import AnonymousResponse


class ProvisionalDeadlineTests(unittest.TestCase):
    def test_blocked_injected_preview_returns_at_deadline_and_cannot_publish_late_proof(self):
        """A noncompliant transport must not delay output or backfill proof cache."""
        class Adapter:
            def search(self, source, query, limit, context):
                row = Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh")
                row.listing_url = "https://skills.sh/one"
                row.listing_role = "listing"
                row.source_evidence = {"expected_identity": {"id": "one"}}
                return [row]

        class BlockedTransport:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.finished = threading.Event()

            def request(self, _method, _url, **_kwargs):
                self.entered.set()
                self.release.wait(5.0)
                self.finished.set()
                return AnonymousResponse(200, body=b'{"id":"one"}', connection_address="8.8.8.8")

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = {
                "id": "preview", "adapter": "skills-sh", "kind": "registry", "base_url": "https://skills.sh",
                "effective_enabled": True, "enabled": True, "trust": "community-index",
            }
            transport = BlockedTransport()
            cache = Cache(root / "cache")
            finder = UniversalSkillFinder(
                EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={}),
                cache=cache, http=object(), validation_transport=transport,
                validation_resolver=lambda _host, _port: ("8.8.8.8",),
            )
            finder.adapter_map = {"skills-sh": Adapter()}
            # This regression isolates provisional joins. Final validation has
            # its own bounded scheduler and is not the blocked worker here.
            finder._validate_ranked_pool = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            proof_finished = threading.Event()
            cached_destination_proof = finder._cached_destination_proof

            def tracked_destination_proof(*args, **kwargs):
                try:
                    return cached_destination_proof(*args, **kwargs)
                finally:
                    proof_finished.set()

            finder._cached_destination_proof = tracked_destination_proof  # type: ignore[method-assign]
            # Leave ample time for the request to start on a loaded runner; the
            # short validation tail still proves that a blocked callback cannot
            # hold the result until the fixture's five-second release timeout.
            policy = NetworkPolicy(1.0, 2.0, 0.10)
            outcome = {}
            done = threading.Event()

            def run_search():
                try:
                    outcome["report"] = finder.search("pdf forms", preview=True)
                except BaseException as exc:
                    outcome["error"] = exc
                finally:
                    done.set()

            try:
                with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=policy):
                    worker = threading.Thread(target=run_search, name="blocked-preview-fixture")
                    worker.start()
                    self.assertTrue(transport.entered.wait(2.0), "preview validation did not start")
                    self.assertTrue(done.wait(2.0), "search waited for a noncompliant preview transport")
                self.assertFalse(transport.finished.is_set())
                self.assertFalse(proof_finished.is_set())
                if "error" in outcome:
                    raise outcome["error"]
                report = outcome["report"]
                self.assertEqual(list((cache.root / "proofs").glob("*.json")), [])
                self.assertEqual(report.notes, [
                    "An optional early destination check did not complete; only final eligible results are shown."
                ])
            finally:
                transport.release.set()
                self.assertTrue(transport.finished.wait(2.0))
                # Wait for the entire detached proof task, including cache and
                # lease cleanup, before TemporaryDirectory removes its root.
                self.assertTrue(proof_finished.wait(2.0))
                worker.join(timeout=0.1)
            self.assertFalse(worker.is_alive())
            # The late worker may complete after `search` returned, but must
            # not turn that old response into durable checked proof evidence.
            self.assertEqual(list((cache.root / "proofs").glob("*.json")), [])

    def test_failed_optional_preview_is_nonfatal_and_never_relays_exception_text(self):
        class Adapter:
            def search(self, source, query, limit, context):
                row = Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh")
                row.listing_url = "https://skills.sh/one"
                row.listing_role = "listing"
                row.source_evidence = {"expected_identity": {"id": "one"}}
                return [row]

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = {
                "id": "preview", "adapter": "skills-sh", "kind": "registry", "base_url": "https://skills.sh",
                "effective_enabled": True, "enabled": True, "trust": "community-index",
            }
            finder = UniversalSkillFinder(
                EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={}),
                cache=Cache(root / "cache"), http=object(), validation_transport=object(),
            )
            finder.adapter_map = {"skills-sh": Adapter()}
            finder._validate_ranked_pool = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            policy = NetworkPolicy(0.10, 0.20, 0.05)
            # Synthetic redaction sentinel: it must never reach report output.
            secret = "fixture-secret-preview-error"  # pragma: allowlist secret - synthetic exception-redaction fixture
            with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=policy), \
                 patch.object(finder, "_cached_destination_proof", side_effect=RuntimeError(secret)):
                report = finder.search("pdf forms", preview=True)

        self.assertEqual(report.notes, [
            "An optional early destination check did not complete; only final eligible results are shown."
        ])
        self.assertNotIn(secret, str(report.to_dict()))
        self.assertNotIn(secret, "\n".join(report.notes))

    def test_exception_after_preview_scheduling_revokes_late_cache_publication(self):
        class Adapter:
            def search(self, source, query, limit, context):
                row = Candidate("one", "PDF forms", "Fill PDF forms", source["id"], "registry", "skills-sh")
                row.listing_url = "https://skills.sh/one"
                row.listing_role = "listing"
                row.source_evidence = {"expected_identity": {"id": "one"}}
                return [row]

        class BlockedTransport:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()

            def request(self, _method, _url, **_kwargs):
                self.entered.set()
                self.release.wait(5.0)
                return AnonymousResponse(200, body=b'{"id":"one"}', connection_address="8.8.8.8")

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [{
                "id": source_id, "adapter": "skills-sh", "kind": "registry", "base_url": "https://skills.sh",
                "effective_enabled": True, "enabled": True, "trust": "community-index",
            } for source_id in ("a-preview", "b-submit-failure")]
            transport = BlockedTransport()
            cache = Cache(root / "cache")
            finder = UniversalSkillFinder(
                EffectiveConfig(settings={"max_workers": 1}, packs=[], sources=sources,
                                overlay_path=root / "sources.json", overlay={}),
                cache=cache, http=object(), validation_transport=transport,
                validation_resolver=lambda _host, _port: ("8.8.8.8",),
            )
            finder.adapter_map = {"skills-sh": Adapter()}
            proof_finished = threading.Event()
            cached_destination_proof = finder._cached_destination_proof

            def tracked_destination_proof(*args, **kwargs):
                try:
                    return cached_destination_proof(*args, **kwargs)
                finally:
                    proof_finished.set()

            finder._cached_destination_proof = tracked_destination_proof  # type: ignore[method-assign]
            original_submit = ThreadPoolExecutor.submit

            def fail_second_source_submit(executor, function, *args, **kwargs):
                if (getattr(executor, "_thread_name_prefix", "") == "skill-source"
                        and args and args[0].get("id") == "b-submit-failure"):
                    self.assertTrue(transport.entered.wait(2.0))
                    raise RuntimeError("controlled source submission failure")
                return original_submit(executor, function, *args, **kwargs)

            policy = NetworkPolicy(2.0, 3.0, 0.10)
            try:
                with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=policy), \
                     patch.object(ThreadPoolExecutor, "submit", new=fail_second_source_submit):
                    with self.assertRaisesRegex(RuntimeError, "controlled source submission failure"):
                        finder.search("pdf forms")
                self.assertFalse(proof_finished.is_set())
                self.assertEqual(list((cache.root / "proofs").glob("*.json")), [])
            finally:
                transport.release.set()
                self.assertTrue(proof_finished.wait(2.0))

            self.assertEqual(list((cache.root / "proofs").glob("*.json")), [])
