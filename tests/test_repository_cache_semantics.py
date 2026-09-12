from __future__ import annotations

import io
import os
import sys
import tarfile
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import AdapterContext
from universal_skill_finder.adapters.repositories import GitHubRepoAdapter
from universal_skill_finder.cache import Cache
from universal_skill_finder.http import FinderHttpError


SKILL = b"---\nname: pdf-reader\ndescription: Read PDF files\n---\n"


def source(source_id: str, **extra: object) -> dict[str, object]:
    return {
        "id": source_id,
        "kind": "repository",
        "adapter": "github-repo",
        "repository": "Acme/Skills",
        "ref": "main",
        "include": ["**/SKILL.md"],
        "exclude": [],
        **extra,
    }


def archive() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        info = tarfile.TarInfo("skills-main/pdf/SKILL.md")
        info.size = len(SKILL)
        tar.addfile(info, io.BytesIO(SKILL))
    return output.getvalue()


class ArchiveHttp:
    def __init__(self, raw: bytes | None = None, error: Exception | None = None):
        self.raw = archive() if raw is None else raw
        self.error = error
        self.archive_calls = 0
        self.metadata_calls = 0
        self.cache_at_metadata: object | None = None

    def get_bytes(self, _url: str, *, max_bytes: int | None = None) -> bytes:
        self.archive_calls += 1
        if self.error is not None:
            raise self.error
        return self.raw

    def get_json(self, _url: str, *, headers: dict[str, str] | None = None) -> dict[str, object]:
        self.metadata_calls += 1
        return {"full_name": "acme/skills", "stargazers_count": 7}


def context(root: Path, http: ArchiveHttp, **settings: object) -> AdapterContext:
    return AdapterContext(
        http=http,
        cache=Cache(root / "cache"),
        settings={
            "max_archive_bytes": 1_048_576,
            "max_archive_members": 100,
            "max_archive_uncompressed_bytes": 1_048_576,
            "max_skill_file_bytes": 1_024,
            "max_skills_per_repository": 10,
            "repository_cache_ttl_seconds": 60,
            **settings,
        },
    )


class RepositoryCatalogueCacheTests(unittest.TestCase):
    def test_aliases_share_catalogue_identity_without_source_id(self) -> None:
        adapter = GitHubRepoAdapter()
        first, alias = source("source-one"), source("source-two")
        self.assertEqual(adapter._catalog_key(first), adapter._catalog_key(alias))
        with TemporaryDirectory() as temporary:
            http = ArchiveHttp()
            first_context = context(Path(temporary), http)
            first_catalogue, first_age = adapter.catalog(first, first_context)
            second_catalogue, second_age = adapter.catalog(alias, context(Path(temporary), http))
        self.assertIsNone(first_age)
        self.assertIsNotNone(second_age)
        self.assertEqual(http.archive_calls, 1)
        self.assertEqual([item.content_sha256 for item in first_catalogue], [item.content_sha256 for item in second_catalogue])

    def test_transient_failure_uses_bounded_stale_catalogue_and_preserves_age(self) -> None:
        adapter = GitHubRepoAdapter()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            priming = context(root, ArchiveHttp())
            adapter.catalog(source("source-one"), priming)
            key = priming.cache.key(adapter._catalog_key(source("source-one"), priming))
            cache_path = priming.cache._path("repositories", key)
            stale_at = time.time() - 2 * 86_400
            os.utime(cache_path, (stale_at, stale_at))

            failing = context(root, ArchiveHttp(error=FinderHttpError("HTTP 503", status=503)))
            catalogue, age = adapter.catalog(source("source-two"), failing)

        self.assertEqual(len(catalogue), 1)
        self.assertGreaterEqual(age or 0, 2 * 86_400 - 2)
        self.assertEqual(failing.cache_age_seconds, age)
        self.assertTrue(failing.incomplete_results)
        self.assertEqual(failing.detail, "cached fallback; source request failed")

    def test_refresh_never_substitutes_stale_catalogue(self) -> None:
        adapter = GitHubRepoAdapter()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            priming = context(root, ArchiveHttp())
            adapter.catalog(source("source-one"), priming)
            key = priming.cache.key(adapter._catalog_key(source("source-one"), priming))
            cache_path = priming.cache._path("repositories", key)
            stale_at = time.time() - 2 * 86_400
            os.utime(cache_path, (stale_at, stale_at))
            refreshed = context(root, ArchiveHttp(error=FinderHttpError("HTTP 503", status=503)))
            refreshed.refresh = True
            with self.assertRaises(FinderHttpError):
                adapter.catalog(source("source-two"), refreshed)
        self.assertFalse(refreshed.incomplete_results)
        self.assertIsNone(refreshed.cache_age_seconds)

    def test_catalogue_older_than_seven_days_is_not_a_fallback(self) -> None:
        adapter = GitHubRepoAdapter()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            priming = context(root, ArchiveHttp())
            adapter.catalog(source("source-one"), priming)
            key = priming.cache.key(adapter._catalog_key(source("source-one"), priming))
            cache_path = priming.cache._path("repositories", key)
            expired_at = time.time() - 8 * 86_400
            os.utime(cache_path, (expired_at, expired_at))
            failed = context(root, ArchiveHttp(error=FinderHttpError("HTTP 503", status=503)))
            with self.assertRaises(FinderHttpError):
                adapter.catalog(source("source-two"), failed)
        self.assertFalse(failed.incomplete_results)
        self.assertIsNone(failed.cache_age_seconds)

    def test_foreground_catalogue_does_not_wait_for_optional_stars(self) -> None:
        with TemporaryDirectory() as temporary:
            http = ArchiveHttp()
            catalogue, _ = GitHubRepoAdapter().catalog(source("source-one"), context(Path(temporary), http))
        self.assertEqual(len(catalogue), 1)
        self.assertEqual(http.metadata_calls, 0)

    def test_critical_catalogue_never_fetches_optional_stars(self) -> None:
        adapter = GitHubRepoAdapter()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            http = ArchiveHttp()
            current = context(root, http, repository_metadata_idle=True)
            key = current.cache.key(adapter._catalog_key(source("source-one"), current))

            catalogue, _ = adapter.catalog(source("source-one"), current)

        self.assertEqual(http.metadata_calls, 0)
        self.assertNotIn("github_stars", catalogue[0].metrics)

    def test_parse_limits_partition_physical_catalogues(self) -> None:
        adapter = GitHubRepoAdapter()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = context(root, ArchiveHttp(), max_skill_file_bytes=1024)
            second = context(root, ArchiveHttp(), max_skill_file_bytes=2048)
            self.assertNotEqual(adapter._catalog_key(source("one"), first), adapter._catalog_key(source("two"), second))

    def test_concurrent_catalogue_consumers_share_one_cross_cache_fetch_lease(self) -> None:
        class BlockingHttp(ArchiveHttp):
            def __init__(self):
                super().__init__()
                self.started = threading.Event()
                self.release = threading.Event()

            def get_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes:
                self.archive_calls += 1
                self.started.set()
                self.release.wait(1)
                return self.raw

        adapter = GitHubRepoAdapter()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            http = BlockingHttp()
            first = context(root, http)
            second = context(root, http)
            outcomes: list[object] = []
            leader = threading.Thread(target=lambda: outcomes.append(adapter.catalog(source("one"), first)))
            follower = threading.Thread(target=lambda: outcomes.append(adapter.catalog(source("two"), second)))
            leader.start()
            self.assertTrue(http.started.wait(0.5))
            follower.start()
            time.sleep(0.05)
            self.assertEqual(http.archive_calls, 1)
            http.release.set()
            leader.join(1)
            follower.join(1)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(http.archive_calls, 1)


if __name__ == "__main__":
    unittest.main()
