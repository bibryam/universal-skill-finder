from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cli import _print_report, main
from universal_skill_finder.config import load_config
from universal_skill_finder.models import Coverage
from universal_skill_finder.presentation import render_markdown
from universal_skill_finder.source_presentation import render_sources_markdown
from test_presentation import report, result


class SourceCommandTests(unittest.TestCase):
    def config_command(self, path: Path, *arguments: str) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(errors))
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            for target in ("socket.socket", "universal_skill_finder.cli.UniversalSkillFinder", "universal_skill_finder.cli.Cache"):
                stack.enter_context(patch(target, side_effect=AssertionError(f"{target} forbidden")))
            code = main(["--config", str(path), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_sources_and_legacy_repositories_list_dispatch_identically(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            canonical = self.config_command(path, "sources", "list", "--json")
            legacy = self.config_command(path, "repositories", "list", "--json")
            self.assertEqual(canonical, legacy)
            self.assertEqual(canonical[0], 0)
            payload = json.loads(canonical[1])
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(len(payload["sources"]), 13)
            self.assertNotIn("repositories", payload)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_source_and_legacy_repository_filters_can_be_mixed_and_repeated(self):
        with patch("universal_skill_finder.cli._search", return_value=0) as search:
            self.assertEqual(main(["pdf", "forms", "--repository", "tessl", "--source", "skills-sh",
                                   "--repository", "skillsmp", "--exclude", "skillsmp"]), 0)
        args = search.call_args.args[0]
        self.assertEqual(args.query, ["pdf", "forms"])
        self.assertEqual(args.source, ["tessl", "skills-sh", "skillsmp"])
        self.assertEqual(args.exclude, ["skillsmp"])

    def test_init_creates_one_editable_list_and_is_idempotent(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "settings" / "sources.json"
            code, output, errors = self.config_command(path, "repositories", "init")
            self.assertEqual((code, errors), (0, ""))
            self.assertIn(str(path.resolve()), output)
            self.assertIn('"enabled" to true or false', output)
            payload = json.loads(path.read_text())
            self.assertEqual(len(payload["sources"]), 13)
            self.assertTrue(all(type(row["enabled"]) is bool for row in payload["sources"]))
            before, modified = path.read_bytes(), path.stat().st_mtime_ns
            code, output, errors = self.config_command(path, "repositories", "init")
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("Existing source configuration", output)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, modified)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_legacy_init_alias_uses_the_same_source_list(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            self.assertEqual(self.config_command(path, "sources", "init")[0], 0)
            before = path.read_bytes()
            self.assertEqual(self.config_command(path, "repositories", "init")[0], 0)
            self.assertEqual(path.read_bytes(), before)

    def test_configuration_reads_never_initialize_or_create_directories(self):
        for arguments in (("repositories", "list"), ("repositories", "list", "--markdown"),
                          ("repositories", "list", "--json"), ("repositories", "explain", "tessl"),
                          ("repositories", "validate"), ("repositories", "config-path"), ("packs", "list")):
            with self.subTest(arguments=arguments), TemporaryDirectory() as temp:
                path = Path(temp) / "settings" / "sources.json"
                with patch("universal_skill_finder.cli.initialize_source_config", side_effect=AssertionError("init forbidden")):
                    code, _, errors = self.config_command(path, *arguments)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(list(Path(temp).iterdir()), [])

    def test_enable_and_disable_edit_flags_without_network_or_cache(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            self.assertEqual(self.config_command(path, "repositories", "init")[0], 0)
            original = load_config(str(path))
            for command, enabled in (("disable", False), ("enable", True)):
                code, _, errors = self.config_command(path, "repositories", command, "tessl")
                self.assertEqual((code, errors), (0, ""))
                current = load_config(str(path))
                self.assertEqual(current.source("tessl")["effective_enabled"], enabled)
                self.assertEqual([item for item in current.sources if item["id"] != "tessl"],
                                 [item for item in original.sources if item["id"] != "tessl"])
                rows = {row["id"]: row for row in json.loads(path.read_text())["sources"]}
                self.assertIs(rows["tessl"]["enabled"], enabled)
            self.assertEqual(list(Path(temp).iterdir()), [path])

    def test_unknown_source_fails_gracefully(self):
        for operation in ("explain", "enable", "disable"):
            with self.subTest(operation=operation), TemporaryDirectory() as temp:
                path = Path(temp) / "sources.json"
                code, _, errors = self.config_command(path, "repositories", operation, "missing")
                self.assertEqual(code, 3)
                self.assertIn("unknown source: missing", errors)
                self.assertNotIn("Traceback", errors)
                self.assertFalse(path.exists())

    def test_init_reports_legacy_pack_guard_without_changing_it(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            path.write_text(json.dumps({"schema_version": 1, "pack_overrides": {"registries": {"enabled": False}},
                                        "source_overrides": {"skillsmp": {"enabled": False}}}))
            code, output, errors = self.config_command(path, "repositories", "init")
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("Disabled packs still block their sources: registries", output)
            config = load_config(str(path))
            self.assertFalse(config.pack("registries")["enabled"])
            self.assertFalse(config.source("skillsmp")["enabled"])
            self.assertFalse(config.source("tessl")["effective_enabled"])

    def test_search_preview_does_not_initialize_configuration(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "settings" / "sources.json"
            with patch("socket.socket", side_effect=AssertionError("network forbidden")), \
                 patch("universal_skill_finder.cli.initialize_source_config", side_effect=AssertionError("init forbidden")), \
                 patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--config", str(path), "--cache-dir", str(root / "cache"),
                                       "pdf", "forms", "--dry-run", "--markdown"]), 0)
            self.assertEqual(list(root.iterdir()), [])


class SourceTerminologyTests(unittest.TestCase):
    def test_help_presents_source_commands_and_keeps_legacy_aliases(self):
        cases = [(["--help"], "sources (repositories)"),
                 (["sources", "--help"], "Create one editable source list"),
                 (["search", "--help"], "--source SOURCE_ID"),
                 (["sources", "enable", "--help"], "source_id")]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    main(arguments)
                self.assertEqual(raised.exception.code, 0)
                self.assertIn(expected, output.getvalue())

    def test_configuration_table_uses_shared_source_type_and_state_columns(self):
        with TemporaryDirectory() as temp:
            markdown = render_sources_markdown(load_config(str(Path(temp) / "sources.json")), environ={})
        self.assertIn("Configured sources: 9 enabled of 13.", markdown)
        self.assertIn("| Source | Type | Requirements | Ask to change | Enabled |", markdown)
        self.assertIn("| Repository | No configured key required | Disable openai-skills | ✅ Enabled |", markdown)
        self.assertIn("| Search index | UNIVERSAL\\_SKILL\\_FINDER\\_GITHUB\\_TOKEN: required, missing | Enable github-code-search | ❌ Disabled |", markdown)
        self.assertIn('"Enable SOURCE-ID"', markdown)

    def test_generated_search_reports_source_coverage_for_every_report_state(self):
        cases = [(report(results=[result(metrics_by_source={"skillsmp": {"stars": 12}})]), False),
                 (report(results=[], coverage=[]), False),
                 (report(results=[], coverage=[Coverage("failed", "timeout")]), False),
                 (report(results=[], coverage=[Coverage("empty", "ok")]), False),
                 (report(results=[], coverage=[Coverage("planned", "planned")]), True)]
        for found, dry_run in cases:
            with self.subTest(coverage=found.coverage, dry_run=dry_run):
                markdown = render_markdown(found, assistant="codex", dry_run=dry_run)
                output = io.StringIO()
                with redirect_stdout(output):
                    _print_report(found, dry_run=dry_run)
                if found.coverage:
                    self.assertIn("| Source | Search status | Candidates returned | Shown | Enabled |", markdown)
                self.assertIn("Enabled sources", output.getvalue())

    def test_registry_only_fallback_names_the_listing_accurately(self):
        row = result(repository=None, skill_path=None, ref=None, install={},
                     canonical_url="https://catalog.example/pdf")
        markdown = render_markdown(report(results=[row]), assistant="codex")
        self.assertIn("[Checked skill destination](https://catalog.example/pdf) · exact skill location unavailable", markdown)
        self.assertIn("[Open checked destination](https://catalog.example/pdf)", markdown)
        repository = render_markdown(report(results=[result(ref=None)]), assistant="codex")
        self.assertIn("exact skill target proof is missing", repository)
        self.assertNotIn("[Open checked destination]", repository)

    def test_remote_description_and_machine_identifiers_are_not_rewritten(self):
        row = result(description="Compare database sources", source_ids=["database-source"],
                     metrics_by_source={"database-source": {"stars": 2}})
        markdown = render_markdown(report(results=[row]))
        self.assertIn("Compare database sources", markdown)
        self.assertIn("database-source", markdown)
        payload = report(results=[row]).to_dict()
        self.assertEqual(payload["results"][0]["source_ids"], ["database-source"])
        self.assertIn("metrics_by_source", payload["results"][0])


if __name__ == "__main__":
    unittest.main()
