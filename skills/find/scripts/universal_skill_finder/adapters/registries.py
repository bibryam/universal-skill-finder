from __future__ import annotations

import os
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlparse

from ..models import Candidate
from ..text import clean_text, parse_github_repository, parse_github_tree_url, safe_install_reference, safe_skill_path, safe_web_url, slugify
from .base import AdapterContext, SourceUnavailable


def _list(value: Any, paths: Iterable[tuple[str, ...]]) -> list[dict[str, Any]]:
    expected: list[str] = []
    for path in paths:
        expected.append(".".join(path))
        current = value
        for part in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if isinstance(current, list):
            return [item for item in current if isinstance(item, dict)]
    raise SourceUnavailable("schema_mismatch", "response did not contain an array at: " + ", ".join(expected))


def _dict_get(value: Any, path: str | None, default: Any = None) -> Any:
    if not path:
        return default
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return default
        if current is None:
            return default
    return current


def _github_install(repository: str | None, skill_path: str | None, ref: str | None, slug: str | None) -> dict[str, Any]:
    repository = parse_github_repository(repository) if repository else None
    if not repository or (skill_path is not None and not safe_skill_path(skill_path)) or (ref is not None and not safe_install_reference(ref)):
        return {}
    if slug is not None and not safe_install_reference(slug):
        return {}
    result: dict[str, Any] = {
        "kind": "github",
        "repository": repository,
        "ref": ref or "HEAD",
        "skill_path": skill_path,
        "skill_installer": {"repository": repository, "path": skill_path, "ref": ref or "HEAD"},
    }
    return result


def _registry_install(kind: str, reference: str) -> dict[str, Any]:
    if not safe_install_reference(reference):
        return {}
    return {"kind": kind, "reference": reference}


def _native_evidence(source: dict[str, Any], rank: int, *, basis: str = "unknown",
                     eligibility: str = "unknown", request_mode: str = "search") -> dict[str, Any]:
    return {"native": {"source_id": source["id"], "provider": source["adapter"],
                       "request_mode": request_mode, "native_rank": rank,
                       "ordering_basis": basis, "eligibility": eligibility}}


def _metric_observations(provider: str, metrics: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    return [{"provider": provider, "name": name, "value": value, "scope": scope,
             "provenance": "source response"}
            for name, value in metrics.items() if type(value) in {int, float} and value >= 0]


def _reviewed_path(base: str, prefix: str, native_id: str) -> str | None:
    if not native_id:
        return None
    segments = native_id.strip("/").split("/")
    if not segments or any(not segment or segment in {".", ".."} for segment in segments):
        return None
    path = "/".join(quote(segment, safe="") for segment in segments)
    middle = prefix.strip("/")
    return safe_web_url(f"{base.rstrip('/')}/{middle + '/' if middle else ''}{path}")


def _auth_headers(source: dict[str, Any], *, required: bool) -> dict[str, str]:
    env_name = source.get("auth_env")
    if not env_name:
        return {}
    token = os.environ.get(str(env_name))
    if not token:
        if required:
            raise SourceUnavailable("auth_missing", f"missing environment variable {env_name}")
        return {}
    return {"Authorization": f"Bearer {token}"}


class SkillsShAdapter:
    name = "skills-sh"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        base = source["base_url"].rstrip("/")
        # Public search is deliberately credential-inert.  In particular, do
        # not adopt an ambient Vercel OIDC token and silently change endpoint,
        # result scope, cache partition, or query disclosure.  An authenticated
        # skills.sh mode needs an explicit, separately reviewed source contract.
        payload = context.http.get_json(f"{base}/api/search", params={"q": query, "limit": limit})
        items = _list(payload, [("skills",), ("data",)])
        results: list[Candidate] = []
        for rank, item in enumerate(items[:limit], 1):
            repository = parse_github_repository(item.get("source") or "")
            native_id = clean_text(item.get("id") or item.get("skillId") or item.get("slug") or "")
            slug = clean_text(item.get("skillId") or item.get("slug") or native_id.rsplit("/", 1)[-1])
            name = clean_text(item.get("name") or slug)
            install_url = clean_text(item.get("installUrl") or "") or None
            if install_url and not repository:
                repository = parse_github_repository(install_url)
            supplied_listing = safe_web_url(item.get("url"))
            canonical = supplied_listing or _reviewed_path(base, "", native_id)
            metrics = {"installs": item.get("installs")}
            results.append(Candidate(
                native_id=native_id or f"{repository}/{slug}", name=name, description=clean_text(item.get("description") or ""),
                source_id=source["id"], source_kind=source["kind"], adapter=self.name, native_rank=rank,
                canonical_url=canonical, repository=repository, slug=slug,
                publisher=repository.split("/", 1)[0] if repository and "/" in repository else None,
                trust=source.get("trust", "unverified"), metrics=metrics,
                install=_github_install(repository, None, None, slug),
                warnings=["skills.sh public legacy API used"],
                listing_url=canonical, listing_role="listing",
                listing_derivation="source_provided" if supplied_listing else "connector_reviewed",
                source_evidence={
                    **_native_evidence(source, rank, request_mode="public_legacy"),
                    **({"expected_identity": {"id": native_id}} if native_id else {}),
                },
                metric_observations=_metric_observations(self.name, metrics, "skill"),
            ))
        return results


class SkillsMpAdapter:
    name = "skillsmp"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        headers = _auth_headers(source, required=not source.get("auth_optional", False))
        payload = context.http.get_json(
            source["base_url"].rstrip("/") + "/api/v1/skills/search",
            params={"q": query, "limit": min(limit, 50)}, headers=headers,
        )
        items = _list(payload, [("data", "skills"), ("skills",), ("data",)])
        results: list[Candidate] = []
        for rank, item in enumerate(items[:limit], 1):
            github_url = clean_text(item.get("githubUrl") or item.get("github_url") or "")
            repository, skill_path, ref = parse_github_tree_url(github_url)
            repository = repository or parse_github_repository(github_url)
            name = clean_text(item.get("name") or item.get("slug") or "skill")
            slug = slugify(name)
            supplied_listing = safe_web_url(item.get("skillUrl") or item.get("url"))
            metrics = {"stars": item.get("stars")}
            results.append(Candidate(
                native_id=clean_text(item.get("id") or github_url or name), name=name,
                description=clean_text(item.get("description") or ""), source_id=source["id"],
                source_kind=source["kind"], adapter=self.name, native_rank=rank,
                canonical_url=supplied_listing or safe_web_url(github_url),
                repository=repository, skill_path=skill_path, ref=ref, slug=slug,
                publisher=clean_text(item.get("author") or "") or None, updated_at=item.get("updatedAt"),
                trust=source.get("trust", "unverified"), metrics=metrics,
                tags=[clean_text(tag) for tag in (item.get("tags") or []) if clean_text(tag)],
                install=_github_install(repository, skill_path, ref, slug),
                listing_url=supplied_listing, listing_role="listing" if supplied_listing else "unavailable",
                listing_derivation="source_provided" if supplied_listing else "unavailable",
                source_evidence={
                    **_native_evidence(source, rank, basis="repository_popularity", eligibility="excluded"),
                    "expected_identity": {"id": clean_text(item.get("id") or github_url or name)},
                },
                metric_observations=_metric_observations(self.name, metrics, "repository"),
            ))
        return results


class ClawHubAdapter:
    name = "clawhub"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        params: dict[str, object] = {"q": query}
        if source.get("non_suspicious_only", True):
            params["nonSuspiciousOnly"] = "true"
        payload = context.http.get_json(source["base_url"].rstrip("/") + "/api/v1/search", params=params)
        items = _list(payload, [("results",), ("skills",), ("data",)])
        results: list[Candidate] = []
        for item in items:
            if source.get("native_only", True) and item.get("source") not in {None, "clawhub"}:
                continue
            owner = clean_text(item.get("ownerHandle") or _dict_get(item, "owner.handle") or _dict_get(item, "publisher.handle") or "")
            slug = clean_text(item.get("slug") or "")
            if not slug:
                continue
            canonical_path = item.get("canonicalUrl") or _dict_get(item, "links.canonical") or ""
            canonical = safe_web_url(urljoin(source["base_url"].rstrip("/") + "/", canonical_path)) if isinstance(canonical_path, str) and canonical_path else None
            canonical = canonical or safe_web_url(f"{source['base_url'].rstrip('/')}/{quote(owner, safe='')}/skills/{quote(slug, safe='')}")
            install_ref = clean_text(_dict_get(item, "install.reference") or f"{owner}/{slug}")
            metrics = {
                "downloads": item.get("downloads") if item.get("downloads") is not None else _dict_get(item, "native.skill.stats.downloads"),
                "installs": _dict_get(item, "native.skill.stats.installs"),
                "stars": _dict_get(item, "native.skill.stats.stars"),
                "bookmarks": _dict_get(item, "metrics.bookmarks"),
            }
            results.append(Candidate(
                native_id=clean_text(item.get("id") or f"{owner}/{slug}"),
                name=clean_text(item.get("displayName") or slug), description=clean_text(item.get("summary") or ""),
                source_id=source["id"], source_kind=source["kind"], adapter=self.name,
                native_rank=len(results) + 1, canonical_url=canonical, slug=slug, publisher=owner or None,
                updated_at=item.get("updatedAt"), trust=source.get("trust", "unverified"), metrics=metrics,
                install=_registry_install("clawhub", install_ref),
                warnings=["registry marks this result suspicious"] if _dict_get(item, "native.skill.isSuspicious") else [],
                listing_url=canonical, listing_role="listing", listing_derivation="source_provided" if canonical_path else "connector_reviewed",
                source_evidence=_native_evidence(source, len(results) + 1),
                metric_observations=_metric_observations(self.name, metrics, "skill"),
            ))
            if len(results) >= limit:
                break
        return results


class SkillHubPublicAdapter:
    name = "skillhub-public"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        payload = context.http.get_json(source["base_url"].rstrip("/") + "/api/skills", params={"q": query, "limit": limit})
        items = _list(payload, [("skills",), ("data",)])
        results: list[Candidate] = []
        for rank, item in enumerate(items[:limit], 1):
            owner = clean_text(item.get("githubOwner") or "")
            repo = clean_text(item.get("githubRepo") or "")
            repository = parse_github_repository(f"{owner}/{repo}") if owner and repo else None
            native_id = clean_text(item.get("id") or item.get("slug") or item.get("name") or "")
            slug = slugify(clean_text(item.get("name") or native_id.rsplit("/", 1)[-1]))
            # This singular hierarchical route is connector-owned and covered
            # by a regression that rejects the unsupported plural route.
            canonical = _reviewed_path(source["base_url"], "skill", native_id)
            metrics = {"github_stars": item.get("githubStars"), "downloads": item.get("downloadCount"), "ai_score": item.get("aiScore")}
            results.append(Candidate(
                native_id=native_id, name=clean_text(item.get("name") or slug),
                description=clean_text(item.get("description") or ""), source_id=source["id"], source_kind=source["kind"],
                adapter=self.name, native_rank=rank, canonical_url=canonical, repository=repository, slug=slug,
                publisher=owner or None, trust=source.get("trust", "unverified"),
                metrics=metrics,
                install={
                    **_github_install(repository, None, None, slug),
                    **({"skillhub_reference": native_id} if safe_install_reference(native_id) else {}),
                },
                warnings=["registry security status is not pass"] if item.get("securityStatus") not in {None, "pass"} else [],
                listing_url=canonical, listing_role="listing" if canonical else "unavailable",
                listing_derivation="connector_reviewed" if canonical else "unavailable",
                source_evidence={**_native_evidence(source, rank), "expected_identity": {"id": native_id}},
                metric_observations=_metric_observations(self.name, metrics, "unknown"),
            ))
        return results


class PolySkillAdapter:
    name = "polyskill"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        payload = context.http.get_json(source["base_url"].rstrip("/") + "/api/skills", params={"q": query, "limit": limit, "sort": "relevance"})
        items = _list(payload, [("skills",), ("data", "skills"), ("data",)])
        results: list[Candidate] = []
        for rank, item in enumerate(items[:limit], 1):
            manifest = item.get("manifest") if isinstance(item.get("manifest"), dict) else item
            name = clean_text(manifest.get("name") or item.get("slug") or "skill")
            publisher = clean_text(manifest.get("author") or item.get("author") or name.split("/", 1)[0].lstrip("@")) or None
            github_url = clean_text(item.get("githubUrl") or item.get("repository") or manifest.get("repository") or "")
            repository, skill_path, ref = parse_github_tree_url(github_url)
            repository = repository or parse_github_repository(github_url)
            # The public detail route is singular and namespaces are path
            # segments: /skill/@publisher/name. The older plural route and an
            # encoded slash both return a confirmed 404.
            name_segments = name.split("/")
            listing = (
                f"{source['base_url'].rstrip('/')}/skill/" + "/".join(quote(part, safe="@") for part in name_segments)
                if safe_install_reference(name) and len(name_segments) >= 2 else None
            )
            results.append(Candidate(
                native_id=clean_text(item.get("id") or name), name=name,
                description=clean_text(manifest.get("description") or item.get("description") or ""),
                source_id=source["id"], source_kind=source["kind"], adapter=self.name, native_rank=rank,
                canonical_url=safe_web_url(listing), repository=repository,
                skill_path=skill_path, ref=ref, slug=slugify(name.rsplit("/", 1)[-1]), publisher=publisher,
                updated_at=item.get("updated_at") or item.get("updatedAt"), trust=source.get("trust", "unverified"),
                metrics={"downloads": item.get("downloads"), "stars": item.get("stars"), "score": item.get("score")},
                install=_github_install(repository, skill_path, ref, slugify(name.rsplit("/", 1)[-1])) or _registry_install("polyskill", name),
                listing_url=safe_web_url(listing), listing_role="listing" if listing else "unavailable",
                listing_derivation="connector_reviewed" if listing else "unavailable",
                source_evidence={
                    **_native_evidence(source, rank),
                    **({"expected_identity": {"name": name}} if listing else {}),
                },
            ))
        return results


class SkillsDirectoryAdapter:
    name = "skills-directory"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        headers = _auth_headers(source, required=True)
        payload = context.http.get_json(
            source["base_url"].rstrip("/") + "/api/v1/skills",
            params={"q": query, "limit": min(limit, 100), "securityGrade": "all"}, headers=headers,
        )
        return _mapped_directory_items(source, _list(payload, [("data",), ("skills",)]), limit, self.name)


class SkillHubProAdapter:
    name = "skillhub-pro"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        headers = _auth_headers(source, required=True)
        payload = context.http.post_json(
            source["base_url"].rstrip("/") + "/api/v1/skills/search",
            body={"query": query, "limit": min(limit, 100), "method": "hybrid"}, headers=headers,
        )
        return _mapped_directory_items(source, _list(payload, [("data",), ("skills",), ("results",)]), limit, self.name)


def _mapped_directory_items(source: dict[str, Any], items: list[dict[str, Any]], limit: int, adapter: str) -> list[Candidate]:
    results: list[Candidate] = []
    for rank, item in enumerate(items[:limit], 1):
        github_url = clean_text(item.get("githubUrl") or item.get("github_url") or item.get("repositoryUrl") or "")
        repository, skill_path, ref = parse_github_tree_url(github_url)
        repository = repository or parse_github_repository(github_url)
        name = clean_text(item.get("name") or item.get("slug") or "skill")
        slug = clean_text(item.get("slug") or slugify(name))
        author = item.get("author")
        publisher = clean_text(author.get("name") if isinstance(author, dict) else author or "") or None
        results.append(Candidate(
            native_id=clean_text(item.get("id") or slug), name=name, description=clean_text(item.get("description") or item.get("summary") or ""),
            source_id=source["id"], source_kind=source["kind"], adapter=adapter, native_rank=rank,
            canonical_url=safe_web_url(item.get("url") or item.get("skillUrl") or github_url),
            repository=repository, skill_path=skill_path, ref=ref, slug=slug, publisher=publisher,
            updated_at=item.get("updatedAt") or item.get("updated_at"), trust=source.get("trust", "unverified"),
            metrics={"stars": item.get("stars"), "votes": item.get("votes"), "security_grade": item.get("securityGrade"), "score": item.get("_score")},
            install=_github_install(repository, skill_path, ref, slug),
        ))
    return results


class HttpJsonAdapter:
    name = "http-json-v1"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        headers: dict[str, str] = {}
        for name, value in source.get("headers", {}).items():
            if isinstance(value, dict):
                env_name = value.get("env")
                secret = os.environ.get(str(env_name)) if env_name else None
                if not secret:
                    if value.get("optional"):
                        continue
                    raise SourceUnavailable("auth_missing", f"missing environment variable {env_name}")
                headers[name] = f"{value.get('prefix', '')}{secret}"
            else:
                headers[name] = clean_text(value, 500)
        query_param = source.get("query_param", "q")
        limit_param = source.get("limit_param", "limit")
        payload = context.http.get_json(
            source.get("endpoint") or source["base_url"], params={query_param: query, limit_param: limit}, headers=headers
        )
        mapping = source["mapping"]
        items = _dict_get(payload, mapping["items"])
        if not isinstance(items, list):
            raise SourceUnavailable("schema_mismatch", f"mapping.items did not resolve to an array: {mapping['items']}")
        results: list[Candidate] = []
        for rank, item in enumerate((entry for entry in items if isinstance(entry, dict)), 1):
            name = clean_text(_dict_get(item, mapping["name"], "skill"))
            repository = parse_github_repository(clean_text(_dict_get(item, mapping.get("repository"), "")))
            skill_path = clean_text(_dict_get(item, mapping.get("skill_path"), "")) or None
            slug = clean_text(_dict_get(item, mapping.get("slug"), "")) or slugify(name)
            ref = clean_text(_dict_get(item, mapping.get("ref"), "")) or None
            results.append(Candidate(
                native_id=clean_text(_dict_get(item, mapping.get("id"), slug)), name=name,
                description=clean_text(_dict_get(item, mapping.get("description"), "")),
                source_id=source["id"], source_kind=source["kind"], adapter=self.name, native_rank=rank,
                canonical_url=safe_web_url(_dict_get(item, mapping.get("url"), "")),
                repository=repository, skill_path=skill_path, ref=ref,
                slug=slug, publisher=clean_text(_dict_get(item, mapping.get("publisher"), "")) or None,
                trust=source.get("trust", "unverified"), install=_github_install(repository, skill_path, ref, slug),
            ))
            if len(results) >= limit:
                break
        return results
