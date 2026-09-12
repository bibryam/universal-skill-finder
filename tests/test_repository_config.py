from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.config import (
    ConfigurationError,
    _overlay_lock,
    add_github_repository,
    add_local_directory,
    empty_overlay,
    import_source_pack,
    initialize_repository_config,
    initialize_source_config,
    load_config,
    remove_custom_pack,
    remove_custom_source,
    save_overlay,
    set_pack_enabled,
    set_source_enabled,
)
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Coverage


class RepositoryConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "repositories.json"

    def write(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        return load_config(str(self.path))

    def read(self):
        return load_config(str(self.path))

    @staticmethod
    def flags(config):
        return {source["id"]: source["enabled"] for source in config.sources}

    @staticmethod
    def effective(config):
        return {source["id"]: source["effective_enabled"] for source in config.sources}

    def saved_flags(self):
        return {entry["id"]: entry["enabled"] for entry in json.loads(self.path.read_text())["sources"]}

    def test_empty_preferences_are_minimal_and_read_only(self):
        self.assertEqual(empty_overlay(), {"sources": []})
        config = self.read()
        self.assertEqual(config.overlay, {"sources": []})
        self.assertFalse(self.path.exists())
        self.assertEqual(len(config.sources), 13)
        self.assertEqual(sum(self.effective(config).values()), 9)

    def test_list_preferences_override_only_listed_ids(self):
        defaults = self.flags(self.read())
        config = self.write({"sources": [{"id": "tessl", "enabled": False}]})
        expected = {**defaults, "tessl": False}
        self.assertEqual(self.flags(config), expected)
        self.assertEqual(config.overlay, {"sources": [{"id": "tessl", "enabled": False}]})

    def test_schema_version_is_optional_but_must_be_supported_integer(self):
        self.write({"schema_version": 1, "sources": []})
        for version in (True, False, "1", 0, 2, 1.0, None):
            with self.subTest(version=version), self.assertRaisesRegex(ConfigurationError, "schema_version"):
                self.write({"schema_version": version, "sources": []})

    def test_sources_and_legacy_repositories_require_exact_id_and_boolean_pairs(self):
        invalid = [
            None, {}, "tessl", True,
            [None], [False], ["tessl"], [[]],
            [{}], [{"id": "tessl"}], [{"enabled": True}],
            [{"id": "tessl", "enabled": True, "url": "https://example.com"}],
            [{"id": "tessl", "enabled": True, "headers": {}}],
            [{"id": "tessl", "enabled": True, "auth_env": "TEST_TOKEN"}],
            [{"id": "tessl", "enabled": True, "endpoint": "https://example.com"}],
            [{"id": ["tessl"], "enabled": True}],
            [{"id": "../tessl", "enabled": True}],
            [{"id": "", "enabled": True}],
        ]
        invalid.extend([[{"id": "tessl", "enabled": value}] for value in (0, 1, "true", "false", None, [], {})])
        for field in ("sources", "repositories"):
            for payload in invalid:
                with self.subTest(field=field, payload=payload), self.assertRaises(ConfigurationError):
                    self.write({field: payload})

    def test_duplicate_ids_are_rejected_even_with_identical_flags(self):
        for second in (True, False):
            with self.subTest(second=second), self.assertRaisesRegex(ConfigurationError, "duplicate source id"):
                self.write({"sources": [{"id": "tessl", "enabled": True}, {"id": "tessl", "enabled": second}]})

    def test_unknown_ids_fail_before_any_network_or_rewrite(self):
        payload = {"sources": [{"id": "missing-repository", "enabled": False}]}
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        before = self.path.read_bytes()
        with patch("universal_skill_finder.http.HttpClient.request", side_effect=AssertionError("network is forbidden")) as request:
            with self.assertRaisesRegex(ConfigurationError, "unknown source id"):
                self.read()
        request.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_preference_forms_never_mix_even_if_empty(self):
        forms = ({"sources": []}, {"repositories": []}, {"source_overrides": {}},
                 {"source_overrides": {"tessl": {"enabled": False}}})
        for first in forms:
            for second in forms:
                if set(first) == set(second):
                    continue
                with self.subTest(first=first, second=second), self.assertRaisesRegex(ConfigurationError, "only one preference form"):
                    self.write({**first, **second})

    def test_legacy_repository_list_is_read_unchanged_then_normalized_on_write(self):
        payload = {
            "schema_version": 1,
            "repositories": [{"id": "tessl", "enabled": False}, {"id": "skills-sh", "enabled": True}],
            "pack_overrides": {"registries": {"enabled": False}},
            "custom_sources": [{"id": "local-skills", "kind": "repository", "adapter": "local-directory", "path": "/local/skills"}],
        }
        config = self.write(payload)
        before = self.path.read_bytes()
        expected = self.effective(config)
        self.assertEqual(config.overlay, payload)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(initialize_source_config(config))
        self.assertNotIn("repositories", config.overlay)
        self.assertNotIn("source_overrides", config.overlay)
        self.assertEqual([row["id"] for row in config.overlay["sources"][:2]], ["tessl", "skills-sh"])
        self.assertEqual(config.overlay["pack_overrides"], payload["pack_overrides"])
        self.assertEqual(config.overlay["custom_sources"], payload["custom_sources"])
        self.assertEqual(self.effective(self.read()), expected)
        self.assertEqual(self.read().overlay_path, self.path)
        self.assertFalse((self.root / "sources.json").exists())
        self.assertFalse(initialize_repository_config(self.read()))

    def test_toggle_legacy_repository_list_preserves_unrelated_flags(self):
        config = self.write({"repositories": [{"id": "skills-sh", "enabled": False}]})
        set_source_enabled(config, "tessl", False)
        self.assertFalse(self.saved_flags()["skills-sh"])
        self.assertFalse(self.saved_flags()["tessl"])
        self.assertNotIn("repositories", config.overlay)

    def test_initialize_creates_complete_flags_without_connector_fields(self):
        config = self.read()
        expected = self.flags(config)
        self.assertTrue(initialize_repository_config(config))
        self.assertEqual(self.saved_flags(), expected)
        payload = json.loads(self.path.read_text())
        self.assertEqual(set(payload), {"sources"})
        self.assertTrue(all(set(entry) == {"id", "enabled"} for entry in payload["sources"]))
        self.assertEqual(config.overlay_fingerprint, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.effective(self.read()), self.effective(config))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_initialize_fresh_nested_directory_is_private(self):
        self.path = self.root / "nested" / "repositories.json"
        self.assertTrue(initialize_repository_config(self.read()))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_simple_file_has_one_repository_per_line_with_id_before_enabled(self):
        config = self.read()
        initialize_repository_config(config)
        lines = self.path.read_text().splitlines()
        self.assertEqual(lines[:2], ["{", '  "sources": ['])
        self.assertEqual(lines[-2:], ["  ]", "}"])
        self.assertEqual(len(lines), len(config.sources) + 4)
        for index, source in enumerate(config.sources):
            row = json.dumps({"id": source["id"], "enabled": source["enabled"]})
            self.assertEqual(lines[index + 2], "    " + row + ("," if index < len(config.sources) - 1 else ""))
        set_source_enabled(config, "tessl", False)
        self.assertIn('    {"id": "tessl", "enabled": false},', self.path.read_text())
        self.assertEqual(len(self.path.read_text().splitlines()), len(config.sources) + 4)

    def test_simple_file_with_schema_keeps_compact_rows(self):
        config = self.write({"schema_version": 1, "sources": []})
        initialize_repository_config(config)
        lines = self.path.read_text().splitlines()
        self.assertEqual(lines[:3], ["{", '  "schema_version": 1,', '  "sources": ['])
        self.assertEqual(len(lines), len(config.sources) + 5)
        self.assertTrue(all(line.startswith('    {"id": ') for line in lines[3:-2]))
        self.assertEqual(json.loads(self.path.read_text()), config.overlay)

    def test_empty_simple_overlay_serializes_as_valid_compact_json(self):
        for payload in ({"sources": []}, {"schema_version": 1, "sources": []}):
            with self.subTest(payload=payload):
                save_overlay(self.path, payload)
                self.assertEqual(json.loads(self.path.read_text()), payload)
                self.assertIn('  "sources": []\n', self.path.read_text())

    def test_advanced_overlay_serialization_is_unchanged(self):
        config = self.write({"sources": [], "pack_overrides": {"registries": {"enabled": False}}})
        initialize_repository_config(config)
        expected = json.dumps(config.overlay, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        self.assertEqual(self.path.read_text(), expected)

    def test_initialize_complete_file_is_noop_preserving_bytes_and_mtime(self):
        config = self.read()
        initialize_repository_config(config)
        payload = json.loads(self.path.read_text())
        payload["sources"].reverse()
        self.path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        config = self.read()
        content, modified = self.path.read_bytes(), self.path.stat().st_mtime_ns
        with patch("universal_skill_finder.config.os.replace", side_effect=AssertionError("must not rewrite")):
            self.assertFalse(initialize_repository_config(config))
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual(self.path.stat().st_mtime_ns, modified)

    def test_initialize_expands_partial_list_preserving_order_flags_and_pack_guard(self):
        config = self.write({
            "sources": [{"id": "tessl", "enabled": False}, {"id": "skills-sh", "enabled": True}],
            "pack_overrides": {"registries": {"enabled": False}},
        })
        before = self.effective(config)
        self.assertTrue(initialize_repository_config(config))
        reloaded = self.read()
        self.assertEqual(self.effective(reloaded), before)
        self.assertTrue(self.saved_flags()["skills-sh"])
        self.assertFalse(reloaded.source("skills-sh")["effective_enabled"])
        self.assertEqual([row["id"] for row in reloaded.overlay["sources"][:2]], ["tessl", "skills-sh"])
        self.assertFalse(initialize_repository_config(reloaded))

    def test_legacy_overlay_is_migrated_only_on_explicit_write(self):
        payload = {"schema_version": 1, "source_overrides": {"tessl": {"enabled": False}}, "custom_packs": []}
        config = self.write(payload)
        before = self.path.read_bytes()
        self.assertEqual(config.overlay, payload)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(initialize_repository_config(config))
        self.assertNotIn("source_overrides", config.overlay)
        self.assertEqual(config.overlay["schema_version"], 1)
        self.assertEqual(config.overlay["custom_packs"], [])
        self.assertFalse(self.saved_flags()["tessl"])

    def test_toggle_writes_complete_list_and_retains_sequential_choices(self):
        config = self.read()
        set_source_enabled(config, "tessl", False)
        set_source_enabled(config, "skills-sh", False)
        self.assertFalse(self.saved_flags()["tessl"])
        self.assertFalse(self.saved_flags()["skills-sh"])
        self.assertEqual(len(self.saved_flags()), len(config.sources))
        self.assertFalse(initialize_repository_config(config))
        self.assertNotIn("source_overrides", config.overlay)

    def test_toggle_migrates_legacy_preserving_unrelated_choices(self):
        config = self.write({"source_overrides": {"skills-sh": {"enabled": False}}})
        set_source_enabled(config, "tessl", False)
        self.assertFalse(self.saved_flags()["skills-sh"])
        self.assertFalse(self.saved_flags()["tessl"])

    def test_invalid_toggle_value_never_writes(self):
        config = self.read()
        for value in (0, 1, "false", None):
            for operation, key in ((set_source_enabled, "tessl"), (set_pack_enabled, "registries")):
                with self.subTest(value=value, operation=operation.__name__), self.assertRaisesRegex(ConfigurationError, "boolean"):
                    operation(config, key, value)
        self.assertFalse(self.path.exists())

    def test_pack_toggles_preserve_direct_flags_and_migrate_legacy(self):
        config = self.write({"source_overrides": {"tessl": {"enabled": False}}})
        set_pack_enabled(config, "registries", False)
        self.assertTrue(self.saved_flags()["skills-sh"])
        self.assertFalse(self.saved_flags()["tessl"])
        self.assertFalse(self.read().source("skills-sh")["effective_enabled"])
        set_pack_enabled(config, "registries", True)
        self.assertTrue(self.read().source("skills-sh")["effective_enabled"])
        self.assertFalse(self.read().source("tessl")["effective_enabled"])

    def test_list_flag_edit_changes_actual_federation_selection(self):
        config = self.write({"sources": [{"id": "tessl", "enabled": False}]})
        for enabled in (False, True):
            if enabled:
                self.path.write_text(self.path.read_text().replace('"enabled": false', '"enabled": true'), encoding="utf-8")
                config = self.read()
            finder = UniversalSkillFinder(config, cache=Cache(self.root / "cache"))
            with patch.object(finder, "_search_source", side_effect=lambda source, *args, **kwargs: ([], Coverage(source_id=source["id"], status="searched"))) as search:
                with patch.object(finder.http, "request", side_effect=AssertionError("network is forbidden")):
                    report = finder.search("pdf forms")
            searched_ids = {call.args[0]["id"] for call in search.call_args_list}
            self.assertEqual("tessl" in searched_ids, enabled)
            self.assertEqual(len(searched_ids), 9 if enabled else 8)
            coverage = next(row for row in report.coverage if row.source_id == "tessl")
            self.assertEqual(coverage.status, "searched" if enabled else "disabled")

    def test_initialize_stale_missing_snapshot_cannot_overwrite_another_writer(self):
        first, second = self.read(), self.read()
        set_source_enabled(first, "tessl", False)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ConfigurationError, "changed since"):
            initialize_repository_config(second)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(second.overlay, empty_overlay())
        self.assertEqual(list(self.root.glob(".sources-*")), [])

    def test_initialize_stale_complete_snapshot_cannot_report_noop(self):
        config = self.read()
        initialize_repository_config(config)
        updated = self.read()
        set_source_enabled(updated, "tessl", False)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ConfigurationError, "changed since"):
            initialize_repository_config(config)
        self.assertEqual(self.path.read_bytes(), before)

    def test_initialize_racing_write_is_detected_before_replace(self):
        config = self.read()
        external = b'{"sources":[{"id":"tessl","enabled":false}]}'
        with patch("universal_skill_finder.config.os.fsync", side_effect=lambda _: self.path.write_bytes(external)):
            with self.assertRaisesRegex(ConfigurationError, "changed since"):
                initialize_repository_config(config)
        self.assertEqual(self.path.read_bytes(), external)
        self.assertEqual(config.overlay, empty_overlay())
        self.assertEqual(list(self.root.glob(".sources-*")), [])

    def test_initialize_respects_active_lock(self):
        config = self.read()
        lock = self.path.with_name(".repositories.json.lock")
        with _overlay_lock(self.path):
            original = lock.read_bytes()
            with self.assertRaisesRegex(ConfigurationError, "busy"):
                initialize_repository_config(config)
            self.assertEqual(lock.read_bytes(), original)
            self.assertFalse(self.path.exists())
        self.assertFalse(lock.exists())

    def test_initialize_replace_failure_preserves_disk_memory_and_cleans_temp(self):
        config = self.write({"sources": []})
        original = deepcopy(config.overlay)
        before = self.path.read_bytes()
        with patch("universal_skill_finder.config.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(ConfigurationError):
                initialize_repository_config(config)
        self.assertEqual(config.overlay, original)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".sources-*")), [])
        self.assertFalse(self.path.with_name(".repositories.json.lock").exists())

    def test_initialize_rejects_symlink_replacement_after_loading(self):
        config = self.read()
        target = self.root / "elsewhere.json"
        target.write_text('{"sources": []}', encoding="utf-8")
        try:
            self.path.symlink_to(target)
        except OSError:
            if os.name == "nt":
                self.skipTest("Windows symlink privilege unavailable")
            raise
        before = target.read_bytes()
        with self.assertRaisesRegex(ConfigurationError, "symlink"):
            initialize_repository_config(config)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(self.path.is_symlink())

    def test_initialize_preserves_selected_legacy_filename(self):
        self.path = self.root / "sources.json"
        config = self.write({"source_overrides": {"tessl": {"enabled": False}}})
        initialize_repository_config(config)
        self.assertEqual(config.overlay_path, self.path)
        self.assertFalse((self.root / "repositories.json").exists())
        self.assertFalse(self.saved_flags()["tessl"])

    def test_custom_repository_add_toggle_remove_keeps_list_valid(self):
        config = self.read()
        added = add_github_repository(config, "example/skills", source_id="example-skills", ref="main", include=None, exclude=None)
        self.assertTrue(self.saved_flags()[added["id"]])
        config = self.read()
        set_source_enabled(config, added["id"], False)
        self.assertFalse(self.read().source(added["id"])["effective_enabled"])
        remove_custom_source(config, added["id"])
        self.assertNotIn(added["id"], self.saved_flags())
        self.assertIsNone(self.read().source(added["id"]))
        self.assertFalse(initialize_repository_config(self.read()))

    def test_local_repository_addition_and_existing_preferences_are_preserved(self):
        config = self.write({"sources": [{"id": "tessl", "enabled": False}]})
        add_local_directory(config, str(self.root / "skills"), source_id="local-skills", include=None, exclude=None)
        self.assertFalse(self.saved_flags()["tessl"])
        self.assertTrue(self.saved_flags()["local-skills"])
        reloaded = self.read()
        self.assertEqual(reloaded.source("local-skills")["adapter"], "local-directory")

    def test_imported_disabled_pack_retains_guard_through_init_toggle_and_removal(self):
        pack_path = self.root / "pack.json"
        pack_path.write_text(json.dumps({
            "schema_version": 1,
            "pack": {"id": "team-pack", "enabled": False},
            "sources": [{"id": "team-skills", "adapter": "github-repo", "repository": "example/skills", "ref": "main"}],
        }), encoding="utf-8")
        config = self.read()
        import_source_pack(config, str(pack_path))
        config = self.read()
        self.assertTrue(self.saved_flags()["team-skills"])
        self.assertFalse(config.source("team-skills")["effective_enabled"])
        initialize_repository_config(config)
        set_source_enabled(config, "team-skills", True)
        self.assertFalse(self.read().source("team-skills")["effective_enabled"])
        set_pack_enabled(config, "team-pack", True)
        self.assertTrue(self.read().source("team-skills")["effective_enabled"])
        self.assertEqual(remove_custom_pack(config, "team-pack"), 1)
        reloaded = self.read()
        self.assertIsNone(reloaded.source("team-skills"))
        self.assertIsNone(reloaded.pack("team-pack"))
        self.assertNotIn("team-skills", self.saved_flags())
        self.assertNotIn("team-pack", reloaded.overlay["pack_overrides"])

    def test_custom_repository_can_be_selected_in_same_input_list(self):
        config = self.write({
            "sources": [{"id": "custom-skills", "enabled": False}],
            "custom_sources": [{"id": "custom-skills", "kind": "repository", "adapter": "github-repo", "repository": "example/skills", "ref": "main"}],
        })
        self.assertFalse(config.source("custom-skills")["effective_enabled"])
        before = self.effective(config)
        initialize_repository_config(config)
        self.assertEqual(self.effective(self.read()), before)


if __name__ == "__main__":
    unittest.main()
