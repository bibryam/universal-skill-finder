"""Shared monotonic budgets, permits, events and frozen publication.

The coordinator owns these primitives. Adapters receive only bounded leases;
they cannot extend the parent timeline or publish after a pool is frozen.
"""
from __future__ import annotations

import math
import hashlib
import multiprocessing
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from .models import ProgressEvent


class RuntimeLimitError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class NetworkPolicy:
    collection_seconds: float
    shared_seconds: float
    validation_seconds: float
    source_workers: int = 6
    active_requests: int = 8
    per_origin_requests: int = 2
    provisional_validation_workers: int = 2
    final_validation_workers: int = 4
    validation_requests: int = 60
    github_api_requests: int = 15
    provisional_identities: int = 3
    provisional_validation_requests: int = 12
    provisional_github_api_requests: int = 3
    network_requests: int = 120
    network_bytes: int = 512 * 1024 * 1024

    @classmethod
    def for_mode(cls, thorough: bool = False) -> "NetworkPolicy":
        return cls(20.0, 35.0, 15.0) if thorough else cls(10.0, 20.0, 8.0)


@dataclass(frozen=True)
class Deadline:
    started_at: float
    collection_cutoff_at: float
    network_deadline_at: float
    validation_cap_seconds: float

    @classmethod
    def start(cls, policy: NetworkPolicy, *, now: float | None = None) -> "Deadline":
        value = time.monotonic() if now is None else _finite(now, "now")
        return cls(value, value + policy.collection_seconds, value + policy.shared_seconds,
                   policy.validation_seconds)

    def remaining(self, now: float | None = None) -> float:
        return max(0.0, self.network_deadline_at - (time.monotonic() if now is None else now))

    def collection_remaining(self, now: float | None = None) -> float:
        value = time.monotonic() if now is None else now
        return max(0.0, min(self.collection_cutoff_at, self.network_deadline_at) - value)

    def validation_deadline(self, frozen_at: float) -> float:
        return min(self.network_deadline_at, _finite(frozen_at, "frozen_at") + self.validation_cap_seconds)


def _finite(value: float, label: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return float(value)


@dataclass
class BudgetLease:
    kind: str
    amount: int
    _release: Callable[[], None] | None = field(default=None, repr=False)

    def release(self) -> None:
        callback, self._release = self._release, None
        if callback:
            callback()

    def __enter__(self) -> "BudgetLease":
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


class RequestBudget:
    """Monotonic counters. Reservations are consumed and never refunded."""

    def __init__(self, *, requests: int = 60, bytes: int = 64 * 1024 * 1024,
                 github_api_requests: int = 15, validation_requests: int = 60,
                 shared_state: "_BudgetState | None" = None):
        if min(requests, bytes, github_api_requests, validation_requests) < 0:
            raise ValueError("budget limits must be nonnegative")
        self.limits = {"request": requests, "bytes": bytes, "github_api": github_api_requests,
                       "validation": validation_requests}
        if shared_state is None:
            self.used = {key: 0 for key in self.limits}
            self._lock = threading.Lock()
        else:
            self.used = shared_state.used
            self._lock = shared_state.lock

    def _used(self, kind: str) -> int:
        value = self.used[kind]
        return int(value.value) if hasattr(value, "value") else int(value)

    def _consume(self, kind: str, amount: int) -> None:
        value = self.used[kind]
        if hasattr(value, "value"):
            value.value += amount
        else:
            self.used[kind] += amount

    def reserve(self, kind: str, amount: int = 1, deadline: Deadline | float | None = None) -> BudgetLease:
        if kind not in self.limits or type(amount) is not int or amount <= 0:
            raise ValueError("invalid budget reservation")
        if deadline is not None and _deadline_remaining(deadline) <= 0:
            raise RuntimeLimitError("deadline_exceeded", "shared network deadline elapsed")
        with self._lock:
            if self._used(kind) + amount > self.limits[kind]:
                raise RuntimeLimitError("budget_exhausted", f"{kind} budget exhausted")
            self._consume(kind, amount)
        return BudgetLease(kind, amount)

    def reserve_many(self, reservations: tuple[tuple[str, int], ...],
                     deadline: Deadline | float | None = None) -> BudgetLease:
        """Atomically consume related counters (for example validation+request)."""
        if (not reservations or any(kind not in self.limits or type(amount) is not int or amount <= 0
                                    for kind, amount in reservations)):
            raise ValueError("invalid budget reservation")
        if deadline is not None and _deadline_remaining(deadline) <= 0:
            raise RuntimeLimitError("deadline_exceeded", "shared network deadline elapsed")
        requested: dict[str, int] = {}
        for kind, amount in reservations:
            requested[kind] = requested.get(kind, 0) + amount
        with self._lock:
            exhausted = next((kind for kind, amount in requested.items()
                              if self._used(kind) + amount > self.limits[kind]), None)
            if exhausted is not None:
                raise RuntimeLimitError("budget_exhausted", f"{exhausted} budget exhausted")
            for kind, amount in requested.items():
                self._consume(kind, amount)
        return BudgetLease("+".join(sorted(requested)), sum(requested.values()))

    def for_phase(self, phase: str) -> "_BudgetPhase":
        if phase not in {"source", "validation"}:
            raise ValueError("unknown budget phase")
        return _BudgetPhase(self, phase)

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {key: {"used": self._used(key), "limit": value} for key, value in self.limits.items()}


class _BudgetPhase:
    """Narrow budget view used by consumers that cannot pass their phase itself."""

    def __init__(self, budget: RequestBudget, phase: str):
        self._budget = budget
        self._phase = phase

    def reserve(self, kind: str, amount: int = 1, deadline: Deadline | float | None = None) -> BudgetLease:
        if self._phase == "validation" and kind == "request":
            return self._budget.reserve_many((("validation", amount), ("request", amount)), deadline)
        return self._budget.reserve(kind, amount, deadline)

    def snapshot(self) -> dict[str, dict[str, int]]:
        return self._budget.snapshot()


@dataclass(frozen=True)
class _BudgetState:
    used: Any
    lock: Any


@dataclass
class PermitLease:
    origin: str
    phase: str
    quota_group: str | None = None
    _pool: "PermitPool" | None = field(default=None, repr=False)
    _slot: int | None = field(default=None, repr=False)
    _generation: int | None = field(default=None, repr=False)

    def release(self) -> None:
        pool, self._pool = self._pool, None
        if pool:
            pool._release(self._slot, self._generation)

    def __enter__(self) -> "PermitLease":
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


@dataclass(frozen=True)
class _PermitState:
    lock: Any
    owners: Any
    origins: Any
    phases: Any
    quota_groups: Any
    generations: Any


class PermitPool:
    """One global/per-origin limiter shared by retrieval and validation."""

    def __init__(self, policy: NetworkPolicy, *, shared_state: _PermitState | None = None):
        self.policy = policy
        if shared_state is None:
            self._lock = threading.RLock()
            self._owners = [0] * policy.active_requests
            self._origins = [-1] * policy.active_requests
            self._phases = [-1] * policy.active_requests
            self._quota_groups = [-1] * policy.active_requests
            self._generations = [0] * policy.active_requests
        else:
            self._lock = shared_state.lock
            self._owners = shared_state.owners
            self._origins = shared_state.origins
            self._phases = shared_state.phases
            self._quota_groups = shared_state.quota_groups
            self._generations = shared_state.generations

    @property
    def _active(self) -> int:
        """Compatibility view used by older callers to inspect active leases."""
        with self._lock:
            return sum(1 for owner in self._owners if owner)

    @staticmethod
    def origin(url_or_origin: str) -> str:
        parsed = urlparse(url_or_origin)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}:{port}"
        return url_or_origin.casefold()

    @staticmethod
    def _phase_code(phase: str) -> int:
        if phase == "provisional_validation":
            return 1
        if phase in {"validation", "final_validation"}:
            return 2
        return 0

    @staticmethod
    def _bucket(value: str) -> int:
        digest = hashlib.sha256(value.encode("utf-8", "strict")).digest()
        return int.from_bytes(digest[:4], "big") % 65_521

    def _phase_limit(self, code: int) -> int:
        return {
            0: self.policy.source_workers,
            1: self.policy.provisional_validation_workers,
            2: self.policy.final_validation_workers,
        }[code]

    def acquire(self, origin: str, phase: str, deadline: Deadline | float,
                quota_group: str | None = None) -> PermitLease:
        key = self.origin(origin)
        group = None
        if quota_group is not None:
            if not isinstance(quota_group, str) or not quota_group.strip() or len(quota_group) > 200:
                raise ValueError("quota group must be a bounded non-empty string")
            group = quota_group.casefold()
        origin_bucket = self._bucket(key)
        quota_bucket = self._bucket("quota:" + group) if group is not None else -1
        phase_code = self._phase_code(phase)
        while True:
            remaining = _deadline_remaining(deadline)
            if remaining <= 0:
                raise RuntimeLimitError("deadline_exceeded", "permit wait exceeded shared deadline")
            with self._lock:
                active = [index for index, owner in enumerate(self._owners) if owner]
                allowed = (
                    len(active) < self.policy.active_requests
                    and sum(1 for index in active if self._phases[index] == phase_code) < self._phase_limit(phase_code)
                    and sum(1 for index in active if self._origins[index] == origin_bucket) < self.policy.per_origin_requests
                    and (quota_bucket < 0 or sum(
                        1 for index in active if self._quota_groups[index] == quota_bucket
                    ) < self.policy.per_origin_requests)
                )
                if allowed:
                    slot = next(index for index, owner in enumerate(self._owners) if not owner)
                    generation = int(self._generations[slot]) + 1
                    self._generations[slot] = generation
                    self._owners[slot] = os.getpid()
                    self._origins[slot] = origin_bucket
                    self._phases[slot] = phase_code
                    self._quota_groups[slot] = quota_bucket
                    return PermitLease(key, phase, group, self, slot, generation)
            time.sleep(min(0.005, remaining))

    def _release(self, slot: int | None, generation: int | None) -> None:
        if slot is None or generation is None:
            return
        with self._lock:
            if not 0 <= slot < len(self._owners) or self._generations[slot] != generation:
                return
            self._owners[slot] = 0
            self._origins[slot] = self._phases[slot] = self._quota_groups[slot] = -1

    def reclaim_owner(self, process_id: int | None) -> int:
        """Revoke permits held by a child that the supervisor has reaped."""
        if type(process_id) is not int or process_id <= 0:
            return 0
        reclaimed = 0
        with self._lock:
            for slot, owner in enumerate(self._owners):
                if owner != process_id:
                    continue
                self._owners[slot] = 0
                self._origins[slot] = self._phases[slot] = self._quota_groups[slot] = -1
                reclaimed += 1
        return reclaimed


class SharedNetworkResources:
    """Pickle-safe counters and permits shared by spawned source workers.

    The manager is intentionally parent-owned. Children receive only proxy-backed
    budget and permit objects, so a killed child releases neither a distinct
    counter nor an independent per-origin lane.
    """

    def __init__(self, policy: NetworkPolicy, *, requests: int | None = None,
                 bytes: int | None = None, github_api_requests: int | None = None,
                 validation_requests: int | None = None):
        context = multiprocessing.get_context("spawn")
        budget_state = _BudgetState(
            used={key: context.Value("q", 0) for key in ("request", "bytes", "github_api", "validation")},
            lock=context.RLock(),
        )
        permit_state = _PermitState(
            lock=context.RLock(),
            owners=context.Array("q", policy.active_requests, lock=False),
            origins=context.Array("q", [-1] * policy.active_requests, lock=False),
            phases=context.Array("b", [-1] * policy.active_requests, lock=False),
            quota_groups=context.Array("q", [-1] * policy.active_requests, lock=False),
            generations=context.Array("q", policy.active_requests, lock=False),
        )
        self.budget = RequestBudget(
            requests=policy.network_requests if requests is None else requests,
            bytes=policy.network_bytes if bytes is None else bytes,
            github_api_requests=policy.github_api_requests if github_api_requests is None else github_api_requests,
            validation_requests=policy.validation_requests if validation_requests is None else validation_requests,
            shared_state=budget_state,
        )
        self.permits = PermitPool(policy, shared_state=permit_state)

    def close(self) -> None:
        # SemLock resources are reclaimed with their owning process. This method
        # keeps the parent lifecycle explicit without a background manager.
        return None


@dataclass(frozen=True)
class SourceJob:
    source_id: str
    origin: str
    payload: Any = None


@dataclass
class SourceOutcome:
    source_id: str
    status: str
    submitted_at: float
    started_at: float | None = None
    request_at: float | None = None
    completed_at: float | None = None
    value: Any = None
    detail: str | None = None


class WorkerSupervisor:
    """Parent-owned acceptance boundary for cooperative source workers.

    Platform process creation and reaping is implemented by the caller-facing
    source runner. This class owns the invariant that a frozen pool is immutable.
    """

    def __init__(self, deadline: Deadline):
        self.deadline = deadline
        self._lock = threading.Lock()
        self._frozen = False
        self._outcomes: dict[str, SourceOutcome] = {}
        self._accepted: dict[str, Any] = {}

    def submit(self, job: SourceJob, *, now: float | None = None) -> SourceOutcome:
        value = time.monotonic() if now is None else now
        with self._lock:
            if self._frozen or self.deadline.collection_remaining(value) <= 0:
                outcome = SourceOutcome(job.source_id, "not_started_budget", value,
                                        detail="collection admission closed")
            else:
                outcome = SourceOutcome(job.source_id, "submitted", value)
            self._outcomes[job.source_id] = outcome
            return outcome

    def started(self, source_id: str, *, now: float | None = None) -> None:
        with self._lock:
            outcome = self._outcomes[source_id]
            if outcome.status == "submitted" and not self._frozen:
                outcome.status = "started"
                outcome.started_at = time.monotonic() if now is None else now

    def publish(self, source_id: str, value: Any, *, checkpointed: bool = False,
                now: float | None = None) -> bool:
        completed = time.monotonic() if now is None else now
        with self._lock:
            outcome = self._outcomes[source_id]
            if self._frozen or completed > self.deadline.collection_cutoff_at:
                outcome.status = "deadline_exceeded"
                outcome.completed_at = completed
                outcome.detail = "late publication rejected"
                return False
            outcome.status = "checkpointed" if checkpointed else "completed"
            outcome.completed_at = completed
            outcome.value = value
            self._accepted[source_id] = value
            return True

    def fail(self, source_id: str, detail: str, *, now: float | None = None) -> None:
        with self._lock:
            outcome = self._outcomes[source_id]
            if not self._frozen:
                outcome.status = "failed"
                outcome.completed_at = time.monotonic() if now is None else now
                outcome.detail = detail

    def freeze(self, reason: str = "collection_cutoff") -> tuple[dict[str, Any], list[SourceOutcome]]:
        with self._lock:
            self._frozen = True
            for outcome in self._outcomes.values():
                if outcome.status in {"submitted", "started"}:
                    outcome.status = "deadline_exceeded"
                    outcome.detail = reason
            return dict(self._accepted), [self._outcomes[key] for key in sorted(self._outcomes)]

    @property
    def frozen(self) -> bool:
        with self._lock:
            return self._frozen


ProgressCallback = Callable[[ProgressEvent], None]


def _deadline_remaining(deadline: Deadline | float) -> float:
    if isinstance(deadline, Deadline):
        return deadline.remaining()
    return max(0.0, _finite(deadline, "deadline") - time.monotonic())


def emit_progress(callback: ProgressCallback | None, event: ProgressEvent) -> None:
    if callback is None:
        return
    try:
        callback(event)
    except Exception:
        # A broken UI stream is not a source failure and must not contaminate
        # coverage. CLI orchestration may stop emitting after its own error.
        return
