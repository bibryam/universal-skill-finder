from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import SourceUnavailable
from universal_skill_finder.cache import Cache
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.health import HealthDecision
from universal_skill_finder.runtime import Deadline, NetworkPolicy
from test_universal_skill_finder import StaticAdapter, candidate, finder_config, registry_source


class CacheHealthIntegrationTests(unittest.TestCase):
    def finder(self, root: Path, adapter) -> tuple[UniversalSkillFinder, dict]:
        source = registry_source("one", "skills-sh", base_url="https://catalog.example")
        config = finder_config(root, [source])
        config.settings["cache_ttl_seconds"] = 300
        finder = UniversalSkillFinder(config, cache=Cache(root / "cache"), http=object())
        finder.adapter_map = {"skills-sh": adapter}
        return finder, source

    def test_transient_failure_uses_bounded_stale_exact_query_but_refresh_does_not(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed, source = self.finder(root, StaticAdapter([candidate("one", "acme/skills", "pdf", rank=1)]))
            self.assertEqual(seed.search("pdf").coverage[0].status, "ok")
            path = seed.cache._path("queries", seed._cache_key(source, "pdf", 10))
            old = time.time() - 600
            os.utime(path, (old, old))

            failing, _ = self.finder(root, StaticAdapter(failure=SourceUnavailable("timeout", "fixture timeout")))
            fallback = failing.search("pdf")
            self.assertEqual((fallback.coverage[0].status, fallback.coverage[0].cache_status),
                             ("cached", "stale_fallback"))
            self.assertTrue(fallback.coverage[0].incomplete_results)
            self.assertIn("cached fallback; source request failed", fallback.coverage[0].detail)
            refreshed = failing.search("pdf", refresh=True)
            self.assertEqual(refreshed.coverage[0].status, "timeout")
            self.assertEqual(refreshed.results, [])

    def test_three_transient_failures_open_without_disabling_the_source(self):
        with TemporaryDirectory() as temporary:
            adapter = StaticAdapter(failure=SourceUnavailable("timeout", "fixture timeout"))
            finder, _ = self.finder(Path(temporary), adapter)
            reports = [finder.search("pdf") for _ in range(4)]
            self.assertEqual([row.coverage[0].status for row in reports[:3]], ["timeout"] * 3)
            self.assertEqual(reports[3].coverage[0].status, "circuit_open")
            self.assertEqual(adapter.calls, 3)
            self.assertTrue(reports[3].coverage[0].enabled)
            self.assertEqual(reports[3].coverage[0].health_status, "circuit_open")

    def test_health_lock_contention_uses_exact_stale_cache_without_live_request(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed, source = self.finder(root, StaticAdapter([candidate("one", "acme/skills", "pdf", rank=1)]))
            self.assertEqual(seed.search("pdf").coverage[0].status, "ok")
            path = seed.cache._path("queries", seed._cache_key(source, "pdf", 10))
            old = time.time() - 600
            os.utime(path, (old, old))
            blocked, _ = self.finder(root, StaticAdapter(failure=AssertionError("live adapter must not run")))
            blocked.health.before_request = lambda _scope: HealthDecision(
                "health_state_unavailable", False, reason="fixture lock contention"
            )

            report = blocked.search("pdf")

            self.assertEqual((report.coverage[0].status, report.coverage[0].cache_status),
                             ("cached", "stale_fallback"))
            self.assertEqual(report.coverage[0].health_status, "health_state_unavailable")

    def test_identical_threaded_cache_misses_coalesce_one_adapter_call(self):
        class SlowAdapter(StaticAdapter):
            def search(self, source, query, limit, context):
                time.sleep(0.05)
                return super().search(source, query, limit, context)

        with TemporaryDirectory() as temporary:
            adapter = SlowAdapter([candidate("one", "acme/skills", "pdf", rank=1)])
            finder, source = self.finder(Path(temporary), adapter)
            policy = NetworkPolicy(1.0, 1.0, 0.1)
            deadline = Deadline.start(policy)
            barrier = threading.Barrier(2)
            outcomes = []

            def run():
                barrier.wait()
                outcomes.append(finder._search_source(
                    source, "pdf", 10, offline=False, refresh=False, deadline=deadline,
                ))

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(1)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(sorted(coverage.status for _rows, coverage in outcomes), ["cached", "ok"])


if __name__ == "__main__":
    unittest.main()
