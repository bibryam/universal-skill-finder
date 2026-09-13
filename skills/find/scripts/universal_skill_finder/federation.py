from __future__ import annotations

import hashlib
import copy
import json
import multiprocessing
import os
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .adapters import ADAPTER_SPECS, adapters
from .adapters.base import AdapterContext, SourceUnavailable
from .cache import Cache
from .config import ConfigurationError, EffectiveConfig
from .health import HealthScope, HealthStore
from .http import FinderHttpError, HttpClient
from .models import Candidate, Coverage, LinkProof, ProgressEvent, Result, SearchReport, UNSAFE_LOCATION_WARNING
from .ranking import RankTrace, compatible_match_percent, rank_results
from .proof_workflow import validate_record
from .runtime import Deadline, NetworkPolicy, PermitPool, RequestBudget, RuntimeLimitError, SharedNetworkResources, SourceJob, WorkerSupervisor, emit_progress
from .snapshot import MAX_PAGE_SCAN
from .source_presentation import public_source_url
from .text import clean_text, occurrence_aliases, safe_skill_path, stable_result_id, weak_occurrence_aliases
from .versioning import ADAPTER_CONTRACT_VERSION, effective_config_revision, release_metadata
from .validation import (
    AnonymousPublicTransport, Destination, TargetResolutionCache, github_skill_destination,
    public_resolver, reviewed_destination, skillhub_destination, validate_destination, validate_ranked,
)

TRUST_PRIORITY = {
    "publisher-owned": 5,
    "security-index": 4,
    "user-configured": 3,
    "community-index": 2,
    "unverified": 1,
}

_REMOTE_ACTIONABLE_PROOF_STATUSES = frozenset({"eligible", "verified", "reachable"})


@dataclass(frozen=True)
class _ValidationRun:
    checked_count: int = 0
    deferred_count: int = 0
    stop_reason: str | None = None
    stopped_reason: str | None = None


def _is_remote_actionable_proof_status(value: object) -> bool:
    """Match every remote spelling the renderer would treat as authority."""
    return isinstance(value, str) and value.casefold() in _REMOTE_ACTIONABLE_PROOF_STATUSES


class _CutoffCache(Cache):
    """A child-process cache that cannot publish after its collection cutoff."""

    def __init__(self, root: Path, cutoff_at: float):
        super().__init__(root)
        self.cutoff_at = cutoff_at

    def write(self, namespace: str, key: str, payload: Any, *, can_publish: Any = None,
              publish_lock: Any = None) -> bool:
        return super().write(
            namespace, key, payload,
            can_publish=lambda: time.monotonic() < self.cutoff_at and (can_publish is None or can_publish()),
            publish_lock=publish_lock,
        )


class _SourceCache:
    """Per-job view preserving shared cache/leases while guarding publication."""

    def __init__(self, cache: Cache, can_publish: Any):
        self._cache = cache
        self._can_publish = can_publish

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cache, name)

    def write(self, namespace: str, key: str, payload: Any, *, can_publish: Any = None,
              publish_lock: Any = None) -> bool:
        return self._cache.write(
            namespace, key, payload,
            can_publish=lambda: self._can_publish() and (can_publish is None or can_publish()),
            publish_lock=publish_lock,
        )


def _process_source_runner(
    config: EffectiveConfig,
    cache_root: Path,
    source: dict[str, Any],
    query: str,
    limit: int,
    offline: bool,
    refresh: bool,
    cutoff_at: float,
    policy: NetworkPolicy,
    output: Any,
    budget: RequestBudget | None = None,
    permits: PermitPool | None = None,
) -> None:
    """Execute a built-in source outside the parent acceptance/cache boundary."""
    deadline = Deadline(0.0, cutoff_at, cutoff_at, 0.0)
    bounded_http = HttpClient(
        timeout=float(config.settings.get("timeout_seconds", 10)),
        max_bytes=int(config.settings.get("max_response_bytes", 5_242_880)),
        deadline=deadline,
        budget=budget or RequestBudget(github_api_requests=policy.github_api_requests),
        permits=permits or PermitPool(policy),
    )
    finder = UniversalSkillFinder(config, cache=_CutoffCache(cache_root, cutoff_at), http=bounded_http)
    try:
        candidates, coverage = finder._search_source(
            source, query, limit, offline=offline, refresh=refresh, deadline=deadline
        )
        output.put(("ok", candidates, coverage))
    except SourceUnavailable as exc:
        output.put(("source_unavailable", exc.status, exc.detail))
    except FinderHttpError as exc:
        output.put(("http_error", exc.status, exc.retry_after, exc.rate_limited,
                    clean_text(str(exc), 500), getattr(exc, "cooldown_until", None)))
    except Exception as exc:
        output.put(("failed", type(exc).__name__, clean_text(str(exc), 500)))


class UniversalSkillFinder:
    def __init__(
        self, config: EffectiveConfig, *, cache: Cache | None = None, http: HttpClient | None = None,
        validation_transport: Any = None, validation_resolver: Any = None,
    ):
        self.config = config
        self.cache = cache or Cache()
        supplied_http = http is not None
        self.http = http or HttpClient(
            timeout=float(config.settings.get("timeout_seconds", 10)),
            max_bytes=int(config.settings.get("max_response_bytes", 5_242_880)),
        )
        self.adapter_map = adapters()
        self.health = HealthStore(self.cache)
        # Test/embedding discovery clients must opt in with a separate
        # anonymous validation transport. This prevents fixture searches from
        # unexpectedly contacting public destinations; the production CLI does
        # not supply ``http`` and therefore retains live validation by default.
        self.validation_transport = validation_transport if validation_transport is not None else (
            None if supplied_http else AnonymousPublicTransport()
        )
        self.validation_resolver = validation_resolver or public_resolver

    def _cached_destination_proof(
        self, destination: Destination, *, phase: str, budget: RequestBudget,
        permits: PermitPool, deadline: Deadline,
        can_publish: Callable[[], bool] | None = None,
        publish_lock: Any = None,
    ):
        """Reuse only an exact, fresh anonymous proof and coalesce its refresh."""
        profile = destination.profile.name if destination.profile is not None else "unreviewed"
        key = self.cache.key(
            "proof-v1", destination.role, destination.url, profile,
            json.dumps(destination.expected_identity, sort_keys=True, default=str),
        )
        positive_ttl = 300
        unavailable_ttl = 60

        def cached():
            # Read to the longest allowed age first so status decides the
            # freshness policy. A confirmed missing destination can change much
            # sooner than a positive exact identity proof.
            entry = self.cache.read("proofs", key, max_age=positive_ttl)
            if not entry or not isinstance(entry[0], dict):
                return None
            payload, age = entry
            fields = LinkProof.__dataclass_fields__
            try:
                proof = LinkProof(**{name: value for name, value in payload.items() if name in fields})
            except (TypeError, ValueError):
                return None
            if proof.status not in {"eligible", "unavailable"}:
                return None
            if proof.status == "unavailable" and age > unavailable_ttl:
                return None
            proof.cache_age_seconds = age
            return proof

        proof = cached()
        if proof is not None:
            return proof
        scope = "anonymous-public-proof-v1"
        with self.cache.singleflight.claim("proofs", key, scope, deadline) as claim:
            if not claim.leader:
                return cached() or LinkProof(
                    destination.role, destination.url, "not_checked",
                    detail="identical destination proof is still in flight",
                )
            remaining = deadline.remaining()
            with self.cache.exclusive_lease(
                "proof-leases", key, timeout=min(3.25, remaining), stale_seconds=5.0,
            ) as acquired:
                proof = cached()
                if proof is not None:
                    return proof
                if not acquired:
                    detail = {
                        "permission_denied": "cache access denied; cannot acquire destination proof lock",
                        "storage_unavailable": "cache storage unavailable; cannot acquire destination proof lock",
                    }.get(getattr(acquired, "status", "busy"), "identical destination proof lease unavailable")
                    return LinkProof(
                        destination.role, destination.url, "not_checked",
                        detail=detail,
                    )
                proof = validate_destination(
                    destination, transport=self.validation_transport, resolver=self.validation_resolver,
                    budget=budget, permits=permits, deadline=deadline, phase=phase,
                )
                if proof.status in {"eligible", "unavailable"}:
                    self.cache.write(
                        "proofs", key, asdict(proof),
                        can_publish=lambda: deadline.remaining() > 0 and (
                            can_publish is None or can_publish()
                        ),
                        publish_lock=publish_lock,
                    )
                return proof

    @staticmethod
    def _relevance_admissible(candidate: Candidate, query: str) -> bool:
        """Require explainable overlap after the code-owned retrieval step."""
        spec = ADAPTER_SPECS.get(candidate.adapter)
        if spec is None:
            return False
        return compatible_match_percent(
            query, candidate.name, candidate.description, candidate.skill_path or "", candidate.repository or "",
        ) > 0

    @staticmethod
    def _result_relevance_admissible(result: Result) -> bool:
        """Defend the frozen pool even if a connector bypassed source admission."""
        lexical = result.ranking.get("components", {}).get("lexical")
        return type(lexical) in {int, float} and lexical > 0

    @staticmethod
    def _host(source: dict[str, Any]) -> str | None:
        value = source.get("endpoint") or source.get("base_url")
        if value:
            return urlparse(str(value)).hostname
        if source.get("repository"):
            return "github.com"
        return None

    @staticmethod
    def _target(source: dict[str, Any]) -> str | None:
        return source.get("repository") or source.get("path") or UniversalSkillFinder._host(source)

    @staticmethod
    def _candidate_payload(payload: Any) -> list[dict[str, Any]] | None:
        if isinstance(payload, list):
            return payload if all(isinstance(item, dict) for item in payload) else None
        if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
            candidates = payload["candidates"]
            return candidates if all(isinstance(item, dict) for item in candidates) else None
        return None

    @staticmethod
    def _partial_metadata(incomplete: Any = False, detail: Any = None) -> tuple[bool, str | None]:
        if type(incomplete) is not bool or (detail is not None and not isinstance(detail, str)):
            raise SourceUnavailable("schema_mismatch", "invalid partial-result metadata")
        detail = clean_text(detail, 500) if detail is not None else None
        if incomplete and not detail:
            detail = "Repository returned incomplete results; coverage is not exhaustive."
        return incomplete, detail or None

    def _cache_key(self, source: dict[str, Any], query: str, limit: int) -> str:
        relevant = {key: source.get(key) for key in sorted(source) if key not in {"effective_enabled", "origin", "pack_enabled"}}
        return self.cache.key(
            source["id"], self._auth_profile(source), query, limit,
            json.dumps(relevant, sort_keys=True, default=str),
        )

    @staticmethod
    def _auth_profile(source: dict[str, Any]) -> str:
        """Return a non-secret cache/health partition for the effective request mode."""
        names: set[str] = set()
        if isinstance(source.get("auth_env"), str):
            names.add(source["auth_env"])
        if isinstance(source.get("headers"), dict):
            for value in source["headers"].values():
                if isinstance(value, dict) and isinstance(value.get("env"), str):
                    names.add(value["env"])
        present = sorted(name for name in names if os.environ.get(name))
        if not present:
            return "anonymous"
        # Bind reuse to the configured credential profile without persisting a
        # token value (or a reversible derivative of one).
        profile = "\n".join(present)
        return "authenticated-profile:" + hashlib.sha256(profile.encode()).hexdigest()[:16]

    @staticmethod
    def _cooldown_until(retry_at: float | None) -> str | None:
        if retry_at is None:
            return None
        try:
            return datetime.fromtimestamp(retry_at, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    @staticmethod
    def _retry_at(exc: FinderHttpError) -> float | None:
        current = time.time()
        candidates: list[float] = []
        retry_after = exc.headers.get("retry-after")
        if retry_after:
            if retry_after.isdigit():
                candidates.append(current + int(retry_after))
            else:
                try:
                    value = parsedate_to_datetime(retry_after).timestamp()
                    if value > current:
                        candidates.append(value)
                except (TypeError, ValueError, OverflowError, OSError):
                    pass
        reset = exc.headers.get("x-ratelimit-reset")
        if reset and reset.isdigit() and float(int(reset)) > current:
            candidates.append(float(int(reset)))
        return max(candidates) if candidates else None

    def _has_usable_query_cache(self, source: dict[str, Any], query: str, limit: int, *, offline: bool, refresh: bool) -> bool:
        """Classify cache-first work without changing cache contents or policy."""
        if refresh and not offline:
            return False
        if ADAPTER_SPECS[source["adapter"]].cache_policy != "query":
            return False
        ttl = int(self.config.settings.get("cache_ttl_seconds", 300))
        cached = self.cache.read(
            "queries", self._cache_key(source, query, limit), max_age=None if offline else ttl
        )
        if not cached:
            return False
        payload, _age = cached
        return self._candidate_payload(payload) is not None

    def _fair_admission_order(
        self, sources: list[dict[str, Any]], query: str, limit: int, *, offline: bool, refresh: bool
    ) -> list[dict[str, Any]]:
        """Cache-first, deterministic, round-robin source admission.

        A round gives every origin a first start opportunity.  Within a round,
        origins with more remaining requests go first and source IDs settle
        ties.  This is deliberately an ordering helper, not a second deadline
        or worker supervisor.
        """
        cached = [
            source for source in sources
            if self._has_usable_query_cache(source, query, limit, offline=offline, refresh=refresh)
        ]
        uncached = [source for source in sources if source not in cached]

        def rounds(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            queues: dict[str, list[dict[str, Any]]] = {}
            for source in sorted(items, key=lambda item: item["id"]):
                origin = self._host(source) or f"source:{source['id']}"
                queues.setdefault(origin, []).append(source)
            ordered: list[dict[str, Any]] = []
            while queues:
                origins = sorted(queues, key=lambda origin: (-len(queues[origin]), origin))
                for origin in origins:
                    queue = queues.get(origin)
                    if queue:
                        ordered.append(queue.pop(0))
                    if not queue:
                        queues.pop(origin, None)
            return ordered

        return rounds(cached) + rounds(uncached)

    def _initial_admission_rounds(
        self, sources: list[dict[str, Any]], query: str, limit: int, *, offline: bool, refresh: bool,
        workers: int,
    ) -> list[list[dict[str, Any]]]:
        """Plan cache-first, origin-capped initial starts before launching work."""
        cached = [
            source for source in sources
            if self._has_usable_query_cache(source, query, limit, offline=offline, refresh=refresh)
        ]
        cached_ids = {source["id"] for source in cached}
        lanes = (cached, [source for source in sources if source["id"] not in cached_ids])
        rounds: list[list[dict[str, Any]]] = []
        for lane in lanes:
            queues: dict[str, list[dict[str, Any]]] = {}
            for source in sorted(lane, key=lambda item: item["id"]):
                queues.setdefault(self._host(source) or f"source:{source['id']}", []).append(source)
            while queues:
                round_sources: list[dict[str, Any]] = []
                started_by_origin: dict[str, int] = {}
                while len(round_sources) < workers:
                    origins = sorted(
                        (origin for origin, items in queues.items() if items and started_by_origin.get(origin, 0) < 2),
                        key=lambda origin: (-len(queues[origin]), origin),
                    )
                    if not origins:
                        break
                    origin = origins[0]
                    round_sources.append(queues[origin].pop(0))
                    started_by_origin[origin] = started_by_origin.get(origin, 0) + 1
                    if not queues[origin]:
                        queues.pop(origin)
                if round_sources:
                    rounds.append(round_sources)
        return rounds

    def _uses_builtin_adapter(self, source: dict[str, Any]) -> bool:
        """Only built-ins get a killable process boundary.

        Tests and embedders may inject adapters that close over in-memory state.
        Those cannot safely cross process start methods, so they use the guarded
        thread fallback below and can never publish to the frozen parent pool.
        """
        adapter = self.adapter_map.get(source["adapter"])
        return (
            isinstance(self.cache, Cache)
            and adapter is not None
            and type(adapter).__module__.startswith("universal_skill_finder.adapters.")
            and getattr(self._search_source, "__func__", None) is UniversalSkillFinder._search_source
            and type(self.http) is HttpClient
        )

    @staticmethod
    def _validated_candidates(payload: list[dict[str, Any]], source: dict[str, Any]) -> list[Candidate]:
        try:
            candidates = [Candidate.from_dict(item) for item in payload]
        except (TypeError, ValueError) as exc:
            raise SourceUnavailable("schema_mismatch", "invalid candidate fields") from exc
        for candidate, raw in zip(candidates, payload):
            # Cached/remote rows cannot impersonate a different source or trust tier.
            candidate.source_id = source["id"]
            candidate.source_kind = source["kind"]
            candidate.adapter = source["adapter"]
            candidate.trust = source.get("trust", "unverified")
            candidate.identity_namespace = source.get("identity_namespace")
            if source["adapter"] != "local-directory":
                # Remote adapters and source caches contribute discovery
                # evidence, never trusted link state. Only the reviewed,
                # separately cached anonymous validator may make a URL active.
                for proof in candidate.link_proofs:
                    if _is_remote_actionable_proof_status(proof.get("status")):
                        proof["status"] = "not_checked"
                        proof["detail"] = "remote candidate link requires reviewed destination validation"
                if candidate.target_proof:
                    candidate.target_proof["status"] = "not_checked"
                    candidate.target_proof["detail"] = "remote target requires fresh exact skill validation"
            if source["adapter"] == "local-directory":
                # Only the configured root and the scanner's relative identity
                # establish local authority. Never reuse an absolute cached path.
                candidate.repository = candidate.ref = candidate.publisher = None
                candidate.canonical_url = None
                candidate.install = {}
                relative = safe_skill_path(raw.get("native_id"))
                try:
                    if relative is None or UNSAFE_LOCATION_WARNING in candidate.warnings:
                        raise ValueError("invalid relative local skill path")
                    root = Path(source["path"]).expanduser().resolve(strict=True)
                    location = (root / relative).resolve(strict=True)
                    if not location.is_relative_to(root) or not location.is_dir():
                        raise ValueError("local skill path escapes the configured root")
                    candidate.skill_path = relative
                    candidate.canonical_url = location.as_uri()
                    candidate.install = {"kind": "local", "path": str(location), "requires_approval": True}
                except (OSError, RuntimeError, ValueError):
                    if UNSAFE_LOCATION_WARNING not in candidate.warnings:
                        candidate.warnings.append(UNSAFE_LOCATION_WARNING)
        return candidates

    def _query_cache_entry(
        self, source: dict[str, Any], key: str, *, max_age: int | None,
    ) -> tuple[list[Candidate], int, bool, str | None, int | None, str] | None:
        cached = self.cache.read("queries", key, max_age=max_age)
        if not cached:
            return None
        payload, age = cached
        candidate_payload = self._candidate_payload(payload)
        if candidate_payload is None:
            return None
        try:
            partial, detail = self._partial_metadata(
                payload.get("incomplete_results", False) if isinstance(payload, dict) else False,
                payload.get("detail") if isinstance(payload, dict) else None,
            )
            source_total = payload.get("source_total") if isinstance(payload, dict) else None
            if type(source_total) is not int or source_total < 0:
                source_total = None
            total_relation = payload.get("total_relation", "unknown") if isinstance(payload, dict) else "unknown"
            if total_relation not in {"exact", "lower_bound", "unknown"}:
                total_relation = "unknown"
            return self._validated_candidates(candidate_payload, source), age, partial, detail, source_total, total_relation
        except SourceUnavailable:
            return None

    def _health_scope(self, source: dict[str, Any]) -> HealthScope | None:
        if source.get("adapter") == "local-directory":
            return None
        endpoint = source.get("endpoint") or source.get("base_url")
        if isinstance(endpoint, str):
            parsed = urlparse(endpoint)
            endpoint = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}" if parsed.scheme and parsed.netloc else source["id"]
        elif source.get("repository"):
            endpoint = f"https://codeload.github.com/{source['repository']}"
        else:
            endpoint = source["id"]
        profile = self._auth_profile(source)
        # GitHub API rate limits are quota-scoped rather than source-scoped.
        # Keep anonymous and authenticated partitions separate without storing
        # a credential value, but share cooldown state across configured aliases.
        quota_group = "github-api" if source.get("adapter") == "github-code-search" else None
        scope_source_id = f"quota:{quota_group}" if quota_group is not None else source["id"]
        return HealthScope(
            source_id=scope_source_id, operation="search",
            operation_revision=f"adapter-{ADAPTER_CONTRACT_VERSION}:{source['adapter']}",
            endpoint=str(endpoint), auth_mode=profile, profile_partition=profile,
            quota_group=quota_group,
        )

    @staticmethod
    def _transient_failure(exc: Exception) -> bool:
        if isinstance(exc, FinderHttpError):
            return exc.rate_limited or exc.status is None or exc.status in {408, 425, 429} or (
                type(exc.status) is int and exc.status >= 500
            )
        if isinstance(exc, SourceUnavailable):
            return exc.status in {"rate_limited", "timeout", "network_error", "unavailable", "failed"}
        return isinstance(exc, (OSError, TimeoutError))

    def _search_source(
        self,
        source: dict[str, Any],
        query: str,
        limit: int,
        *,
        offline: bool,
        refresh: bool,
        deadline: Deadline | None = None,
        cancelled: Any = None,
    ) -> tuple[list[Candidate], Coverage]:
        started = time.monotonic()
        if (deadline is not None and deadline.collection_remaining() <= 0) or (cancelled is not None and cancelled.is_set()):
            raise SourceUnavailable("deadline_exceeded", "collection cutoff elapsed before source start")
        source_cache = _SourceCache(
            self.cache,
            lambda: (deadline is None or deadline.collection_remaining() > 0)
            and (cancelled is None or not cancelled.is_set()),
        ) if deadline is not None or cancelled is not None else self.cache
        # Keep the shared health lock, clock and injected behavior while giving
        # this job its own publication guard. Never mutate the parent store.
        source_health = copy.copy(self.health) if source_cache is not self.cache else self.health
        if source_health is not self.health:
            source_health.cache = source_cache
        host = self._host(source)
        adapter = self.adapter_map[source["adapter"]]
        query_cache = ADAPTER_SPECS[source["adapter"]].cache_policy == "query"
        key = self._cache_key(source, query, limit)
        ttl = int(self.config.settings.get("cache_ttl_seconds", 300))
        stale: tuple[list[Candidate], int, bool, str | None, int | None, str] | None = None
        if query_cache:
            fresh = self._query_cache_entry(source, key, max_age=None if offline else ttl)
            if fresh and (offline or not refresh):
                candidates, age, partial, detail, source_total, total_relation = fresh
                if (deadline is not None and deadline.collection_remaining() <= 0) or (cancelled is not None and cancelled.is_set()):
                    raise SourceUnavailable("deadline_exceeded", "collection cutoff elapsed before cached result publication")
                return candidates, Coverage(
                    source_id=source["id"], status="cached", result_count=len(candidates),
                    elapsed_ms=int((time.monotonic() - started) * 1000), cache_age_seconds=age,
                    host=host, target=self._target(source), incomplete_results=partial, detail=detail,
                    live_status="not_contacted", cache_status="fresh", health_status="not_contacted",
                    requested_limit=limit, effective_limit=limit,
                    source_total=source_total, total_relation=total_relation,
                )
            if not offline and not refresh:
                stale = self._query_cache_entry(source, key, max_age=900)
            if offline:
                available = [
                    item for item in self.cache.metadata("queries") if item.get("source_id") == source["id"]
                ]
                hint = ""
                if available:
                    examples = ", ".join(
                        f"{item['query']!r} (limit {item['limit']})" for item in available[:3]
                    )
                    hint = f"; cached: {examples}"
                raise SourceUnavailable(
                    "offline_miss",
                    f"no exact cached query for {source['id']} (query={query!r}, limit={limit}){hint}",
                )

        def cached_fallback(reason: str, health_status: str,
                            cooldown_until: str | None = None) -> tuple[list[Candidate], Coverage] | None:
            if stale is None:
                return None
            candidates, age, _partial, prior_detail, source_total, total_relation = stale
            detail = reason + (f"; cached response was already partial: {prior_detail}" if prior_detail else "")
            return candidates, Coverage(
                source_id=source["id"], status="cached", result_count=len(candidates),
                elapsed_ms=int((time.monotonic() - started) * 1000), cache_age_seconds=age,
                host=host, target=self._target(source), incomplete_results=True, detail=clean_text(detail, 500),
                live_status=health_status, cache_status="stale_fallback", health_status=health_status,
                cooldown_until=cooldown_until,
                requested_limit=limit, effective_limit=limit,
                source_total=source_total, total_relation=total_relation,
            )

        health_scope = self._health_scope(source)
        if health_scope is not None:
            health = source_health.before_request(health_scope)
            if not health.allowed:
                reason = "cached fallback; rate cooldown" if health.status == "rate_cooldown" else "cached fallback; source temporarily skipped"
                if health.status == "health_state_unavailable" and health.reason:
                    reason = "cached fallback; " + health.reason
                fallback = cached_fallback(reason, health.status, self._cooldown_until(health.retry_at))
                if fallback is not None:
                    return fallback
                if ADAPTER_SPECS[source["adapter"]].cache_policy == "catalogue" and not refresh:
                    fallback_context = AdapterContext(
                        http=self.http, cache=source_cache, settings=self.config.settings,
                        offline=True, refresh=False, stale_max_age_seconds=7 * 86_400,
                    )
                    try:
                        cached_candidates = adapter.search(source, query, limit, fallback_context)
                        candidates = self._validated_candidates(
                            [item.to_dict() for item in cached_candidates], source,
                        )
                    except (FinderHttpError, SourceUnavailable, RuntimeLimitError, OSError):
                        pass
                    else:
                        return candidates, Coverage(
                            source_id=source["id"], status="cached", result_count=len(candidates),
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            cache_age_seconds=fallback_context.cache_age_seconds,
                            host=host, target=self._target(source), incomplete_results=True,
                            detail=reason, live_status=health.status,
                            cache_status="stale_fallback", health_status=health.status,
                            cooldown_until=self._cooldown_until(health.retry_at),
                            requested_limit=limit, effective_limit=limit,
                        )
                raise SourceUnavailable(health.status, health.reason or "source temporarily skipped")

        flight = None
        if query_cache and deadline is not None:
            scope = self._auth_profile(source)
            while True:
                claim = self.cache.singleflight.claim("queries", key, scope, deadline)
                if claim.leader:
                    flight = claim
                    break
                if claim.state == "expired":
                    raise SourceUnavailable("deadline_exceeded", "query-cache lease wait exceeded collection cutoff")
                fresh = self._query_cache_entry(source, key, max_age=ttl)
                if fresh is not None:
                    candidates, age, partial, detail, source_total, total_relation = fresh
                    return candidates, Coverage(
                        source_id=source["id"], status="cached", result_count=len(candidates),
                        elapsed_ms=int((time.monotonic() - started) * 1000), cache_age_seconds=age,
                        host=host, target=self._target(source), incomplete_results=partial, detail=detail,
                        live_status="coalesced", cache_status="fresh", health_status="not_contacted",
                        requested_limit=limit, effective_limit=limit,
                        source_total=source_total, total_relation=total_relation,
                    )
        context = AdapterContext(
            http=self.http, cache=source_cache, settings=self.config.settings, offline=offline, refresh=refresh
        )
        try:
            raw_candidates = adapter.search(source, query, limit, context)
            if (deadline is not None and deadline.collection_remaining() <= 0) or (cancelled is not None and cancelled.is_set()):
                raise SourceUnavailable("deadline_exceeded", "collection cutoff elapsed before source publication")
            candidates = self._validated_candidates([item.to_dict() for item in raw_candidates], source)
            partial, detail = self._partial_metadata(context.incomplete_results, context.detail)
            cooldown_until = None
            if health_scope is not None:
                if context.rate_limit_headers is not None:
                    limited = FinderHttpError(
                        "source returned partial results before rate limiting", status=429,
                        rate_limited=True, headers=context.rate_limit_headers,
                    )
                    retry_at = self._retry_at(limited)
                    retry_at = source_health.record_rate_limit(health_scope, retry_at)
                    cooldown_until = self._cooldown_until(retry_at)
                else:
                    source_health.record_success(health_scope)
            if query_cache:
                source_cache.write("queries", key, {
                    "source_id": source["id"], "query": query, "limit": limit,
                    "cached_at": datetime.now(timezone.utc).isoformat(),
                    "candidates": [item.to_dict() for item in candidates],
                    "incomplete_results": partial, "detail": detail,
                    "source_total": context.source_total, "total_relation": context.total_relation,
                })
            return candidates, Coverage(
                source_id=source["id"], status="cached" if context.cache_age_seconds is not None else "ok",
                result_count=len(candidates), elapsed_ms=int((time.monotonic() - started) * 1000),
                cache_age_seconds=context.cache_age_seconds, host=host, target=self._target(source),
                incomplete_results=partial, detail=detail, live_status="ok", cache_status="miss",
                health_status="rate_cooldown" if cooldown_until else "closed",
                cooldown_until=cooldown_until,
                requested_limit=limit, effective_limit=context.effective_limit or limit,
                source_total=context.source_total, total_relation=context.total_relation,
            )
        except Exception as exc:
            transient = self._transient_failure(exc)
            if health_scope is not None:
                if isinstance(exc, FinderHttpError) and exc.rate_limited:
                    retry_at = self._retry_at(exc)
                    retry_at = source_health.record_rate_limit(health_scope, retry_at)
                    exc.cooldown_until = self._cooldown_until(retry_at)
                else:
                    source_health.record_failure(
                        health_scope, transient=transient,
                        local_cause=isinstance(exc, SourceUnavailable) and exc.status in {"deadline_exceeded", "preempted", "not_started_budget"},
                    )
            if transient and not refresh:
                status = exc.status if isinstance(exc, SourceUnavailable) else "rate_limited" if isinstance(exc, FinderHttpError) and exc.rate_limited else "failed"
                fallback = cached_fallback("cached fallback; source request failed", status)
                if fallback is not None:
                    return fallback
            raise
        finally:
            if flight is not None:
                flight.release()

    @staticmethod
    def _failure_coverage(source: dict[str, Any], exc: Exception, elapsed_ms: int) -> Coverage:
        status = "failed"
        detail = clean_text(str(exc), 500)
        if isinstance(exc, SourceUnavailable):
            status = exc.status
            detail = clean_text(exc.detail, 500)
        elif isinstance(exc, FinderHttpError):
            if exc.rate_limited:
                status = "rate_limited"
                if exc.retry_after is not None:
                    detail = clean_text(f"{detail}; retry after {exc.retry_after}", 500)
            elif exc.status in {401, 403}:
                status = "auth_failed"
            elif "timed out" in detail.lower() or "timeout" in detail.lower():
                status = "timeout"
            elif "invalid json" in detail.lower():
                status = "schema_mismatch"
        return Coverage(
            source_id=source["id"], status=status, result_count=0, elapsed_ms=elapsed_ms,
            detail=detail, host=UniversalSkillFinder._host(source), target=UniversalSkillFinder._target(source),
            incomplete_results=status in {"deadline_exceeded", "not_started_budget", "preempted"},
            cooldown_until=getattr(exc, "cooldown_until", None),
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        max_results: int = 10,
        count: int | None = None,
        page_size: int = 10,
        source_ids: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        offline: bool = False,
        refresh: bool = False,
        dry_run: bool = False,
        thorough: bool = False,
        progress_callback: Any = None,
        preview: bool = False,
    ) -> SearchReport:
        started_at = time.monotonic()
        query = clean_text(query, 500)
        if len(query) < 2:
            raise ConfigurationError("search query must contain at least two characters")
        if type(max_results) is not int or not 1 <= max_results <= 500:
            raise ConfigurationError("max_results must be an integer from 1 to 500")
        if count is not None and (type(count) is not int or not 1 <= count <= 100):
            raise ConfigurationError("count must be an integer from 1 to 100")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ConfigurationError("page_size must be an integer from 1 to 100")
        requested_count = count if count is not None else max_results
        # Schema 2 deliberately interprets legacy max_results as the overall
        # snapshot cap. It never bypasses the separately bounded page size.
        display_cap = min(requested_count, page_size)
        known = {source["id"] for source in self.config.sources}
        requested = set(source_ids or [])
        excluded = set(exclude_ids or [])
        unknown = (requested | excluded) - known
        if unknown:
            raise ConfigurationError("unknown repository ID(s): " + ", ".join(sorted(unknown)))

        policy = NetworkPolicy.for_mode(thorough)
        deadline = Deadline.start(policy)
        # Provisional proofs are useful only while collection is still open.
        # Their deadline is deliberately not the later shared-network tail, so
        # joining their executor cannot leave anonymous proof traffic running
        # after this search has frozen its accepted pool.
        provisional_deadline = Deadline(
            deadline.started_at, deadline.collection_cutoff_at,
            min(deadline.collection_cutoff_at, deadline.network_deadline_at), 0.0,
        )
        preview_publication_deadline = provisional_deadline
        preview_publication_open = threading.Event()
        preview_publication_open.set()
        preview_publication_lock = threading.Lock()

        def can_publish_preview() -> bool:
            return preview_publication_open.is_set() and preview_publication_deadline.remaining() > 0

        collection_frozen_at: float | None = None
        supervisor = WorkerSupervisor(deadline)
        validation_budget: Any = RequestBudget(
            requests=policy.validation_requests,
            validation_requests=policy.validation_requests,
            github_api_requests=policy.github_api_requests,
        )
        provisional_budget: Any = RequestBudget(
            requests=policy.provisional_validation_requests,
            validation_requests=policy.provisional_validation_requests,
            github_api_requests=policy.provisional_github_api_requests,
        )
        validation_permits = PermitPool(policy)
        shared_resources: SharedNetworkResources | None = None
        source_budget: RequestBudget | None = None
        source_permits: PermitPool | None = None
        # Provisional proof scheduling is independent of preview visibility.
        # `--preview` changes only whether eligible evidence is emitted.
        provisional_enabled = bool(
            not offline and not dry_run and self.validation_transport is not None
        )
        preview_candidates: list[Candidate] = []
        preview_attempted: set[str] = set()
        preview_futures: dict[Future[Any], Result] = {}
        preview_failures: set[str] = set()
        preview_executor = ThreadPoolExecutor(
            max_workers=policy.provisional_validation_workers,
            thread_name_prefix="skill-preview",
        ) if provisional_enabled else None
        preview_executor_closed = False

        def close_preview_publication() -> None:
            """Revoke cache authority and stop accepting queued preview work."""
            nonlocal preview_executor_closed
            with preview_publication_lock:
                preview_publication_open.clear()
                should_shutdown = preview_executor is not None and not preview_executor_closed
                preview_executor_closed = True
            if should_shutdown:
                preview_executor.shutdown(wait=False, cancel_futures=True)

        def schedule_previews() -> None:
            if preview_executor is None or len(preview_attempted) >= policy.provisional_identities:
                return
            for item in filter(self._result_relevance_admissible, self._merge(list(preview_candidates), query)):
                if item.id in preview_attempted:
                    continue
                destinations, _expected = self._validation_destinations([item])
                if not destinations:
                    preview_attempted.add(item.id)
                    continue
                # One ranked primary per identity bounds three provisional
                # identities to at most 12 requests including redirects.
                destination = destinations[0]
                preview_attempted.add(item.id)
                future = preview_executor.submit(
                    self._cached_destination_proof, destination, phase="provisional",
                    budget=provisional_budget, permits=validation_permits, deadline=provisional_deadline,
                    can_publish=can_publish_preview,
                    publish_lock=preview_publication_lock,
                )
                preview_futures[future] = item
                if len(preview_attempted) >= policy.provisional_identities:
                    break

        def poll_previews(*, emit_events: bool) -> None:
            for future, item in list(preview_futures.items()):
                if not future.done():
                    continue
                preview_futures.pop(future, None)
                try:
                    proof = future.result()
                except Exception:
                    # A failed optional check grants no proof. Preserve only a
                    # fixed report diagnostic, never arbitrary exception text.
                    preview_failures.add(item.id)
                    continue
                if proof.status == "eligible" and emit_events:
                    emit_progress(progress_callback, ProgressEvent(
                        "early_verified", query=query, proof_id=item.id,
                        status="eligible", name=item.name, description=item.description,
                        link_proof=asdict(proof),
                    ))
        coverage_by_id: dict[str, Coverage] = {}
        runnable: list[dict[str, Any]] = []
        finished_sources = 0
        for source in self.config.sources:
            source_id = source["id"]
            if requested and source_id not in requested:
                coverage_by_id[source_id] = Coverage(source_id=source_id, status="not_selected", host=self._host(source), target=self._target(source))
            elif source_id in excluded:
                coverage_by_id[source_id] = Coverage(source_id=source_id, status="excluded", host=self._host(source), target=self._target(source))
            elif not source.get("effective_enabled", False):
                detail = "repository disabled" if not source.get("enabled", True) else f"pack disabled: {source.get('pack')}"
                coverage_by_id[source_id] = Coverage(source_id=source_id, status="disabled", detail=detail, host=self._host(source), target=self._target(source))
            elif dry_run:
                coverage_by_id[source_id] = Coverage(source_id=source_id, status="planned", host=self._host(source), target=self._target(source))
            else:
                runnable.append(source)

        emit_progress(progress_callback, ProgressEvent(
            "search_started", query=query, total=len(runnable),
            status="offline" if offline else "dry_run" if dry_run else "searching",
        ))

        if runnable:
            # Spawned built-in workers otherwise receive independent in-memory
            # counters. Give them proxy-backed state shared with parent proof work.
            if any(self._uses_builtin_adapter(source) for source in runnable):
                shared_resources = SharedNetworkResources(
                    policy,
                    validation_requests=policy.validation_requests,
                )
                source_budget = shared_resources.budget
                source_permits = shared_resources.permits
                validation_budget = shared_resources.budget.for_phase("validation")
                validation_permits = shared_resources.permits
            workers = min(len(runnable), policy.source_workers, int(self.config.settings.get("max_workers", 6)))
            admission_rounds = self._initial_admission_rounds(
                runnable, query, limit, offline=offline, refresh=refresh, workers=max(1, workers)
            )
            workers = min(
                len(runnable), policy.source_workers, int(self.config.settings.get("max_workers", 6)),
            )
            # Injected adapters can retain in-memory fixtures and cannot be
            # moved to a spawn process. A preempted Python thread cannot be
            # killed, so it continues to occupy a physical source-worker slot
            # until it exits. Do not queue a replacement behind that detached
            # thread: doing so would make a later source appear admitted while
            # exceeding the real worker cap or starting after collection.
            pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="skill-source")
            active: dict[str, dict[str, Any]] = {}
            detached_threads: set[Future[Any]] = set()
            source_round = {
                source["id"]: round_index
                for round_index, sources in enumerate(admission_rounds)
                for source in sources
            }
            started_ids: set[str] = set()
            current_round = 0
            reserve = min(0.05 * max(0, len(admission_rounds) - 1), policy.collection_seconds / 4)
            usable_collection = max(0.0, policy.collection_seconds - reserve)
            round_boundaries = [
                deadline.started_at + usable_collection * (index + 1) / len(admission_rounds)
                for index in range(max(0, len(admission_rounds) - 1))
            ]

            def next_unstarted(maximum_round: int) -> dict[str, Any] | None:
                for round_index, sources in enumerate(admission_rounds):
                    if round_index > maximum_round:
                        break
                    for source in sources:
                        if source["id"] not in started_ids:
                            return source
                return None

            def submit_source(source: dict[str, Any]) -> bool:
                started_ids.add(source["id"])
                outcome = supervisor.submit(SourceJob(source["id"], self._host(source) or source["id"]))
                if outcome.status != "submitted":
                    coverage_by_id[source["id"]] = Coverage(
                        source_id=source["id"], status=outcome.status, detail=outcome.detail,
                        host=self._host(source), target=self._target(source),
                        admission_status="not_started_budget", live_status="not_started",
                    )
                    return False
                started = time.monotonic()
                if self._uses_builtin_adapter(source):
                    context = multiprocessing.get_context("spawn")
                    output = context.Queue(maxsize=1)
                    process = context.Process(
                        target=_process_source_runner,
                        args=(self.config, self.cache.root, source, query, limit, offline, refresh,
                              deadline.collection_cutoff_at, policy, output, source_budget, source_permits), daemon=True,
                    )
                    process.start()
                    active[source["id"]] = {
                        "kind": "process", "handle": process, "output": output, "source": source,
                        "started": started, "round": source_round[source["id"]],
                    }
                else:
                    cancelled = threading.Event()
                    future = pool.submit(
                        self._search_source, source, query, limit, offline=offline, refresh=refresh,
                        deadline=deadline, cancelled=cancelled,
                    )
                    active[source["id"]] = {
                        "kind": "thread", "handle": future, "cancelled": cancelled, "source": source,
                        "started": started, "round": source_round[source["id"]],
                    }
                supervisor.started(source["id"])
                return True

            def reap_detached_threads() -> None:
                completed = {future for future in detached_threads if future.done()}
                detached_threads.difference_update(completed)

            def fill_available(maximum_round: int) -> None:
                reap_detached_threads()
                # ``active`` represents admitted live work. Detached injected
                # futures represent logically preempted but still-running work.
                # Both consume the same physical worker budget.
                while len(active) + len(detached_threads) < workers:
                    source = next_unstarted(maximum_round)
                    if source is None:
                        return
                    submit_source(source)

            def source_finished(source: dict[str, Any], started: float, candidates: list[Candidate] | None,
                                coverage: Coverage | None, failure: Exception | None = None) -> None:
                nonlocal finished_sources
                if failure is None and candidates is not None and coverage is not None:
                    candidates = [
                        candidate for candidate in candidates
                        if self._relevance_admissible(candidate, query)
                    ]
                    coverage.result_count = len(candidates)
                    if supervisor.publish(source["id"], (candidates, coverage)):
                        coverage.admission_status = "admitted"
                        coverage.live_status = coverage.live_status or coverage.status
                        coverage.cache_status = coverage.cache_status or ("hit" if coverage.status == "cached" else "miss")
                        coverage_by_id[source["id"]] = coverage
                        if provisional_enabled:
                            preview_candidates.extend(candidates)
                            schedule_previews()
                    else:
                        coverage_by_id[source["id"]] = Coverage(
                            source_id=source["id"], status="deadline_exceeded", detail="late publication rejected",
                            host=self._host(source), target=self._target(source),
                            admission_status="admitted", live_status="deadline_exceeded", incomplete_results=True,
                        )
                else:
                    failure = failure or RuntimeError("source returned no outcome")
                    supervisor.fail(source["id"], str(failure))
                    coverage_by_id[source["id"]] = self._failure_coverage(
                        source, failure, int((time.monotonic() - started) * 1000)
                    )
                    coverage_by_id[source["id"]].admission_status = "admitted"
                    coverage_by_id[source["id"]].live_status = coverage_by_id[source["id"]].status
                    if coverage_by_id[source["id"]].status in {"circuit_open", "half_open_wait", "rate_cooldown"}:
                        coverage_by_id[source["id"]].health_status = coverage_by_id[source["id"]].status
                finished_sources += 1
                item = coverage_by_id[source["id"]]
                emit_progress(progress_callback, ProgressEvent(
                    "source_finished", query=query, source_id=source["id"], completed=finished_sources,
                    total=len(runnable), status=item.status, candidate_count=item.result_count, elapsed_ms=item.elapsed_ms,
                ))

            def reap_process(item: dict[str, Any], *, preempt: bool) -> None:
                process = item["handle"]
                if process.is_alive():
                    process.terminate()
                process.join(timeout=0.05 if preempt else 0.25)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(timeout=0.05 if preempt else 0.25)
                if source_permits is not None:
                    source_permits.reclaim_owner(process.pid)
                item["output"].close()

            def preempt_source(source_id: str) -> None:
                nonlocal finished_sources
                item = active.pop(source_id)
                source = item["source"]
                if item["kind"] == "process":
                    reap_process(item, preempt=True)
                else:
                    item["cancelled"].set()
                    future: Future[Any] = item["handle"]
                    cancelled = future.cancel()
                    if not cancelled and not future.done():
                        detached_threads.add(future)
                supervisor.fail(source_id, "initial admission round preempted")
                coverage_by_id[source_id] = Coverage(
                    source_id=source_id, status="preempted", detail="preempted to admit a later initial source",
                    host=self._host(source), target=self._target(source), admission_status="preempted", live_status="preempted",
                    incomplete_results=True,
                )
                finished_sources += 1
                emit_progress(progress_callback, ProgressEvent(
                    "source_finished", query=query, source_id=source_id, completed=finished_sources,
                    total=len(runnable), status="preempted", candidate_count=0,
                ))

            def advance_round_if_due() -> None:
                nonlocal current_round
                if current_round >= len(round_boundaries) or time.monotonic() < round_boundaries[current_round]:
                    return
                next_round = current_round + 1
                required = [source for source in admission_rounds[next_round] if source["id"] not in started_ids]
                missing_slots = max(0, len(required) - max(0, workers - len(active)))
                victims = sorted(
                    (item for item in active.values() if item["round"] < next_round),
                    key=lambda item: (item["round"], item["source"]["id"]),
                )[:missing_slots]
                for item in victims:
                    preempt_source(item["source"]["id"])
                current_round = next_round
                fill_available(current_round)

            def poll_active() -> None:
                reap_detached_threads()
                completed_ids: list[str] = []
                for source_id, item in sorted(active.items()):
                    source, started = item["source"], item["started"]
                    if item["kind"] == "thread":
                        future: Future[tuple[list[Candidate], Coverage]] = item["handle"]
                        if not future.done():
                            continue
                        completed_ids.append(source_id)
                        try:
                            candidates, coverage = future.result()
                            source_finished(source, started, candidates, coverage)
                        except Exception as exc:
                            source_finished(source, started, None, None, exc)
                        continue
                    try:
                        message = item["output"].get_nowait()
                    except queue.Empty:
                        if item["handle"].is_alive():
                            continue
                        try:
                            message = item["output"].get(timeout=0.01)
                        except queue.Empty:
                            completed_ids.append(source_id)
                            source_finished(source, started, None, None, RuntimeError("source process exited without an outcome"))
                            continue
                    completed_ids.append(source_id)
                    if message[0] == "ok":
                        source_finished(source, started, message[1], message[2])
                    elif message[0] == "source_unavailable":
                        source_finished(source, started, None, None, SourceUnavailable(message[1], message[2]))
                    elif message[0] == "http_error":
                        error = FinderHttpError(message[4], status=message[1], retry_after=message[2], rate_limited=message[3])
                        error.cooldown_until = message[5]
                        if error.rate_limited and error.cooldown_until is None:
                            error.cooldown_until = self._cooldown_until(self._retry_at(error))
                        source_finished(source, started, None, None, error)
                    else:
                        source_finished(source, started, None, None, RuntimeError(message[2]))
                for source_id in completed_ids:
                    item = active.pop(source_id)
                    if item["kind"] == "process":
                        reap_process(item, preempt=False)
                reap_detached_threads()
                poll_previews(emit_events=bool(
                    preview and progress_callback is not None and deadline.collection_remaining() > 0
                ))

            collection_completed = False
            try:
                fill_available(0)
                while (active or len(started_ids) < len(runnable)) and deadline.collection_remaining() > 0:
                    poll_active()
                    fill_available(min(current_round + 1, len(admission_rounds) - 1))
                    advance_round_if_due()
                    if deadline.collection_remaining() > 0:
                        next_boundary = round_boundaries[current_round] if current_round < len(round_boundaries) else deadline.collection_cutoff_at
                        time.sleep(min(0.01, deadline.collection_remaining(), max(0.0, next_boundary - time.monotonic())))
                collection_completed = True
            finally:
                cleanup_completed = False
                try:
                    # Freeze first: no completed worker can publish while processes
                    # are being terminated or injected threads finish in the background.
                    if deadline.collection_remaining() <= 0:
                        for sources in admission_rounds:
                            for source in sources:
                                if source["id"] in started_ids:
                                    continue
                                started_ids.add(source["id"])
                                outcome = supervisor.submit(SourceJob(source["id"], self._host(source) or source["id"]))
                                coverage_by_id[source["id"]] = Coverage(
                                    source_id=source["id"], status=outcome.status, detail=outcome.detail,
                                    host=self._host(source), target=self._target(source),
                                    admission_status="not_started_budget", live_status="not_started", incomplete_results=True,
                                )
                    collection_frozen_at = time.monotonic()
                    supervisor.freeze("collection_cutoff")
                    for item in active.values():
                        if item["kind"] == "process":
                            item["handle"].terminate()
                    for item in active.values():
                        if item["kind"] == "process":
                            reap_process(item, preempt=False)
                        else:
                            item["cancelled"].set()
                            item["handle"].cancel()
                    # Python cannot safely kill an arbitrary injected thread. It is
                    # detached from the frozen parent and its deadline-aware source
                    # path cannot write the query cache after cutoff.
                    pool.shutdown(wait=False, cancel_futures=True)
                    cleanup_completed = True
                finally:
                    if not collection_completed or not cleanup_completed:
                        close_preview_publication()

        if collection_frozen_at is None:
            collection_frozen_at = time.monotonic()
        final_validation_deadline = Deadline(
            deadline.started_at, deadline.collection_cutoff_at,
            deadline.validation_deadline(collection_frozen_at), deadline.validation_cap_seconds,
        )
        # When collection finishes before its nominal cutoff, the shorter
        # final-validation tail becomes the publication boundary for any
        # provisional worker still in flight.
        with preview_publication_lock:
            preview_publication_deadline = final_validation_deadline
        # Production requests are process-bounded. Also bound joins themselves:
        # an injected callback that violates its timeout cannot delay this
        # result or publish late proof/cache state. Such in-process callbacks
        # remain responsible for stopping their own outstanding I/O.
        if preview_executor is not None:
            try:
                for future, item in list(preview_futures.items()):
                    try:
                        proof = future.result(timeout=final_validation_deadline.remaining())
                    except Exception:
                        future.cancel()
                        preview_failures.add(item.id)
                        continue
                    # Provisional validation can emit an unnumbered preview and
                    # warm the exact proof cache. It never grants final authority.
            finally:
                close_preview_publication()
                preview_futures.clear()

        accepted, outcomes = supervisor.freeze("collection_complete")
        for outcome in outcomes:
            if outcome.source_id not in coverage_by_id:
                source = next(item for item in self.config.sources if item["id"] == outcome.source_id)
                coverage_by_id[outcome.source_id] = Coverage(
                    source_id=outcome.source_id, status=outcome.status, detail=outcome.detail,
                    host=self._host(source), target=self._target(source),
                    admission_status=outcome.status, live_status=outcome.status,
                    incomplete_results=outcome.status in {
                        "deadline_exceeded", "not_started_budget", "preempted", "checkpointed",
                    },
                )
        candidates = [
            candidate
            for source_id in sorted(accepted)
            for candidate in accepted[source_id][0]
        ]
        ranking_started_at = time.monotonic()
        emit_progress(progress_callback, ProgressEvent(
            "ranking_started", query=query, completed=len(candidates), total=len(candidates), status="started",
        ))
        ranked_pool = [
            item for item in self._merge(candidates, query)
            if self._result_relevance_admissible(item)
        ]
        ranking_elapsed_ms = int((time.monotonic() - ranking_started_at) * 1000)
        validation_started_at = time.monotonic()
        emit_progress(progress_callback, ProgressEvent(
            "validation_started", query=query, completed=0, total=len(ranked_pool), status="not_checked",
        ))
        validation_run = _ValidationRun()
        if not offline and not dry_run:
            validation_outcome = self._validate_ranked_pool(
                ranked_pool, deadline, policy, budget=validation_budget, permits=validation_permits,
                validation_deadline=final_validation_deadline,
                requested_size=display_cap,
            )
            validation_run = validation_outcome if isinstance(validation_outcome, _ValidationRun) else _ValidationRun(
                deferred_count=len(ranked_pool),
                stop_reason="validation_state_unavailable",
                stopped_reason="destination-verification completion state is unavailable",
            )
        partitions: dict[str, list[Result]] = {
            "eligible": [], "unavailable": [], "inconclusive": [], "not_checked": [],
        }
        for item in ranked_pool:
            item.validation_status = self._validation_status(item)
            partitions[item.validation_status].append(item)
        # Replenishment is a stable filter over the single frozen ranked pool:
        # an unavailable or unchecked identity never occupies a normal card.
        materialized = partitions["eligible"][:display_cap]
        for number, item in enumerate(materialized, 1):
            item.result_number = number
        validation_elapsed_ms = int((time.monotonic() - validation_started_at) * 1000)
        emit_progress(progress_callback, ProgressEvent(
            "validation_finished", query=query, completed=len(materialized), total=len(ranked_pool),
            status="eligible" if materialized else "not_checked",
            elapsed_ms=validation_elapsed_ms,
        ))
        if offline:
            results: list[Result] = []
            candidate_previews = [self._candidate_preview(item, offline=True) for item in ranked_pool[:3]]
        else:
            results = materialized
            candidate_previews = [self._candidate_preview(item) for item in ranked_pool[:3]] if not materialized else []
        for source in self.config.sources:
            # Enablement is configuration state, independent of availability or
            # explicit per-query filters. Keep it visible even on failed sources.
            coverage_by_id[source["id"]].enabled = bool(source.get("effective_enabled", False))
            coverage_by_id[source["id"]].public_url = public_source_url(source)
            # Repository discovery reads optional stars only from the separate
            # metadata cache. No secondary metadata host is contacted here.
            coverage_by_id[source["id"]].metadata_hosts = []
            coverage_by_id[source["id"]].requested_limit = limit
            if coverage_by_id[source["id"]].effective_limit is None:
                coverage_by_id[source["id"]].effective_limit = limit
            coverage_by_id[source["id"]].shown = sum(
                1 for result in results if source["id"] in result.source_ids
            )
        ordered_coverage = [coverage_by_id[source["id"]] for source in self.config.sources]
        report = SearchReport(
            query=query,
            results=results,
            coverage=ordered_coverage,
            generated_at=datetime.now(timezone.utc).isoformat(),
            configuration_path=str(self.config.overlay_path),
            provenance={
                **release_metadata(),
                "effective_configuration_revision": effective_config_revision(
                    self.config.settings, self.config.packs, self.config.sources
                ),
            },
            mode="offline_preview" if offline else "dry_run" if dry_run else "online",
            requested_count=requested_count,
            page_size=page_size,
            accepted_occurrences=len(candidates),
            unique_count=len(ranked_pool),
            eligible_count=len(partitions["eligible"]),
            unavailable_count=len(partitions["unavailable"]),
            inconclusive_count=len(partitions["inconclusive"]),
            not_checked_count=len(partitions["not_checked"]),
            validation_checked_count=validation_run.checked_count,
            validation_deferred_count=validation_run.deferred_count,
            validation_stop_reason=validation_run.stop_reason,
            validation_stopped_reason=validation_run.stopped_reason,
            page_incomplete=(
                0 < len(results) < display_cap
                and validation_run.stopped_reason is not None
            ),
            page_start=1 if results else 0,
            page_shown=len(results),
            materialized_total=len(results),
            candidate_previews=candidate_previews,
            preview_count=len(candidate_previews),
            snapshot={
                "status": "available_to_persist",
                "reason": "pass --report-json to save this frozen pool",
                "ordered_pool_size": len(ranked_pool),
                "ranking_algorithm": ranked_pool[0].ranking.get("algorithm_version") if ranked_pool else None,
                "ordered_pool": [item.id for item in ranked_pool],
                "result_records": {item.id: item.to_dict() for item in ranked_pool},
                "ranking_traces": {item.id: dict(item.ranking) for item in ranked_pool},
                "ranking_pool": [
                    {
                        "id": item.id,
                        "score": item.ranking.get("score"),
                        "algorithm_version": item.ranking.get("algorithm_version"),
                    }
                    for item in ranked_pool
                ],
            },
            timings={
                "collection_ms": int((ranking_started_at - started_at) * 1000),
                "ranking_ms": ranking_elapsed_ms,
                "validation_ms": validation_elapsed_ms,
                "total_ms": int((time.monotonic() - started_at) * 1000),
                "collection_cutoff_seconds": policy.collection_seconds,
                "network_deadline_seconds": policy.shared_seconds,
                # Safe counters only. Values, headers, and credential material
                # never enter the report or the live-source summary.
                "request_budget": validation_budget.snapshot(),
                "provisional_request_budget": provisional_budget.snapshot(),
            },
            notes=(self._verification_notes(ordered_coverage, ranked_pool) + (
                ["An optional early destination check did not complete; only final eligible results are shown."]
                if preview_failures else []
            )) if not offline and not dry_run else [],
        )
        emit_progress(progress_callback, ProgressEvent(
            "search_finished", query=query, completed=len(runnable), total=len(runnable),
            status=report.mode, candidate_count=len(ranked_pool), elapsed_ms=report.timings["total_ms"],
        ))
        if shared_resources is not None:
            shared_resources.close()
        return report

    @staticmethod
    def _verification_notes(coverage: list[Coverage], results: list[Result]) -> list[str]:
        """Summarize known local failures without relaying arbitrary error text."""
        notes: list[str] = []
        details = [row.detail or "" for row in coverage if row.status == "health_state_unavailable"
                   or row.health_status == "health_state_unavailable"]
        unresolved = [item for item in results if item.validation_status != "eligible"]
        resolution_failures = 0
        for item in unresolved:
            proofs = [item.target_proof, *item.link_proofs]
            failure_details = [proof.get("detail", "") for proof in proofs if isinstance(proof, dict)
                               and proof.get("status") in {"inconclusive", "not_checked"}]
            resolution_failures += int("destination resolution failed" in failure_details)
            details.extend(detail for detail in failure_details if isinstance(detail, str))
        if any(detail.startswith(("cache access denied;", "cached fallback; cache access denied;")) for detail in details):
            notes.append("Cache access denied. Allow access to the configured cache directory shown by doctor, then retry. Do not delete health state or switch caches to bypass cooldowns.")
        elif any(detail.startswith(("cache storage unavailable;", "cached fallback; cache storage unavailable;")) for detail in details):
            notes.append("Cache storage is unavailable. Check the configured cache directory shown by doctor before retrying; source cooldowns were not bypassed.")
        if resolution_failures:
            noun = "candidate" if resolution_failures == 1 else "candidates"
            notes.append(f"Destination resolver could not complete checks for {resolution_failures} {noun}. Check DNS/network access, sandbox permissions and resolver-process availability before retrying. These are unverified candidates, not confirmed missing skills.")
        return notes

    @staticmethod
    def _candidate_preview(result: Result, *, offline: bool = False) -> dict[str, Any]:
        return {
            "name": result.name,
            "description": result.description,
            "repository": result.repository,
            "source_ids": list(result.source_ids),
            "reason": "offline cached candidate; destination not checked" if offline else f"destination {result.validation_status}",
        }

    def _validation_destinations(self, results: list[Result]) -> tuple[list[Any], dict[str, dict[str, Any]]]:
        """Build only adapter-preserved or exact repository proof requests."""
        source_by_id = {source["id"]: source for source in self.config.sources}
        destinations: list[Any] = []
        expected_by_id: dict[str, dict[str, Any]] = {}
        seen: set[tuple[str, str, str, str]] = set()

        def add(destination: Destination, expected: dict[str, Any]) -> bool:
            if destination.profile is None:
                return False
            key = (
                destination.candidate_id, destination.role, destination.url or "",
                json.dumps(destination.expected_identity, sort_keys=True, ensure_ascii=True, default=str),
            )
            if key in seen:
                return False
            seen.add(key)
            destinations.append(destination)
            expected_by_id[destination.candidate_id] = expected
            return True

        for result in results:
            for occurrence in result.occurrences:
                if not isinstance(occurrence, dict):
                    continue
                adapter = occurrence.get("adapter")
                source = source_by_id.get(occurrence.get("source_id"), {})
                evidence = occurrence.get("source_evidence")
                expected = evidence.get("expected_identity") if isinstance(evidence, dict) else None
                listing_url = occurrence.get("listing_url")
                listing_role = occurrence.get("listing_role")
                listing_added = False
                if isinstance(listing_url, str) and listing_role in {
                    "listing", "bundle_listing", "source_page", "repository",
                }:
                    destination = reviewed_destination(
                        result.id, role=listing_role, url=listing_url, adapter=str(adapter), expected_identity=expected,
                    )
                    listing_added = add(destination, {
                        "kind": "reviewed_listing", "expected": destination.expected_identity,
                    })
                if (not listing_added and adapter in {"skillhub-public", "skillhub-pro"}
                        and isinstance(source.get("base_url"), str)):
                    native_id = occurrence.get("native_id")
                    if isinstance(native_id, str) and native_id:
                        try:
                            destination = skillhub_destination(
                                result.id, base_url=source["base_url"], native_id=native_id, expected_identity=expected,
                            )
                            add(destination, {
                                "kind": "skillhub_listing", "expected": expected or {"id": native_id},
                            })
                        except ValueError:
                            pass
                repository, ref, skill_path = occurrence.get("repository"), occurrence.get("ref"), occurrence.get("skill_path")
                if all(isinstance(value, str) and value for value in (repository, ref, skill_path)):
                    try:
                        destination = github_skill_destination(
                            result.id, repository=repository, ref=ref, skill_path=skill_path,
                        )
                        add(destination, {
                            "kind": "github_tree", "expected": {"repository": repository, "ref": ref, "skill_path": skill_path},
                        })
                    except ValueError:
                        pass
        return destinations, expected_by_id

    def _validate_ranked_pool(
        self, results: list[Result], deadline: Deadline, policy: NetworkPolicy, *,
        budget: RequestBudget | None = None, permits: PermitPool | None = None,
        validation_deadline: Deadline | None = None,
        requested_size: int = 10,
    ) -> _ValidationRun:
        if self.validation_transport is None:
            count = len(results)
            return _ValidationRun(
                deferred_count=count,
                stop_reason="validation_unavailable" if count else "pool_exhausted",
                stopped_reason="destination verification is unavailable" if count else None,
            )
        destinations, _expected_by_id = self._validation_destinations(results)
        validation_deadline = validation_deadline or Deadline(
            deadline.started_at, deadline.collection_cutoff_at,
            deadline.validation_deadline(time.monotonic()), deadline.validation_cap_seconds,
        )
        budget = budget or RequestBudget(
            requests=policy.validation_requests, validation_requests=policy.validation_requests,
            github_api_requests=policy.github_api_requests,
        )
        permits = permits or PermitPool(policy)
        # A large source pool is not a request to check every destination now.
        # Replenish the first page in frozen rank order, at most 30 identities;
        # leave the remaining pool for an explicit continuation.
        deltas: dict[str, dict[str, Any]] = {}
        target_cache = TargetResolutionCache()
        checked_results = results[:MAX_PAGE_SCAN]
        offset = 0

        def validation_budget_exhausted() -> bool:
            state = budget.snapshot().get("validation", {})
            return (
                type(state.get("used")) is int
                and type(state.get("limit")) is int
                and state["used"] >= state["limit"]
            )

        while offset < len(checked_results):
            if validation_deadline.remaining() <= 0:
                break
            if validation_budget_exhausted():
                break
            eligible = sum(
                self._validation_status(item) == "eligible"
                or deltas.get(item.id, {}).get("status") == "eligible"
                for item in checked_results[:offset]
            )
            if eligible >= requested_size:
                break
            batch = checked_results[offset:offset + min(policy.final_validation_workers, requested_size - eligible)]
            offset += len(batch)
            def check(item: Result) -> tuple[str, dict[str, Any]]:
                outcome = validate_record(
                    item.id, item.to_dict(),
                    destinations=[destination for destination in destinations if destination.candidate_id == item.id],
                    transport=self.validation_transport, resolver=self.validation_resolver,
                    budget=budget, permits=permits, deadline=validation_deadline,
                    target_cache=target_cache,
                    validate_one=lambda destination: self._cached_destination_proof(
                        destination, phase="final", budget=budget, permits=permits,
                        deadline=validation_deadline,
                    ),
                )
                return item.id, outcome

            workers = ThreadPoolExecutor(max_workers=policy.final_validation_workers)
            futures = [workers.submit(check, item) for item in batch]
            try:
                for future in futures:
                    try:
                        result_id, delta = future.result(timeout=validation_deadline.remaining())
                    except TimeoutError:
                        future.cancel()
                        continue
                    deltas[result_id] = delta
            finally:
                workers.shutdown(wait=False, cancel_futures=True)
        by_id = {result.id: result for result in results}
        for result_id, delta in deltas.items():
            target = delta.get("target_proof")
            if isinstance(target, dict) and target:
                by_id[result_id].target_proof = dict(target)
                if target.get("status") == "eligible":
                    by_id[result_id].content_sha256 = target.get("content_sha256")
        proof_priority = {"not_checked": 0, "unavailable": 1, "inconclusive": 2, "eligible": 3}
        for result_id, delta in deltas.items():
            result = by_id[result_id]
            for raw_proof in delta.get("link_proofs", []):
                if not isinstance(raw_proof, dict):
                    continue
                fields = LinkProof.__dataclass_fields__
                try:
                    proof = LinkProof(**{key: value for key, value in raw_proof.items() if key in fields})
                except (TypeError, ValueError):
                    continue
                proof_data = asdict(proof)
                proof_key = (proof.role, proof.url)
                existing_index = next((
                    index for index, existing in enumerate(result.link_proofs)
                    if isinstance(existing, dict) and (existing.get("role"), existing.get("url")) == proof_key
                ), None)
                if existing_index is None:
                    result.link_proofs.append(proof_data)
                elif proof_priority.get(proof.status, -1) >= proof_priority.get(
                    str(result.link_proofs[existing_index].get("status", "not_checked")), -1,
                ):
                    result.link_proofs[existing_index] = proof_data
                if proof.status != "eligible":
                    continue
                for occurrence in result.occurrences:
                    if not isinstance(occurrence, dict) or occurrence.get("listing_url") != proof.url:
                        continue
                    attribution = {
                        "source_id": occurrence.get("source_id"), "label": occurrence.get("source_id"),
                        "role": proof.role, "url": proof.url, "status": "eligible",
                    }
                    native_rank = occurrence.get("native_rank")
                    if type(native_rank) is int and native_rank > 0:
                        attribution["native_rank"] = native_rank
                    if not any(existing == attribution for existing in result.attributions):
                        result.attributions.append(attribution)

        completed_count = len(deltas)
        eligible_count = sum(self._validation_status(item) == "eligible" for item in results)

        def stopped_by(delta: dict[str, Any], marker: str) -> bool:
            details = [delta.get("detail")]
            target = delta.get("target_proof")
            if isinstance(target, dict):
                details.append(target.get("detail"))
            proofs = delta.get("link_proofs")
            if isinstance(proofs, list):
                details.extend(proof.get("detail") for proof in proofs if isinstance(proof, dict))
            return any(
                isinstance(detail, str) and marker in detail.casefold()
                for detail in details
            )

        budget_blocked = sum(
            delta.get("status") == "not_checked" and stopped_by(delta, "budget")
            for delta in deltas.values()
        )
        deadline_blocked = sum(
            delta.get("status") == "not_checked" and stopped_by(delta, "deadline")
            for delta in deltas.values()
        )
        stop_reason: str | None = None
        stopped_reason: str | None = None
        checked_count = completed_count
        deferred_count = 0
        if eligible_count < requested_size:
            if budget_blocked or (validation_budget_exhausted() and completed_count < len(results)):
                stop_reason = "request_budget_reached"
                checked_count -= budget_blocked
                deferred_count = len(results) - completed_count + budget_blocked
                stopped_reason = (
                    "destination-verification request budget reached after final validation "
                    f"completed for {checked_count} of {len(results)} candidates"
                )
            elif deadline_blocked or (
                validation_deadline.remaining() <= 0 and completed_count < len(results)
            ):
                stop_reason = "deadline_reached"
                checked_count -= deadline_blocked
                deferred_count = len(results) - completed_count + deadline_blocked
                seconds = f"{policy.validation_seconds:g}"
                stopped_reason = (
                    f"{seconds}-second destination-verification deadline reached after final validation "
                    f"completed for {checked_count} of {len(results)} candidates"
                )
            elif len(results) > len(checked_results):
                stop_reason = "scan_limit_reached"
                deferred_count = len(results) - completed_count
                stopped_reason = (
                    f"{MAX_PAGE_SCAN}-candidate destination-verification scan limit reached after final validation "
                    f"completed for {checked_count} of {len(results)} candidates"
                )
        if stop_reason is None:
            stop_reason = "page_full" if eligible_count >= requested_size else "pool_exhausted"
        return _ValidationRun(
            checked_count=checked_count,
            deferred_count=deferred_count,
            stop_reason=stop_reason,
            stopped_reason=stopped_reason,
        )

    @staticmethod
    def _validation_status(result: Result) -> str:
        """Classify a merged identity without treating an unchecked URL as safe."""
        destinations = [
            (proof.get("role"), proof.get("status", "not_checked"))
            for proof in result.link_proofs
            if isinstance(proof, dict) and proof.get("role") in {
                "skill", "skill_destination", "repository", "listing", "bundle_listing", "source_page",
            }
        ]
        if any(status == "eligible" for _role, status in destinations):
            return "eligible"
        statuses = {str(status) for _role, status in destinations}
        target_status = result.target_proof.get("status") if isinstance(result.target_proof, dict) else None
        if target_status == "eligible" and "unavailable" in statuses:
            statuses.add("inconclusive")
        if target_status in {"inconclusive", "not_checked", "unavailable"}:
            statuses.add(target_status)
        if "inconclusive" in statuses:
            return "inconclusive"
        if "unavailable" in statuses:
            return "inconclusive" if "not_checked" in statuses else "unavailable"
        return "not_checked"

    def _merge(self, candidates: list[Candidate], query: str) -> list[Result]:
        candidates = sorted(candidates, key=lambda item: json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=True))
        parent = list(range(len(candidates)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        alias_to_candidate: dict[str, int] = {}
        strong_aliases: list[set[str]] = []
        for index, candidate in enumerate(candidates):
            aliases = occurrence_aliases(candidate)
            strong_aliases.append(aliases)
            for alias in aliases:
                if alias in alias_to_candidate:
                    union(index, alias_to_candidate[alias])
                else:
                    alias_to_candidate[alias] = index

        weak_to_candidates: dict[str, list[int]] = {}
        for index, candidate in enumerate(candidates):
            for alias in weak_occurrence_aliases(candidate):
                weak_to_candidates.setdefault(alias, []).append(index)
        for indexes in weak_to_candidates.values():
            path_roots = {find(index) for index in indexes if candidates[index].skill_path}
            pathless = [index for index in indexes if not candidates[index].skill_path]
            if len(path_roots) == 1:
                target = next(iter(path_roots))
                for index in pathless:
                    union(target, index)
            elif not path_roots and pathless:
                target = pathless[0]
                for index in pathless[1:]:
                    union(target, index)

        grouped: dict[int, dict[str, Any]] = {}
        for index, candidate in enumerate(candidates):
            root = find(index)
            group = grouped.setdefault(root, {"aliases": set(), "occurrences": []})
            group["aliases"].update(strong_aliases[index])
            group["occurrences"].append(candidate)

        results: list[Result] = []
        for group in grouped.values():
            occurrences: list[Candidate] = group["occurrences"]
            aliases: set[str] = group["aliases"]
            preferred = max(
                occurrences,
                key=lambda item: (
                    bool(item.repository and item.skill_path),
                    TRUST_PRIORITY.get(item.trust, 0),
                    len(item.description),
                    -item.native_rank,
                ),
            )
            source_ids = sorted({item.source_id for item in occurrences})
            metrics = {item.source_id: item.metrics for item in occurrences if item.metrics}
            warnings = sorted({warning for item in occurrences for warning in item.warnings})
            trust = sorted({item.trust for item in occurrences}, key=lambda value: (-TRUST_PRIORITY.get(value, 0), value))
            proofs_by_key: dict[str, dict[str, Any]] = {}
            attributions_by_key: dict[str, dict[str, Any]] = {}
            for item in occurrences:
                for proof in item.link_proofs:
                    if not isinstance(proof, dict):
                        continue
                    proof_key = json.dumps(proof, sort_keys=True, ensure_ascii=True, default=str)
                    proofs_by_key[proof_key] = dict(proof)
                    # An adapter-proven repository/local target is useful source
                    # attribution. Registry canonical/listing URLs remain absent
                    # until the validator supplies their own proof.
                    role = proof.get("role")
                    if role in {"repository", "skill_destination", "listing", "bundle_listing", "source_page"} and proof.get("status") == "eligible":
                        attribution = {
                            "source_id": item.source_id,
                            "label": item.source_id,
                            "role": role,
                            "url": proof.get("url"),
                            "status": proof.get("status", "not_checked"),
                        }
                        attribution_key = json.dumps(attribution, sort_keys=True, ensure_ascii=True, default=str)
                        attributions_by_key[attribution_key] = attribution
            target_proofs = [item.target_proof for item in occurrences if isinstance(item.target_proof, dict) and item.target_proof]
            target_proof = max(
                target_proofs,
                key=lambda proof: (proof.get("status") == "eligible", str(proof.get("checked_at", ""))),
                default={},
            )
            best_rank_by_source: dict[str, int] = {}
            for item in occurrences:
                best_rank_by_source[item.source_id] = min(
                    best_rank_by_source.get(item.source_id, item.native_rank or 1), item.native_rank or 1
                )
            rrf = sum(1.0 / (60 + max(1, rank)) for rank in best_rank_by_source.values())
            match = compatible_match_percent(
                query, preferred.name, preferred.description, preferred.skill_path or "", preferred.repository or "",
            )
            install = dict(preferred.install)
            if install:
                install["requires_approval"] = True
            results.append(Result(
                id=stable_result_id(aliases), name=preferred.name, description=preferred.description,
                canonical_url=preferred.canonical_url, repository=preferred.repository, skill_path=preferred.skill_path,
                ref=preferred.ref, publisher=preferred.publisher, content_sha256=preferred.content_sha256,
                source_ids=source_ids, trust=trust, text_match_percent=match, rank_fusion_score=round(rrf, 8),
                metrics_by_source=metrics, install=install, warnings=warnings,
                occurrences=[item.to_dict() for item in occurrences],
                link_proofs=[proofs_by_key[key] for key in sorted(proofs_by_key)],
                target_proof=dict(target_proof),
                attributions=[attributions_by_key[key] for key in sorted(attributions_by_key)],
            ))
        ranked = rank_results(results, query)
        for result, trace in ranked:
            self._attach_ranking_trace(result, trace)
        return [result for result, _trace in ranked]

    @staticmethod
    def _attach_ranking_trace(result: Result, trace: RankTrace) -> None:
        """Serialize the frozen shared RankingRecord into Result.ranking."""
        result.ranking = asdict(trace)
