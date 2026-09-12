from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


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

    def test_publish_lock_orders_gate_close_before_the_final_atomic_replace(self):
        with TemporaryDirectory() as temporary:
            cache = Cache(Path(temporary) / "cache")
            self.assertTrue(cache.write("queries", "entry", {"state": "before"}))
            gate = threading.Event()
            gate.set()
            publish_lock = threading.Lock()
            temporary_written = threading.Event()
            outcome: list[bool] = []
            original_fdopen = os.fdopen

            class TrackedWriter:
                def __init__(self, stream):
                    self.stream = stream
                    self.active = None

                def __enter__(self):
                    self.active = self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def write(self, payload):
                    result = self.active.write(payload)
                    temporary_written.set()
                    return result

            def tracked_fdopen(*args, **kwargs):
                return TrackedWriter(original_fdopen(*args, **kwargs))

            publish_lock.acquire()
            worker = threading.Thread(target=lambda: outcome.append(cache.write(
                "queries", "entry", {"state": "late"},
                can_publish=gate.is_set, publish_lock=publish_lock,
            )))
            try:
                with patch("universal_skill_finder.cache.os.fdopen", side_effect=tracked_fdopen):
                    worker.start()
                    self.assertTrue(temporary_written.wait(0.5))
                    gate.clear()
            finally:
                publish_lock.release()
                worker.join(0.5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome, [False])
            self.assertEqual(cache.read("queries", "entry"), ({"state": "before"}, 0))
            self.assertEqual(list((Path(temporary) / "cache" / "queries").glob(".cache-*")), [])
