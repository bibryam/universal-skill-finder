from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any

from .text import clean_text, parse_github_repository, safe_install_reference, safe_skill_path, safe_web_url
from .versioning import REPORT_FORMAT_VERSION, SEARCH_REPORT_SCHEMA_VERSION, release_metadata

UNSAFE_LOCATION_WARNING = "unsafe installation location; handoff withheld"
INVALID_METRICS_WARNING = "unsupported or non-finite registry metrics omitted"


@dataclass
class NativeEvidence:
    source_id: str
    provider: str
    request_mode: str
    native_rank: int | None = None
    ordering_basis: str = "unknown"
    eligibility: str = "unknown"
    observed_at: str | None = None


@dataclass
class MetricObservation:
    provider: str
    name: str
    value: int | float | None
    scope: str = "unknown"
    observed_at: str | None = None
    provenance: str | None = None


@dataclass
class LinkProof:
    role: str
    url: str | None
    status: str = "not_checked"
    method: str | None = None
    checked_at: str | int | float | None = None
    http_status: int | None = None
    final_url: str | None = None
    redirect_chain: list[str] = field(default_factory=list)
    identity_basis: str | None = None
    cache_age_seconds: int | None = None
    detail: str | None = None


@dataclass
class TargetProof:
    kind: str
    status: str = "not_checked"
    reported: dict[str, Any] = field(default_factory=dict)
    resolved: dict[str, Any] = field(default_factory=dict)
    ref: str | None = None
    skill_path: str | None = None
    actual_name: str | None = None
    content_sha256: str | None = None
    checked_at: str | int | float | None = None
    detail: str | None = None


@dataclass
class RankingRecord:
    algorithm_version: str
    candidate_id: str
    score: float
    components: dict[str, float] = field(default_factory=dict)
    tie_breaks: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressEvent:
    type: str
    query: str | None = None
    source_id: str | None = None
    completed: int | None = None
    total: int | None = None
    status: str | None = None
    candidate_count: int | None = None
    elapsed_ms: int | None = None
    proof_id: str | None = None
    detail: str | None = None
    name: str | None = None
    description: str | None = None
    link_proof: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class Candidate:
    native_id: str
    name: str
    description: str
    source_id: str
    source_kind: str
    adapter: str
    native_rank: int = 0
    canonical_url: str | None = None
    repository: str | None = None
    skill_path: str | None = None
    ref: str | None = None
    slug: str | None = None
    publisher: str | None = None
    identity_namespace: str | None = None
    updated_at: str | int | float | None = None
    content_sha256: str | None = None
    trust: str = "unverified"
    metrics: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    install: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    listing_url: str | None = None
    listing_role: str = "unavailable"
    listing_derivation: str = "unavailable"
    source_evidence: dict[str, Any] = field(default_factory=dict)
    metric_observations: list[dict[str, Any]] = field(default_factory=list)
    target_proof: dict[str, Any] = field(default_factory=dict)
    link_proofs: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        if not isinstance(data, dict):
            raise ValueError("candidate must be an object")
        fields = cls.__dataclass_fields__
        values = {key: value for key, value in data.items() if key in fields}
        required = {"native_id", "name", "description", "source_id", "source_kind", "adapter"}
        optional = {"canonical_url", "repository", "skill_path", "ref", "slug", "publisher", "identity_namespace", "content_sha256", "listing_url"}
        for key in required | optional | {"trust"}:
            value = values.get(key)
            if key in required and not isinstance(value, str):
                raise ValueError(f"candidate {key} must be a string")
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"candidate {key} must be a string")
                values[key] = clean_text(value, 4000)
        rank = values.get("native_rank", 0)
        if type(rank) is not int or not 0 <= rank <= 1_000_000:
            raise ValueError("candidate native_rank must be a nonnegative integer")
        for key in ("tags", "warnings"):
            value = values.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"candidate {key} must be an array of strings")
            values[key] = [clean_text(item) for item in value[:100]]
        for key in ("metrics", "install", "source_evidence", "target_proof"):
            if not isinstance(values.get(key, {}), dict):
                raise ValueError(f"candidate {key} must be an object")
        observations = values.get("metric_observations", [])
        if not isinstance(observations, list) or not all(isinstance(item, dict) for item in observations):
            raise ValueError("candidate metric_observations must be an array of objects")
        values["metric_observations"] = observations[:100]
        proofs = values.get("link_proofs", [])
        if not isinstance(proofs, list) or not all(isinstance(item, dict) for item in proofs):
            raise ValueError("candidate link_proofs must be an array of objects")
        safe_proofs: list[dict[str, Any]] = []
        for item in proofs[:20]:
            proof = dict(item)
            # Older repository caches incorrectly treated archive bytes as
            # proof that a GitHub browser URL was safe to link. Preserve the
            # separate exact target proof, but require the current anonymous
            # GET validator before this legacy link can become clickable.
            if (proof.get("method") == "bounded_archive" and proof.get("status") == "eligible"
                    and proof.get("role") in {"repository", "skill_destination"}):
                proof["status"] = "not_checked"
                proof["detail"] = "legacy archive link proof requires anonymous GET validation"
            safe_proofs.append(proof)
        values["link_proofs"] = safe_proofs
        if values.get("listing_role", "unavailable") not in {"listing", "bundle_listing", "source_page", "repository", "unavailable"}:
            raise ValueError("candidate listing_role is invalid")
        if values.get("listing_derivation", "unavailable") not in {"source_provided", "connector_reviewed", "configured", "unavailable"}:
            raise ValueError("candidate listing_derivation is invalid")
        metrics = values.get("metrics", {})
        if len(metrics) > 100:
            raise ValueError("candidate metrics must contain at most 100 fields")
        safe_metrics: dict[str, Any] = {}
        omitted_metric = False
        for key, value in metrics.items():
            if not isinstance(key, str) or not key or len(key) > 100:
                omitted_metric = True
                continue
            key = clean_text(key, 100)
            if value is None or type(value) is bool:
                safe_metrics[key] = value
            elif type(value) is int and value.bit_length() <= 256:
                safe_metrics[key] = value
            elif type(value) is float and math.isfinite(value):
                safe_metrics[key] = value
            elif isinstance(value, str):
                safe_metrics[key] = clean_text(value)
            else:
                omitted_metric = True
        values["metrics"] = safe_metrics
        if omitted_metric and INVALID_METRICS_WARNING not in values["warnings"]:
            values["warnings"].insert(0, INVALID_METRICS_WARNING)
        updated = values.get("updated_at")
        if updated is not None and type(updated) not in {str, int, float}:
            raise ValueError("candidate updated_at must be a scalar")
        if isinstance(updated, str):
            values["updated_at"] = clean_text(updated, 100)
        elif (type(updated) is float and not math.isfinite(updated)) or (type(updated) is int and updated.bit_length() > 256):
            values["updated_at"] = None
        # Reconstruct handoffs instead of trusting executable hints in old caches.
        repository = parse_github_repository(data.get("repository"))
        path = safe_skill_path(data.get("skill_path"))
        ref = safe_install_reference(data.get("ref"))
        unsafe_location = UNSAFE_LOCATION_WARNING in values["warnings"] or any(
            data.get(key) is not None and normalized is None
            for key, normalized in (("repository", repository), ("skill_path", path), ("ref", ref))
        )
        if unsafe_location and UNSAFE_LOCATION_WARNING not in values["warnings"]:
            values["warnings"].insert(0, UNSAFE_LOCATION_WARNING)
        values.update(repository=repository, skill_path=path, ref=ref,
                      canonical_url=safe_web_url(data.get("canonical_url")),
                      listing_url=safe_web_url(data.get("listing_url")))
        install = values.get("install", {})
        if unsafe_location:
            values["install"] = {}
        elif repository:
            values["install"] = {"kind": "github", "repository": repository, "skill_path": path,
                                 "ref": ref or "HEAD", "requires_approval": True,
                                 "skill_installer": {"repository": repository, "path": path, "ref": ref or "HEAD"}}
        elif install.get("kind") in {"clawhub", "polyskill", "skillhub"} and safe_install_reference(install.get("reference")):
            values["install"] = {"kind": install["kind"], "reference": install["reference"], "requires_approval": True}
        else:
            values["install"] = {}
        values["warnings"] = values["warnings"][:100]
        return cls(**values)


@dataclass
class Coverage:
    source_id: str
    status: str
    result_count: int = 0
    elapsed_ms: int = 0
    detail: str | None = None
    cache_age_seconds: int | None = None
    host: str | None = None
    target: str | None = None
    enabled: bool = True
    public_url: str | None = None
    metadata_hosts: list[str] = field(default_factory=list)
    incomplete_results: bool = False
    requested_limit: int | None = None
    effective_limit: int | None = None
    source_total: int | None = None
    total_relation: str = "unknown"
    admission_status: str | None = None
    live_status: str | None = None
    cache_status: str | None = None
    health_status: str | None = None
    cooldown_until: str | None = None
    link_proof: dict[str, Any] = field(default_factory=dict)
    shown: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Result:
    id: str
    name: str
    description: str
    canonical_url: str | None
    repository: str | None
    skill_path: str | None
    ref: str | None
    publisher: str | None
    content_sha256: str | None
    source_ids: list[str]
    trust: list[str]
    text_match_percent: int
    rank_fusion_score: float
    metrics_by_source: dict[str, dict[str, Any]]
    install: dict[str, Any]
    warnings: list[str]
    occurrences: list[dict[str, Any]]
    installed: dict[str, Any] = field(default_factory=dict)
    ranking: dict[str, Any] = field(default_factory=dict)
    link_proofs: list[dict[str, Any]] = field(default_factory=list)
    target_proof: dict[str, Any] = field(default_factory=dict)
    attributions: list[dict[str, Any]] = field(default_factory=list)
    validation_status: str = "not_checked"
    result_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchReport:
    query: str
    results: list[Result]
    coverage: list[Coverage]
    generated_at: str
    configuration_path: str
    provenance: dict[str, Any] = field(default_factory=release_metadata)
    installed_scan: dict[str, Any] = field(default_factory=dict)
    mode: str = "online"
    report_format_version: int = REPORT_FORMAT_VERSION
    requested_count: int = 10
    page_size: int = 10
    accepted_occurrences: int = 0
    unique_count: int = 0
    eligible_count: int = 0
    unavailable_count: int = 0
    inconclusive_count: int = 0
    not_checked_count: int = 0
    validation_checked_count: int = 0
    validation_deferred_count: int = 0
    validation_stop_reason: str | None = None
    validation_stopped_reason: str | None = None
    page_incomplete: bool = False
    page_start: int = 0
    page_shown: int = 0
    materialized_total: int = 0
    candidate_previews: list[dict[str, Any]] = field(default_factory=list)
    preview_count: int = 0
    snapshot: dict[str, Any] = field(default_factory=dict)
    continuation: dict[str, Any] = field(default_factory=dict)
    show_more_available: bool = False
    show_more_cursor: str | None = None
    can_explain: bool = False
    timings: dict[str, Any] = field(default_factory=dict)
    footer_link_proof: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SEARCH_REPORT_SCHEMA_VERSION,
            "report_format_version": self.report_format_version,
            "mode": self.mode,
            "query": self.query,
            "generated_at": self.generated_at,
            "configuration_path": self.configuration_path,
            "provenance": dict(self.provenance),
            "installed_scan": dict(self.installed_scan),
            "requested_count": self.requested_count,
            "page_size": self.page_size,
            "accepted_occurrences": self.accepted_occurrences,
            "unique_count": self.unique_count,
            "eligible_count": self.eligible_count,
            "unavailable_count": self.unavailable_count,
            "inconclusive_count": self.inconclusive_count,
            "not_checked_count": self.not_checked_count,
            "validation_checked_count": self.validation_checked_count,
            "validation_deferred_count": self.validation_deferred_count,
            "validation_stop_reason": self.validation_stop_reason,
            "validation_stopped_reason": self.validation_stopped_reason,
            "page_incomplete": self.page_incomplete,
            "page_start": self.page_start,
            "page_shown": self.page_shown,
            "materialized_total": self.materialized_total,
            "candidate_previews": list(self.candidate_previews),
            "preview_count": self.preview_count,
            "snapshot": dict(self.snapshot),
            "continuation": dict(self.continuation),
            "show_more_available": self.show_more_available,
            "show_more_cursor": self.show_more_cursor,
            "can_explain": self.can_explain,
            "timings": dict(self.timings),
            "footer_link_proof": dict(self.footer_link_proof),
            "notes": list(self.notes),
            "results": [item.to_dict() for item in self.results],
            "coverage": [item.to_dict() for item in self.coverage],
        }
