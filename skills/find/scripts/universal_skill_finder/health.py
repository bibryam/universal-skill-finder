"""Bounded foreground source-health and rate-cooldown state."""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from .cache import Cache


@dataclass(frozen=True)
class HealthScope:
    source_id: str
    operation: str
    operation_revision: str
    endpoint: str
    auth_mode: str = "anonymous"
    profile_partition: str = "public"
    quota_group: str | None = None

    def key(self) -> str:
        payload = asdict(self)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class HealthDecision:
    status: str
    allowed: bool
    retry_at: float | None = None
    probe: bool = False
    reason: str | None = None


@dataclass
class _State:
    failures: list[float] = field(default_factory=list)
    state: str = "closed"
    retry_at: float | None = None
    probe_failures: int = 0
    probe_lease_until: float | None = None
    rate_retry_at: float | None = None
    retry_bypass_until: float | None = None


class HealthStore:
    MAX_STATES = 512
    WINDOW_SECONDS = 600.0

    def __init__(self, cache: Cache, *, clock=time.time):
        self.cache = cache
        self.clock = clock
        self._lock = threading.RLock()

    def _read(self, scope: HealthScope) -> _State:
        cached = self.cache.read("health", scope.key())
        if not cached or not isinstance(cached[0], dict):
            return _State()
        raw = cached[0]
        try:
            failures = [float(item) for item in raw.get("failures", [])[-3:] if math.isfinite(float(item))]
            return _State(
                failures=failures,
                state=raw.get("state") if raw.get("state") in {"closed", "open", "half_open"} else "closed",
                retry_at=_number(raw.get("retry_at")),
                probe_failures=max(0, min(10, int(raw.get("probe_failures", 0)))),
                probe_lease_until=_number(raw.get("probe_lease_until")),
                rate_retry_at=_number(raw.get("rate_retry_at")),
                retry_bypass_until=_number(raw.get("retry_bypass_until")),
            )
        except (TypeError, ValueError, OverflowError):
            return _State()

    def _write(self, scope: HealthScope, state: _State) -> None:
        self.cache.write("health", scope.key(), asdict(state))

    @contextmanager
    def _guard(self, scope: HealthScope):
        started = time.monotonic()
        if not self._lock.acquire(timeout=0.05):
            yield False
            return
        try:
            remaining = max(0.0, 0.05 - (time.monotonic() - started))
            with self.cache.exclusive_lease("health", scope.key(), timeout=remaining) as acquired:
                yield acquired
        finally:
            self._lock.release()

    def before_request(self, scope: HealthScope, now: float | None = None) -> HealthDecision:
        current = self.clock() if now is None else _required_number(now)
        with self._guard(scope) as acquired:
            if not acquired:
                # A second foreground request must not bypass a just-recorded
                # cooldown merely because another process owns the bounded
                # health-state lease. The caller may still use its exact cache
                # fallback, but live network work fails closed here.
                return self._unavailable_decision(acquired)
            state = self._read(scope)
            if state.rate_retry_at and current < state.rate_retry_at:
                return HealthDecision("rate_cooldown", False, state.rate_retry_at, reason="server rate cooldown")
            if state.retry_bypass_until and current >= state.retry_bypass_until:
                state.retry_bypass_until = None
            if state.state == "open" and state.retry_at and current < state.retry_at:
                if state.retry_bypass_until and current < state.retry_bypass_until:
                    state.retry_bypass_until = None
                    state.state = "half_open"
                    state.probe_lease_until = current + 30.0
                    self._write(scope, state)
                    return HealthDecision("retry_bypass", True, probe=True, reason="one foreground retry bypass consumed")
                return HealthDecision("circuit_open", False, state.retry_at, reason="transient service cooldown")
            if state.state in {"open", "half_open"}:
                if state.probe_lease_until and current < state.probe_lease_until:
                    return HealthDecision("half_open_wait", False, state.probe_lease_until, reason="recovery probe already leased")
                state.state = "half_open"
                state.probe_lease_until = current + 30.0
                self._write(scope, state)
                return HealthDecision("half_open", True, probe=True, reason="foreground recovery probe")
            return HealthDecision("closed", True)

    @staticmethod
    def _unavailable_decision(lease: Any) -> HealthDecision:
        status = getattr(lease, "status", "busy")
        reason = {
            "permission_denied": "cache access denied; cannot acquire health-state lock",
            "storage_unavailable": "cache storage unavailable; cannot acquire health-state lock",
        }.get(status, "health-state lock budget exhausted")
        return HealthDecision("health_state_unavailable", False, reason=reason)

    def record_success(self, scope: HealthScope, now: float | None = None) -> None:
        _ = self.clock() if now is None else _required_number(now)
        with self._guard(scope) as acquired:
            if acquired:
                self._write(scope, _State())

    def record_failure(self, scope: HealthScope, *, transient: bool,
                       local_cause: bool = False, now: float | None = None) -> None:
        current = self.clock() if now is None else _required_number(now)
        if not transient or local_cause:
            return
        with self._guard(scope) as acquired:
            if not acquired:
                return
            state = self._read(scope)
            state.failures = [item for item in state.failures if current - item <= self.WINDOW_SECONDS]
            state.failures.append(current)
            if state.state == "half_open" or len(state.failures) >= 3:
                state.probe_failures += int(state.state == "half_open")
                delay = min(300.0, 60.0 * (2 ** min(state.probe_failures, 3)))
                # Deterministic bounded jitter avoids a synchronized stampede while
                # keeping tests and persisted decisions reproducible.
                jitter = int(scope.key()[:2], 16) / 2550.0 * delay
                state.state = "open"
                state.retry_at = current + delay + jitter
                state.probe_lease_until = None
            self._write(scope, state)

    def record_rate_limit(self, scope: HealthScope, retry_at: float | None,
                          now: float | None = None) -> float:
        current = self.clock() if now is None else _required_number(now)
        until = current + 60.0 if retry_at is None else _required_number(retry_at)
        if until <= current or until - current > 31_536_000:
            until = current + 60.0
        with self._guard(scope) as acquired:
            if acquired:
                state = self._read(scope)
                state.rate_retry_at = until
                self._write(scope, state)
        return until

    def arm_retry(self, scope: HealthScope, now: float | None = None) -> HealthDecision:
        """Arm one expiring breaker bypass; never bypass a server rate cooldown."""
        current = self.clock() if now is None else _required_number(now)
        with self._guard(scope) as acquired:
            if not acquired:
                return self._unavailable_decision(acquired)
            state = self._read(scope)
            if state.rate_retry_at and current < state.rate_retry_at:
                return HealthDecision("rate_cooldown", False, state.rate_retry_at, reason="server rate cooldown remains active")
            if state.state != "open" or not state.retry_at or current >= state.retry_at:
                return HealthDecision("retry_not_needed", False, reason="source has no active outage circuit")
            state.retry_bypass_until = current + 300.0
            self._write(scope, state)
            return HealthDecision("retry_armed", True, state.retry_bypass_until,
                                  reason="next selected foreground search may bypass the outage circuit once")


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _required_number(value: Any) -> float:
    number = _number(value)
    if number is None:
        raise ValueError("time value must be finite")
    return number
