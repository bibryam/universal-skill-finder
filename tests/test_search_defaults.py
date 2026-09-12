from __future__ import annotations

import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import SourceUnavailable
from universal_skill_finder.cache import Cache
from universal_skill_finder.cli import _print_report, main
from universal_skill_finder.config import ConfigurationError
from universal_skill_finder.federation import UniversalSkillFinder
from test_universal_skill_finder import StaticAdapter, candidate, finder_config, fixture_finder, registry_source


class RecordingAdapter(StaticAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests = []

    def search(self, source, query, limit, context):
        self.requests.append((source["id"], query, limit))
        return super().search(source, query, limit, context)


def default_search_fixture(root: Path, *, count: int = 8):
    sources = [
        registry_source("registry-one", "skills-sh", base_url="https://catalog.example"),
        {
            "id": "publisher-repository", "kind": "repository", "adapter": "github-repo",
            "repository": "publisher/skills", "ref": "main", "enabled": True,
            "effective_enabled": True, "trust": "publisher-owned",
        },
        registry_source("empty-registry", "skillsmp"),
        registry_source("limited-registry", "clawhub"),
        registry_source("disabled-registry", "polyskill", enabled=False, effective_enabled=False),
    ]
    registry_rows = [
        candidate("registry-one", "community/skills", f"pdf-{index}",
                  rank=index, name=f"PDF Forms Community {index:02}")
        for index in range(1, count + 1)
    ]
    publisher_rows = [
        candidate("publisher-repository", "publisher/skills", f"pdf-{index}",
                  rank=index, name=f"PDF Forms Publisher {index:02}")
        for index in range(1, count + 1)
    ]
    adapters = {
        "skills-sh": RecordingAdapter(registry_rows),
        "github-repo": RecordingAdapter(publisher_rows),
        "skillsmp": RecordingAdapter(),
        "clawhub": RecordingAdapter(failure=SourceUnavailable("rate_limited", "quota exhausted")),
        "polyskill": RecordingAdapter([candidate("disabled-registry", "disabled/skills", "pdf", rank=1)]),
    }
    finder = fixture_finder(finder_config(root, sources), Cache(root / "cache"))
    finder.adapter_map = adapters
    return finder, adapters


class SearchDefaultTests(unittest.TestCase):
    def test_default_queries_every_enabled_source_and_never_a_disabled_source(self):
        with TemporaryDirectory() as temp:
            finder, adapters = default_search_fixture(Path(temp))
            report = finder.search("pdf forms")

        self.assertEqual({key: adapter.calls for key, adapter in adapters.items()}, {
            "skills-sh": 1, "github-repo": 1, "skillsmp": 1, "clawhub": 1, "polyskill": 0,
        })
        for adapter in list(adapters.values())[:-1]:
            self.assertEqual(adapter.requests[0][1:], ("pdf forms", 10))
        self.assertEqual([(item.source_id, item.status) for item in report.coverage], [
            ("registry-one", "ok"), ("publisher-repository", "ok"),
            ("empty-registry", "ok"), ("limited-registry", "rate_limited"),
            ("disabled-registry", "disabled"),
        ])
        self.assertEqual(report.coverage[2].result_count, 0)

    def test_top_ten_is_the_default_not_a_per_source_total(self):
        with TemporaryDirectory() as temp:
            finder, _ = default_search_fixture(Path(temp))
            default = finder.search("pdf forms")
            expanded = finder.search("pdf forms", max_results=20)
            shorter = finder.search("pdf forms", max_results=3)

        self.assertEqual(len(default.results), 10)
        self.assertEqual(len(expanded.results), 10)
        self.assertEqual(expanded.requested_count, 20)
        self.assertEqual(expanded.page_size, 10)
        self.assertEqual([item.id for item in default.results], [item.id for item in expanded.results])
        self.assertEqual([item.id for item in shorter.results], [item.id for item in expanded.results[:3]])
        self.assertEqual({source for item in default.results for source in item.source_ids},
                         {"registry-one", "publisher-repository"})

    def test_programmatic_schema_two_bounds_are_explicit(self):
        with TemporaryDirectory() as temp:
            finder, _ = default_search_fixture(Path(temp))
            with self.assertRaisesRegex(ConfigurationError, "max_results"):
                finder.search("pdf forms", max_results=501)
            with self.assertRaisesRegex(ConfigurationError, "count"):
                finder.search("pdf forms", count=101)
            with self.assertRaisesRegex(ConfigurationError, "page_size"):
                finder.search("pdf forms", page_size=101)

    def test_fewer_than_ten_available_results_are_not_padded(self):
        with TemporaryDirectory() as temp:
            finder, _ = default_search_fixture(Path(temp), count=1)
            report = finder.search("pdf forms")
        self.assertEqual(len(report.results), 2)
        self.assertEqual(len({item.id for item in report.results}), 2)

    def test_plain_and_explicit_search_keep_unrestricted_defaults(self):
        for argv in (["pdf", "forms"], ["search", "pdf", "forms"]):
            with self.subTest(argv=argv):
                with patch("universal_skill_finder.cli._search", return_value=0) as search:
                    self.assertEqual(main(argv), 0)
                args = search.call_args.args[0]
                self.assertEqual(args.query, ["pdf", "forms"])
                self.assertEqual(args.source, [])
                self.assertEqual(args.exclude, [])
                self.assertEqual(args.limit, 10)
                self.assertEqual(args.max_results, 10)

    def test_explicit_result_count_and_source_filters_remain_available(self):
        with patch("universal_skill_finder.cli._search", return_value=0) as search:
            self.assertEqual(main(["pdf", "forms", "--max-results", "4", "--source", "one",
                                   "--source", "two", "--exclude", "two"]), 0)
        args = search.call_args.args[0]
        self.assertEqual(args.max_results, 4)
        self.assertEqual(args.source, ["one", "two"])
        self.assertEqual(args.exclude, ["two"])

    def test_cli_passes_unrestricted_defaults_to_finder_and_returns_partial_results(self):
        with TemporaryDirectory() as temp:
            finder, adapters = default_search_fixture(Path(temp))
            output = io.StringIO()
            with patch("universal_skill_finder.cli._load", return_value=(finder.config, finder.cache)), \
                 patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder), \
                 patch.object(finder, "search", wraps=finder.search) as search, \
                 redirect_stdout(output):
                self.assertEqual(main(["pdf", "forms"]), 0)

        self.assertEqual(search.call_args.args, ("pdf forms",))
        self.assertEqual(search.call_args.kwargs["source_ids"], [])
        self.assertEqual(search.call_args.kwargs["exclude_ids"], [])
        self.assertEqual(search.call_args.kwargs["max_results"], 10)
        self.assertEqual(adapters["polyskill"].calls, 0)
        self.assertEqual(len(re.findall(r"^\s*\d+\. ", output.getvalue(), re.MULTILINE)), 10)

    def test_each_result_automatically_includes_link_discovery_source_and_install_choice(self):
        with TemporaryDirectory() as temp:
            finder, _ = default_search_fixture(Path(temp), count=1)
            report = finder.search("pdf forms")
        output = io.StringIO()
        with redirect_stdout(output):
            _print_report(report, dry_run=False)

        results_text, separator, coverage_text = output.getvalue().partition("\nEnabled sources\n")
        self.assertTrue(separator)
        rows = re.split(r"^\s*\d+\. ", results_text, flags=re.MULTILINE)[1:]
        self.assertEqual(len(rows), len(report.results))
        for number, (row, result) in enumerate(zip(rows, report.results), 1):
            with self.subTest(result=result.name):
                self.assertIn(result.name, row)
                self.assertIn(f"Skill link: {result.canonical_url}", row)
                found_in = next(line for line in row.splitlines() if "Found in:" in line)
                for source_id in result.source_ids:
                    self.assertIn(source_id, found_in)
                self.assertIn(f"Install: Install #{number} (review required)", row)

        for item in report.coverage[:-1]:
            line = next(line for line in coverage_text.splitlines() if item.source_id in line)
            self.assertIn(item.status, line)
        zero_line = next(line for line in coverage_text.splitlines() if "empty-registry" in line)
        self.assertIn("0 results", zero_line)
        self.assertIn("quota exhausted", coverage_text)
        self.assertIn("disabled-registry", coverage_text)

    def test_unresolved_install_targets_offer_inspection_without_inventing_a_command(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = registry_source("registry-one", "skills-sh")
            pathless = candidate("registry-one", "acme/skills", "placeholder", rank=1, name="Pathless PDF")
            pathless.skill_path = None
            pathless.canonical_url = "https://catalog.example/pathless"
            unavailable = candidate("registry-one", "other/skills", "placeholder", rank=2, name="Hosted PDF")
            unavailable.repository = unavailable.skill_path = unavailable.ref = None
            unavailable.canonical_url = "https://catalog.example/hosted"
            unavailable.install = {}
            finder = fixture_finder(finder_config(root, [source]), Cache(root / "cache"))
            finder.adapter_map = {"skills-sh": StaticAdapter([pathless, unavailable])}
            report = finder.search("pdf forms")

        output = io.StringIO()
        with redirect_stdout(output):
            _print_report(report, dry_run=False)
        self.assertEqual(report.results, [])
        self.assertEqual(len(report.candidate_previews), 2)
        self.assertNotIn("Install:", output.getvalue())
        self.assertNotIn("npx", output.getvalue())

    def test_json_coverage_exposes_effective_enabled_state_even_when_filtered(self):
        with TemporaryDirectory() as temp:
            finder, _ = default_search_fixture(Path(temp), count=1)
            default = finder.search("pdf forms").to_dict()
            filtered = finder.search("pdf forms", source_ids=["registry-one"],
                                     exclude_ids=["registry-one"]).to_dict()

        self.assertEqual(default["schema_version"], 2)
        self.assertEqual([item["enabled"] for item in default["coverage"]], [True, True, True, True, False])
        self.assertEqual([item["enabled"] for item in filtered["coverage"]], [True, True, True, True, False])
        self.assertEqual(filtered["coverage"][0]["status"], "excluded")
        self.assertEqual(filtered["coverage"][1]["status"], "not_selected")
        for result in default["results"]:
            self.assertTrue(result["canonical_url"])
            self.assertTrue(result["source_ids"])
            self.assertTrue(result["install"]["requires_approval"])


if __name__ == "__main__":
    unittest.main()
