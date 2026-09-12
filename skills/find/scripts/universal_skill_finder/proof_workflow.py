"""One bounded per-identity proof path shared by search and continuation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import json
from typing import Any

from .models import LinkProof
from .validation import (
    github_repository_destination,
    github_skill_destination,
    resolve_github_target,
    validate_destination,
)


def reported_target(record: Mapping[str, Any]) -> dict[str, Any]:
    """Keep indexed fields separate from the eventual resolved installation."""
    occurrences = record.get("occurrences", ())
    occurrences = occurrences if isinstance(occurrences, (list, tuple)) else ()
    pinned = any(isinstance(row, Mapping) and row.get("adapter") == "github-repo"
                 and row.get("ref") not in {None, "HEAD"} for row in occurrences)
    return {
        "repository": record.get("repository"), "ref": record.get("ref"),
        "skill_path": record.get("skill_path"), "name": record.get("name"),
        "path_is_exact": bool(record.get("skill_path")), "ref_pinned": pinned,
    }


def validate_record(candidate_id: str, record: Mapping[str, Any], *, destinations: list[Any],
                    transport: Any, resolver: Any, budget: Any, permits: Any, deadline: Any,
                    phase: str = "final", target_cache: Any = None,
                    validate_one: Any = None) -> dict[str, Any]:
    """Validate exact content and its independent inspection destinations.

    The raw ``SKILL.md`` target is install/hash evidence only. A GitHub tree
    page and every reviewed native listing each require their own bounded
    anonymous proof. No candidate fields, source query, rank, or number is
    changed by this function.
    """
    target: dict[str, Any] = {}
    if record.get("repository"):
        target = resolve_github_target(
            candidate_id, reported_target(record), transport=transport, resolver=resolver,
            budget=budget, permits=permits, deadline=deadline, phase=phase, cache=target_cache,
        )

    resolved_browse = None
    resolved_repository = None
    if target.get("status") == "eligible":
        resolved = target.get("resolved")
        if isinstance(resolved, Mapping):
            repository, ref, skill_path = (resolved.get("repository"), resolved.get("ref"), resolved.get("skill_path"))
            if all(isinstance(value, str) and value for value in (repository, ref, skill_path)):
                try:
                    resolved_browse = github_skill_destination(
                        candidate_id, repository=repository, ref=ref, skill_path=skill_path,
                    )
                    resolved_repository = github_repository_destination(candidate_id, repository=repository)
                except ValueError:
                    resolved_browse = resolved_repository = None

    # Keep one request per exact route/profile. When a stale ref was repaired,
    # replace occurrence-derived GitHub trees with the newly resolved tree.
    reviewed: list[Any] = []
    seen: set[tuple[str, str, str, str]] = set()
    resolved_destinations = [
        destination for destination in (resolved_browse, resolved_repository) if destination is not None
    ]
    candidates = resolved_destinations + list(destinations)
    for item in candidates:
        if (item is None or getattr(item, "candidate_id", None) != candidate_id
                or getattr(item, "profile", None) is None):
            continue
        if (resolved_browse is not None and item is not resolved_browse
                and getattr(item, "role", None) == "skill_destination"):
            continue
        profile_name = getattr(item.profile, "name", "")
        key = (
            str(getattr(item, "role", "")), str(getattr(item, "url", "")), str(profile_name),
            json.dumps(getattr(item, "expected_identity", None), sort_keys=True, ensure_ascii=True, default=str),
        )
        if key in seen:
            continue
        seen.add(key)
        reviewed.append(item)
        if len(reviewed) >= 20:
            break

    checked: list[LinkProof] = []
    for destination in reviewed:
        proof = validate_one(destination) if validate_one is not None else validate_destination(
            destination, transport=transport, resolver=resolver, budget=budget,
            permits=permits, deadline=deadline, phase=phase,
        )
        # An injected/cache validator cannot substitute a different URL or
        # semantic role for the destination that was reviewed here.
        if proof.role != destination.role or proof.url != destination.url:
            proof = LinkProof(
                destination.role, destination.url, "not_checked",
                detail="destination validator returned mismatched proof identity",
            )
        checked.append(proof)

    target_status = target.get("status") if target else None
    proof_statuses = [proof.status for proof in checked]
    if "eligible" in proof_statuses:
        status = "eligible"
    elif target_status == "eligible":
        status = "inconclusive" if any(value in {"inconclusive", "unavailable"} for value in proof_statuses) else "not_checked"
    elif target_status == "inconclusive" or "inconclusive" in proof_statuses:
        status = "inconclusive"
    elif "unavailable" in proof_statuses:
        status = "inconclusive" if target_status == "not_checked" else "unavailable"
    elif target_status in {"unavailable", "not_checked"}:
        status = target_status
    else:
        status = "not_checked"

    detail = next(
        (proof.detail for proof in checked if proof.detail),
        target.get("detail") if target else "no reviewed public destination",
    )
    outcome: dict[str, Any] = {"status": status, "detail": detail or "destination validation completed"}
    if checked:
        outcome["link_proofs"] = [asdict(proof) for proof in checked]
    if target:
        outcome["target_proof"] = target
    return outcome
