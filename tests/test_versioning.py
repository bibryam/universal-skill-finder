from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder import __version__
from universal_skill_finder.cache import Cache
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import SearchReport
from universal_skill_finder.versioning import (
    ADAPTER_CONTRACT_VERSION, CACHE_FORMAT_VERSION, REPORT_FORMAT_VERSION, SCHEMA_VERSION,
    SEARCH_REPORT_SCHEMA_VERSION, VERSION,
    effective_config_revision, release_metadata,
)
from test_universal_skill_finder import FakeHttp, StaticAdapter, candidate, finder_config, registry_source


class VersioningTests(unittest.TestCase):
    @staticmethod
    def package(root: Path, *, skill: bool = False) -> Path:
        package = root / "scripts" / "universal_skill_finder"
        (package / "data").mkdir(parents=True)
        (package / "__init__.py").write_text('VERSION = "0.1.0"\n', encoding="utf-8")
        (package / "data" / "sources.default.json").write_text('{"schema_version":1,"sources":[]}', encoding="utf-8")
        if skill:
            (root / "SKILL.md").write_text("# Finder\n", encoding="utf-8")
            (root / "scripts" / "run.sh").write_text("exit 0\n", encoding="utf-8")
        return package

    def test_release_metadata_has_explicit_contract_versions(self):
        metadata = release_metadata()
        self.assertEqual(metadata["release_version"], VERSION)
        self.assertEqual(__version__, VERSION)
        self.assertEqual(metadata["schema_version"], SCHEMA_VERSION)
        self.assertEqual(metadata["adapter_contract_version"], ADAPTER_CONTRACT_VERSION)
        self.assertEqual(metadata["cache_format_version"], CACHE_FORMAT_VERSION)
        self.assertEqual(metadata["report_format_version"], REPORT_FORMAT_VERSION)
        self.assertEqual(metadata["revision_scope"], "skill")
        for field in ("code_revision", "catalogue_revision"):
            self.assertRegex(metadata[field], r"^sha256:[0-9a-f]{64}$")
        self.assertNotIn("git", json.dumps(metadata))

    def test_metadata_return_value_does_not_mutate_cached_identity(self):
        metadata = release_metadata()
        metadata["release_version"] = "changed"
        self.assertEqual(release_metadata()["release_version"], VERSION)

    def test_revision_stable_across_copies_and_timestamps(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = self.package(root / "original", skill=True)
            shutil.copytree(root / "original", root / "copied")
            copied = root / "copied" / "scripts" / "universal_skill_finder"
            os.utime(copied / "__init__.py", (1, 1))
            self.assertEqual(release_metadata(original), release_metadata(copied))

    def test_engine_change_changes_code_revision_only(self):
        with TemporaryDirectory() as temp:
            package = self.package(Path(temp))
            before = release_metadata(package)
            (package / "adapter.py").write_text("CONTRACT = 1\n", encoding="utf-8")
            after = release_metadata(package)
            self.assertNotEqual(before["code_revision"], after["code_revision"])
            self.assertEqual(before["catalogue_revision"], after["catalogue_revision"])

    def test_instructions_and_launchers_are_revisioned(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            package = self.package(root, skill=True)
            before = release_metadata(package)["code_revision"]
            (root / "SKILL.md").write_text("# Finder with new behavior\n", encoding="utf-8")
            changed_skill = release_metadata(package)["code_revision"]
            self.assertNotEqual(before, changed_skill)
            (root / "scripts" / "run.sh").write_text("exit 4\n", encoding="utf-8")
            self.assertNotEqual(changed_skill, release_metadata(package)["code_revision"])

    def test_catalogue_revision_ignores_formatting_but_tracks_data(self):
        with TemporaryDirectory() as temp:
            package = self.package(Path(temp))
            catalogue = package / "data" / "sources.default.json"
            before = release_metadata(package)
            catalogue.write_text('{\n "sources": [], "schema_version": 1\n}\n', encoding="utf-8")
            self.assertEqual(before, release_metadata(package))
            catalogue.write_text('{"schema_version":1,"sources":[{"id":"new"}]}', encoding="utf-8")
            after = release_metadata(package)
            self.assertNotEqual(before["catalogue_revision"], after["catalogue_revision"])
            self.assertEqual(before["code_revision"], after["code_revision"])

    def test_minimal_wheel_without_skill_instructions_is_supported(self):
        with TemporaryDirectory() as temp:
            package = self.package(Path(temp))
            self.assertEqual(release_metadata(package)["revision_scope"], "engine")

    def test_revision_rejects_symlink_and_bounded_files(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            package = self.package(root)
            with patch("universal_skill_finder.versioning._MAX_REVISION_FILE_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    release_metadata(package)
            try:
                (package / "linked.py").symlink_to(package / "__init__.py")
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                release_metadata(package)

    def test_config_revision_tracks_effective_state_not_dictionary_order(self):
        source = {"id": "one", "effective_enabled": True, "auth_env": "EXAMPLE_TOKEN", "origin": "bundled"}
        before = effective_config_revision({"timeout_seconds": 10}, [], [source])
        reordered = {key: source[key] for key in reversed(source)}
        self.assertEqual(before, effective_config_revision({"timeout_seconds": 10}, [], [reordered]))
        self.assertEqual(before, effective_config_revision({"timeout_seconds": 10}, [], [{**source, "origin": "user"}]))
        for changed in ({"effective_enabled": False}, {"auth_env": "OTHER_TOKEN"}):
            self.assertNotEqual(before, effective_config_revision({"timeout_seconds": 10}, [], [{**source, **changed}]))
        self.assertNotEqual(before, effective_config_revision({"timeout_seconds": 20}, [], [source]))

    def test_config_revision_never_reads_environment_or_hashes_secret_values(self):
        source = {
            "id": "one", "auth_env": "EXAMPLE_TOKEN", "token": "synthetic-first",
            "headers": {"Authorization": {"env": "EXAMPLE_TOKEN", "prefix": "Bearer "}},
            "endpoint": "https://registry.example/search?token=synthetic-first&format=json",
        }
        first = effective_config_revision({}, [], [source])
        with patch.dict(os.environ, {"EXAMPLE_TOKEN": "synthetic-rotated"}):
            self.assertEqual(first, effective_config_revision({}, [], [source]))
        changed = {**source, "token": "synthetic-other", "endpoint": "https://registry.example/search?token=synthetic-other&format=json"}
        self.assertEqual(first, effective_config_revision({}, [], [changed]))
        changed["endpoint"] = "https://registry.example/search?token=synthetic-other&format=xml"
        self.assertNotEqual(first, effective_config_revision({}, [], [changed]))

    def test_search_report_uses_the_frozen_schema_two_contract(self):
        report = SearchReport("pdf", [], [], "2026-01-01", "config.json").to_dict()
        self.assertEqual(report["schema_version"], SEARCH_REPORT_SCHEMA_VERSION)
        self.assertEqual(report["query"], "pdf")
        self.assertEqual(report["results"], [])
        self.assertEqual(report["report_format_version"], REPORT_FORMAT_VERSION)
        self.assertFalse(report["page_incomplete"])
        self.assertEqual(report["validation_deferred_count"], 0)
        self.assertEqual(report["provenance"]["release_version"], VERSION)
        json.dumps(report, allow_nan=False)

    def test_finder_reports_effective_configuration_revision(self):
        with TemporaryDirectory() as temp:
            configuration = finder_config(Path(temp), [])
            finder = UniversalSkillFinder(configuration, cache=Cache(Path(temp) / "cache"))
            report = finder.search("pdf", dry_run=True).to_dict()
            self.assertEqual(report["provenance"]["effective_configuration_revision"], effective_config_revision(
                configuration.settings, configuration.packs, configuration.sources,
            ))
            self.assertNotIn("overlay.json", json.dumps(report["provenance"]))


class CacheVersionTests(unittest.TestCase):
    def test_envelope_is_transparent_and_checks_both_versions(self):
        with TemporaryDirectory() as temp:
            cache = Cache(Path(temp))
            payload = {"source_id": "one", "query": "pdf", "limit": 10, "candidates": []}
            cache.write("queries", "entry", payload)
            self.assertEqual(cache.read("queries", "entry")[0], payload)
            path = Path(temp) / "queries" / "entry.json"
            envelope = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(envelope["cache_format_version"], CACHE_FORMAT_VERSION)
            self.assertEqual(envelope["adapter_contract_version"], ADAPTER_CONTRACT_VERSION)
            for field in ("cache_format_version", "adapter_contract_version"):
                for invalid in (0, 999, True, "1", None):
                    with self.subTest(field=field, invalid=invalid):
                        path.write_text(json.dumps({**envelope, field: invalid}), encoding="utf-8")
                        self.assertIsNone(cache.read("queries", "entry"))
                        self.assertEqual(cache.metadata("queries"), [])

    def test_legacy_cache_envelopes_and_lists_are_misses(self):
        with TemporaryDirectory() as temp:
            cache = Cache(Path(temp))
            cache.write("queries", "entry", {})
            path = Path(temp) / "queries" / "entry.json"
            for old in ([{"name": "pdf"}], {"schema_version": 1, "candidates": []}, {"payload": []}):
                with self.subTest(old=old):
                    path.write_text(json.dumps(old), encoding="utf-8")
                    self.assertIsNone(cache.read("queries", "entry"))

    def test_cache_key_versions_and_boundaries_are_explicit(self):
        current = Cache.key("source", "pdf", 10)
        for field in ("CACHE_FORMAT_VERSION", "ADAPTER_CONTRACT_VERSION"):
            with patch("universal_skill_finder.cache." + field, 999):
                self.assertNotEqual(current, Cache.key("source", "pdf", 10))
        self.assertNotEqual(Cache.key("one\ntwo", "three"), Cache.key("one", "two\nthree"))

    def test_incompatible_cache_fails_offline_without_calling_connector(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = registry_source("one", "skills-sh")
            cache = Cache(root / "cache")
            adapter = StaticAdapter([candidate("one", "acme/skills", "pdf", rank=1)])
            finder = UniversalSkillFinder(finder_config(root, [source]), cache=cache, http=FakeHttp())
            finder.adapter_map = {"skills-sh": adapter}
            self.assertEqual(finder.search("pdf").coverage[0].status, "ok")
            self.assertEqual(adapter.calls, 1)
            path = cache.root / "queries" / (finder._cache_key(source, "pdf", 10) + ".json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["adapter_contract_version"] = 999
            path.write_text(json.dumps(payload), encoding="utf-8")
            offline = finder.search("pdf", offline=True)
            self.assertEqual(offline.coverage[0].status, "offline_miss")
            self.assertEqual(adapter.calls, 1)
            self.assertEqual(offline.results, [])
            self.assertEqual(finder.search("pdf").coverage[0].status, "ok")
            self.assertEqual(adapter.calls, 2)


if __name__ == "__main__":
    unittest.main()
