from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cli import main
from universal_skill_finder.config import EffectiveConfig, load_config, set_pack_enabled, set_source_enabled
from universal_skill_finder.source_presentation import (
    public_source_url, render_sources_markdown, render_sources_table, source_rows, source_type,
)


def fixture_config(sources):
    return EffectiveConfig(settings={}, packs=[], sources=sources, overlay_path=Path("/private/overlay.json"), overlay={})


class SourceControlTests(unittest.TestCase):
    def test_source_types_describe_discovery_while_preserving_adapter_kinds(self):
        cases = [({"kind": "registry", "adapter": "tessl"}, "Registry"),
                 ({"kind": "repository", "adapter": "github-repo"}, "Repository"),
                 ({"kind": "repository", "adapter": "local-directory"}, "Local directory"),
                 ({"kind": "registry", "adapter": "github-code-search"}, "Search index")]
        for source, expected in cases:
            with self.subTest(adapter=source["adapter"]):
                original = dict(source)
                self.assertEqual(source_type(source), expected)
                self.assertEqual(source, original)
                row = source_rows(fixture_config([{"id": "example", **source}]), environ={})[0]
                self.assertEqual(row["type"], expected)

    def test_public_links_are_registry_origins_or_exact_repositories(self):
        self.assertEqual(public_source_url({"base_url": "https://catalog.example"}), "https://catalog.example")
        self.assertEqual(public_source_url({"repository": "owner/skills"}), "https://github.com/owner/skills")
        self.assertEqual(public_source_url({"base_url": "https://catalog.example/private/key-in-path?key=query-value#private-fragment"}),
                         "https://catalog.example")
        self.assertEqual(public_source_url({"endpoint": "https://catalog.example:8443/private/search?secret=query-value"}),
                         "https://catalog.example:8443")
        self.assertIsNone(public_source_url({"adapter": "local-directory", "path": "/private/local-skills"}))

    def test_rejected_urls_never_become_clickable_or_leak_credentials(self):
        for url in ("javascript:alert(1)", "file:///private/secret", "http://remote.example",
                    "https://user:password@catalog.example/path", "https://catalog.example\\@evil.example",  # pragma: allowlist secret - synthetic URL rejection fixture
                    "https://catalog.example/\n[click](https://evil.example)"):
            with self.subTest(url=url):
                self.assertIsNone(public_source_url({"base_url": url}))
        self.assertIsNone(public_source_url({"repository": "owner/../private"}))
        self.assertEqual(public_source_url({"endpoint": "http://127.0.0.1:9000/search?secret=value", "allow_insecure_local": True}),
                         "http://127.0.0.1:9000")
        self.assertIsNone(public_source_url({"endpoint": "http://remote.example/search", "allow_insecure_local": True}))

    def test_bundled_list_covers_every_source_with_effective_state_and_links(self):
        with TemporaryDirectory() as temp:
            config = load_config(str(Path(temp) / "missing.json"))
            rows = source_rows(config, environ={})
            markdown = render_sources_markdown(config, environ={})
            self.assertEqual(len(rows), 13)
        self.assertEqual(sum(row["enabled"] for row in rows), 9)
        self.assertEqual([row["id"] for row in rows], [source["id"] for source in config.sources])
        for row in rows:
            with self.subTest(source=row["id"]):
                self.assertIn(f"[{row['id']}]({row['public_url']})", markdown)
                self.assertIn(("Disable " if row["enabled"] else "Enable ") + row["id"], markdown)
        self.assertIn("| Registry | No configured key required | Disable skills-sh | ✅ Enabled |", markdown)
        self.assertIn("| Registry | No configured key required | Enable polyskill | ❌ Disabled |", markdown)
        self.assertIn("not live availability", markdown)
        self.assertNotIn(str(config.overlay_path), markdown)

    def test_missing_required_credentials_do_not_change_enabled_state(self):
        config = fixture_config([{
            "id": "custom", "kind": "registry", "base_url": "https://catalog.example",
            "auth_env": "CUSTOM_KEY", "enabled": True,
        }])
        rows = source_rows(config, environ={})
        self.assertTrue(rows[0]["enabled"])
        self.assertIn("| Registry | CUSTOM\\_KEY: required, missing | Disable custom | ✅ Enabled |", render_sources_table(rows))
        defaults = render_sources_table(rows, show_credential_presence=False)
        self.assertIn("| Registry | CUSTOM\\_KEY: required | Disable custom | ✅ Enabled |", defaults)
        self.assertEqual(source_rows(config, environ={"CUSTOM_KEY": "synthetic-value"})[0]["enabled"], True)

    def test_credential_status_reports_presence_only_and_distinguishes_optional_keys(self):
        config = fixture_config([{
            "id": "custom", "kind": "registry", "base_url": "https://catalog.example",
            "auth_env": "PUBLIC_API_KEY", "auth_optional": True,
            "headers": {
                "Authorization": {"env": "REQUIRED_API_KEY", "prefix": "Bearer "},
                "X-Optional-Key": {"env": "OPTIONAL_API_KEY", "optional": True},
                "X-Static": "private-header-value",
            },
        }])
        environment = {"PUBLIC_API_KEY": "private-credential-value"}  # pragma: allowlist secret - synthetic value verifies credentials are never displayed
        rows = source_rows(config, environ=environment)
        credentials = {item["environment_variable"]: item for item in rows[0]["credentials"]}
        self.assertEqual(credentials["PUBLIC_API_KEY"], {
            "environment_variable": "PUBLIC_API_KEY", "required": False, "present": True,
        })
        self.assertTrue(credentials["REQUIRED_API_KEY"]["required"])
        self.assertFalse(credentials["REQUIRED_API_KEY"]["present"])
        self.assertFalse(credentials["OPTIONAL_API_KEY"]["required"])
        output = render_sources_markdown(config, environ=environment) + json.dumps(rows)
        self.assertIn("present, not verified", output)
        self.assertIn("required, missing", output)
        self.assertIn("optional, missing", output)
        self.assertNotIn(environment["PUBLIC_API_KEY"], output)
        self.assertNotIn("private-header-value", output)
        self.assertNotIn("Bearer", output)

    def test_duplicate_credential_reference_preserves_the_required_dependency(self):
        config = fixture_config([{
            "id": "custom", "kind": "registry", "auth_env": "SHARED_KEY", "auth_optional": True,
            "headers": {
                "X-First": {"env": "SHARED_KEY", "optional": False},
                "X-Second": {"env": "SHARED_KEY", "optional": True},
            },
        }])
        self.assertEqual(source_rows(config, environ={})[0]["credentials"], [
            {"environment_variable": "SHARED_KEY", "required": True, "present": False},
        ])

    def test_markdown_escapes_untrusted_text_and_omits_local_paths(self):
        config = fixture_config([{
            "id": "custom|[bad](https://evil.example)\n<img src=x>", "kind": "registry",
            "base_url": "https://catalog.example/private-path?key=private-query#private-fragment",
            "pack": "pack|`bad`", "pack_enabled": False,
        }, {
            "id": "local", "kind": "repository", "adapter": "local-directory", "path": "/private/local-skills",
        }])
        markdown = render_sources_markdown(config, environ={})
        self.assertIn("&#124;", markdown)
        self.assertIn("&lt;img src=x&gt;", markdown)
        self.assertIn("local | Local directory", markdown)
        for value in ("private-path", "private-query", "private-fragment", "/private/local-skills", "<img", "[bad](https://evil.example)"):
            self.assertNotIn(value, markdown)
        self.assertEqual(len([line for line in markdown.splitlines() if line.startswith("| ")]), 3)

    def test_listing_is_read_only_and_makes_no_network_or_cache_calls(self):
        text_outputs = []
        for option in ("--markdown", "--json", None):
            with self.subTest(option=option), TemporaryDirectory() as temp:
                root = Path(temp)
                overlay = root / "missing.json"
                output = io.StringIO()
                with patch("universal_skill_finder.cli.UniversalSkillFinder", side_effect=AssertionError("search forbidden")), \
                     patch("universal_skill_finder.cli.Cache", side_effect=AssertionError("cache forbidden")), \
                     patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                     patch("universal_skill_finder.config.save_overlay", side_effect=AssertionError("write forbidden")), \
                     patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
                    argv = ["--config", str(overlay), "sources", "list"] + ([option] if option else [])
                    self.assertEqual(main(argv), 0)
                self.assertFalse(overlay.exists())
                self.assertEqual(list(root.iterdir()), [])
                self.assertIn("skills-sh", output.getvalue())
                if option != "--json":
                    text_outputs.append(output.getvalue())
        self.assertEqual(*text_outputs)

    def test_json_list_is_an_allowlisted_view_without_config_paths_or_secrets(self):
        config = fixture_config([{
            "id": "custom", "kind": "registry", "endpoint": "https://catalog.example/private?key=private-query",
            "auth_env": "CUSTOM_KEY", "headers": {"X-Private": "private-header"},
            "provenance": "private-provenance", "path": "/private/skills",
        }])
        output = io.StringIO()
        with patch("universal_skill_finder.cli.load_config", return_value=config), \
             patch.dict(os.environ, {"CUSTOM_KEY": "private-value"}, clear=True), redirect_stdout(output):
            self.assertEqual(main(["sources", "list", "--json"]), 0)
        document = json.loads(output.getvalue())
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["sources"][0]["public_url"], "https://catalog.example")
        self.assertTrue(document["sources"][0]["credentials"][0]["present"])
        for value in ("/private", "private-query", "private-header", "private-provenance", "private-value"):
            self.assertNotIn(value, output.getvalue())

    def test_enable_disable_persist_source_preference_without_changing_other_sources(self):
        with TemporaryDirectory() as temp:
            overlay = Path(temp) / "sources.json"
            before = load_config(str(overlay))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(overlay), "sources", "disable", "skillsmp"]), 0)
            disabled = load_config(str(overlay))
            self.assertFalse(disabled.source("skillsmp")["effective_enabled"])
            self.assertEqual([source for source in before.sources if source["id"] != "skillsmp"],
                             [source for source in disabled.sources if source["id"] != "skillsmp"])
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(overlay), "sources", "enable", "skillsmp"]), 0)
            enabled = load_config(str(overlay))
            self.assertTrue(enabled.source("skillsmp")["effective_enabled"])
            self.assertEqual(source_rows(enabled, environ={})[1]["suggested_request"], "Disable skillsmp")

    def test_disabled_pack_blocks_source_and_enable_requires_an_explicit_pack_change(self):
        with TemporaryDirectory() as temp:
            overlay = Path(temp) / "sources.json"
            config = load_config(str(overlay))
            set_source_enabled(config, "skillsmp", False)
            config = load_config(str(overlay))
            set_pack_enabled(config, "registries", False)
            config = load_config(str(overlay))
            row = next(row for row in source_rows(config, environ={}) if row["id"] == "skillsmp")
            self.assertFalse(row["enabled"])
            self.assertFalse(row["direct_enabled"])
            self.assertFalse(row["pack_enabled"])
            self.assertIn("Enable pack registries", row["suggested_request"])
            self.assertIn("then Enable skillsmp", row["suggested_request"])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(overlay), "sources", "enable", "skillsmp"]), 0)
            still_blocked = load_config(str(overlay))
            self.assertTrue(still_blocked.source("skillsmp")["enabled"])
            self.assertFalse(still_blocked.source("skillsmp")["effective_enabled"])
            self.assertFalse(still_blocked.pack("registries")["enabled"])
            self.assertIn("also affects its other enabled sources", output.getvalue())
            rendered = render_sources_markdown(still_blocked, environ={})
            self.assertIn("disabled (blocks this source)", rendered)
            self.assertIn("Enable pack registries", rendered)


if __name__ == "__main__":
    unittest.main()
