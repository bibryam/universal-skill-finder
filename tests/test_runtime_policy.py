from __future__ import annotations

import sys
import threading
import time
import unittest
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache, SingleFlight
from universal_skill_finder.health import HealthScope, HealthStore
from universal_skill_finder.runtime import Deadline, NetworkPolicy, PermitPool, RequestBudget, RuntimeLimitError, SharedNetworkResources


def _consume_shared_budget(budget, response_queue):
    budget.reserve("request", 1, time.monotonic() + 2)
    response_queue.put(budget.snapshot()["request"]["used"])


def _hold_shared_permit(permits, response_queue, release_event):
    lease = permits.acquire("https://shared.example", "source", time.monotonic() + 3)
    response_queue.put(os.getpid())
    release_event.wait(3)
    lease.release()


class Remaining:
    def __init__(self, value: float):
        self.value = value

    def remaining(self, _now=None):
        return self.value


class RuntimePolicyTests(unittest.TestCase):
    def test_selected_default_and_thorough_deadlines_are_exact(self):
        default = NetworkPolicy.for_mode(False)
        self.assertEqual((default.collection_seconds, default.shared_seconds,
                          default.validation_seconds), (10.0, 20.0, 8.0))
        thorough = NetworkPolicy.for_mode(True)
        self.assertEqual((thorough.collection_seconds, thorough.shared_seconds,
                          thorough.validation_seconds), (20.0, 35.0, 15.0))
        deadline = Deadline.start(NetworkPolicy.for_mode(False), now=100.0)
        self.assertEqual((deadline.collection_cutoff_at, deadline.network_deadline_at,
                          deadline.validation_deadline(109.0)), (110.0, 120.0, 117.0))

    def test_request_budget_is_monotonic_and_never_refunded(self):
        budget = RequestBudget(requests=1, bytes=10, github_api_requests=0, validation_requests=0)
        deadline = time.monotonic() + 1
        lease = budget.reserve("request", 1, deadline)
        lease.release()
        with self.assertRaisesRegex(RuntimeLimitError, "request budget exhausted"):
            budget.reserve("request", 1, deadline)
        self.assertEqual(budget.snapshot()["request"], {"used": 1, "limit": 1})

    def test_shared_resources_keep_budget_across_spawned_worker(self):
        resources = SharedNetworkResources(NetworkPolicy.for_mode(), requests=3)
        context = __import__("multiprocessing").get_context("spawn")
        response_queue = context.Queue()
        process = context.Process(target=_consume_shared_budget, args=(resources.budget, response_queue))
        process.start()
        self.assertEqual(response_queue.get(timeout=3), 1)
        process.join(3)
        self.assertFalse(process.is_alive())
        self.assertEqual(resources.budget.snapshot()["request"]["used"], 1)

    def test_shared_permit_pool_enforces_global_origin_and_quota_limits(self):
        policy = NetworkPolicy(1, 1, 1, active_requests=2, per_origin_requests=2)
        permits = SharedNetworkResources(policy).permits
        deadline = time.monotonic() + 0.02
        first = permits.acquire("https://one.example", "source", deadline, quota_group="provider")
        second = permits.acquire("https://two.example", "source", deadline, quota_group="provider")
        with self.assertRaisesRegex(RuntimeLimitError, "permit wait"):
            permits.acquire("https://three.example", "source", time.monotonic() + 0.01, quota_group="provider")
        second.release()
        first.release()

    def test_shared_origin_limit_and_reaping_work_across_spawned_children(self):
        policy = NetworkPolicy(2, 2, 1, active_requests=4, per_origin_requests=2)
        permits = SharedNetworkResources(policy).permits
        context = __import__("multiprocessing").get_context("spawn")
        response_queue = context.Queue()
        release_events = [context.Event(), context.Event()]
        children = [context.Process(target=_hold_shared_permit, args=(permits, response_queue, release_events[index]))
                    for index in range(2)]
        for child in children:
            child.start()
        owners = {response_queue.get(timeout=3), response_queue.get(timeout=3)}
        with self.assertRaisesRegex(RuntimeLimitError, "permit wait"):
            permits.acquire("https://shared.example", "source", time.monotonic() + 0.02)
        children[0].terminate()
        children[0].join(2)
        self.assertIn(children[0].pid, owners)
        self.assertEqual(permits.reclaim_owner(children[0].pid), 1)
        replacement = permits.acquire("https://shared.example", "source", time.monotonic() + 1)
        replacement.release()
        release_events[1].set()
        children[1].join(2)
        self.assertFalse(children[1].is_alive())

    def test_reaped_child_permits_are_revoked_without_over_releasing_reused_slots(self):
        policy = NetworkPolicy(1, 1, 1, active_requests=1, per_origin_requests=1)
        permits = SharedNetworkResources(policy).permits
        abandoned = permits.acquire("https://one.example", "source", time.monotonic() + 1)
        self.assertEqual(permits.reclaim_owner(os.getpid()), 1)
        replacement = permits.acquire("https://one.example", "source", time.monotonic() + 1)
        abandoned.release()
        self.assertEqual(permits._active, 1)
        replacement.release()
        self.assertEqual(permits._active, 0)

    def test_validation_budget_view_consumes_validation_and_request_counters_atomically(self):
        budget = RequestBudget(requests=1, validation_requests=1)
        budget.for_phase("validation").reserve("request", 1, time.monotonic() + 1)
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["request"]["used"], 1)
        self.assertEqual(snapshot["validation"]["used"], 1)

    def test_singleflight_releases_waiters_without_a_duplicate_leader(self):
        flight = SingleFlight(lease_seconds=10)
        leader = flight.claim("queries", "key", "public", Remaining(1))
        self.assertTrue(leader.leader)
        claimed = []
        started = threading.Event()

        def wait():
            started.set()
            claimed.append(flight.claim("queries", "key", "public", Remaining(1)))

        thread = threading.Thread(target=wait)
        thread.start()
        self.assertTrue(started.wait(0.2))
        leader.release()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(claimed[0].state, "waiter")

    def test_health_circuit_and_half_open_probe_are_bounded_and_do_not_change_enablement(self):
        with TemporaryDirectory() as temporary:
            now = [1000.0]
            store = HealthStore(Cache(Path(temporary) / "cache"), clock=lambda: now[0])
            scope = HealthScope("source", "search", "v1", "https://catalog.example", "anonymous")
            for _ in range(3):
                store.record_failure(scope, transient=True)
            opened = store.before_request(scope)
            self.assertFalse(opened.allowed)
            self.assertEqual(opened.status, "circuit_open")
            now[0] = opened.retry_at + 0.001
            probe = store.before_request(scope)
            blocked = store.before_request(scope)
            self.assertTrue(probe.allowed)
            self.assertTrue(probe.probe)
            self.assertEqual(blocked.status, "half_open_wait")
            store.record_success(scope)
            self.assertEqual(store.before_request(scope).status, "closed")

    def test_nontransient_and_local_failures_do_not_open_a_circuit(self):
        with TemporaryDirectory() as temporary:
            store = HealthStore(Cache(Path(temporary) / "cache"), clock=lambda: 10.0)
            scope = HealthScope("source", "search", "v1", "https://catalog.example")
            for _ in range(10):
                store.record_failure(scope, transient=False)
                store.record_failure(scope, transient=True, local_cause=True)
            self.assertEqual(store.before_request(scope).status, "closed")

    def test_retry_bypass_is_armed_without_a_request_and_consumed_once(self):
        with TemporaryDirectory() as temporary:
            now = [1_000.0]
            store = HealthStore(Cache(Path(temporary) / "cache"), clock=lambda: now[0])
            scope = HealthScope("source", "search", "v1", "https://catalog.example")
            for _ in range(3):
                store.record_failure(scope, transient=True)
            armed = store.arm_retry(scope)
            first = store.before_request(scope)
            second = store.before_request(scope)
            self.assertEqual((armed.status, armed.allowed), ("retry_armed", True))
            self.assertEqual((first.status, first.allowed, first.probe), ("retry_bypass", True, True))
            self.assertEqual((second.status, second.allowed), ("half_open_wait", False))

    def test_cross_instance_health_lease_has_a_bounded_wait(self):
        with TemporaryDirectory() as temporary:
            first = Cache(Path(temporary) / "cache")
            second = Cache(Path(temporary) / "cache")
            key = "a" * 64
            with first.exclusive_lease("health", key, timeout=0.05) as leader:
                started = time.monotonic()
                with second.exclusive_lease("health", key, timeout=0.02) as follower:
                    elapsed = time.monotonic() - started
            self.assertTrue(leader)
            self.assertFalse(follower)
            self.assertLess(elapsed, 0.08)

    def test_live_lease_is_not_stolen_after_a_forward_wall_clock_jump(self):
        with TemporaryDirectory() as temporary:
            first = Cache(Path(temporary) / "cache")
            second = Cache(Path(temporary) / "cache")
            key = "b" * 64
            with first.exclusive_lease("catalogues", key, timeout=0.05) as leader:
                self.assertTrue(leader)
                with patch("universal_skill_finder.cache.time.time", return_value=time.time() + 86_400):
                    with second.exclusive_lease("catalogues", key, timeout=0.02) as follower:
                        self.assertFalse(follower)

    def test_health_lock_contention_fails_closed_for_live_work(self):
        with TemporaryDirectory() as temporary:
            store = HealthStore(Cache(Path(temporary) / "cache"), clock=lambda: 10.0)
            scope = HealthScope("source", "search", "v1", "https://catalog.example")

            @contextmanager
            def unavailable(_scope):
                yield False

            store._guard = unavailable  # type: ignore[method-assign]
            decision = store.before_request(scope)
            self.assertEqual((decision.status, decision.allowed), ("health_state_unavailable", False))


if __name__ == "__main__":
    unittest.main()
