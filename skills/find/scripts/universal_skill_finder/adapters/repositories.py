from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import json
import os
import stat
import time
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from ..models import Candidate
from ..frontmatter import first_summary, parse_frontmatter
from ..http import FinderHttpError
from ..ranking import compatible_match_percent
from ..runtime import RuntimeLimitError
from ..text import clean_text, parse_github_repository, safe_install_reference, safe_skill_path, slugify
from .base import AdapterContext, SourceUnavailable


class _CatalogueDeadline:
    """Minimal monotonic deadline view for cache single-flight waiters."""

    def __init__(self, cutoff: float):
        self.cutoff = cutoff

    def remaining(self, _now: float | None = None) -> float:
        return max(0.0, self.cutoff - time.monotonic())

    def collection_remaining(self, _now: float | None = None) -> float:
        return self.remaining()


class _BoundedArchiveReader:
    """Bound actual gzip expansion, including headers tarfile consumes internally."""

    def __init__(self, stream: gzip.GzipFile, limit: int):
        self.stream = stream
        self.limit = limit
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.consumed
        requested = remaining + 1 if size < 0 else min(size, remaining + 1)
        raw = self.stream.read(requested)
        self.consumed += len(raw)
        if self.consumed > self.limit:
            raise SourceUnavailable("archive_limit", f"repository archive exceeded {self.limit} uncompressed bytes")
        return raw


def _safe_archive_path(name: str) -> str | None:
    # Never interpret a tar member as an installation path outside its archive root.
    if name.startswith("/") or "\\" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        return None
    parts = name.split("/")
    if len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts[1:])


def _included(path: str, source: dict[str, Any]) -> bool:
    include = source.get("include") or ["**/SKILL.md"]
    exclude = source.get("exclude") or []

    def matches(pattern: str) -> bool:
        return fnmatch.fnmatchcase(path, pattern) or (
            pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
        )

    matched = any(matches(pattern) for pattern in include)
    blocked = any(matches(pattern) for pattern in exclude)
    return matched and not blocked


def _repo_candidate(source: dict[str, Any], relative: str, markdown: str, content_hash: str, rank: int = 0) -> Candidate:
    metadata = parse_frontmatter(markdown)
    skill_path = str(PurePosixPath(relative).parent)
    name = clean_text(metadata.get("name") or PurePosixPath(skill_path).name)
    description = clean_text(metadata.get("description") or first_summary(markdown), 1500)
    repository = source.get("repository")
    ref = source.get("ref") or "HEAD"
    slug = slugify(PurePosixPath(skill_path).name or name)
    canonical_path = quote(skill_path, safe="/")
    canonical = f"https://github.com/{repository}/tree/{quote(str(ref), safe='/')}/{canonical_path}" if repository else None
    install = {
        "kind": "github",
        "repository": repository,
        "ref": ref,
        "skill_path": skill_path,
        "skill_installer": {"repository": repository, "path": skill_path, "ref": ref},
        "requires_approval": True,
    } if repository and parse_github_repository(repository) == repository and safe_skill_path(skill_path) and safe_install_reference(ref) else {}
    warnings = ["Installation handoff omitted: repository, path, or ref requires manual review"] if repository and not install else []
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    target_proof: dict[str, Any] = {}
    link_proofs: list[dict[str, Any]] = []
    if install:
        target_proof = {
            "kind": "github_archive", "status": "eligible",
            "reported": {"repository": repository, "ref": ref, "skill_path": skill_path, "name": name},
            "resolved": {"repository": repository, "ref": ref, "skill_path": skill_path, "name": name},
            "ref": ref, "skill_path": skill_path, "actual_name": name, "content_sha256": content_hash,
            "checked_at": checked_at, "detail": "SKILL.md parsed from the bounded configured repository archive",
        }
        # Archive bytes prove the exact SKILL.md and are sufficient for the
        # separate install-target gate.  They do *not* prove that a browser can
        # safely present either GitHub URL: that requires the anonymous,
        # bounded GET proof collected by validation.py.  Keep locations as
        # unverified evidence so federation can construct that request without
        # mistaking archive parsing for clickable-link authorization.
        link_proofs = [
            {
                "role": "skill_destination", "url": canonical, "status": "not_checked", "method": "bounded_archive",
                "checked_at": checked_at, "identity_basis": "repository_ref_skill_path_content_sha256",
                "detail": "archive content proof; public destination requires anonymous GET validation",
            },
        ]
    return Candidate(
        native_id=f"{repository}:{skill_path}" if repository else skill_path,
        name=name, description=description, source_id=source["id"], source_kind=source["kind"],
        adapter=source["adapter"], native_rank=rank, canonical_url=canonical, repository=repository,
        skill_path=skill_path, ref=ref, slug=slug,
        publisher=repository.split("/", 1)[0] if repository else None,
        content_sha256=content_hash, trust=source.get("trust", "unverified"), install=install, warnings=warnings,
        target_proof=target_proof, link_proofs=link_proofs,
    )


class GitHubRepoAdapter:
    name = "github-repo"

    def _catalog_key(self, source: dict[str, Any], context: AdapterContext | None = None) -> str:
        """Describe physical catalogue contents, deliberately excluding source authority.

        Federation attaches each cached occurrence to the source that admitted it.
        This lets aliases of one public repository share the bounded archive parse
        without allowing source IDs, labels, or enablement to leak into cache
        identity.
        """
        include = tuple(sorted(set(source.get("include") or ["**/SKILL.md"])))
        exclude = tuple(sorted(set(source.get("exclude") or [])))
        settings = context.settings if context is not None else {}
        limits = {
            key: int(settings.get(key, default))
            for key, default in {
                "max_archive_bytes": 52_428_800,
                "max_skill_file_bytes": 524_288,
                "max_skills_per_repository": 1000,
                "max_archive_members": 100_000,
                "max_archive_uncompressed_bytes": 268_435_456,
            }.items()
        }
        material = {
            "scanner": "github-repository-catalogue-v3",
            # GitHub owner/repository spelling is case-insensitive. Refs are not.
            "repository": str(source["repository"]).casefold(),
            "ref": str(source.get("ref") or "HEAD"),
            "include": include,
            "exclude": exclude,
            "limits": limits,
        }
        return json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _cached_catalogue(payload: Any, maximum: int) -> list[Candidate] | None:
        try:
            if not isinstance(payload, list) or len(payload) > maximum:
                return None
            return [Candidate.from_dict(item) for item in payload]
        except (TypeError, ValueError, AttributeError, KeyError):
            return None

    @staticmethod
    def _fallback_reason(error: BaseException) -> str | None:
        """Return only documented stale-fallback reasons for typed transient failures."""
        if isinstance(error, RuntimeLimitError):
            return "cached fallback; this search stopped waiting" if error.code == "deadline_exceeded" else None
        if isinstance(error, SourceUnavailable):
            if error.status in {"deadline_exceeded", "preempted", "not_started_budget"}:
                return "cached fallback; this search stopped waiting"
            if error.status in {"rate_limited", "rate_cooldown", "cooldown"}:
                return "cached fallback; rate cooldown"
            if error.status in {"timeout", "transient_failure", "service_unavailable"}:
                return "cached fallback; source request failed"
            return None
        if isinstance(error, FinderHttpError):
            if error.rate_limited:
                return "cached fallback; rate cooldown"
            if error.status is None or error.status in {408, 425, 502, 503, 504}:
                return "cached fallback; source request failed"
        return None

    def _repository_metrics(self, source: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        """Get optional repository-wide metrics without affecting skill discovery."""
        repository = source["repository"]
        get_json = getattr(context.http, "get_json", None)
        if context.offline or not isinstance(repository, str) or parse_github_repository(repository) != repository or not callable(get_json):
            return {}
        try:
            # Use the existing response-size and timeout limits. Never read ambient
            # GitHub credentials, send the user's query, retry, or fetch per skill.
            payload = get_json(
                f"https://api.github.com/repos/{repository}",
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"},
            )
        except FinderHttpError:
            # Rate limits, network failures, and invalid JSON must not discard a
            # valid archive. A future catalogue refresh can try again.
            return {}
        if not isinstance(payload, dict):
            return {}
        full_name = payload.get("full_name")
        stars = payload.get("stargazers_count")
        if (
            not isinstance(full_name, str)
            or parse_github_repository(full_name) != full_name
            or full_name.casefold() != repository.casefold()
            or type(stars) is not int
            or not 0 <= stars <= 2**63 - 1
        ):
            return {}
        return {
            "github_stars": stars,
            "github_stars_scope": "repository",
            "github_stars_repository": repository,
            "github_stars_observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    @staticmethod
    def _metrics_cache_key(source: dict[str, Any], context: AdapterContext) -> str:
        return context.cache.key("github-repository-metrics-v1", str(source.get("repository", "")).casefold())

    def _cached_repository_metrics(self, source: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        ttl = int(context.settings.get("repository_metadata_cache_ttl_seconds", 86_400))
        cached = context.cache.read(
            "repository-metadata", self._metrics_cache_key(source, context),
            max_age=None if context.offline else ttl,
        )
        if not cached or not isinstance(cached[0], dict):
            return {}
        metrics = cached[0]
        repository = parse_github_repository(source.get("repository"))
        reported_repository = metrics.get("github_stars_repository")
        stars = metrics.get("github_stars")
        if (not repository or metrics.get("github_stars_scope") != "repository"
                or not isinstance(reported_repository, str)
                or reported_repository.casefold() != repository.casefold()
                or parse_github_repository(reported_repository) != reported_repository
                or type(stars) is not int or not 0 <= stars <= 2**63 - 1
                or not isinstance(metrics.get("github_stars_observed_at"), str)):
            return {}
        return dict(metrics)

    def _fetch_catalog(self, source: dict[str, Any], context: AdapterContext) -> list[Candidate]:
        repository = source["repository"]
        ref = source.get("ref") or "HEAD"
        url = f"https://codeload.github.com/{repository}/tar.gz/{quote(str(ref), safe='')}"
        max_archive = int(context.settings.get("max_archive_bytes", 52_428_800))
        archive = context.http.get_bytes(url, max_bytes=max_archive)
        if len(archive) > max_archive:
            raise SourceUnavailable("archive_limit", f"repository archive exceeded {max_archive} compressed bytes")
        max_file = int(context.settings.get("max_skill_file_bytes", 524_288))
        max_skills = int(context.settings.get("max_skills_per_repository", 1000))
        max_members = int(context.settings.get("max_archive_members", 100_000))
        max_uncompressed = int(context.settings.get("max_archive_uncompressed_bytes", 268_435_456))
        uncompressed = 0
        results: list[Candidate] = []
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(archive)) as expanded, tarfile.open(
                fileobj=_BoundedArchiveReader(expanded, max_uncompressed), mode="r|"
            ) as tar:
                for member_index, member in enumerate(tar, 1):
                    if member_index > max_members:
                        raise SourceUnavailable("archive_limit", f"repository archive exceeded {max_members} members")
                    uncompressed += max(0, member.size)
                    if uncompressed > max_uncompressed:
                        raise SourceUnavailable(
                            "archive_limit", f"repository archive exceeded {max_uncompressed} declared uncompressed bytes"
                        )
                    if not member.isfile() or member.issym() or member.islnk() or member.issparse() or member.size > max_file:
                        continue
                    relative = _safe_archive_path(member.name)
                    if relative is None:
                        continue
                    if PurePosixPath(relative).name != "SKILL.md" or not _included(relative, source):
                        continue
                    stream = tar.extractfile(member)
                    if stream is None:
                        continue
                    with stream:
                        raw = stream.read(max_file + 1)
                    if len(raw) > max_file:
                        continue
                    try:
                        markdown = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    if len(results) >= max_skills:
                        raise SourceUnavailable("archive_limit", f"repository archive exceeded {max_skills} skills")
                    content_hash = hashlib.sha256(raw).hexdigest()
                    results.append(_repo_candidate(source, relative, markdown, content_hash))
        except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
            raise SourceUnavailable("schema_mismatch", f"invalid repository archive for {repository}: {exc}") from exc
        return results

    def _enrich_if_idle(self, candidates: list[Candidate], source: dict[str, Any], context: AdapterContext) -> None:
        """Apply explicitly idle metadata only after core catalogue publication.

        The adapter receives no independently cancellable idle lease. Therefore
        metadata is opt-in for an already-reserved idle slot; foreground search
        never initiates a stars request or waits for one.
        """
        if not candidates:
            return
        cached = self._cached_repository_metrics(source, context)
        if cached:
            for candidate in candidates:
                candidate.metrics.update(cached)
        # A foreground catalogue never starts metadata I/O. A future explicit
        # idle lane may populate this cache, but decorative stars cannot delay
        # archive discovery or consume its request/permit budget.

    def catalog(self, source: dict[str, Any], context: AdapterContext) -> tuple[list[Candidate], int | None]:
        key = context.cache.key(self._catalog_key(source, context))
        ttl = int(context.settings.get("repository_cache_ttl_seconds", 86_400))
        maximum = int(context.settings.get("max_skills_per_repository", 1000))
        offline_max_age = context.stale_max_age_seconds if context.offline else None
        cached = context.cache.read("repositories", key, max_age=offline_max_age if context.offline else ttl)
        if cached and (context.offline or not context.refresh):
            payload, age = cached
            catalogue = self._cached_catalogue(payload, maximum)
            if catalogue is not None:
                self._enrich_if_idle(catalogue, source, context)
                return catalogue, age
            # A corrupt cache must not prevent a fresh fetch, or trigger one offline.
        if context.offline:
            raise SourceUnavailable("offline_miss", f"no cached catalogue for {source['id']}")
        stale = None if context.refresh else context.cache.read("repositories", key, max_age=7 * 86_400)

        def fallback(error: BaseException):
            reason = self._fallback_reason(error)
            if stale is not None and reason is not None:
                payload, age = stale
                catalogue = self._cached_catalogue(payload, maximum)
                if catalogue is not None:
                    context.cache_age_seconds = age
                    context.incomplete_results = True
                    context.detail = reason
                    return catalogue, age
            return None

        # Cross-process aliases have separate in-memory SingleFlight instances.
        # Hold a short filesystem lease while fetching, and let waiters poll the
        # exact cache until their source deadline instead of duplicating downloads.
        source_deadline = getattr(context.http, "deadline", None)
        remaining = getattr(source_deadline, "collection_remaining", None)
        cutoff = time.monotonic() + (max(0.0, remaining()) if callable(remaining) else 5.0)
        cache_path = context.cache._path("repositories", key)
        try:
            initial_cache_info = cache_path.stat()
            initial_cache_identity = (initial_cache_info.st_ino, initial_cache_info.st_mtime_ns)
        except OSError:
            initial_cache_identity = None

        def published_during_refresh() -> bool:
            if not context.refresh:
                return True
            try:
                current = cache_path.stat()
                return (current.st_ino, current.st_mtime_ns) != initial_cache_identity
            except OSError:
                return False

        scope = "anonymous-public-catalogue-v1"
        while True:
            with context.cache.singleflight.claim("catalogues", key, scope, _CatalogueDeadline(cutoff)) as claim:
                if not claim.leader:
                    cached = context.cache.read("repositories", key, max_age=ttl)
                    if cached and published_during_refresh():
                        catalogue = self._cached_catalogue(cached[0], maximum)
                        if catalogue is not None:
                            self._enrich_if_idle(catalogue, source, context)
                            return catalogue, cached[1]
                    if time.monotonic() >= cutoff:
                        failure = SourceUnavailable("deadline_exceeded", "catalogue single-flight wait exceeded source deadline")
                        recovered = fallback(failure)
                        if recovered is not None:
                            return recovered
                        raise failure
                    continue
                acquired = False
                while time.monotonic() < cutoff:
                    with context.cache.exclusive_lease(
                        "catalogues", key, timeout=0.05,
                        stale_seconds=max(5.0, cutoff - time.monotonic() + 1.0),
                    ) as acquired:
                        if acquired:
                            cached = context.cache.read("repositories", key, max_age=ttl)
                            if cached and published_during_refresh():
                                catalogue = self._cached_catalogue(cached[0], maximum)
                                if catalogue is not None:
                                    self._enrich_if_idle(catalogue, source, context)
                                    return catalogue, cached[1]
                            try:
                                results = self._fetch_catalog(source, context)
                            except (FinderHttpError, SourceUnavailable, RuntimeLimitError) as error:
                                recovered = fallback(error)
                                if recovered is not None:
                                    return recovered
                                raise
                            context.cache.write("repositories", key, [item.to_dict() for item in results])
                            self._enrich_if_idle(results, source, context)
                            return results, None
                    cached = context.cache.read("repositories", key, max_age=ttl)
                    if cached and published_during_refresh():
                        catalogue = self._cached_catalogue(cached[0], maximum)
                        if catalogue is not None:
                            self._enrich_if_idle(catalogue, source, context)
                            return catalogue, cached[1]
                failure = SourceUnavailable("deadline_exceeded", "catalogue fetch lease wait exceeded source deadline")
                recovered = fallback(failure)
                if recovered is not None:
                    return recovered
                raise failure

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        catalog, age = self.catalog(source, context)
        context.cache_age_seconds = age
        ranked: list[tuple[int, Candidate]] = []
        for candidate in catalog:
            # Use the selected ranker's compatible word families before the
            # bounded local/repository catalogue is truncated.  Keep zero-match
            # rows at the tail: a caller's limit must not turn source retrieval
            # into an undocumented hard lexical exclusion.
            score = compatible_match_percent(query, candidate.name, candidate.description, candidate.skill_path or "")
            ranked.append((score, candidate))
        ranked.sort(key=lambda item: (-item[0], item[1].name.lower(), item[1].skill_path or ""))
        results = [candidate for _, candidate in ranked[:limit]]
        for rank, candidate in enumerate(results, 1):
            candidate.native_rank = rank
        return results


class LocalDirectoryAdapter:
    name = "local-directory"

    def search(self, source: dict[str, Any], query: str, limit: int, context: AdapterContext) -> list[Candidate]:
        root = Path(source["path"]).expanduser().resolve()
        if not root.is_dir():
            raise SourceUnavailable("not_found", f"local directory does not exist: {root}")
        max_file = int(context.settings.get("max_skill_file_bytes", 524_288))
        max_files = int(context.settings.get("max_archive_members", 100_000))
        max_skills = int(context.settings.get("max_skills_per_repository", 1000))
        max_uncompressed = int(context.settings.get("max_archive_uncompressed_bytes", 268_435_456))
        visited = 0
        indexed = 0
        bytes_read = 0
        ranked: list[tuple[int, Candidate]] = []
        # fwalk binds reads to the directory being visited and avoids a symlink swap
        # between traversal and opening a file. The portable fallback still refuses
        # static symlinks and bounds every read.
        anchored = hasattr(os, "fwalk") and os.open in os.supports_dir_fd
        walker = os.fwalk(root, follow_symlinks=False) if anchored else (
            (directory, dirs, files, None) for directory, dirs, files in os.walk(root, followlinks=False)
        )
        for directory, dirs, files, directory_fd in walker:
            visited += len(dirs) + len(files)
            if visited > max_files:
                raise SourceUnavailable("archive_limit", f"local directory exceeded {max_files} entries")
            if "SKILL.md" not in files:
                continue
            path = Path(directory) / "SKILL.md"
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    continue
                relative = path.relative_to(root).as_posix()
                if not _included(relative, source):
                    continue
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                descriptor = os.open("SKILL.md", flags, dir_fd=directory_fd) if anchored else os.open(path, flags)
                with os.fdopen(descriptor, "rb") as stream:
                    file_stat = os.fstat(stream.fileno())
                    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > max_file:
                        continue
                    raw = stream.read(max_file + 1)
                if len(raw) > max_file:
                    continue
                bytes_read += len(raw)
                if bytes_read > max_uncompressed:
                    raise SourceUnavailable("archive_limit", f"local directory exceeded {max_uncompressed} skill bytes")
                markdown = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if indexed >= max_skills:
                raise SourceUnavailable("archive_limit", f"local directory exceeded {max_skills} skills")
            candidate = _repo_candidate(source, relative, markdown, hashlib.sha256(raw).hexdigest())
            candidate.canonical_url = path.parent.as_uri()
            candidate.repository = None
            candidate.install = {"kind": "local", "path": str(path.parent), "requires_approval": True}
            checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            candidate.target_proof = {
                "kind": "local_directory", "status": "eligible",
                "reported": {"skill_path": relative, "name": candidate.name},
                "resolved": {"skill_path": relative, "name": candidate.name},
                "skill_path": relative, "actual_name": candidate.name, "content_sha256": candidate.content_sha256,
                "checked_at": checked_at, "detail": "SKILL.md read from the bounded configured local directory",
            }
            candidate.link_proofs = [{
                "role": "skill_destination", "url": candidate.canonical_url, "status": "eligible",
                "method": "local_bounded_read", "checked_at": checked_at,
                "identity_basis": "local_relative_path_content_sha256",
            }]
            score = compatible_match_percent(query, candidate.name, candidate.description, candidate.skill_path or "")
            ranked.append((score, candidate))
            indexed += 1
        ranked.sort(key=lambda item: (-item[0], item[1].name.lower()))
        results = [candidate for _, candidate in ranked[:limit]]
        for rank, candidate in enumerate(results, 1):
            candidate.native_rank = rank
        return results
