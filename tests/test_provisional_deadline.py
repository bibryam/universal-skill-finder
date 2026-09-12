from __future__ import annotations

import sys
import threading
import time
import unittest
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
                self.release.wait(2.0)
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
            policy = NetworkPolicy(0.06, 0.20, 0.03)
            started = time.monotonic()
            try:
                with patch("universal_skill_finder.federation.NetworkPolicy.for_mode", return_value=policy):
                    report = finder.search("pdf forms", preview=True)
                elapsed = time.monotonic() - started
                self.assertTrue(transport.entered.is_set())
                self.assertLess(elapsed, 0.16)
                self.assertFalse(transport.finished.is_set())
                self.assertEqual(cache.metadata("proofs"), [])
                self.assertEqual(report.notes, [
                    "An optional early destination check did not complete; only final eligible results are shown."
                ])
            finally:
                transport.release.set()
                self.assertTrue(transport.finished.wait(1.0))
            # The late worker may complete after `search` returned, but must
            # not turn that old response into durable checked proof evidence.
            self.assertEqual(cache.metadata("proofs"), [])

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
