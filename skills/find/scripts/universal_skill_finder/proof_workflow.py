"""One bounded per-identity proof path shared by search and continuation."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from .validation import resolve_github_target, target_proof_link, validate_ranked


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
    """Try exact content first, then a reviewed inspection destination.

    Both attempts consume the caller's existing phase budget. No candidate
    fields, source query, rank, or number is changed by this function.
    """
    target: dict[str, Any] = {}
    if record.get("repository"):
        target = resolve_github_target(
            candidate_id, reported_target(record), transport=transport, resolver=resolver,
            budget=budget, permits=permits, deadline=deadline, phase=phase, cache=target_cache,
        )
        if target.get("status") == "eligible":
            return {"status": "eligible", "target_proof": target,
                    "link_proofs": [asdict(target_proof_link(target))]}
    reviewed = [item for item in destinations if item.candidate_id == candidate_id and item.profile is not None]
    if not reviewed:
        outcome = {"status": target.get("status", "not_checked"),
                   "detail": target.get("detail", "no reviewed public destination")}
    else:
        proofs = validate_ranked(
            reviewed, phase=phase, transport=transport, resolver=resolver,
            budget=budget, permits=permits, deadline=deadline, max_identities=1,
            workers=1, validate_one=validate_one,
        )
        proof = proofs.get(candidate_id)
        outcome = ({"status": proof.status, "detail": proof.detail or "destination checked",
                    "link_proofs": [asdict(proof)]} if proof is not None else
                   {"status": "not_checked", "detail": "destination validation was not scheduled"})
        # A missing listing cannot turn an unresolved target into confirmed
        # candidate absence. A checked alternate can still support inspection.
        if outcome["status"] == "unavailable" and target.get("status") in {"inconclusive", "not_checked"}:
            outcome["status"] = "inconclusive"
    if target:
        outcome["target_proof"] = target
    return outcome
