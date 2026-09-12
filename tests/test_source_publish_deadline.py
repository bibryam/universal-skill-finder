from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import SourceUnavailable
from universal_skill_finder.cache import Cache
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate
from universal_skill_finder.runtime import Deadline, NetworkPolicy


def _source() -> dict[str, object]:
    return {
        "id": "deadline-source",
        "adapter": "skills-sh",
        "kind": "registry",
        "base_url": "https://skills.sh",
        "effective_enabled": True,
        "enabled": True,
        "trust": "community-index",
    }


class SourcePublishDeadlineTests(unittest.TestCase):
    def _finder(self, root: Path) -> tuple[UniversalSkillFinder, Cache, dict[str, object]]:
        source = _source()
        cache = Cache(root / "cache")
        finder = UniversalSkillFinder(
            EffectiveConfig(settings={}, packs=[], sources=[source], overlay_path=root / "sources.json", overlay={}),
            cache=cache,
            # This deliberately selects the injected/thread path. Production
            # built-ins have their own process boundary.
            http=object(),
        )
        return finder, cache, source

    @staticmethod
    def _short_deadline() -> Deadline:
        return Deadline.start(NetworkPolicy(0.025, 0.10, 0.05))

    def test_cutoff_during_candidate_processing_cannot_publish_query_or_health(self) -> None:
        """The post-adapter candidate phase must recheck ownership before writes."""
        class Adapter:
            def search(self, source, query, limit, context):
                return [Candidate("late", "Late skill", "Delayed normalization", source["id"], "registry", "skills-sh")]

        with TemporaryDirectory() as temporary:
            finder, cache, source = self._finder(Path(temporary))
            finder.adapter_map = {"skills-sh": Adapter()}
            original = finder._validated_candidates

            def delayed(payload, source_value):
                time.sleep(0.045)
                return original(payload, source_value)

            finder._validated_candidates = delayed  # type: ignore[method-assign]
            try:
                finder._search_source(
                    source, "late", 1, offline=False, refresh=True, deadline=self._short_deadline(),
                )
            except SourceUnavailable as exc:
                self.assertEqual(exc.status, "deadline_exceeded")

            self.assertEqual(cache.metadata("queries"), [])
            self.assertEqual(cache.metadata("health"), [])

    def test_injected_adapter_late_context_cache_write_is_refused(self) -> None:
        """An injected adapter must receive the same cutoff-aware cache surface."""
        class LateWriter:
            def search(self, source, query, limit, context):
                time.sleep(0.045)
                self.published = context.cache.write("queries", "late-adapter-write", {"unsafe": True})
                return []

        with TemporaryDirectory() as temporary:
            finder, cache, source = self._finder(Path(temporary))
            adapter = LateWriter()
            finder.adapter_map = {"skills-sh": adapter}
            try:
                finder._search_source(
                    source, "late", 1, offline=False, refresh=True, deadline=self._short_deadline(),
                )
            except SourceUnavailable as exc:
                self.assertEqual(exc.status, "deadline_exceeded")

            self.assertFalse(adapter.published)
            self.assertIsNone(cache.read("queries", "late-adapter-write"))
            self.assertEqual(cache.metadata("queries"), [])
            self.assertEqual(cache.metadata("health"), [])


if __name__ == "__main__":
    unittest.main()
