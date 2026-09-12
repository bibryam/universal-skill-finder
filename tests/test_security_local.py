from __future__ import annotations

import io
import json
import os
import stat
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
from universal_skill_finder.config import (
    ConfigurationError,
    add_github_repository,
    empty_overlay,
    import_source_pack,
    load_config,
    save_overlay,
)
from universal_skill_finder.frontmatter import parse_frontmatter


SKILL = b"---\nname: pdf-reader\ndescription: Read PDF files\n---\n"


class ArchiveHttp:
    def __init__(self, raw: bytes = b""):
        self.raw = raw
        self.calls = 0

    def get_bytes(self, url, *, max_bytes=None):
        self.calls += 1
        return self.raw


def repository_source(**extra):
    return {
        "id": "repo-test", "kind": "repository", "adapter": "github-repo",
        "repository": "acme/skills", "ref": "main", "include": ["**/SKILL.md"],
        "exclude": [], **extra,
    }


def context(root: Path, http=None, **settings):
    return AdapterContext(
        http=http or ArchiveHttp(), cache=Cache(root / "cache"), settings={
            "max_archive_bytes": 1024 * 1024, "max_archive_members": 100,
            "max_archive_uncompressed_bytes": 1024 * 1024,
            "max_skill_file_bytes": 1024, "max_skills_per_repository": 10,
            **settings,
        },
    )


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for info, data in entries:
            if isinstance(info, str):
                info = tarfile.TarInfo(info)
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return output.getvalue()


class ArchiveSecurityTests(unittest.TestCase):
    def test_pax_metadata_expansion_is_bounded_before_tar_parses_it(self):
        info = tarfile.TarInfo("repository-main/pdf/SKILL.md")
        info.size = len(SKILL)
        info.pax_headers = {"comment": "x" * 200_000}
        raw = archive([(info, SKILL)])
        self.assertLess(len(raw), 2000)
        with TemporaryDirectory() as temp:
            ctx = context(Path(temp), ArchiveHttp(raw), max_archive_uncompressed_bytes=32_768)
            with self.assertRaises(SourceUnavailable) as caught:
                GitHubRepoAdapter().catalog(repository_source(), ctx)
            self.assertEqual(caught.exception.status, "archive_limit")

    def test_gnu_longname_metadata_expansion_is_bounded(self):
        info = tarfile.TarInfo("././@LongLink")
        info.type = tarfile.GNUTYPE_LONGNAME
        info.size = 100_000
        raw = archive([(info, b"x" * 99_999 + b"\0"), ("root/pdf/SKILL.md", SKILL)])
        with TemporaryDirectory() as temp:
            ctx = context(Path(temp), ArchiveHttp(raw), max_archive_uncompressed_bytes=32_768)
            with self.assertRaises(SourceUnavailable) as caught:
                GitHubRepoAdapter().catalog(repository_source(), ctx)
            self.assertEqual(caught.exception.status, "archive_limit")

    def test_unsafe_archive_paths_links_and_special_files_are_not_candidates(self):
        entries = [(name, SKILL) for name in (
            "/root/absolute/SKILL.md", "root/../traversal/SKILL.md",
            "root/back\\slash/SKILL.md", "root/control\x1b/SKILL.md", "root/valid/SKILL.md",
        )]
        for kind, target in ((tarfile.SYMTYPE, "root/valid/SKILL.md"), (tarfile.LNKTYPE, "root/valid/SKILL.md"), (tarfile.FIFOTYPE, "")):
            info = tarfile.TarInfo(f"root/type-{kind.decode()}/SKILL.md")
            info.type = kind
            info.linkname = target
            entries.append((info, b""))
        with TemporaryDirectory() as temp:
            results, _ = GitHubRepoAdapter().catalog(repository_source(), context(Path(temp), ArchiveHttp(archive(entries))))
            self.assertEqual([item.skill_path for item in results], ["valid"])
            self.assertNotIn("skills_cli", results[0].install)
            self.assertTrue(results[0].install["requires_approval"])

    def test_skill_limit_reports_incomplete_archive_instead_of_success(self):
        raw = archive([("root/one/SKILL.md", SKILL), ("root/two/SKILL.md", SKILL)])
        with TemporaryDirectory() as temp:
            ctx = context(Path(temp), ArchiveHttp(raw), max_skills_per_repository=1)
            with self.assertRaises(SourceUnavailable) as caught:
                GitHubRepoAdapter().catalog(repository_source(), ctx)
            self.assertEqual(caught.exception.status, "archive_limit")

    def test_direct_archive_handoffs_reject_option_like_and_ambiguous_paths(self):
        raw = archive([(f"root/{path}/SKILL.md", SKILL) for path in ("-option", "white space", "$(command)", "valid")])
        with TemporaryDirectory() as temp:
            candidates, _ = GitHubRepoAdapter().catalog(repository_source(), context(Path(temp), ArchiveHttp(raw)))
            self.assertEqual(len(candidates), 4)
            for candidate in candidates:
                with self.subTest(path=candidate.skill_path):
                    if candidate.skill_path == "valid":
                        self.assertEqual(candidate.install["skill_installer"]["path"], "valid")
                    else:
                        self.assertEqual(candidate.install, {})
                        self.assertIn("manual review", candidate.warnings[0])

    def test_corrupt_repository_cache_recovers_online_and_never_fetches_offline(self):
        raw = archive([("root/valid/SKILL.md", SKILL)])
        with TemporaryDirectory() as temp:
            ctx = context(Path(temp), ArchiveHttp(raw))
            adapter = GitHubRepoAdapter()
            source = repository_source()
            key = ctx.cache.key(adapter._catalog_key(source, ctx))
            for payload in ({"bad": "shape"}, [None], [{"name": "missing required fields"}]):
                ctx.cache.write("repositories", key, payload)
                ctx.offline = True
                with self.assertRaises(SourceUnavailable) as caught:
                    adapter.catalog(source, ctx)
                self.assertEqual(caught.exception.status, "offline_miss")
                self.assertEqual(ctx.http.calls, 0)
            ctx.offline = False
            candidates, age = adapter.catalog(source, ctx)
            self.assertEqual(len(candidates), 1)
            self.assertIsNone(age)
            self.assertEqual(ctx.http.calls, 1)

    def test_yaml_tags_remain_text_and_are_never_constructed(self):
        metadata = parse_frontmatter("---\nname: pdf-reader\ndescription: !!python/object/apply:os.system ['touch marker']\n---\n")
        self.assertEqual(metadata["description"], "!!python/object/apply:os.system ['touch marker']")


class LocalFilesystemSecurityTests(unittest.TestCase):
    def test_local_scan_ignores_symlink_files_and_directories(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            tree = root / "skills"
            tree.mkdir()
            external = root / "outside"
            external.mkdir()
            (external / "SKILL.md").write_bytes(SKILL)
            try:
                (tree / "alias").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            (tree / "SKILL.md").symlink_to(external / "SKILL.md")
            (tree / "valid").mkdir()
            (tree / "valid" / "SKILL.md").write_bytes(SKILL)
            source = repository_source(adapter="local-directory", repository=None, path=str(tree))
            results = LocalDirectoryAdapter().search(source, "pdf", 10, context(root))
            self.assertEqual([item.skill_path for item in results], ["valid"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO checks require POSIX")
    def test_local_scan_does_not_block_on_fifo(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            os.mkfifo(root / "SKILL.md")
            source = repository_source(adapter="local-directory", repository=None, path=str(root))
            self.assertEqual(LocalDirectoryAdapter().search(source, "pdf", 10, context(root)), [])

    def test_local_traversal_has_entry_and_skill_limits(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("one", "two"):
                (root / name).mkdir()
                (root / name / "SKILL.md").write_bytes(SKILL)
            source = repository_source(adapter="local-directory", repository=None, path=str(root))
            for settings in ({"max_archive_members": 1}, {"max_skills_per_repository": 1}, {"max_archive_uncompressed_bytes": len(SKILL)}):
                with self.subTest(settings=settings), self.assertRaises(SourceUnavailable) as caught:
                    LocalDirectoryAdapter().search(source, "pdf", 10, context(root, **settings))
                self.assertEqual(caught.exception.status, "archive_limit")


class CacheSecurityTests(unittest.TestCase):
    def test_cache_rejects_path_traversal(self):
        with TemporaryDirectory() as temp:
            cache = Cache(Path(temp))
            for namespace, key in (("../outside", "key"), ("queries", "../../outside"), ("/tmp", "key"), ("..", "key")):
                with self.subTest(namespace=namespace, key=key), self.assertRaises(ValueError):
                    cache.write(namespace, key, {})

    def test_cache_namespace_and_entry_symlinks_cannot_escape_root(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.mkdir()
            target = outside / "key.json"
            target.write_text('{"private": true}')
            cache = Cache(root / "cache")
            cache.root.mkdir()
            try:
                (cache.root / "queries").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            self.assertIsNone(cache.read("queries", "key"))
            self.assertEqual(cache.metadata("queries"), [])
            cache.write("queries", "key", {"overwritten": True})
            self.assertEqual(json.loads(target.read_text()), {"private": True})
            (cache.root / "queries").unlink()
            (cache.root / "queries").mkdir()
            (cache.root / "queries" / "key.json").symlink_to(target)
            self.assertIsNone(cache.read("queries", "key"))
            cache.write("queries", "key", {"fresh": True})
            self.assertEqual(json.loads(target.read_text()), {"private": True})
            self.assertFalse((cache.root / "queries" / "key.json").is_symlink())

    def test_invalid_utf8_deep_json_and_oversized_cache_are_misses(self):
        with TemporaryDirectory() as temp:
            cache = Cache(Path(temp))
            (cache.root / "queries").mkdir()
            entry = cache.root / "queries" / "key.json"
            for raw in (b"\xff", b"[" * 2000 + b"]" * 2000):
                entry.write_bytes(raw)
                self.assertIsNone(cache.read("queries", "key"))
                self.assertEqual(cache.metadata("queries"), [])
            entry.write_bytes(b'"' + b"x" * 1000 + b'"')
            with patch("universal_skill_finder.cache.MAX_CACHE_BYTES", 100):
                self.assertIsNone(cache.read("queries", "key"))

    def test_cache_write_failure_does_not_raise_and_new_files_are_private(self):
        with TemporaryDirectory() as temp:
            cache = Cache(Path(temp) / "cache")
            cache.write("queries", "key", {"query": "private"})
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE((cache.root / "queries" / "key.json").stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE((cache.root / "queries").stat().st_mode), 0o700)
            with patch("universal_skill_finder.cache.os.replace", side_effect=OSError("disk full")):
                cache.write("queries", "key", {"query": "new"})
            self.assertEqual(cache.read("queries", "key")[0], {"query": "private"})
            self.assertEqual(list((cache.root / "queries").glob(".cache-*")), [])

    def test_malformed_metadata_does_not_break_cli_or_emit_controls(self):
        with TemporaryDirectory() as temp:
            cache = Cache(Path(temp))
            cache.write("queries", "bad", {"source_id": {}, "query": ["bad"], "limit": 10})
            cache.write("queries", "good", {"source_id": "repo-test", "query": "hello\x1b[31m", "limit": 10})
            metadata = cache.metadata("queries")
            self.assertEqual(len(metadata), 1)
            self.assertNotIn("\x1b", metadata[0]["query"])


class ConfigurationSecurityTests(unittest.TestCase):
    def _import(self, root: Path, source):
        pack = root / "pack.json"
        pack.write_text(json.dumps({"schema_version": 1, "pack": {"id": "custom-pack"}, "sources": [source]}))
        return import_source_pack(load_config(str(root / "sources.json")), str(pack))

    def _http_source(self, **extra):
        return {"id": "custom-http", "adapter": "http-json-v1", "endpoint": "https://example.test/search", "mapping": {"items": "items", "name": "name"}, **extra}

    def test_config_rejects_invalid_utf8_depth_and_size_with_friendly_errors(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            for raw in (b"\xff", b"[" * 2000 + b"]" * 2000):
                path.write_bytes(raw)
                with self.assertRaisesRegex(ConfigurationError, "invalid JSON"):
                    load_config(str(path))
            path.write_bytes(b" " * 200)
            with patch("universal_skill_finder.config.MAX_CONFIG_BYTES", 100), self.assertRaisesRegex(ConfigurationError, "exceeds"):
                load_config(str(path))

    def test_invalid_overlay_ids_and_types_fail_before_merging(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            overlays = (
                {"custom_sources": [{"id": [], "adapter": "local-directory"}]},
                {"custom_sources": [{"id": "valid-id", "adapter": {}}]},
                {"custom_sources": [{"id": "valid-id", "adapter": "local-directory", "pack": []}]},
                {"custom_packs": [{"id": {}}]}, {"schema_version": True},
                {"settings": {"endpoint": "ignored-typo"}},
            )
            for overlay in overlays:
                path.write_text(json.dumps(overlay))
                with self.subTest(overlay=overlay), self.assertRaises(ConfigurationError):
                    load_config(str(path))

    def test_pack_rejects_invalid_hosts_url_credentials_and_non_http_local_scheme(self):
        with TemporaryDirectory() as temp:
            for endpoint in ("https://", "https://[bad", "https://example.test:bad/path", "https://user:secret@example.test/path", "https://example.test/path#fragment", "https://example.test/white space", "ftp://localhost/catalog"):  # pragma: allowlist secret - synthetic URL rejection fixture
                with self.subTest(endpoint=endpoint), self.assertRaises(ConfigurationError):
                    self._import(Path(temp), self._http_source(endpoint=endpoint, allow_insecure_local=True))

    def test_pack_rejects_plaintext_cookies_header_injection_and_nonboolean_local_opt_in(self):
        with TemporaryDirectory() as temp:
            sources = (
                self._http_source(headers={"Cookie": "session=secret"}),
                self._http_source(headers={"Proxy-Authorization": "Basic secret"}),
                self._http_source(headers={"X-Custom": "value\r\nAuthorization: secret"}),
                self._http_source(headers={"Authorization": {"env": "API_TOKEN", "prefix": "Bearer\r\n"}}),
                self._http_source(endpoint="http://localhost/search", allow_insecure_local="true"),
                self._http_source(adapter=[]), self._http_source(id=[]), self._http_source(kind={}),
            )
            for source in sources:
                with self.subTest(source=source), self.assertRaises(ConfigurationError):
                    self._import(Path(temp), source)
            self.assertFalse((Path(temp) / "sources.json").exists())

    def test_explicit_local_http_and_env_credentials_remain_supported(self):
        with TemporaryDirectory() as temp:
            _, sources = self._import(Path(temp), self._http_source(endpoint="http://localhost:8000/search", allow_insecure_local=True, headers={"Authorization": {"env": "API_TOKEN", "prefix": "Bearer "}}))
            self.assertEqual(sources[0]["headers"]["Authorization"]["env"], "API_TOKEN")

    def test_invalid_add_repo_does_not_poison_overlay(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            config = load_config(str(path))
            with self.assertRaises(ConfigurationError):
                add_github_repository(config, "acme/skills", source_id=None, ref="", include=None, exclude=None)
            self.assertFalse(path.exists())
            self.assertEqual(config.overlay, empty_overlay())

    def test_atomic_overlay_write_failure_preserves_old_file_and_cleans_temp(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            save_overlay(path, empty_overlay())
            original = path.read_bytes()
            updated = empty_overlay()
            updated["sources"] = [{"id": "skills-sh", "enabled": False}]
            with patch("universal_skill_finder.config.os.replace", side_effect=OSError("disk full")) as replace, self.assertRaises(ConfigurationError):
                save_overlay(path, updated)
            replace.assert_called_once()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(temp).glob(".sources-*")), [])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
