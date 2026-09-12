from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache


class CachePublishGuardTests(unittest.TestCase):
    def test_guard_rechecked_at_atomic_publish_boundary_preserves_prior_entry(self):
        """A cutoff crossed during temporary-file work cannot replace a cache hit."""
        with TemporaryDirectory() as temporary:
            cache = Cache(Path(temporary) / "cache")
            self.assertTrue(cache.write("queries", "entry", {"state": "before"}))
            calls: list[str] = []

            def before_cutoff_then_frozen() -> bool:
                calls.append("checked")
                return len(calls) == 1

            self.assertFalse(cache.write(
                "queries", "entry", {"state": "late"}, can_publish=before_cutoff_then_frozen,
            ))
            self.assertEqual(calls, ["checked", "checked"])
            self.assertEqual(cache.read("queries", "entry"), ({"state": "before"}, 0))
            self.assertEqual(list((Path(temporary) / "cache" / "queries").glob(".cache-*")), [])

    def test_guard_can_reject_before_serialization_or_filesystem_publication(self):
        with TemporaryDirectory() as temporary:
            cache = Cache(Path(temporary) / "cache")
            self.assertFalse(cache.write("queries", "entry", {"state": "never"}, can_publish=lambda: False))
            self.assertIsNone(cache.read("queries", "entry"))
            self.assertFalse((Path(temporary) / "cache").exists())

