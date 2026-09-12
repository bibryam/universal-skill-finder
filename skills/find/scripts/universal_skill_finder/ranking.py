"""Pure, versioned ranking for merged Universal Skill Finder results.

This module deliberately consumes recorded occurrence fields only.  In
particular, it never converts a provider's native order, repository stars, or
an unknown metric into a cross-source relevance signal.
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

from .models import RankingRecord


ALGORITHM_VERSION = "soft-native-v2"
CANDIDATE_ID = "training-selected-title-description-adoption-v2"

_TOKEN_RE = re.compile(r"c\+\+|c#|\.net|node\.js|[a-z0-9]+(?:[.#][a-z0-9]+)*", re.IGNORECASE)
_SEPARATORS_RE = re.compile(r"[\\/_-]+")
_FAMILY_EQUIVALENTS = {
    "humanize": "humanize",
    "humanizer": "humanize",
    "humanizers": "humanize",
}


RankTrace = RankingRecord


def compatible_tokens(value: object) -> list[str]:
    """Tokenize user and catalogue text without splitting common code tokens."""
    if not isinstance(value, str):
        return []
    return [match.group(0).casefold() for match in _TOKEN_RE.finditer(_SEPARATORS_RE.sub(" ", value.casefold()))]


def word_family(token: str) -> str:
    """Return the declared, deliberately small compatible word family."""
    token = token.casefold()
    if token in _FAMILY_EQUIVALENTS:
        return _FAMILY_EQUIVALENTS[token]
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def compatible_match_percent(query: str, name: str, description: str = "", path: str = "") -> int:
    """Legacy-compatible retrieval score using the selected token families.

    It is intentionally only a local catalogue retrieval ordering signal.  The
    final score is calculated by :func:`rank_results` from a merged occurrence.
    """
    query_groups = _query_groups(query)
    if not query_groups:
        return 0
    available = set()
    for field in (name, description, path):
        available.update(word_family(token) for token in compatible_tokens(field))
    return int(round(100 * len(query_groups & available) / len(query_groups)))


def _query_groups(query: str) -> set[str]:
    return {word_family(token) for token in compatible_tokens(query)}


def _field_groups(value: object, query_groups: set[str]) -> set[str]:
    return {word_family(token) for token in compatible_tokens(value)} & query_groups


def _phrase_present(query_tokens: list[str], field_tokens: list[str]) -> bool:
    if not query_tokens or len(query_tokens) > len(field_tokens):
        return False
    query_families = [word_family(token) for token in query_tokens]
    field_families = [word_family(token) for token in field_tokens]
    width = len(query_families)
    return any(field_families[index:index + width] == query_families for index in range(len(field_families) - width + 1))


def _complete_name_equivalent(query_tokens: list[str], name_tokens: list[str]) -> bool:
    return bool(query_tokens) and [word_family(token) for token in query_tokens] == [word_family(token) for token in name_tokens]


def _navigation_identity(query: str, occurrence: dict[str, Any]) -> float:
    """Recognise an explicit owner/repository navigation query only."""
    query_parts = compatible_tokens(query)
    if "/" not in query or len(query_parts) != 2:
        return 0.0
    repository = occurrence.get("repository")
    if not isinstance(repository, str):
        return 0.0
    return float(compatible_tokens(repository) == query_parts)


def _occurrence_lexical(query: str, occurrence: dict[str, Any]) -> tuple[float, dict[str, Any], bool]:
    query_tokens = compatible_tokens(query)
    query_groups = {word_family(token) for token in query_tokens}
    if not query_groups:
        return 0.0, {"query_groups": []}, False
    name_tokens = compatible_tokens(occurrence.get("name"))
    description_tokens = compatible_tokens(occurrence.get("description"))
    path_tokens = compatible_tokens(occurrence.get("skill_path"))
    name_groups = _field_groups(occurrence.get("name"), query_groups)
    description_groups = _field_groups(occurrence.get("description"), query_groups)
    path_groups = _field_groups(occurrence.get("skill_path"), query_groups)
    denominator = float(len(query_groups))
    name_coverage = len(name_groups) / denominator
    description_coverage = len(description_groups) / denominator
    path_coverage = len(path_groups) / denominator
    phrase = float(_phrase_present(query_tokens, name_tokens))
    whole_name = float(_complete_name_equivalent(query_tokens, name_tokens))
    navigation = _navigation_identity(query, occurrence)
    score = (
        0.45 * name_coverage
        + 0.25 * description_coverage
        + 0.05 * path_coverage
        + 0.05 * phrase
        + 0.15 * whole_name
        + 0.05 * navigation
    )
    # This is only a tie-break.  It must be independently present in the
    # description, not inferred from a title/path match or stitched occurrence.
    corroborates = bool(whole_name and query_groups <= description_groups)
    return score, {
        "query_groups": sorted(query_groups),
        "name_groups": sorted(name_groups),
        "description_groups": sorted(description_groups),
        "path_groups": sorted(path_groups),
        "name_coverage": name_coverage,
        "description_coverage": description_coverage,
        "path_coverage": path_coverage,
        "name_phrase": phrase,
        "whole_name": whole_name,
        "navigational_identity": navigation,
    }, corroborates


def _typed_skill_installs(occurrence: dict[str, Any]) -> list[dict[str, Any]]:
    """Read only skills.sh skill-level install observations.

    ``metric_observations`` is the frozen typed model field.  The narrow
    fallback records the existing skills.sh per-skill ``installs`` field as the
    same typed observation so schema-v1 cache rows remain replayable.  No other
    provider, metric, scope, native order, or repository metric is accepted.
    """
    observations = occurrence.get("metric_observations")
    accepted: list[dict[str, Any]] = []
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            value = observation.get("value")
            if (
                observation.get("provider") == "skills-sh"
                and observation.get("name") == "installs"
                and observation.get("scope") == "skill"
                and type(value) in {int, float}
                and math.isfinite(float(value))
                and value >= 0
            ):
                accepted.append({"value": float(value), "provenance": observation.get("provenance", "source_provided")})
    if accepted:
        return accepted
    metrics = occurrence.get("metrics")
    value = metrics.get("installs") if isinstance(metrics, dict) else None
    if occurrence.get("source_id") == "skills-sh" and type(value) in {int, float} and math.isfinite(float(value)) and value >= 0:
        return [{"value": float(value), "provenance": "schema-v1-skills-sh-skill-listing"}]
    return []


def _adoption(occurrences: Iterable[dict[str, Any]]) -> tuple[float, list[dict[str, Any]]]:
    # One listing can be duplicated in a merged result.  Count its greatest
    # observation once, rather than letting duplicate aliases inflate it.
    observations = [item for occurrence in occurrences for item in _typed_skill_installs(occurrence)]
    if not observations:
        return 0.0, []
    value = max(item["value"] for item in observations)
    normalized = math.log1p(value) / math.log1p(50_000)
    evidence = sorted(observations, key=lambda item: (-item["value"], str(item["provenance"])))
    return min(1.0, normalized), evidence


def rank_result(result: Any, query: str) -> RankTrace:
    """Score a merged result and return a serializable selected-policy trace."""
    raw_occurrences = getattr(result, "occurrences", [])
    occurrences = [item for item in raw_occurrences if isinstance(item, dict)]
    if not occurrences:
        occurrences = [{
            "name": getattr(result, "name", ""),
            "description": getattr(result, "description", ""),
            "skill_path": getattr(result, "skill_path", ""),
            "repository": getattr(result, "repository", ""),
            "source_id": None,
            "metrics": {},
        }]
    scored = []
    for occurrence in occurrences:
        lexical, evidence, corroborates = _occurrence_lexical(query, occurrence)
        scored.append((lexical, corroborates, occurrence, evidence))
    # One coherent occurrence supplies all lexical evidence.  Stable fields keep
    # replay ordering independent of arrival timing.
    lexical, corroborates, occurrence, lexical_evidence = max(
        scored,
        key=lambda item: (item[0], item[1], str(item[2].get("name", "")).casefold(), str(item[2].get("native_id", ""))),
    )
    adoption, adoption_evidence = _adoption(occurrences)
    score = lexical + 0.10 * adoption
    return RankingRecord(
        algorithm_version=ALGORITHM_VERSION,
        candidate_id=CANDIDATE_ID,
        score=round(score, 12),
        components={"lexical": round(lexical, 12), "skill_adoption": round(0.10 * adoption, 12)},
        tie_breaks={
            "description_corroboration": bool(corroborates),
            "name": str(getattr(result, "name", "")).casefold(),
            "identity": str(getattr(result, "id", "")),
        },
        evidence={
            "lexical_occurrence": {
                "source_id": occurrence.get("source_id"),
                "native_id": occurrence.get("native_id"),
                **lexical_evidence,
            },
            "skills_sh_skill_installs": adoption_evidence,
            "excluded": ["native_rank", "repository_stars", "skillsmp_order", "unknown_native_order"],
        },
    )


def rank_results(results: Iterable[Any], query: str) -> list[tuple[Any, RankTrace]]:
    """Return selected-policy order and traces, without mutating presentation data."""
    pairs = [(result, rank_result(result, query)) for result in results]
    return sorted(
        pairs,
        key=lambda item: (
            -item[1].score,
            -int(bool(item[1].tie_breaks["description_corroboration"])),
            item[1].tie_breaks["name"],
            item[1].tie_breaks["identity"],
        ),
    )
