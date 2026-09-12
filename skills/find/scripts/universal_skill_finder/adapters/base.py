from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..cache import Cache
from ..http import HttpClient
from ..models import Candidate
from ..versioning import ADAPTER_CONTRACT_VERSION


class SourceUnavailable(RuntimeError):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class AdapterContext:
    http: HttpClient
    cache: Cache
    settings: dict[str, Any]
    offline: bool = False
    refresh: bool = False
    cache_age_seconds: int | None = None
    incomplete_results: bool = False
    detail: str | None = None
    effective_limit: int | None = None
    source_total: int | None = None
    total_relation: str = "unknown"
    stale_max_age_seconds: int | None = None
    rate_limit_headers: dict[str, str] | None = None


class Adapter(Protocol):
    """Reviewed, stateless connector shared by concurrent source searches.

    Return at most ``limit`` candidates, in native relevance order, or raise
    SourceUnavailable for a recognized source failure. Never execute discovered
    code, mutate source configuration, or bypass context.http's transport limits.
    The federation layer validates candidates and rebinds source authority.
    """

    name: str

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]: ...


@dataclass(frozen=True)
class AdapterSpec:
    """Code-owned registration; never constructed from source-pack metadata."""

    factory: type[Adapter]
    kind: Literal["registry", "repository"]
    cache_policy: Literal["query", "catalogue", "none"]
    required_fields: tuple[str, ...] = ()
    contract_version: int = ADAPTER_CONTRACT_VERSION

    @property
    def name(self) -> str:
        return self.factory.name
