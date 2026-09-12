"""Immutable schema-1 search snapshots and opaque continuation cursors.

Snapshots retain a frozen ordered pool and validation ledger.  This module has
no source, cache, transport, ranking, or rendering dependency.
"""
from __future__ import annotations

import base64
import hashlib
import inspect
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse

from .versioning import SNAPSHOT_SCHEMA_VERSION


MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
SNAPSHOT_LOCK_SECONDS = 0.050
MAX_PAGE_SCAN = 30
MAX_REPORT_METADATA_BYTES = 256 * 1024
_ID = re.compile(r"[a-f0-9]{32}")
_PROOF_MAX_AGE_SECONDS = 300
_LOCAL_PROOF_BASES = frozenset({"local_relative_path_content_sha256"})
_PORTABLE_ACTIONABLE_STATUSES = frozenset({"eligible", "verified", "reachable"})


class SnapshotError(ValueError):
    pass


def _is_portable_actionable_status(value: object) -> bool:
    """Demote canonical and legacy actionable spellings in portable data."""
    return isinstance(value, str) and value.lower() in _PORTABLE_ACTIONABLE_STATUSES


def _proof_time(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _stored_proof_is_current(proof: Mapping[str, Any], *, now: float | None = None) -> bool:
    """Accept only fresh bounded local proof from portable snapshot JSON.

    Snapshot files are deliberately portable JSON, not signed authority.  A
    valid JSON object therefore cannot preserve remote link authority, even if
    it copies a reviewed profile name and recent timestamp.  Remote links must
    pass the reviewed validator again when a continuation reaches them.
    """
    if proof.get("status") not in {"eligible", "verified"}:
        return False
    url = proof.get("url") or proof.get("final_url")
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    checked_at = _proof_time(proof.get("checked_at"))
    moment = time.time() if now is None else now
    if checked_at is None or checked_at > moment + 60 or moment - checked_at > _PROOF_MAX_AGE_SECONDS:
        return False
    if parsed.scheme == "file":
        return (proof.get("method") == "local_bounded_read"
                and proof.get("identity_basis") in _LOCAL_PROOF_BASES)
    return False


def _record_has_trusted_stored_proof(record: object) -> bool:
    if isinstance(record, Mapping):
        if _stored_proof_is_current(record):
            return True
        return any(_record_has_trusted_stored_proof(value) for value in record.values())
    if isinstance(record, list):
        return any(_record_has_trusted_stored_proof(value) for value in record)
    return False


def _sanitize_loaded_records(records: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Downgrade portable proof assertions before a snapshot is resumed.

    A snapshot is evidence for the frozen pool and its numbering, not a signed
    validation cache.  In particular, an exact-target flag has no URL-shaped
    field to trigger the ordinary link-proof check, but would otherwise gate an
    install action.  It must always be freshly re-established.
    """
    downgraded: set[str] = set()

    def visit(value: Any, candidate_id: str, field_name: str | None = None) -> Any:
        if isinstance(value, Mapping):
            updated = {key: visit(item, candidate_id, key) for key, item in value.items()}
            if (field_name == "target_proof"
                    and _is_portable_actionable_status(updated.get("status"))):
                updated["status"] = "not_checked"
                updated["detail"] = "stored target proof requires fresh exact target validation"
                downgraded.add(candidate_id)
            elif (_is_portable_actionable_status(updated.get("status"))
                    and isinstance(updated.get("url") or updated.get("final_url"), str)
                    and not _stored_proof_is_current(updated)):
                updated["status"] = "not_checked"
                updated["detail"] = "stored link proof is stale or lacks a reviewed validation record"
                downgraded.add(candidate_id)
            return updated
        if isinstance(value, list):
            return [visit(item, candidate_id, field_name) for item in value]
        return value

    return ({key: visit(value, key) for key, value in records.items()}, downgraded)


def _frozen(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _freeze_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 50:
        raise SnapshotError("snapshot JSON nesting exceeds bound")
    if value is None or type(value) in {bool, str, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise SnapshotError("snapshot JSON contains non-finite number")
        return value
    if isinstance(value, Mapping) and all(isinstance(key, str) for key in value):
        return MappingProxyType({key: _freeze_json(item, depth=depth + 1) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    raise SnapshotError("snapshot result record is not JSON-safe")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


_REPORT_METADATA_FIELDS = frozenset({
    "coverage", "accepted_occurrences", "unique_count", "merged_duplicates",
    "timings", "mode", "warnings", "provenance", "notes",
})


def _clean_metadata(value: Any, *, depth: int = 0) -> Any:
    """Keep the report context useful, bounded, and inert as snapshot data."""
    if depth > 20:
        raise SnapshotError("snapshot report metadata nesting exceeds bound")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise SnapshotError("snapshot report metadata contains non-finite number")
        return value
    if isinstance(value, str):
        # Rendering applies its own Markdown-safe escaping.  This removes control
        # characters now so a saved report cannot smuggle terminal controls into
        # a later page renderer.
        return "".join(character for character in value if character >= " " and character != "\x7f")[:4000]
    if isinstance(value, Mapping):
        if len(value) > 128 or not all(isinstance(key, str) and len(key) <= 128 for key in value):
            raise SnapshotError("snapshot report metadata mapping exceeds bound")
        return {
            key: _clean_metadata(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise SnapshotError("snapshot report metadata list exceeds bound")
        return [_clean_metadata(item, depth=depth + 1) for item in value]
    raise SnapshotError("snapshot report metadata is not JSON-safe")


def _report_metadata(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if value is None:
        return _frozen()
    if not isinstance(value, Mapping):
        raise SnapshotError("snapshot report metadata is invalid")
    clean = {
        key: _clean_metadata(item)
        for key, item in value.items()
        if key in _REPORT_METADATA_FIELDS
    }
    # Coverage is display provenance, never a portable permission to revive a
    # registry/listing card.  Demote any stored web proof there as well as the
    # result record proofs above.  It is intentionally conservative because a
    # later renderer has no validator context for coverage rows.
    def sanitize_coverage(value: Any) -> Any:
        if isinstance(value, Mapping):
            updated = {key: sanitize_coverage(item) for key, item in value.items()}
            if (_is_portable_actionable_status(updated.get("status"))
                    and isinstance(updated.get("url") or updated.get("final_url"), str)):
                updated["status"] = "not_checked"
                updated["detail"] = "stored coverage link proof requires fresh validation"
            return updated
        if isinstance(value, list):
            return [sanitize_coverage(item) for item in value]
        return value
    if "coverage" in clean:
        clean["coverage"] = sanitize_coverage(clean["coverage"])
    if len(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")) > MAX_REPORT_METADATA_BYTES:
        raise SnapshotError("snapshot report metadata exceeds byte bound")
    return _freeze_json(clean)


@dataclass(frozen=True)
class SearchSnapshot:
    snapshot_id: str
    query: str
    options: Mapping[str, Any]
    config_revision: str
    ordered_pool: tuple[str, ...]
    result_records: Mapping[str, Mapping[str, Any]] = field(default_factory=_frozen)
    ranking_traces: Mapping[str, Any] = field(default_factory=_frozen)
    validation_ledger: Mapping[str, Mapping[str, Any]] = field(default_factory=_frozen)
    result_numbers: Mapping[str, int] = field(default_factory=_frozen)
    requested_cap: int = 10
    page_size: int = 10
    cursor_history: Mapping[str, Mapping[str, Any]] = field(default_factory=_frozen)
    cap_extensions: tuple[Mapping[str, int], ...] = ()
    inventory_evidence: Mapping[str, Any] = field(default_factory=_frozen)
    report_metadata: Mapping[str, Any] = field(default_factory=_frozen)
    created_at: float = field(default_factory=time.time)
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SNAPSHOT_SCHEMA_VERSION or not _ID.fullmatch(self.snapshot_id):
            raise SnapshotError("invalid snapshot schema or identifier")
        if not 1 <= self.requested_cap <= 500 or not 1 <= self.page_size <= 100:
            raise SnapshotError("snapshot requested cap must be 1..500 and page size 1..100")
        if len(set(self.ordered_pool)) != len(self.ordered_pool):
            raise SnapshotError("frozen ordered pool contains duplicate identities")
        if (not isinstance(self.result_records, Mapping)
                or not all(isinstance(key, str) and isinstance(record, Mapping)
                           for key, record in self.result_records.items())):
            raise SnapshotError("snapshot result records are invalid")
        record_keys = set(self.result_records)
        if not record_keys <= set(self.ordered_pool):
            raise SnapshotError("snapshot records are not in the frozen pool")
        if record_keys and record_keys != set(self.ordered_pool):
            raise SnapshotError("snapshot records must cover the frozen pool when present")
        if (len(self.cap_extensions) > 100
                or any(not isinstance(item, Mapping)
                       or set(item) != {"from", "to"}
                       or type(item["from"]) is not int or type(item["to"]) is not int
                       or not 1 <= item["from"] < item["to"] <= 100
                       for item in self.cap_extensions)):
            raise SnapshotError("snapshot cap extension history is invalid")
        object.__setattr__(self, "options", _frozen(self.options))
        object.__setattr__(self, "result_records", _freeze_json(dict(self.result_records)))
        object.__setattr__(self, "ranking_traces", _frozen(self.ranking_traces))
        object.__setattr__(self, "validation_ledger", _frozen(self.validation_ledger))
        object.__setattr__(self, "result_numbers", _frozen(self.result_numbers))
        object.__setattr__(self, "cursor_history", _frozen(self.cursor_history))
        object.__setattr__(self, "cap_extensions", tuple(_frozen(item) for item in self.cap_extensions))
        object.__setattr__(self, "inventory_evidence", _frozen(self.inventory_evidence))
        object.__setattr__(self, "report_metadata", _report_metadata(self.report_metadata))


@dataclass(frozen=True)
class Page:
    ids: tuple[str, ...]
    next_cursor: str | None
    resume_cursor: str | None
    snapshot: SearchSnapshot
    reused: bool = False
    scanned_count: int = 0
    is_exhausted: bool = False
    has_pending: bool = False
    pending_detail: str | None = None


def create_snapshot(*, query: str, options: Mapping[str, Any], config_revision: str, ordered_pool: list[str] | tuple[str, ...],
                    requested_cap: int = 10, page_size: int = 10, ranking_traces: Mapping[str, Any] | None = None,
                    inventory_evidence: Mapping[str, Any] | None = None,
                    report_metadata: Mapping[str, Any] | None = None,
                    result_records: Mapping[str, Mapping[str, Any]] | None = None) -> SearchSnapshot:
    return SearchSnapshot(snapshot_id=uuid.uuid4().hex, query=query, options=dict(options), config_revision=config_revision,
                          ordered_pool=tuple(ordered_pool), result_records=dict(result_records or {}),
                          ranking_traces=_frozen(ranking_traces), validation_ledger=_frozen(), result_numbers=_frozen(),
                          requested_cap=requested_cap, page_size=page_size, cursor_history=_frozen(),
                          cap_extensions=(),
                          inventory_evidence=_frozen(inventory_evidence), report_metadata=_report_metadata(report_metadata))


def seed_initial_page(snapshot: SearchSnapshot, candidate_ids: Iterable[str]) -> Page:
    """Persist an already-validated live first page without re-scanning its pool.

    Federation validates the ranked pool before it chooses the first display
    page. Replaying a separate bounded snapshot scan here can omit a selected
    candidate beyond that scan window, losing a safe card while source
    coverage still refers to it. This records only that authoritative,
    already-eligible page. Later pages still use fresh validation over the
    frozen pool.
    """
    ids = tuple(candidate_ids)
    if len(set(ids)) != len(ids) or not set(ids) <= set(snapshot.ordered_pool):
        raise SnapshotError("initial page identities are not a unique frozen-pool subset")
    if len(ids) > min(snapshot.page_size, snapshot.requested_cap):
        raise SnapshotError("initial page exceeds the configured display cap")
    if snapshot.result_numbers or snapshot.cursor_history:
        raise SnapshotError("initial page can only seed a new snapshot")

    numbers = {candidate_id: number for number, candidate_id in enumerate(ids, 1)}
    ledger = dict(snapshot.validation_ledger)
    for candidate_id in ids:
        ledger[candidate_id] = {"status": "eligible", "detail": "live first-page destination validation"}
    seeded = replace(snapshot, validation_ledger=_frozen(ledger), result_numbers=_frozen(numbers))
    resume_cursor = next_safe_cursor(seeded)
    next_cursor = resume_cursor if len(ids) < seeded.requested_cap else None
    if ids:
        root = encode_cursor(seeded.snapshot_id, 0, 0)
        seeded = replace(seeded, cursor_history=_frozen({
            root: {
                "ids": list(ids), "next_cursor": next_cursor, "resume_cursor": resume_cursor,
                "scanned_count": 0, "has_pending": False, "pending_detail": None,
            },
        }))
    return Page(ids, next_cursor, resume_cursor, seeded, is_exhausted=resume_cursor is None)


def _cursor_data(snapshot_id: str, position: int, materialized: int) -> dict[str, Any]:
    data = {"v": SNAPSHOT_SCHEMA_VERSION, "s": snapshot_id, "p": position, "m": materialized}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    return {**data, "h": digest}


def encode_cursor(snapshot_id: str, position: int, materialized: int) -> str:
    if not _ID.fullmatch(snapshot_id) or position < 0 or materialized < 0:
        raise SnapshotError("invalid cursor fields")
    raw = json.dumps(_cursor_data(snapshot_id, position, materialized), sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_fields(token: str, snapshot_id: str) -> tuple[int, int]:
    if not isinstance(token, str) or len(token) > 512:
        raise SnapshotError("malformed cursor")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("malformed cursor") from exc
    if not isinstance(data, dict):
        raise SnapshotError("malformed cursor")
    expected = _cursor_data(snapshot_id, data.get("p"), data.get("m")) if isinstance(data.get("p"), int) and isinstance(data.get("m"), int) else None
    if not expected or data != expected:
        raise SnapshotError("tampered, mismatched, or stale cursor")
    return data["p"], data["m"]


def _is_deferred(proof: object) -> bool:
    return isinstance(proof, Mapping) and proof.get("status") == "not_checked" and proof.get("deferred") is True


def _is_scanned_without_number(proof: object) -> bool:
    if not isinstance(proof, Mapping):
        return False
    return proof.get("status") in {"unavailable", "inconclusive"} or _is_deferred(proof)


def _safe_continuation_state(snapshot: SearchSnapshot) -> tuple[int, int]:
    """Find the next frozen identity without trusting cursor-history output."""
    numbers = dict(snapshot.result_numbers)
    _validate_result_numbers(numbers, snapshot.ordered_pool)
    position = 0
    while position < len(snapshot.ordered_pool):
        candidate_id = snapshot.ordered_pool[position]
        if candidate_id in numbers or _is_scanned_without_number(snapshot.validation_ledger.get(candidate_id)):
            position += 1
            continue
        break
    return position, len(numbers)


def next_safe_cursor(snapshot: SearchSnapshot) -> str | None:
    """Return the source-query-free continuation/retry cursor, if the pool remains.

    This intentionally derives state from immutable numbered results and the
    validation ledger rather than an optional old cursor-history entry.  It
    therefore recovers snapshots written by the former empty-page bug without
    reopening discovery or inventing a cursor position.
    """
    position, materialized = _safe_continuation_state(snapshot)
    if position >= len(snapshot.ordered_pool):
        return None
    return encode_cursor(snapshot.snapshot_id, position, materialized)


def decode_cursor(token: str, snapshot: SearchSnapshot) -> tuple[int, int]:
    position, materialized = _cursor_fields(token, snapshot.snapshot_id)
    if position > len(snapshot.ordered_pool) or materialized > snapshot.requested_cap:
        raise SnapshotError("cursor outside frozen snapshot")
    root = encode_cursor(snapshot.snapshot_id, 0, 0)
    known = token == root or token in snapshot.cursor_history or any(
        entry.get("next_cursor") == token or entry.get("resume_cursor") == token
        for entry in snapshot.cursor_history.values() if isinstance(entry, Mapping)
    ) or token == next_safe_cursor(snapshot)
    if not known:
        raise SnapshotError("cursor is not in this snapshot's continuation lineage")
    return position, materialized


def _can_validate(gate: Callable[..., bool], candidate_id: str,
                  record: Mapping[str, Any] | None) -> bool:
    """Allow callers with a shared request budget to stop before dispatch."""
    try:
        signature = inspect.signature(gate)
        try:
            signature.bind(candidate_id, record)
        except TypeError:
            return bool(gate())
    except (TypeError, ValueError):
        return bool(gate())
    return bool(gate(candidate_id, record))


def _is_unscheduled_not_checked(proof: Mapping[str, Any]) -> bool:
    """Recognize the bounded validator's explicit no-dispatch outcomes."""
    if proof.get("status") != "not_checked":
        return False
    detail = proof.get("detail")
    if not isinstance(detail, str):
        return False
    normalized = detail.lower()
    return any(marker in normalized for marker in (
        "not scheduled", "budget", "deadline", "in flight", "lease unavailable",
    ))


def materialize_page(snapshot: SearchSnapshot, cursor: str | None, *, validate: Callable[..., Mapping[str, Any]],
                     page_size: int | None = None,
                     can_validate: Callable[..., bool] | None = None) -> Page:
    """Validate only the next frozen slice. ``validate`` must not source-search."""
    size = page_size or snapshot.page_size
    if not 1 <= size <= 100:
        raise SnapshotError("invalid page size")
    if cursor is None:
        position, materialized = 0, 0
        cursor = encode_cursor(snapshot.snapshot_id, 0, 0)
    else:
        position, materialized = decode_cursor(cursor, snapshot)
    _check_materialized_state(snapshot, cursor, materialized)
    previous = snapshot.cursor_history.get(cursor)
    if previous is not None:
        previous_ids = previous.get("ids")
        if not isinstance(previous_ids, (list, tuple)) or not all(isinstance(item, str) for item in previous_ids):
            raise SnapshotError("snapshot cursor history is invalid")
        expected_numbers = list(range(materialized + 1, materialized + len(previous_ids) + 1))
        actual_numbers = [snapshot.result_numbers.get(candidate_id) for candidate_id in previous_ids]
        if actual_numbers != expected_numbers:
            # Older sparse-page snapshots could append a result that had
            # already been numbered on the seeded first page. Preserve the
            # genuinely new part of that page and discard only those repeats.
            repaired_ids = [
                candidate_id for candidate_id in previous_ids
                if isinstance(snapshot.result_numbers.get(candidate_id), int)
                and snapshot.result_numbers[candidate_id] > materialized
            ]
            repaired_ids.sort(key=lambda candidate_id: snapshot.result_numbers[candidate_id])
            repaired_numbers = [snapshot.result_numbers[candidate_id] for candidate_id in repaired_ids]
            if (len(set(repaired_ids)) != len(repaired_ids)
                    or repaired_numbers != list(range(materialized + 1, materialized + len(repaired_ids) + 1))):
                raise SnapshotError("snapshot cursor history numbering is inconsistent")
            safe_cursor = next_safe_cursor(snapshot)
            next_cursor = safe_cursor if len(snapshot.result_numbers) < snapshot.requested_cap else None
            return Page(tuple(repaired_ids), next_cursor, safe_cursor, snapshot, reused=True,
                        scanned_count=int(previous.get("scanned_count", 0) or 0),
                        is_exhausted=safe_cursor is None,
                        has_pending=bool(previous.get("has_pending", False)),
                        pending_detail=previous.get("pending_detail"))
        safe_cursor = next_safe_cursor(snapshot)
        return Page(tuple(previous_ids), previous.get("next_cursor"),
                    previous.get("resume_cursor", previous.get("next_cursor")), snapshot, reused=True,
                    scanned_count=int(previous.get("scanned_count", 0) or 0),
                    is_exhausted=safe_cursor is None,
                    has_pending=bool(previous.get("has_pending", False)),
                    pending_detail=previous.get("pending_detail"))
    selected: list[str] = []
    records = _thaw_json(snapshot.result_records)
    ledger = dict(snapshot.validation_ledger)
    numbers = dict(snapshot.result_numbers)
    cap = min(size, snapshot.requested_cap - materialized)
    start_position = position
    scanned_count = 0
    has_pending = False
    pending_detail: str | None = None
    while (position < len(snapshot.ordered_pool) and len(selected) < cap
           and scanned_count < MAX_PAGE_SCAN):
        candidate_id = snapshot.ordered_pool[position]
        if candidate_id in numbers:
            # A seeded first page can be a sparse subset of the frozen pool.
            # Continuation resumes at the earliest hole and must pass already
            # numbered identities without emitting or validating them again.
            position += 1
            continue
        proof = ledger.get(candidate_id)
        if proof is None or (proof.get("status") == "not_checked" and not _is_deferred(proof)):
            if can_validate is not None and not _can_validate(can_validate, candidate_id, snapshot.result_records.get(candidate_id)):
                has_pending = True
                pending_detail = "validation budget or deadline reached before this destination was scheduled"
                break
            outcome = _validation_outcome(_call_validator(validate, candidate_id, snapshot.result_records.get(candidate_id)))
            proof = {key: value for key, value in outcome.items() if key not in {"link_proofs", "target_proof"}}
            if _is_unscheduled_not_checked(proof):
                has_pending = True
                pending_detail = proof.get("detail")
                break
            if proof.get("status") == "not_checked":
                # No reviewed destination is a recorded deferral, not an excuse
                # to burn the entire tail or retry this identity on every page.
                proof["deferred"] = True
            if "link_proofs" in outcome or "target_proof" in outcome:
                record = records.get(candidate_id)
                if record is None:
                    raise SnapshotError("snapshot has no frozen result record for evidence update")
                records[candidate_id] = _update_record_evidence(record, outcome)
            ledger[candidate_id] = proof
        if proof.get("status") == "eligible":
            if candidate_id not in numbers:
                numbers[candidate_id] = max([materialized, *numbers.values()], default=materialized) + 1
            selected.append(candidate_id)
        position += 1
        scanned_count += 1
    resume_cursor = encode_cursor(snapshot.snapshot_id, position, materialized + len(selected)) if position < len(snapshot.ordered_pool) else None
    next_cursor = resume_cursor if materialized + len(selected) < snapshot.requested_cap else None
    history = dict(snapshot.cursor_history)
    # A no-progress budget stop must be retryable.  Persisting an empty history
    # for it would make the same cursor idempotently return empty forever.
    if position != start_position or selected:
        history[cursor] = {
            "ids": list(selected), "next_cursor": next_cursor, "resume_cursor": resume_cursor,
            "scanned_count": scanned_count, "has_pending": has_pending,
            "pending_detail": pending_detail,
        }
    updated = replace(snapshot, result_records=_frozen(records), validation_ledger=_frozen(ledger),
                      result_numbers=_frozen(numbers), cursor_history=_frozen(history))
    return Page(tuple(selected), next_cursor, resume_cursor, updated,
                scanned_count=scanned_count, is_exhausted=position >= len(snapshot.ordered_pool),
                has_pending=has_pending, pending_detail=pending_detail)


def extend_snapshot(snapshot: SearchSnapshot, requested_cap: int) -> SearchSnapshot:
    """Raise a public snapshot cap without changing its frozen candidate pool."""
    if type(requested_cap) is not int or not 1 <= requested_cap <= 100:
        raise SnapshotError("extended count must be 1..100")
    if snapshot.requested_cap >= 100:
        raise SnapshotError("snapshot is already at the public result cap")
    if requested_cap <= snapshot.requested_cap:
        raise SnapshotError("extended count must exceed the saved snapshot cap")
    extensions = (*snapshot.cap_extensions, {"from": snapshot.requested_cap, "to": requested_cap})
    return replace(snapshot, requested_cap=requested_cap, cap_extensions=extensions)


def _call_validator(validate: Callable[..., Mapping[str, Any]], candidate_id: str,
                    record: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Support legacy ``validate(id)`` and record-aware ``validate(id, record)``."""
    try:
        signature = inspect.signature(validate)
        try:
            signature.bind(candidate_id, record)
        except TypeError:
            return validate(candidate_id)
    except (TypeError, ValueError):
        return validate(candidate_id)
    return validate(candidate_id, record)


def _validation_outcome(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotError("validator must return an object")
    allowed = {"status", "detail", "link_proofs", "target_proof"}
    if set(value) - allowed or value.get("status") not in {"eligible", "unavailable", "inconclusive", "not_checked"}:
        raise SnapshotError("validator returned an invalid proof update")
    if "detail" in value and not isinstance(value["detail"], str):
        raise SnapshotError("validator detail is invalid")
    outcome = dict(value)
    if "link_proofs" in outcome:
        links = outcome["link_proofs"]
        if not isinstance(links, (list, tuple)) or len(links) > 20:
            raise SnapshotError("validator link proof update is invalid")
        for link in links:
            if not isinstance(link, Mapping) or link.get("status") not in {"eligible", "unavailable", "inconclusive", "not_checked"}:
                raise SnapshotError("validator link proof update is invalid")
    if "target_proof" in outcome and not isinstance(outcome["target_proof"], Mapping):
        raise SnapshotError("validator target proof update is invalid")
    try:
        _freeze_json(outcome)
    except SnapshotError as exc:
        raise SnapshotError("validator proof update is not JSON-safe") from exc
    return outcome


def _update_record_evidence(record: Mapping[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
    updated = _thaw_json(record)
    if not isinstance(updated, dict):
        raise SnapshotError("frozen result record is invalid")
    if "link_proofs" in outcome:
        existing = updated.get("link_proofs", [])
        if not isinstance(existing, list):
            raise SnapshotError("frozen result record link proofs are invalid")
        links = [_thaw_json(value) for value in outcome["link_proofs"]]
        # Portable remote proofs are demoted on load and then checked again.
        # Replace the same exact destination instead of appending a stale copy;
        # keep fresh outcomes first so newly checked source links are never
        # crowded out by an older 20-proof record.
        merged: list[Any] = []
        seen: set[tuple[object, object]] = set()

        def proof_key(value: object) -> tuple[object, object]:
            if not isinstance(value, Mapping):
                return (None, None)
            return (value.get("role"), value.get("url") or value.get("final_url"))

        for link in [*links, *existing]:
            key = proof_key(link)
            if key in seen:
                continue
            seen.add(key)
            merged.append(link)
        updated["link_proofs"] = merged[:20]

        # Loading a portable snapshot correctly demotes its old attribution
        # links. Rebuild source attribution only when this validation pass has
        # independently proved the exact native listing URL retained in the
        # frozen occurrence. A fresh generic/repository proof must not be
        # relabelled as a registry result.
        occurrences = updated.get("occurrences", [])
        existing_attributions = updated.get("attributions", [])
        if not isinstance(occurrences, list) or not isinstance(existing_attributions, list):
            raise SnapshotError("frozen result record attribution evidence is invalid")
        fresh_attributions: list[dict[str, Any]] = []
        for link in links:
            if not isinstance(link, Mapping) or link.get("status") != "eligible":
                continue
            url, role = link.get("url") or link.get("final_url"), link.get("role")
            if not isinstance(url, str) or role not in {"listing", "bundle_listing", "source_page", "repository"}:
                continue
            for occurrence in occurrences:
                if (not isinstance(occurrence, Mapping)
                        or occurrence.get("listing_url") != url
                        or occurrence.get("listing_role") != role):
                    continue
                source_id = occurrence.get("source_id")
                if not isinstance(source_id, str) or not source_id:
                    continue
                attribution: dict[str, Any] = {
                    "source_id": source_id, "label": source_id, "role": role,
                    "url": url, "status": "eligible",
                }
                native_rank = occurrence.get("native_rank")
                if type(native_rank) is int and native_rank > 0:
                    attribution["native_rank"] = native_rank
                fresh_attributions.append(attribution)

        attribution_keys: set[tuple[object, object, object]] = set()
        rebuilt_attributions: list[Any] = []
        for attribution in [*fresh_attributions, *existing_attributions]:
            if not isinstance(attribution, Mapping):
                continue
            key = (attribution.get("source_id"), attribution.get("role"), attribution.get("url"))
            if key in attribution_keys:
                continue
            attribution_keys.add(key)
            rebuilt_attributions.append(attribution)
        updated["attributions"] = rebuilt_attributions[:20]
    if "target_proof" in outcome:
        current = updated.get("target_proof")
        proposed = _thaw_json(outcome["target_proof"])
        if isinstance(current, Mapping):
            # A page validator may replace archive/local evidence with a fresh
            # exact raw-target proof.  Its kind, resolved URL, and hash are
            # validation facts, not ranking identity.  The reported identity
            # remains frozen and must agree with both the record and any prior
            # target proof that supplied it.
            proposed_reported = proposed.get("reported")
            if not isinstance(proposed_reported, Mapping):
                raise SnapshotError("validator target proof lacks reported identity")
            previous_reported = current.get("reported")
            for key in ("repository", "ref", "skill_path", "name"):
                frozen_value = updated.get(key)
                if frozen_value is not None and proposed_reported.get(key) != frozen_value:
                    raise SnapshotError("validator attempted to mutate frozen target identity")
                if isinstance(previous_reported, Mapping):
                    prior_value = previous_reported.get(key)
                    if prior_value is not None and proposed_reported.get(key) != prior_value:
                        raise SnapshotError("validator attempted to mutate frozen target identity")
        updated["target_proof"] = proposed
    _freeze_json(updated)
    return updated


def _check_materialized_state(snapshot: SearchSnapshot, cursor: str, materialized: int) -> None:
    """Reject cursors that claim results not retained in persisted state."""
    numbers = list(snapshot.result_numbers.values())
    _validate_result_numbers(dict(snapshot.result_numbers), snapshot.ordered_pool)
    if materialized == 0:
        return
    history = snapshot.cursor_history.get(cursor)
    # A continuation after any materialized result is usable only if its
    # preceding numbered state was persisted.  Its own history is permitted
    # for a repeated page request; otherwise it must begin exactly after the
    # persisted prefix.
    if history is not None:
        if len(numbers) < materialized:
            raise SnapshotError("cursor materialized state is not persisted")
        return
    if len(numbers) != materialized or set(numbers) != set(range(1, materialized + 1)):
        raise SnapshotError("cursor materialized state disagrees with snapshot")


def _validate_result_numbers(numbers: Mapping[str, Any], ordered_pool: tuple[str, ...]) -> None:
    values = list(numbers.values())
    if (not set(numbers) <= set(ordered_pool)
            or len(set(values)) != len(values)
            or any(type(number) is not int or number < 1 for number in values)
            or set(values) != set(range(1, len(values) + 1))):
        raise SnapshotError("snapshot result numbering is invalid")


def snapshot_dict(snapshot: SearchSnapshot) -> dict[str, Any]:
    return {
        "schema_version": snapshot.schema_version, "snapshot_id": snapshot.snapshot_id, "query": snapshot.query,
        "options": dict(snapshot.options), "config_revision": snapshot.config_revision, "ordered_pool": list(snapshot.ordered_pool),
        "result_records": _thaw_json(snapshot.result_records),
        "ranking_traces": dict(snapshot.ranking_traces), "validation_ledger": dict(snapshot.validation_ledger),
        "result_numbers": dict(snapshot.result_numbers), "requested_cap": snapshot.requested_cap, "page_size": snapshot.page_size,
        "cursor_history": dict(snapshot.cursor_history), "cap_extensions": [_thaw_json(item) for item in snapshot.cap_extensions],
        "inventory_evidence": dict(snapshot.inventory_evidence), "created_at": snapshot.created_at,
        "report_metadata": _thaw_json(snapshot.report_metadata),
    }


def _recover_empty_history(snapshot_id: str, ordered_pool: tuple[str, ...],
                           ledger: Mapping[str, Mapping[str, Any]],
                           history: Mapping[str, Any]) -> dict[str, Any]:
    """Discard retryable empty entries whose remaining tail was not validated.

    Only entries with no emitted IDs or cursors and no completed proof in the
    remaining tail are eligible. Any page that emitted a result or observed an
    unavailable or inconclusive target remains immutable and is never renumbered.
    """
    recovered = dict(history)
    for token, entry in tuple(recovered.items()):
        if not isinstance(token, str) or not isinstance(entry, Mapping):
            continue
        if entry.get("ids") not in ([], ()) or entry.get("next_cursor") is not None or entry.get("resume_cursor") is not None:
            continue
        try:
            position, _materialized = _cursor_fields(token, snapshot_id)
        except SnapshotError:
            continue
        if position >= len(ordered_pool):
            continue
        tail = ordered_pool[position:]
        if all(
            (candidate_id not in ledger
             or ledger[candidate_id].get("status") == "not_checked")
            for candidate_id in tail
        ):
            recovered.pop(token, None)
    return recovered


def load_snapshot(path: Path) -> SearchSnapshot:
    """Read one bounded regular snapshot file without following links."""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise SnapshotError("snapshot does not exist") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SnapshotError("snapshot must be a regular non-symlink file")
    if info.st_size > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot exceeds byte bound")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
    except OSError as exc:
        raise SnapshotError("cannot read snapshot") from exc
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot exceeds byte bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError("snapshot JSON is malformed") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotError("snapshot schema is stale or invalid")
    required_strings = ("snapshot_id", "query", "config_revision")
    if any(not isinstance(value.get(key), str) for key in required_strings):
        raise SnapshotError("snapshot required fields are invalid")
    snapshot_id = value["snapshot_id"]
    if not _ID.fullmatch(snapshot_id):
        raise SnapshotError("snapshot identifier is invalid")
    # A caller-selected report filename is valid.  The generated-name form
    # remains self-authenticating when its stem looks like a snapshot id.
    if _ID.fullmatch(path.stem) and path.stem != snapshot_id:
        raise SnapshotError("snapshot identifier does not match file")
    if not isinstance(value.get("ordered_pool"), list) or not all(isinstance(item, str) for item in value["ordered_pool"]):
        raise SnapshotError("snapshot ordered pool is invalid")
    # ``result_records`` was added without changing schema 1.  Older frozen
    # schema-1 snapshots remain pageable, but cannot render a persisted record.
    result_records = value.get("result_records", {})
    report_metadata = value.get("report_metadata", {})
    mapping_fields = ("options", "ranking_traces", "validation_ledger", "result_numbers", "cursor_history", "inventory_evidence")
    if any(not isinstance(value.get(key), dict) for key in mapping_fields):
        raise SnapshotError("snapshot object field is invalid")
    if not isinstance(result_records, dict):
        raise SnapshotError("snapshot result record field is invalid")
    if not isinstance(report_metadata, dict):
        raise SnapshotError("snapshot report metadata field is invalid")
    cap_extensions = value.get("cap_extensions", [])
    if not isinstance(cap_extensions, list):
        raise SnapshotError("snapshot cap extension field is invalid")
    if (type(value.get("requested_cap")) is not int or type(value.get("page_size")) is not int
            or type(value.get("created_at")) not in {int, float} or not math.isfinite(value["created_at"])):
        raise SnapshotError("snapshot scalar field is invalid")
    if any(type(number) is not int or number < 1 for number in value["result_numbers"].values()):
        raise SnapshotError("snapshot result-number map is invalid")
    for proof in value["validation_ledger"].values():
        if not isinstance(proof, dict) or proof.get("status") not in {"eligible", "unavailable", "inconclusive", "not_checked"}:
            raise SnapshotError("snapshot validation ledger is invalid")
    result_records, downgraded = _sanitize_loaded_records(result_records)
    ledger = dict(value["validation_ledger"])
    # A record with a forged/stale rendered proof must be scheduled again when
    # a continuation reaches it.  Do not let its old ledger entry short-circuit
    # the reviewed destination constructor and bounded validator.
    for candidate_id, proof in tuple(ledger.items()):
        if (candidate_id in downgraded
                or proof.get("status") == "eligible"
                and not _record_has_trusted_stored_proof(result_records.get(candidate_id))):
            ledger[candidate_id] = {"status": "not_checked", "detail": "stored link proof requires revalidation"}
    history = _recover_empty_history(snapshot_id, tuple(value["ordered_pool"]), ledger, value["cursor_history"])
    try:
        return SearchSnapshot(snapshot_id=snapshot_id, query=value["query"], options=value["options"],
                              config_revision=value["config_revision"], ordered_pool=tuple(value["ordered_pool"]),
                              result_records=result_records, ranking_traces=value["ranking_traces"],
                              validation_ledger=ledger, result_numbers=value["result_numbers"],
                              requested_cap=value["requested_cap"], page_size=value["page_size"],
                              cursor_history=history, cap_extensions=tuple(cap_extensions),
                              inventory_evidence=value["inventory_evidence"],
                              report_metadata=report_metadata,
                              created_at=float(value["created_at"]), schema_version=value["schema_version"])
    except (SnapshotError, TypeError, ValueError) as exc:
        raise SnapshotError("snapshot state is invalid") from exc


def _snapshot_payload(snapshot: SearchSnapshot) -> bytes:
    payload = json.dumps(snapshot_dict(snapshot), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("snapshot exceeds byte bound")
    return payload


def save_exclusive_path(path: Path, snapshot: SearchSnapshot) -> Path:
    """Publish a complete snapshot at an exact caller-selected path once."""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = _snapshot_payload(snapshot)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # link(2) is an atomic exclusive publication: readers see either no
        # snapshot or a complete fsynced one, never an overwritten partial file.
        os.link(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    else:
        os.unlink(temporary_name)
    return target


def save_exclusive(root: Path, snapshot: SearchSnapshot) -> Path:
    """Publish under the generated ``<snapshot-id>.json`` convention."""
    return save_exclusive_path(Path(root) / f"{snapshot.snapshot_id}.json", snapshot)


def update_snapshot(path: Path, snapshot: SearchSnapshot) -> Path:
    """Atomically replace a previously verified regular snapshot in place."""
    target = Path(path)
    existing = load_snapshot(target)
    if existing.snapshot_id != snapshot.snapshot_id:
        raise SnapshotError("replacement snapshot identifier does not match target")
    # Re-check immediately before replacement so a target changed to a link or
    # a non-regular file is rejected rather than silently replaced.
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise SnapshotError("snapshot disappeared before update") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SnapshotError("snapshot must be a regular non-symlink file")
    payload = _snapshot_payload(snapshot)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return target


@contextmanager
def snapshot_writer_lock(path: Path, *, timeout_seconds: float = SNAPSHOT_LOCK_SECONDS):
    """Serialize one snapshot load/materialize/update transaction across processes.

    The stable sidecar remains in place; the OS releases its advisory lock if a
    writer exits or crashes, avoiding stale-file guesses and inode-replacement
    races. Contending page requests fail within the shared-state lock budget.
    """
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= SNAPSHOT_LOCK_SECONDS:
        raise SnapshotError("snapshot lock timeout is invalid")
    target = Path(path)
    lock_path = target.with_name(f".{target.name}.lock")
    deadline = time.monotonic() + float(timeout_seconds)
    descriptor: int | None = None
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        info = os.fstat(descriptor)
        path_info = lock_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(path_info.st_mode)
                or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)):
            raise SnapshotError("snapshot writer lock must be a regular file")
        if info.st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
    except (OSError, SnapshotError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, SnapshotError):
            raise
        raise SnapshotError("cannot open snapshot writer lock") from exc

    def try_acquire() -> bool:
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False
            return True
        import fcntl
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    acquired = False
    while not acquired:
        try:
            acquired = try_acquire()
        except OSError as exc:
            os.close(descriptor)
            raise SnapshotError("cannot acquire snapshot writer lock") from exc
        if not acquired:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise SnapshotError("snapshot is busy; retry the page request") from None
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        except OSError as exc:
            raise SnapshotError("could not release snapshot writer lock") from exc


def resolve_materialized_result(snapshot: SearchSnapshot, number: int) -> Mapping[str, Any]:
    """Resolve a stable visible number from the frozen, persisted result record."""
    if type(number) is not int or number < 1:
        raise SnapshotError("result number is invalid")
    candidate_id = next((item for item, value in snapshot.result_numbers.items() if value == number), None)
    if candidate_id is None:
        raise SnapshotError("result number was not materialized in this snapshot")
    record = snapshot.result_records.get(candidate_id)
    if record is None:
        raise SnapshotError("snapshot lacks the persisted result record")
    return record


def validate_frozen_result_record(candidate_id: str, record: Mapping[str, Any] | None, *,
                                  destinations: Iterable[Any], transport: Any, resolver: Any, budget: Any,
                                  permits: Any, deadline: Any, phase: str = "final", target_cache: Any = None,
                                  validate_one: Any = None) -> dict[str, Any]:
    """Validate one frozen row through the shared target-first proof workflow."""
    from .proof_workflow import validate_record

    if not isinstance(record, Mapping):
        return {"status": "not_checked", "detail": "snapshot has no frozen result record"}
    # Keep the compatibility wrapper tolerant of generic iterables and only
    # hand the workflow real reviewed destination objects. The frozen record can
    # contain MappingProxy/tuple-shaped JSON; proof_workflow handles both.
    reviewed = [destination for destination in destinations
                if getattr(destination, "candidate_id", None) == candidate_id
                and getattr(destination, "profile", None) is not None]
    return validate_record(
        candidate_id, record, destinations=reviewed, transport=transport, resolver=resolver,
        budget=budget, permits=permits, deadline=deadline, phase=phase,
        target_cache=target_cache, validate_one=validate_one,
    )
