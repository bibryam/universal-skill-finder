"""Bounded public SKILL.md discovery through GitHub's authenticated code API."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

from ..frontmatter import parse_frontmatter
from ..http import FinderHttpError
from ..models import Candidate
from ..text import clean_text, parse_github_repository, safe_skill_path, safe_web_url
from .base import AdapterContext, SourceUnavailable

API_ORIGIN = "https://api.github.com"
MAX_SEARCH_ROWS = 20
MAX_FILE_REQUESTS = 10
MAX_METADATA_REQUESTS = 3
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_SEARCH_BYTES = 512 * 1024
MAX_BLOB_BYTES = 384 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_SECONDS = 20.0
SEARCH_SCOPE_NOTE = "GitHub search scope follows the configured token; only public results are retained"
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_ENV = re.compile(r"[A-Z][A-Z0-9_]{1,127}")


class _BudgetExceeded(RuntimeError):
    pass


class _Budget:
    def __init__(self, context: AdapterContext, headers: dict[str, str]):
        self.context = context
        self.headers = headers
        self.deadline = time.monotonic() + MAX_SECONDS
        self.bytes_left = MAX_TOTAL_BYTES
        self.requests_left = 1 + MAX_FILE_REQUESTS + MAX_METADATA_REQUESTS
        configured = context.settings.get("max_response_bytes", MAX_TOTAL_BYTES)
        self.response_limit = configured if type(configured) is int and configured > 0 else MAX_TOTAL_BYTES

    def get(self, path: str, *, max_bytes: int, params: dict[str, object] | None = None) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.bytes_left <= 0 or self.requests_left <= 0:
            raise _BudgetExceeded("GitHub discovery reached its request, byte, or time budget")
        self.requests_left -= 1
        cap = min(max_bytes, self.bytes_left, self.response_limit)
        # Authentication is required only for code search. Public blob/metadata
        # reads must remain anonymous: stale search metadata must never grant
        # access to content after a repository becomes private. These reads use
        # GitHub's lower anonymous quota rather than the token's private access.
        headers = self.headers if path == "/search/code" else {
            name: value for name, value in self.headers.items() if name != "Authorization"
        }
        response = self.context.http.request(
            "GET", API_ORIGIN + path, params=params, headers=headers,
            max_bytes=cap, timeout=remaining, phase="source", quota_group="github-api",
        )
        self.bytes_left -= len(response.data)
        if len(response.data) > cap:
            raise _BudgetExceeded("GitHub discovery exceeded its response byte budget")
        if time.monotonic() > self.deadline:
            raise _BudgetExceeded("GitHub discovery reached its time budget")
        # The HTTP client already refuses cross-origin redirects. Never consume a
        # substituted response from another host, including in injected clients.
        parsed = urlparse(response.final_url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise SourceUnavailable("schema_mismatch", "GitHub response origin did not match")
        try:
            payload = json.loads(response.data.decode("utf-8"))
            # Match the shared HTTP client's nesting limit, including fields
            # outside the small schema this connector consumes.
            pending = [(payload, 0)]
            while pending:
                value, depth = pending.pop()
                if depth > 100:
                    raise ValueError("JSON nesting exceeds 100 levels")
                children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
                pending.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
            return payload
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise SourceUnavailable("schema_mismatch", "GitHub returned invalid JSON") from exc


def _search_query(query: str) -> tuple[str, bool]:
    """Treat input as capability words, never as GitHub operators or qualifiers."""
    words = list(dict.fromkeys(re.findall(r"[^\W_][\w.+#-]*", clean_text(query, 500).lower())))
    if not words:
        raise SourceUnavailable("invalid_query", "GitHub discovery needs a capability containing words or numbers")
    # The legacy REST code API documents no public/private visibility qualifier.
    # Do not invent `is:public` from repository search: scope follows the token,
    # and _location rejects non-public matches before fetching any file.
    suffix = "filename:SKILL.md"
    selected: list[str] = []
    shortened = False
    for word in words:
        available = 256 - len(" ".join([*selected, suffix])) - 3
        if available < 1:
            shortened = True
            break
        if len(word) > available:
            word = word[:available]
            shortened = True
        selected.append(f'"{word}"')
        if shortened:
            break
    return " ".join([*selected, suffix]), shortened


def _location(item: object) -> tuple[str, str, str, str | None, str] | None:
    if not isinstance(item, dict) or not isinstance(item.get("repository"), dict):
        return None
    repository_data = item["repository"]
    repository = repository_data.get("full_name")
    path = item.get("path")
    sha = item.get("sha")
    if (
        repository_data.get("private") is not False
        or not isinstance(repository, str) or parse_github_repository(repository) != repository
        or not isinstance(path, str) or safe_skill_path(path) != path
        or PurePosixPath(path).name != "SKILL.md"
        or not isinstance(sha, str) or not _SHA.fullmatch(sha)
    ):
        return None
    sha = sha.lower()
    ref = None
    canonical = f"https://github.com/{repository}"
    html_url = safe_web_url(item.get("html_url"))
    if html_url:
        parsed = urlparse(html_url)
        parts = parsed.path.lstrip("/").split("/")
        if (
            parsed.scheme == "https" and parsed.netloc == "github.com"
            and not (parsed.query or parsed.fragment or parsed.params)
            and len(parts) >= 5 and "/".join(parts[:2]) == repository
            and parts[2] == "blob" and _SHA.fullmatch(parts[3])
            and "/".join(parts[4:]) == path
        ):
            ref = parts[3].lower()
            canonical = f"https://github.com/{repository}/blob/{ref}/{quote(path, safe='/')}"
    return repository, path, sha, ref, canonical


def _blob_bytes(payload: object, expected_sha: str, max_file: int) -> bytes:
    if not isinstance(payload, dict):
        raise ValueError("blob must be an object")
    size, content = payload.get("size"), payload.get("content")
    if (
        payload.get("encoding") != "base64" or payload.get("sha") != expected_sha
        or type(size) is not int or not 0 < size <= max_file
        or not isinstance(content, str) or len(content) > MAX_BLOB_BYTES
        or not re.fullmatch(r"[A-Za-z0-9+/=\r\n]*", content)
    ):
        raise ValueError("invalid bounded GitHub blob")
    encoded = content.replace("\r", "").replace("\n", "")
    if len(encoded) > 4 * ((max_file + 2) // 3):
        raise ValueError("encoded blob exceeds file limit")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) != size:
        raise ValueError("blob size mismatch")
    # SHA-1 is Git's object identifier, not a security endorsement. Keep a
    # separate SHA-256 of the actual skill bytes for our provenance contract.
    actual_sha = hashlib.sha1(b"blob " + str(size).encode("ascii") + b"\0" + raw, usedforsecurity=False).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError("blob identity mismatch")
    return raw


def _metadata(raw: bytes) -> tuple[str, str]:
    markdown = raw.decode("utf-8")
    metadata = parse_frontmatter(markdown)
    name, description = metadata.get("name", ""), metadata.get("description", "")
    # Do not turn a file named SKILL.md, an example, or a YAML collection into a
    # skill through filename fallbacks. No YAML object constructors are invoked.
    if (
        not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64
        or not description or len(description) > 1024
    ):
        raise ValueError("missing or invalid required skill metadata")
    lines = markdown.splitlines()
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise ValueError("required frontmatter must have a closing delimiter")
    header = "\n".join(lines[1:end])
    for field in ("name", "description"):
        declarations = re.findall(rf"^{field}:[ \t]*(.*)$", header, re.MULTILINE)
        if len(declarations) != 1:
            raise ValueError("required metadata must have one scalar declaration")
        value = declarations[0].strip()
        quoted = len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}
        if not quoted and value not in {">", ">-", "|", "|-"} and (
            value.startswith(("[", "{", "!", "&", "*"))
            or value.lower() in {"null", "true", "false", "yes", "no", "on", "off", "~"}
            or re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?", value)
        ):
            raise ValueError("required metadata must be text, not a YAML object or scalar type")
    return name, description


def _metrics(payload: object, repository: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("private") is not False:
        return {}
    full_name, stars = payload.get("full_name"), payload.get("stargazers_count")
    if (
        not isinstance(full_name, str) or full_name.casefold() != repository.casefold()
        or type(stars) is not int or not 0 <= stars <= 2**63 - 1
    ):
        return {}
    return {"github_stars": stars, "github_stars_scope": "repository", "github_stars_repository": repository,
            "github_stars_observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _cached_metrics(context: AdapterContext, repository: str) -> dict[str, Any]:
    """Read optional repository metrics without putting GitHub I/O on search's critical path."""
    key = context.cache.key("github-repository-metrics-v1", repository.casefold())
    ttl = int(context.settings.get("repository_metadata_cache_ttl_seconds", 86_400))
    cached = context.cache.read("repository-metadata", key, max_age=None if context.offline else ttl)
    if not cached or not isinstance(cached[0], dict):
        return {}
    value = cached[0]
    stars = value.get("github_stars")
    if (value.get("github_stars_scope") != "repository"
            or value.get("github_stars_repository", "").casefold() != repository.casefold()
            or type(stars) is not int or not 0 <= stars <= 2**63 - 1
            or not isinstance(value.get("github_stars_observed_at"), str)):
        return {}
    return dict(value)


class GitHubCodeSearchAdapter:
    name = "github-code-search"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        if context.offline:
            raise SourceUnavailable("offline_miss", "GitHub discovery needs an exact cached search in offline mode")
        if (
            source.get("base_url") != API_ORIGIN or "endpoint" in source or "headers" in source
            or source.get("public_only", True) is not True or source.get("auth_optional", False) is not False
        ):
            raise SourceUnavailable("invalid_config", "GitHub discovery requires its fixed API origin, explicit authentication, and public-only results")
        env_name = source.get("auth_env")
        token = os.environ.get(env_name) if isinstance(env_name, str) and _ENV.fullmatch(env_name) else None
        if not token:
            raise SourceUnavailable("auth_missing", "missing configured GitHub token environment variable")
        if len(token) > 4096 or not re.fullmatch(r"[\x21-\x7e]+", token):
            raise SourceUnavailable("auth_failed", "configured GitHub token is not a valid header value")
        if type(limit) is not int or limit < 1:
            raise SourceUnavailable("invalid_query", "GitHub discovery limit must be positive")
        search_query, shortened = _search_query(query)
        reasons: list[str] = ["GitHub search terms were shortened to the query limit"] if shortened else []
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
                   "X-GitHub-Api-Version": "2026-03-10"}
        budget = _Budget(context, headers)
        page_size = min(MAX_SEARCH_ROWS, max(10, limit * 2))
        try:
            payload = budget.get("/search/code", params={"q": search_query, "per_page": page_size, "page": 1}, max_bytes=MAX_SEARCH_BYTES)
        except _BudgetExceeded as exc:
            raise SourceUnavailable("timeout", str(exc)) from exc
        if (
            not isinstance(payload, dict) or not isinstance(payload.get("items"), list)
            or type(payload.get("total_count")) is not int or not 0 <= payload["total_count"] <= 2**63 - 1
            or type(payload.get("incomplete_results")) is not bool
        ):
            raise SourceUnavailable("schema_mismatch", "GitHub search response did not match its documented schema")
        if payload["incomplete_results"]:
            reasons.append("GitHub reported incomplete search results")
        results: list[Candidate] = []
        seen: set[tuple[str, str]] = set()
        checked = fetched = skipped = 0
        throttled = False
        configured_file_limit = context.settings.get("max_skill_file_bytes", MAX_FILE_BYTES)
        max_file = min(MAX_FILE_BYTES, configured_file_limit) if type(configured_file_limit) is int and configured_file_limit > 0 else MAX_FILE_BYTES
        for rank, item in enumerate(payload["items"][:page_size], 1):
            if len(results) >= limit:
                break
            checked += 1
            location = _location(item)
            if location is None:
                skipped += 1
                continue
            repository, path, blob_sha, ref, canonical = location
            identity = (repository.casefold(), path)
            if identity in seen:
                continue
            seen.add(identity)
            if fetched >= MAX_FILE_REQUESTS:
                reasons.append("GitHub discovery reached its file-fetch limit")
                break
            fetched += 1
            try:
                blob_cap = min(MAX_BLOB_BYTES, 4 * ((max_file + 2) // 3) + max_file // 16 + 4096)
                blob = budget.get(f"/repos/{repository}/git/blobs/{blob_sha}", max_bytes=blob_cap)
                raw = _blob_bytes(blob, blob_sha, max_file)
                name, description = _metadata(raw)
            except _BudgetExceeded as exc:
                reasons.append(str(exc))
                break
            except (FinderHttpError, SourceUnavailable, ValueError, UnicodeError, binascii.Error) as exc:
                skipped += 1
                if isinstance(exc, FinderHttpError) and exc.rate_limited:
                    throttled = True
                    context.rate_limit_headers = dict(exc.headers)
                    reasons.append("GitHub rate-limited file verification; retry later")
                    break
                continue
            results.append(Candidate(
                native_id=f"{repository}:{path}", name=name, description=description,
                source_id=source["id"], source_kind=source["kind"], adapter=self.name, native_rank=rank,
                canonical_url=canonical, repository=repository, skill_path=str(PurePosixPath(path).parent),
                ref=ref, slug=name, publisher=repository.split("/", 1)[0], content_sha256=hashlib.sha256(raw).hexdigest(),
                trust=source.get("trust", "unverified"), warnings=["GitHub discovery is not a skill security review"],
            ))
        if skipped:
            reasons.append(f"Skipped {skipped} private, invalid, or unavailable search matches")
        if len(results) < limit and payload["total_count"] > checked:
            reasons.append("More GitHub matches exist outside this bounded search")
        if reasons:
            context.incomplete_results = True
        context.detail = "; ".join([*dict.fromkeys(reasons), SEARCH_SCOPE_NOTE])

        # Counts are optional cached evidence. A foreground search never spends
        # request or permit budget refreshing decorative repository metadata.
        repositories = list(dict.fromkeys(candidate.repository for candidate in results))
        for repository in repositories:
            metrics = _cached_metrics(context, repository)
            for candidate in results:
                if candidate.repository == repository:
                    candidate.metrics.update(metrics)
        return results
