from __future__ import annotations

import errno
import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache, CacheLease
from universal_skill_finder.health import HealthScope, HealthStore


class CacheLeaseOutcomeTests(unittest.TestCase):
    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def __init__(self, failure: OSError):
            self.failure = failure

        def locking(self, _descriptor: int, mode: int, _length: int) -> None:
            if mode == self.LK_NBLCK:
                raise self.failure

    @contextmanager
    def fake_windows_locking(self, failure: OSError):
        # Exercise the Windows branch on POSIX without claiming native Windows
        # coverage or depending on the platform-only msvcrt module.
        with patch("universal_skill_finder.cache.os.name", "nt"), \
             patch.dict(sys.modules, {"msvcrt": self.FakeMsvcrt(failure)}):
            yield

    def test_permission_denied_lease_is_typed_and_does_not_leak_path(self):
        with TemporaryDirectory() as temporary:
            cache = Cache(Path(temporary) / "cache")
            denied_path = "/private/not-for-output/universal-skill-finder-cache"
            with patch.object(cache, "_directory", side_effect=PermissionError(13, "denied", denied_path)):
                with cache.exclusive_lease("health", "a" * 64, timeout=0.01) as lease:
                    self.assertEqual(lease.status, "permission_denied")
                    self.assertFalse(lease)
                    self.assertNotIn(denied_path, str(lease))

    def test_genuine_contention_is_busy_and_bounded(self):
        with TemporaryDirectory() as temporary:
            first = Cache(Path(temporary) / "cache")
            second = Cache(Path(temporary) / "cache")
            key = "b" * 64
            with first.exclusive_lease("health", key, timeout=0.05) as leader:
                self.assertEqual(leader.status, "acquired")
                started = time.monotonic()
                with second.exclusive_lease("health", key, timeout=0.02) as follower:
                    elapsed = time.monotonic() - started
                    self.assertEqual(follower.status, "busy")
                    self.assertFalse(follower)
            self.assertLess(elapsed, 0.10)

    def test_other_storage_failure_is_not_mislabeled_as_contention(self):
        with TemporaryDirectory() as temporary:
            cache = Cache(Path(temporary) / "cache")
            with patch.object(cache, "_directory", side_effect=OSError(errno.ENOSPC, "no space")):
                with cache.exclusive_lease("health", "d" * 64, timeout=0.01) as lease:
                    self.assertEqual(lease.status, "storage_unavailable")
                    self.assertFalse(lease)

    def test_fake_windows_lock_contention_errno_is_busy(self):
        with TemporaryDirectory() as temporary:
            cache = Cache(Path(temporary) / "cache")
            with self.fake_windows_locking(OSError(errno.EACCES, "already locked")):
                with cache.exclusive_lease("health", "e" * 64, timeout=0.01) as lease:
                    self.assertEqual(lease.status, "busy")
                    self.assertFalse(lease)

    def test_fake_windows_unrelated_lock_error_reaches_storage_classifier(self):
        with TemporaryDirectory() as temporary:
            cache = Cache(Path(temporary) / "cache")
            with self.fake_windows_locking(OSError(errno.EIO, "fixture I/O failure")):
                with cache.exclusive_lease("health", "f" * 64, timeout=0.01) as lease:
                    self.assertEqual(lease.status, "storage_unavailable")
                    self.assertFalse(lease)

    def test_lease_releases_after_body_exception(self):
        with TemporaryDirectory() as temporary:
            first = Cache(Path(temporary) / "cache")
            second = Cache(Path(temporary) / "cache")
            key = "c" * 64
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                with first.exclusive_lease("health", key, timeout=0.05) as lease:
                    self.assertEqual(lease.status, "acquired")
                    raise RuntimeError("fixture")
            with second.exclusive_lease("health", key, timeout=0.02) as retry:
                self.assertEqual(retry.status, "acquired")


class HealthStorageFailureTests(unittest.TestCase):
    class LeaseCache:
        def __init__(self, status: str):
            self.lease = CacheLease(status)

        def read(self, _namespace, _key):
            return None

        def write(self, _namespace, _key, _value):
            return False

        @contextmanager
        def exclusive_lease(self, _namespace, _key, *, timeout=0.05, stale_seconds=5.0):
            del timeout, stale_seconds
            yield self.lease

    @staticmethod
    def scope() -> HealthScope:
        return HealthScope("registry", "search", "v1", "https://registry.example")

    def test_permission_denied_health_is_fail_closed_but_distinguished_from_busy(self):
        decision = HealthStore(self.LeaseCache("permission_denied"), clock=lambda: 10.0).before_request(self.scope())
        self.assertEqual((decision.status, decision.allowed), ("health_state_unavailable", False))
        self.assertEqual(decision.reason, "cache access denied; cannot acquire health-state lock")
        self.assertNotIn("lock budget exhausted", decision.reason or "")

    def test_busy_health_remains_fail_closed(self):
        decision = HealthStore(self.LeaseCache("busy"), clock=lambda: 10.0).before_request(self.scope())
        self.assertEqual((decision.status, decision.allowed), ("health_state_unavailable", False))
        self.assertIn("lock budget exhausted", decision.reason or "")
        self.assertNotIn("permission", decision.reason or "")


if __name__ == "__main__":
    unittest.main()
