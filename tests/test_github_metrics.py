from __future__ import annotations

import io
import os
import sys
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import AdapterContext, SourceUnavailable
from universal_skill_finder.adapters.repositories import GitHubRepoAdapter, LocalDirectoryAdapter
from universal_skill_finder.cache import Cache
from universal_skill_finder.http import FinderHttpError, HttpClient


def archive(paths=("pdf", "forms")):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for path in paths:
            raw = f"---\nname: {path}\ndescription: Read and fill PDF forms\n---\n".encode()
            info = tarfile.TarInfo(f"root/{path}/SKILL.md")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return output.getvalue()


def repository_source(**extra):
    return {
        "id": "repo", "kind": "repository", "adapter": "github-repo",
        "repository": "acme/skills", "ref": "main", "include": ["**/SKILL.md"],
        "exclude": [], **extra,
    }


class MetricsHttp:
    def __init__(self, payload=None, error=None, raw=None):
        self.payload = payload
        self.error = error
        self.raw = archive() if raw is None else raw
        self.calls = []

    def get_bytes(self, url, *, max_bytes=None):
        self.calls.append(("archive", url, {"max_bytes": max_bytes}))
        return self.raw

    def get_json(self, url, *, headers=None):
        self.calls.append(("metadata", url, headers or {}))
        if self.error:
            raise self.error
        return self.payload


def context(root, http, *, metadata_idle=False, **options):
    settings = {
        "max_archive_bytes": 1_048_576, "max_archive_members": 100,
        "max_archive_uncompressed_bytes": 1_048_576,
        "max_skill_file_bytes": 1024, "max_skills_per_repository": 10,
        "repository_cache_ttl_seconds": 86_400,
    }
    if metadata_idle:
        settings["repository_metadata_idle"] = True
    return AdapterContext(
        http=http, cache=Cache(root / "cache"), settings=settings, **options,
    )


def seed_metrics(ctx, *, repository="acme/skills", stars=42, observed_at="2026-09-10T00:00:00+00:00"):
    key = ctx.cache.key("github-repository-metrics-v1", repository.casefold())
    ctx.cache.write("repository-metadata", key, {
        "github_stars": stars,
        "github_stars_scope": "repository",
        "github_stars_repository": repository,
        "github_stars_observed_at": observed_at,
    })


class GitHubMetricsTests(unittest.TestCase):
    def test_reuses_one_cached_repository_snapshot_for_all_skills_without_network(self):
        http = MetricsHttp({"full_name": "Acme/Skills", "stargazers_count": 9999})
        with TemporaryDirectory() as temp, patch.dict(os.environ, {
            "GITHUB_TOKEN": "must-not-be-sent", "GH_TOKEN": "must-not-be-sent",
        }):
            ctx = context(Path(temp), http, metadata_idle=True)
            seed_metrics(ctx, stars=1234)
            candidates, age = GitHubRepoAdapter().catalog(repository_source(), ctx)
        self.assertIsNone(age)
        self.assertEqual(len(candidates), 2)
        self.assertEqual([call[0] for call in http.calls], ["archive"])
        for candidate in candidates:
            self.assertEqual(candidate.metrics["github_stars"], 1234)
            self.assertEqual(candidate.metrics["github_stars_scope"], "repository")
            self.assertEqual(candidate.metrics["github_stars_repository"], "acme/skills")
            self.assertEqual(candidate.metrics["github_stars_observed_at"], "2026-09-10T00:00:00+00:00")
        candidates[0].metrics["github_stars"] = 3
        self.assertEqual(candidates[1].metrics["github_stars"], 1234)

    def test_zero_stars_is_valid_not_missing(self):
        with TemporaryDirectory() as temp:
            ctx = context(Path(temp), MetricsHttp())
            seed_metrics(ctx, stars=0)
            candidates, _ = GitHubRepoAdapter().catalog(repository_source(), ctx)
        self.assertEqual(candidates[0].metrics["github_stars"], 0)

    def test_invalid_counts_or_repository_identity_never_discard_skills(self):
        payloads = [None, [], {}, {"github_stars": 10}]
        payloads += [{"github_stars_scope": "repository", "github_stars_repository": name,
                      "github_stars": 10, "github_stars_observed_at": "2026-09-10T00:00:00+00:00"} for name in (
            "other/skills", "acme/renamed", "https://github.com/acme/skills", "acme/skills\n",
        )]
        payloads += [{"github_stars_scope": "repository", "github_stars_repository": "acme/skills",
                      "github_stars": count, "github_stars_observed_at": "2026-09-10T00:00:00+00:00"} for count in (
            None, True, False, -1, 1.5, "1234", float("inf"), {}, [], 2**63,
        )]
        for payload in payloads:
            with self.subTest(payload=payload), TemporaryDirectory() as temp:
                ctx = context(Path(temp), MetricsHttp(payload))
                key = ctx.cache.key("github-repository-metrics-v1", "acme/skills")
                ctx.cache.write("repository-metadata", key, payload)
                candidates, _ = GitHubRepoAdapter().catalog(repository_source(), ctx)
                self.assertEqual(len(candidates), 2)
                self.assertTrue(all(candidate.metrics == {} for candidate in candidates))

    def test_foreground_catalogue_never_calls_metadata_even_if_helper_would_fail(self):
        for error in (
            FinderHttpError("HTTP 403", status=403), FinderHttpError("HTTP 429", status=429),
            FinderHttpError("response timeout exceeded"), FinderHttpError("invalid JSON"),
            FinderHttpError("response exceeded 100 bytes"), FinderHttpError("refused cross-origin redirect"),
        ):
            with self.subTest(error=str(error)), TemporaryDirectory() as temp:
                http = MetricsHttp(error=error)
                candidates, _ = GitHubRepoAdapter().catalog(repository_source(), context(Path(temp), http, metadata_idle=True))
                self.assertEqual(len(candidates), 2)
                self.assertEqual(candidates[0].metrics, {})
                self.assertEqual([call[0] for call in http.calls], ["archive"])

    def test_cached_and_offline_catalogues_reuse_timestamp_without_requests(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            http = MetricsHttp({"full_name": "acme/skills", "stargazers_count": 42})
            ctx = context(root, http, metadata_idle=True)
            seed_metrics(ctx, stars=42)
            adapter = GitHubRepoAdapter()
            original, _ = adapter.catalog(repository_source(), ctx)
            for offline in (False, True):
                ctx.offline = offline
                cached, age = adapter.catalog(repository_source(), ctx)
                self.assertIsNotNone(age)
                self.assertEqual(cached[0].metrics, original[0].metrics)
                self.assertEqual(len(http.calls), 1)
            ctx.offline = False
            ctx.refresh = True
            refreshed, age = adapter.catalog(repository_source(), ctx)
            self.assertIsNone(age)
            self.assertEqual(refreshed[0].metrics["github_stars"], 42)
            self.assertEqual(len(http.calls), 2)

    def test_offline_miss_does_not_fetch_anything(self):
        with TemporaryDirectory() as temp:
            http = MetricsHttp()
            with self.assertRaises(SourceUnavailable) as caught:
                GitHubRepoAdapter().catalog(repository_source(), context(Path(temp), http, offline=True))
            self.assertEqual(caught.exception.status, "offline_miss")
            self.assertEqual(http.calls, [])

    def test_old_cached_catalogue_without_metrics_does_not_trigger_enrichment(self):
        with TemporaryDirectory() as temp:
            http = MetricsHttp(error=FinderHttpError("HTTP 403", status=403))
            ctx = context(Path(temp), http)
            adapter = GitHubRepoAdapter()
            adapter.catalog(repository_source(), ctx)
            http.payload = {"full_name": "acme/skills", "stargazers_count": 42}
            http.error = None
            cached, age = adapter.catalog(repository_source(), ctx)
            self.assertIsNotNone(age)
            self.assertEqual(cached[0].metrics, {})
            self.assertEqual(len(http.calls), 1)

    def test_metadata_does_not_run_for_invalid_or_empty_archives(self):
        with TemporaryDirectory() as temp:
            http = MetricsHttp(raw=b"not a tar archive")
            with self.assertRaises(SourceUnavailable):
                GitHubRepoAdapter().catalog(repository_source(), context(Path(temp), http))
            self.assertEqual([call[0] for call in http.calls], ["archive"])
        with TemporaryDirectory() as temp:
            http = MetricsHttp(raw=archive(paths=()))
            candidates, _ = GitHubRepoAdapter().catalog(repository_source(), context(Path(temp), http))
            self.assertEqual(candidates, [])
            self.assertEqual([call[0] for call in http.calls], ["archive"])

    def test_local_directories_make_no_metadata_requests(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "SKILL.md").write_text("---\nname: pdf\ndescription: Read PDF forms\n---\n", encoding="utf-8")
            http = MetricsHttp({"full_name": "acme/skills", "stargazers_count": 10})
            candidates = LocalDirectoryAdapter().search(
                repository_source(adapter="local-directory", repository=None, path=str(root)),
                "pdf", 10, context(root, http),
            )
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].metrics, {})
            self.assertEqual(http.calls, [])

    def test_metadata_uses_existing_bounded_http_json_method(self):
        with TemporaryDirectory() as temp:
            http = HttpClient(timeout=0.25, max_bytes=100)
            ctx = context(Path(temp), http)
            with patch.object(http, "request", side_effect=FinderHttpError("response exceeded 100 bytes")) as request:
                metrics = GitHubRepoAdapter()._repository_metrics(repository_source(), ctx)
            self.assertEqual(metrics, {})
            self.assertEqual(http.timeout, 0.25)
            self.assertEqual(http.max_bytes, 100)
            request.assert_called_once_with(
                "GET", "https://api.github.com/repos/acme/skills", params=None,
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"},
            )

    def test_metadata_helper_rejects_invalid_repository_or_offline_context(self):
        with TemporaryDirectory() as temp:
            http = MetricsHttp({"full_name": "acme/skills", "stargazers_count": 10})
            ctx = context(Path(temp), http)
            adapter = GitHubRepoAdapter()
            for repository in (None, "", "../skills", "https://github.com/acme/skills", "acme/skills\n"):
                self.assertEqual(adapter._repository_metrics(repository_source(repository=repository), ctx), {})
            ctx.offline = True
            self.assertEqual(adapter._repository_metrics(repository_source(), ctx), {})
            self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
