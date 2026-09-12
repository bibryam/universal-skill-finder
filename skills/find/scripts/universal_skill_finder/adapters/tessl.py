"""Anonymous, bounded discovery from Tessl's public indexed skill catalogue."""
from __future__ import annotations

import math
import re
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from ..http import HttpClient
from ..models import Candidate
from ..text import clean_text, parse_github_repository, safe_skill_path, safe_web_url
from .base import AdapterContext, SourceUnavailable


API_ORIGIN = "https://api.tessl.io"
REGISTRY_ORIGIN = "https://tessl.io"
SEARCH_ENDPOINT = API_ORIGIN + "/experimental/search"
MAX_RESULTS = 100
MAX_RESPONSE_BYTES = 512 * 1024
MAX_QUERY_LENGTH = 500
SECURITY_LEVELS = frozenset({"NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"})
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?")
_NUMERIC_SCORES = {
    "aggregate": "tessl_aggregate", "quality": "tessl_quality", "impact": "tessl_impact",
    "evalAvg": "tessl_eval_avg", "evalBaseline": "tessl_eval_baseline",
    "evalImprovement": "tessl_eval_improvement",
    "evalImprovementMultiplier": "tessl_eval_improvement_multiplier",
}


def _location(attributes: dict[str, Any]) -> tuple[str, str | None, str] | None:
    """Keep source identity without guessing a branch from a path or score hash."""
    url = safe_web_url(attributes.get("sourceUrl"))
    path = attributes.get("path")
    if not url or not isinstance(path, str) or safe_skill_path(path) != path:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.query or parsed.fragment or parsed.params:
        return None
    if PurePosixPath(path).name != "SKILL.md":
        return None
    directory = str(PurePosixPath(path).parent)
    # Other hosts need a reviewed identity mapping. A shared GitLab repository
    # URL alone would otherwise merge different skill paths as one result.
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    repository = parse_github_repository(url)
    # The API sourceUrl contract identifies a repository. A tree/blob URL
    # could have ambiguous slash refs, and must not silently change identity.
    if not repository or parsed.path.strip("/").count("/") != 1:
        return None
    url = "https://github.com/" + repository
    return url, repository, directory


def _individual_listing(repository: str, name: object) -> str | None:
    """Build Tessl's reviewed route without repairing untrusted identity fields."""
    if (not isinstance(name, str) or clean_text(name, 101) != name
            or not _PACKAGE_NAME.fullmatch(name)):
        return None
    parts = repository.split("/")
    if len(parts) != 2 or parse_github_repository(repository) != repository:
        return None
    return f"{REGISTRY_ORIGIN}/registry/skills/github/{parts[0]}/{parts[1]}/{name}"


def _bundle(item: dict[str, Any], attributes: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """An exact scored, public skill-bearing version is an inspection-only bundle."""
    full_name, name = attributes.get("fullName"), attributes.get("name")
    if not isinstance(full_name, str) or not isinstance(name, str):
        return None
    parts = full_name.split("/")
    if len(parts) != 2 or not all(_PACKAGE_NAME.fullmatch(part) for part in parts) or parts[1] != name:
        return None
    workspace: Any = item
    for key in ("relationships", "workspace", "data"):
        workspace = workspace.get(key) if isinstance(workspace, dict) else None
    if (not isinstance(workspace, dict) or workspace.get("type") != "workspace"
            or not isinstance(workspace.get("attributes"), dict)
            or workspace["attributes"].get("name") != parts[0]):
        return None
    scores, versions = attributes.get("scores"), attributes.get("versions")
    if not isinstance(scores, dict) or not isinstance(versions, list) or len(versions) > 100:
        return None
    version = scores.get("version")
    if not isinstance(version, str) or len(version) > 100 or not _VERSION.fullmatch(version):
        return None
    matched = [value for value in versions if isinstance(value, dict) and value.get("version") == version]
    if (len(matched) != 1 or matched[0].get("hasSkills") is not True or matched[0].get("archived") is not False
            or not isinstance(matched[0].get("summary"), str) or not clean_text(matched[0]["summary"])):
        return None
    # Multiple versions can remain active. Link and label exactly the scored
    # version; never assume the first array entry is current.
    return full_name, version, clean_text(matched[0]["summary"]), f"{REGISTRY_ORIGIN}/registry/{full_name}/{version}"


def _metrics(attributes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    metrics: dict[str, Any] = {}
    warnings: list[str] = []
    validation = attributes.get("validationPassed")
    if type(validation) is bool:
        metrics["tessl_validation_passed"] = validation
    scores = attributes.get("scores")
    if scores is None:
        return metrics, warnings
    if not isinstance(scores, dict):
        return metrics, ["Tessl score metadata was invalid and omitted"]
    malformed = False
    for original, key in _NUMERIC_SCORES.items():
        value = scores.get(original)
        if value is None:
            continue
        if type(value) in {int, float}:
            try:
                if math.isfinite(value) and (type(value) is not int or value.bit_length() <= 256):
                    metrics[key] = value
                    continue
            except OverflowError:
                pass
        malformed = True
    count = scores.get("evalCount")
    if type(count) is int and 0 <= count <= 2**63 - 1:
        metrics["tessl_eval_count"] = count
    elif count is not None:
        malformed = True
    level = scores.get("securityLevel")
    if isinstance(level, str) and level in SECURITY_LEVELS:
        metrics["tessl_security_level"] = level
        if level != "NONE":
            warnings.append(f"Tessl reports security level: {level}; inspect the repository before installing")
    elif level is not None:
        malformed = True
    # The deprecated scores.security grade is intentionally not interpreted.
    # A score's version is opaque provenance, never a Git ref or content hash.
    for original, key, limit in (("version", "tessl_score_version", 200),
                                 ("lastScoredAt", "tessl_scored_at", 100)):
        value = scores.get(original)
        if isinstance(value, str) and value and len(value) <= limit:
            metrics[key] = clean_text(value, limit)
        elif value is not None:
            malformed = True
    if malformed:
        warnings.append("Some Tessl score metadata was invalid and omitted")
    return metrics, warnings


class TesslAdapter:
    name = "tessl"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        if (source.get("base_url") != API_ORIGIN
                or any(key in source for key in ("endpoint", "headers", "auth_env", "auth_optional"))
                or source.get("allow_insecure_local", False) is not False):
            raise SourceUnavailable("invalid_config", "Tessl requires its fixed anonymous HTTPS API endpoint")
        if context.offline:
            raise SourceUnavailable("offline_miss", "Tessl has no cached result for this query")
        if type(limit) is not int or limit < 1:
            raise SourceUnavailable("invalid_query", "Tessl result limit must be a positive integer")
        if not isinstance(query, str) or not clean_text(query, MAX_QUERY_LENGTH):
            raise SourceUnavailable("invalid_query", "Tessl needs a nonempty capability query")
        normalized = clean_text(query, MAX_QUERY_LENGTH + 1)
        query_shortened = len(normalized) > MAX_QUERY_LENGTH
        normalized = normalized[:MAX_QUERY_LENGTH]
        requested = min(limit, MAX_RESULTS)
        configured = context.settings.get("max_response_bytes", MAX_RESPONSE_BYTES)
        cap = min(configured, MAX_RESPONSE_BYTES) if type(configured) is int and configured > 0 else MAX_RESPONSE_BYTES
        # All parameters are code-owned except the capability string. Do not
        # inspect environment credentials, run Tessl, or follow remote next URLs.
        response = context.http.request("GET", SEARCH_ENDPOINT, params={
            "q": normalized, "searchMode": "hybrid", "filter[hasSkills]": "true", "include": "tile-skills",
            "filter[includePrivate]": "false", "filter[includeInvalid]": "false",
            "page[number]": 1, "page[size]": requested,
        }, max_bytes=cap)
        if len(response.data) > cap:
            raise SourceUnavailable("schema_mismatch", "Tessl response exceeded the bounded response size")
        parsed = urlparse(response.final_url)
        if parsed.scheme != "https" or parsed.netloc != "api.tessl.io":
            raise SourceUnavailable("schema_mismatch", "Tessl response origin did not match")
        payload = HttpClient._parse_json(context.http, response, SEARCH_ENDPOINT)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise SourceUnavailable("schema_mismatch", "Tessl response did not contain a data array")
        rows = payload["data"]
        notes: list[str] = ["Tessl public skills and skill bundles; hybrid search"]
        partial: list[str] = []
        if query_shortened:
            partial.append("capability query shortened to 500 characters")
        if limit > MAX_RESULTS:
            partial.append("result request capped at 100")
        if len(rows) > requested:
            partial.append("response exceeded the requested result count")
        pagination = payload.get("meta", {}).get("pagination") if isinstance(payload.get("meta"), dict) else None
        pagination_valid = (
            isinstance(pagination, dict)
            and all(type(pagination.get(key)) is int and 0 <= pagination[key] <= 2**63 - 1
                    for key in ("total", "pages", "number", "size"))
            and pagination["number"] == 1 and pagination["size"] == requested
            and (pagination["pages"] in {0, 1} if pagination["total"] == 0
                 else pagination["pages"] == (pagination["total"] + requested - 1) // requested)
        )
        if not pagination_valid:
            partial.append("pagination metadata missing or invalid")
        elif len(rows) != min(pagination["total"], requested):
            partial.append("response count did not match pagination metadata")
        elif pagination["total"] > requested:
            # Top-N is the requested scope, not an exhaustive registry crawl.
            notes.append(f"top {requested} of {pagination['total']} indexed matches")
        if pagination_valid:
            context.source_total = pagination["total"]
            context.total_relation = "exact"
        context.effective_limit = requested
        for container in (payload, payload.get("meta")):
            if isinstance(container, dict) and "incomplete_results" in container:
                if container["incomplete_results"] is not False:
                    partial.append("upstream search reported incomplete results")
        results: list[Candidate] = []
        seen: set[str] = set()
        omitted = 0
        for rank, item in enumerate(rows[:requested], 1):
            if not isinstance(item, dict) or not isinstance(item.get("type"), str) or item["type"] not in {"skill", "tile"}:
                omitted += 1
                continue
            attributes, native_id = item.get("attributes"), item.get("id")
            if (not isinstance(attributes, dict) or not isinstance(native_id, str)
                    or not _UUID.fullmatch(native_id) or native_id.lower() in seen
                    or attributes.get("isPrivate") is not False
                    or attributes.get("validationPassed") is False
                    or (attributes.get("validationPassed") is not None and type(attributes.get("validationPassed")) is not bool)):
                omitted += 1
                continue
            metrics, warnings = _metrics(attributes)
            if item["type"] == "skill":
                location = _location(attributes)
                if (not location or not isinstance(attributes.get("name"), str) or not clean_text(attributes["name"])
                        or not isinstance(attributes.get("description"), str) or not clean_text(attributes["description"])):
                    omitted += 1
                    continue
                canonical, repository, directory = location
                name, description = clean_text(attributes["name"]), clean_text(attributes["description"])
                publisher = repository.split("/", 1)[0] if repository else None
                metrics["tessl_metric_scope"] = "skill"
                listing_url = _individual_listing(repository, attributes["name"])
                listing_role = "listing" if listing_url else "unavailable"
                listing_derivation = "connector_reviewed" if listing_url else "unavailable"
                expected_identity = {"repository": repository, "name": name} if listing_url else None
            else:
                bundle = _bundle(item, attributes)
                if not bundle:
                    omitted += 1
                    continue
                full_name, version, summary, canonical = bundle
                name, description = full_name + " (skill bundle)", f"Bundle version {version}. {summary}"
                repository, directory, publisher = None, None, full_name.split("/", 1)[0]
                metrics.update(tessl_metric_scope="bundle", tessl_bundle_version=version)
                warnings.append("Tessl skill bundle, not an individual skill; inspect this version and its contents before installation")
                listing_url = canonical
                listing_role = "bundle_listing"
                listing_derivation = "source_provided"
                expected_identity = None
            seen.add(native_id.lower())
            updated = attributes.get("updatedAt")
            results.append(Candidate(
                native_id=native_id.lower(), name=name, description=description,
                source_id=source["id"], source_kind=source["kind"], adapter=self.name,
                native_rank=rank, canonical_url=canonical, repository=repository,
                skill_path=directory, publisher=publisher,
                updated_at=clean_text(updated, 100) if isinstance(updated, str) else None,
                trust=source.get("trust", "unverified"), metrics=metrics, warnings=warnings,
                listing_url=listing_url, listing_role=listing_role, listing_derivation=listing_derivation,
                source_evidence={"native": {"source_id": source["id"], "provider": self.name,
                    "request_mode": "public_hybrid", "native_rank": rank, "ordering_basis": "unknown",
                    "eligibility": "unknown"},
                    **({"expected_identity": expected_identity} if expected_identity else {})},
                metric_observations=[{"provider": self.name, "name": key, "value": value,
                    "scope": metrics.get("tessl_metric_scope", "unknown"), "observed_at": metrics.get("tessl_scored_at"),
                    "provenance": "Tessl score response"}
                    for key, value in metrics.items() if type(value) in {int, float} and value >= 0],
            ))
        if omitted:
            partial.append(f"{omitted} nonpublic, invalid, or unsupported rows omitted")
        context.incomplete_results = bool(partial)
        context.detail = "; ".join(notes + list(dict.fromkeys(partial)))
        return results
