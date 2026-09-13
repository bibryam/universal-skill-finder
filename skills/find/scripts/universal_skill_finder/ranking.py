"""Pure, deterministic ranking for the frozen discovery pool.

The selected policy keeps query relevance dominant while allowing a bounded
source-local signal and independent-source corroboration to settle useful
ties. Destination reachability, installation readiness, source completion
order, and raw cross-source popularity never affect ranking.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping

from .models import RankingRecord


ALGORITHM_VERSION = "discovery-70-20-10-v1"
CANDIDATE_ID = "query-source-corroboration-v1"
DEFAULT_SOURCE_DEPTH = 20

_TOKEN_RE = re.compile(r"c\+\+|c#|\.net|node\.js|[a-z0-9]+(?:[.#][a-z0-9]+)*", re.IGNORECASE)
_SEPARATORS_RE = re.compile(r"[\\/_-]+")
_FAMILY_EQUIVALENTS = {
    "humanize": "humanize",
    "humanizer": "humanize",
    "humanizers": "humanize",
}
_METRIC_PRIORITY = {
    "installs": 0,
    "downloads": 1,
    "votes": 2,
    "bookmarks": 3,
    "stars": 4,
}


RankTrace = RankingRecord
OccurrenceKey = tuple[str, str, str]


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


def _query_groups(query: str) -> set[str]:
    return {word_family(token) for token in compatible_tokens(query)}


def _compact_phrase_present(query_tokens: list[str], field_tokens: list[str]) -> bool:
    """Match separator-only spelling differences such as anti-slop/antislop."""
    query_families = [word_family(token) for token in query_tokens]
    field_families = [word_family(token) for token in field_tokens]
    compact_query = "".join(query_families)
    if len(compact_query) < 6:
        return False
    for start in range(len(field_families)):
        compact_field = ""
        for token in field_families[start:]:
            compact_field += token
            if compact_field == compact_query:
                return True
            if len(compact_field) >= len(compact_query):
                break
    return False


def _phrase_present(query_tokens: list[str], field_tokens: list[str]) -> bool:
    if not query_tokens:
        return False
    if _compact_phrase_present(query_tokens, field_tokens):
        return True
    if len(query_tokens) > len(field_tokens):
        return False
    query_families = [word_family(token) for token in query_tokens]
    field_families = [word_family(token) for token in field_tokens]
    width = len(query_families)
    return any(
        field_families[index:index + width] == query_families
        for index in range(len(field_families) - width + 1)
    )


def _field_groups(value: object, query_groups: set[str], query_tokens: list[str]) -> set[str]:
    field_tokens = compatible_tokens(value)
    groups = {word_family(token) for token in field_tokens} & query_groups
    if _compact_phrase_present(query_tokens, field_tokens):
        groups.update(query_groups)
    return groups


def compatible_match_percent(
    query: str, name: str, description: str = "", path: str = "", repository: str = "",
) -> int:
    """Return broad lexical coverage for local catalogue retrieval diagnostics."""
    query_groups = _query_groups(query)
    if not query_groups:
        return 0
    query_tokens = compatible_tokens(query)
    available: set[str] = set()
    for field in (name, description, path):
        available.update(_field_groups(field, query_groups, query_tokens))
    score = int(round(100 * len(query_groups & available) / len(query_groups)))
    if "/" in query and len(query_tokens) == 2 and compatible_tokens(repository) == query_tokens:
        return 100
    return score


def _coverage(value: object, query_groups: set[str], query_tokens: list[str]) -> float:
    if not query_groups:
        return 0.0
    return len(_field_groups(value, query_groups, query_tokens)) / float(len(query_groups))


def _path_and_tags(occurrence: Mapping[str, Any]) -> str:
    tags = occurrence.get("tags")
    tag_text = " ".join(item for item in tags if isinstance(item, str)) if isinstance(tags, list) else ""
    return " ".join(
        value for value in (occurrence.get("skill_path"), occurrence.get("repository"), tag_text)
        if isinstance(value, str)
    )


def _occurrence_relevance(query: str, occurrence: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    query_tokens = compatible_tokens(query)
    query_groups = {word_family(token) for token in query_tokens}
    if not query_groups:
        return 0.0, {"query_groups": []}
    name = occurrence.get("name")
    description = occurrence.get("description")
    path_tags = _path_and_tags(occurrence)
    name_coverage = _coverage(name, query_groups, query_tokens)
    description_coverage = _coverage(description, query_groups, query_tokens)
    path_tag_coverage = _coverage(path_tags, query_groups, query_tokens)
    phrase = float(any(
        _phrase_present(query_tokens, compatible_tokens(field))
        for field in (name, description, path_tags)
    ))
    score = min(1.0, (
        0.50 * name_coverage
        + 0.30 * description_coverage
        + 0.10 * path_tag_coverage
        + 0.10 * phrase
    ))
    return score, {
        "query_groups": sorted(query_groups),
        "name_coverage": round(name_coverage, 12),
        "description_coverage": round(description_coverage, 12),
        "path_tag_coverage": round(path_tag_coverage, 12),
        "exact_or_ordered_phrase": bool(phrase),
    }


def _occurrence_key(occurrence: Mapping[str, Any]) -> OccurrenceKey:
    return (
        str(occurrence.get("source_id", "")),
        str(occurrence.get("native_id", "")),
        str(occurrence.get("adapter", "")),
    )


def _primary_skill_metric(occurrence: Mapping[str, Any]) -> tuple[str, float] | None:
    observations = occurrence.get("metric_observations")
    candidates: list[tuple[int, str, float]] = []
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, Mapping) or observation.get("scope") != "skill":
                continue
            name = observation.get("name")
            value = observation.get("value")
            if (not isinstance(name, str) or name not in _METRIC_PRIORITY
                    or type(value) not in {int, float} or not math.isfinite(float(value)) or value < 0):
                continue
            candidates.append((_METRIC_PRIORITY[name], name, float(value)))
    if not candidates:
        return None
    _priority, name, value = min(candidates)
    return name, value


def _metric_percentiles(results: Iterable[Any]) -> dict[OccurrenceKey, tuple[float, dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[tuple[OccurrenceKey, float]]] = defaultdict(list)
    for result in results:
        for occurrence in getattr(result, "occurrences", []):
            if not isinstance(occurrence, Mapping):
                continue
            metric = _primary_skill_metric(occurrence)
            if metric is not None:
                name, value = metric
                grouped[(str(occurrence.get("source_id", "")), name)].append((_occurrence_key(occurrence), value))
    normalized: dict[OccurrenceKey, tuple[float, dict[str, Any]]] = {}
    for (source_id, name), entries in grouped.items():
        values = sorted({value for _key, value in entries})
        for key, value in entries:
            percentile = 0.5 if len(values) == 1 else values.index(value) / float(len(values) - 1)
            normalized[key] = (percentile, {
                "source_id": source_id,
                "metric": name,
                "raw_value": value,
                "source_percentile": round(percentile, 12),
            })
    return normalized


def _requested_depth(occurrence: Mapping[str, Any]) -> int:
    evidence = occurrence.get("source_evidence")
    native = evidence.get("native") if isinstance(evidence, Mapping) else None
    depth = native.get("requested_depth") if isinstance(native, Mapping) else None
    return depth if type(depth) is int and 1 < depth <= 200 else DEFAULT_SOURCE_DEPTH


def _source_signal(
    occurrence: Mapping[str, Any],
    metric_percentiles: Mapping[OccurrenceKey, tuple[float, dict[str, Any]]],
) -> tuple[float, dict[str, Any]]:
    rank = occurrence.get("native_rank")
    depth = _requested_depth(occurrence)
    if type(rank) is int and rank > 0:
        rank_percentile = max(0.0, min(1.0, (depth - min(rank, depth)) / float(depth - 1)))
    else:
        rank_percentile = 0.5
    metric_percentile, metric_evidence = metric_percentiles.get(
        _occurrence_key(occurrence),
        (0.5, {"metric": None, "source_percentile": 0.5, "reason": "no comparable skill-level metric"}),
    )
    signal = 0.70 * rank_percentile + 0.30 * metric_percentile
    return signal, {
        "source_id": occurrence.get("source_id"),
        "native_rank": rank,
        "requested_depth": depth,
        "rank_percentile": round(rank_percentile, 12),
        "metric": metric_evidence,
    }


def _source_family(occurrence: Mapping[str, Any]) -> str:
    adapter = occurrence.get("adapter")
    if isinstance(adapter, str) and adapter:
        return adapter.casefold()
    return str(occurrence.get("source_id", "unknown")).casefold()


def _corroboration(occurrences: Iterable[Mapping[str, Any]]) -> tuple[float, list[str]]:
    families = sorted({_source_family(occurrence) for occurrence in occurrences})
    score = 0.0 if len(families) <= 1 else 0.5 if len(families) == 2 else 1.0
    return score, families


def rank_result(
    result: Any,
    query: str,
    metric_percentiles: Mapping[OccurrenceKey, tuple[float, dict[str, Any]]] | None = None,
) -> RankTrace:
    """Score one merged result using one coherent lexical occurrence."""
    raw_occurrences = getattr(result, "occurrences", [])
    occurrences = [item for item in raw_occurrences if isinstance(item, Mapping)]
    if not occurrences:
        occurrences = [{
            "name": getattr(result, "name", ""),
            "description": getattr(result, "description", ""),
            "skill_path": getattr(result, "skill_path", ""),
            "repository": getattr(result, "repository", ""),
            "source_id": "",
            "native_id": getattr(result, "id", ""),
            "adapter": "",
            "native_rank": 0,
        }]
    lexical_rows = [(*_occurrence_relevance(query, occurrence), occurrence) for occurrence in occurrences]
    relevance, relevance_evidence, lexical_occurrence = max(
        lexical_rows,
        key=lambda item: (
            item[0],
            int(bool(item[1].get("exact_or_ordered_phrase"))),
            str(item[2].get("name", "")).casefold(),
            str(item[2].get("native_id", "")),
        ),
    )
    percentiles = metric_percentiles or {}
    source_rows = [(*_source_signal(occurrence, percentiles), occurrence) for occurrence in occurrences]
    source_signal, source_evidence, _source_occurrence = max(
        source_rows,
        key=lambda item: (item[0], str(item[2].get("source_id", "")), str(item[2].get("native_id", ""))),
    )
    corroboration, families = _corroboration(occurrences)
    score = 0.70 * relevance + 0.20 * source_signal + 0.10 * corroboration
    return RankingRecord(
        algorithm_version=ALGORITHM_VERSION,
        candidate_id=CANDIDATE_ID,
        score=round(score, 12),
        components={
            "query_relevance": round(relevance, 12),
            "source_signal": round(source_signal, 12),
            "corroboration": round(corroboration, 12),
        },
        tie_breaks={
            "exact_or_ordered_phrase": bool(relevance_evidence.get("exact_or_ordered_phrase")),
            "name": str(getattr(result, "name", "")).casefold(),
            "identity": str(getattr(result, "id", "")),
        },
        evidence={
            "weights": {"query_relevance": 0.70, "source_signal": 0.20, "corroboration": 0.10},
            "relevance_occurrence": {
                "source_id": lexical_occurrence.get("source_id"),
                "native_id": lexical_occurrence.get("native_id"),
                **relevance_evidence,
            },
            "source_signal": source_evidence,
            "source_families": families,
            "excluded": ["destination_status", "installation_readiness", "completion_order", "cross_source_raw_metrics"],
        },
    )


def rank_results(results: Iterable[Any], query: str) -> list[tuple[Any, RankTrace]]:
    """Return stable discovery order and traces without mutating presentation data."""
    materialized = list(results)
    percentiles = _metric_percentiles(materialized)
    pairs = [(result, rank_result(result, query, percentiles)) for result in materialized]
    return sorted(
        pairs,
        key=lambda item: (
            -item[1].score,
            -item[1].components["query_relevance"],
            -int(bool(item[1].tie_breaks["exact_or_ordered_phrase"])),
            -item[1].components["source_signal"],
            -item[1].components["corroboration"],
            item[1].tie_breaks["name"],
            item[1].tie_breaks["identity"],
        ),
    )
