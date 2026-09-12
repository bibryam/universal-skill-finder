from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from ..text import SOURCE_ID_RE
from ..versioning import ADAPTER_CONTRACT_VERSION
from .base import Adapter, AdapterSpec
from .github_search import GitHubCodeSearchAdapter
from .tessl import TesslAdapter
from .registries import (
    ClawHubAdapter,
    HttpJsonAdapter,
    PolySkillAdapter,
    SkillHubProAdapter,
    SkillHubPublicAdapter,
    SkillsDirectoryAdapter,
    SkillsMpAdapter,
    SkillsShAdapter,
)
from .repositories import GitHubRepoAdapter, LocalDirectoryAdapter


def _catalogue(specs: Iterable[AdapterSpec]) -> Mapping[str, AdapterSpec]:
    """Fail early on registration mistakes instead of silently replacing a connector."""
    registered: dict[str, AdapterSpec] = {}
    for spec in specs:
        name = spec.name
        if not isinstance(name, str) or not SOURCE_ID_RE.fullmatch(name):
            raise ValueError("adapter name must be a valid source identifier")
        if name in registered:
            raise ValueError(f"duplicate adapter registration: {name}")
        if spec.kind not in {"registry", "repository"}:
            raise ValueError(f"invalid adapter kind: {name}")
        if spec.cache_policy not in {"query", "catalogue", "none"}:
            raise ValueError(f"invalid adapter cache policy: {name}")
        if type(spec.contract_version) is not int or spec.contract_version != ADAPTER_CONTRACT_VERSION:
            raise ValueError(f"unsupported adapter contract: {name}")
        if not callable(getattr(spec.factory, "search", None)):
            raise ValueError(f"adapter has no search method: {name}")
        registered[name] = spec
    return MappingProxyType(registered)


# One registration controls construction, source validation, kind inference and
# cache ownership. Extending this table requires reviewed local code, never a
# module path or executable supplied by a registry or source pack.
ADAPTER_SPECS = _catalogue((
    AdapterSpec(SkillsShAdapter, "registry", "query", ("base_url",)),
    AdapterSpec(SkillsMpAdapter, "registry", "query", ("base_url",)),
    AdapterSpec(ClawHubAdapter, "registry", "query", ("base_url",)),
    AdapterSpec(SkillHubPublicAdapter, "registry", "query", ("base_url",)),
    AdapterSpec(SkillHubProAdapter, "registry", "query", ("base_url",)),
    AdapterSpec(PolySkillAdapter, "registry", "query", ("base_url",)),
    AdapterSpec(SkillsDirectoryAdapter, "registry", "query", ("base_url",)),
    AdapterSpec(GitHubCodeSearchAdapter, "registry", "query", ("base_url", "auth_env")),
    AdapterSpec(TesslAdapter, "registry", "query", ("base_url",)),
    AdapterSpec(GitHubRepoAdapter, "repository", "catalogue", ("repository", "ref")),
    AdapterSpec(LocalDirectoryAdapter, "repository", "none", ("path",)),
    AdapterSpec(HttpJsonAdapter, "registry", "query", ("mapping",)),
))


def adapters() -> dict[str, Adapter]:
    return {name: spec.factory() for name, spec in ADAPTER_SPECS.items()}
