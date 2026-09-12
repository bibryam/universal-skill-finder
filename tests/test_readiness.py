from __future__ import annotations

import io
import itertools
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.cli import main
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate, INVALID_METRICS_WARNING, UNSAFE_LOCATION_WARNING
from test_universal_skill_finder import StaticAdapter, candidate, finder_config, fixture_finder, registry_source


class ReadinessTests(unittest.TestCase):
    def test_empty_invocation_shows_help_successfully(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        self.assertIn("Search enabled", output.getvalue())

    def test_equal_content_does_not_merge_different_install_locations(self):
        with TemporaryDirectory() as temp:
            finder = UniversalSkillFinder(finder_config(Path(temp), []), cache=Cache(Path(temp) / "cache"))
            one = candidate("source", "one/skills", "pdf", rank=1)
            two = candidate("source", "two/skills", "pdf", rank=1)
            one.content_sha256 = two.content_sha256 = "a" * 64
            self.assertEqual(len(finder._merge([one, two], "pdf")), 2)

    def test_merge_is_independent_of_source_completion_order(self):
        with TemporaryDirectory() as temp:
            finder = UniversalSkillFinder(finder_config(Path(temp), []), cache=Cache(Path(temp) / "cache"))
            rows = [candidate("one", "acme/skills", "pdf", rank=1),
                    candidate("two", "acme/skills", "pdf", rank=1),
                    candidate("one", "other/skills", "pdf", rank=1)]
            expected = [item.to_dict() for item in finder._merge(rows, "pdf")]
            for permutation in itertools.permutations(rows):
                self.assertEqual([item.to_dict() for item in finder._merge(list(permutation), "pdf")], expected)

    def test_malformed_candidate_fields_are_rejected(self):
        row = candidate("one", "acme/skills", "pdf", rank=1).to_dict()
        for key, value in [("native_rank", "1"), ("native_rank", -1), ("warnings", [[]]),
                           ("name", {}), ("metrics", []), ("install", "run me")]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Candidate.from_dict({**row, key: value})

    def test_cached_commands_are_not_forwarded_to_installer(self):
        row = candidate("one", "acme/skills", "pdf", rank=1).to_dict()
        row["install"] = {"kind": "github", "command": ["curl", "https://evil.test"], "repository": "evil/other"}
        result = Candidate.from_dict(row)
        self.assertEqual(result.install["repository"], "acme/skills")
        self.assertNotIn("command", result.install)

    def test_corrupt_cache_is_a_miss_and_cannot_forge_source_trust(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = registry_source("one", "skills-sh", trust="community-index")
            cache = Cache(root / "cache")
            finder = fixture_finder(finder_config(root, [source]), cache)
            adapter = StaticAdapter([candidate("forged", "acme/skills", "pdf", rank=1)])
            finder.adapter_map = {"skills-sh": adapter}
            key = finder._cache_key(source, "pdf", 10)
            cache.write("queries", key, {"candidates": [{"name": "broken"}]})
            report = finder.search("pdf")
            self.assertEqual(report.coverage[0].status, "ok")
            self.assertEqual(report.results[0].source_ids, ["one"])
            self.assertEqual(report.results[0].trust, ["community-index"])
            self.assertEqual(report.to_dict()["schema_version"], 2)
            self.assertEqual(adapter.calls, 1)

    def test_metrics_are_bounded_scalars_and_json_is_finite(self):
        row = candidate("one", "acme/skills", "pdf", rank=1).to_dict()
        row["metrics"] = {"stars": 12, "score": 0.9, "reviewed": True, "grade": "A\u202e", "missing": None,
                          "nested": {"run": "instructions"}, "array": [], "nan": float("nan"),
                          "infinity": float("inf"), "huge_integer": 1 << 300}
        row["updated_at"] = float("inf")
        result = Candidate.from_dict(row)
        self.assertEqual(result.metrics, {"stars": 12, "score": 0.9, "reviewed": True, "grade": "A", "missing": None})
        self.assertIsNone(result.updated_at)
        self.assertIn(INVALID_METRICS_WARNING, result.warnings)
        json.dumps(result.to_dict(), allow_nan=False)
        with self.assertRaisesRegex(ValueError, "at most 100"):
            Candidate.from_dict({**row, "metrics": {str(index): index for index in range(101)}})

    def test_deep_cached_metrics_cannot_break_merge(self):
        nested = {}
        for _ in range(3000):
            nested = {"nested": nested}
        row = candidate("one", "acme/skills", "pdf", rank=1).to_dict()
        row["metrics"] = {"malicious": nested, "stars": 12}
        source = registry_source("one", "skills-sh")
        validated = UniversalSkillFinder._validated_candidates([row], source)
        with TemporaryDirectory() as temp:
            finder = UniversalSkillFinder(finder_config(Path(temp), [source]), cache=Cache(Path(temp) / "cache"))
            results = finder._merge(validated, "pdf")
        self.assertEqual(results[0].metrics_by_source, {"one": {"stars": 12}})
        json.dumps(results[0].to_dict(), allow_nan=False)

    def test_unsafe_location_stays_withheld_after_repeated_cache_round_trips(self):
        base = candidate("one", "acme/skills", "pdf", rank=1).to_dict()
        for field, unsafe in (("repository", "../evil"), ("skill_path", "../../outside"), ("ref", "--upload-pack=evil")):
            with self.subTest(field=field):
                row = {**base, field: unsafe, "warnings": [f"registry warning {index}" for index in range(100)]}
                for _ in range(3):
                    result = Candidate.from_dict(row)
                    self.assertEqual(result.install, {})
                    self.assertIn(UNSAFE_LOCATION_WARNING, result.warnings)
                    row = result.to_dict()

    def test_local_handoff_uses_configured_root_and_ignores_absolute_payload_path(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "Local Skills"
            (root / "pdf").mkdir(parents=True)
            source = {"id": "local", "kind": "repository", "adapter": "local-directory", "path": str(root)}
            row = {"native_id": "pdf", "name": "PDF", "description": "Read PDFs", "source_id": "forged",
                   "source_kind": "repository", "adapter": "local-directory", "skill_path": "pdf",
                   "canonical_url": "file:///untrusted/path", "install": {"kind": "local", "path": "/untrusted/path"}}
            result = UniversalSkillFinder._validated_candidates([row], source)[0]
            self.assertEqual(result.install, {"kind": "local", "path": str(root / "pdf"), "requires_approval": True})
            self.assertEqual(result.canonical_url, (root / "pdf").as_uri())
            self.assertEqual(result.source_id, "local")
            repeated = UniversalSkillFinder._validated_candidates([result.to_dict()], source)[0]
            self.assertEqual(repeated.install, result.install)

    def test_local_handoff_rejects_traversal_and_missing_paths(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "skills"
            outside = Path(temp).resolve() / "outside"
            root.mkdir()
            outside.mkdir()
            source = {"id": "local", "kind": "repository", "adapter": "local-directory", "path": str(root)}
            base = {"name": "PDF", "description": "Read PDFs", "source_id": "local", "source_kind": "repository", "adapter": "local-directory"}
            for relative in ("../outside", str(outside), "missing", "pdf\u202e"):
                with self.subTest(relative=relative):
                    result = UniversalSkillFinder._validated_candidates([{**base, "native_id": relative}], source)[0]
                    self.assertEqual(result.install, {})
                    self.assertIsNone(result.canonical_url)
                    self.assertIn(UNSAFE_LOCATION_WARNING, result.warnings)

    def test_local_handoff_rejects_symlink_escape(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "skills"
            outside = Path(temp).resolve() / "outside"
            root.mkdir()
            outside.mkdir()
            try:
                (root / "escape").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            source = {"id": "local", "kind": "repository", "adapter": "local-directory", "path": str(root)}
            row = {"name": "PDF", "description": "Read PDFs", "source_id": "local", "source_kind": "repository", "adapter": "local-directory", "native_id": "escape"}
            result = UniversalSkillFinder._validated_candidates([row], source)[0]
            self.assertEqual(result.install, {})
            self.assertIsNone(result.canonical_url)

    def test_local_root_skill_has_handoff(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = {"id": "local", "kind": "repository", "adapter": "local-directory", "path": str(root)}
            row = {"native_id": ".", "name": "PDF", "description": "Read PDFs", "source_id": "local", "source_kind": "repository", "adapter": "local-directory", "skill_path": "."}
            result = UniversalSkillFinder._validated_candidates([row], source)[0]
            self.assertEqual(result.install["path"], str(root))
            self.assertEqual(result.canonical_url, root.as_uri())


if __name__ == "__main__":
    unittest.main()
