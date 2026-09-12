from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate
from test_universal_skill_finder import FakeHttp, finder_config


def candidate(native_id: str, name: str, url: str, *, source: str = "custom-registry") -> Candidate:
    return Candidate(native_id=native_id, name=name, description=name, source_id=source,
                     source_kind="registry", adapter="http-json-v1", native_rank=1, canonical_url=url)


class UrlIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.finder = UniversalSkillFinder(finder_config(root, []), cache=Cache(root / "cache"), http=FakeHttp())

    def test_different_query_ids_do_not_merge_and_query_keeps_correct_metadata(self):
        rows = [candidate("pdf", "PDF forms", "https://registry.example/skill?id=pdf"),
                candidate("react", "React performance", "https://registry.example/skill?id=react")]
        results = self.finder._merge(rows, "pdf forms")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].name, "PDF forms")
        self.assertEqual(results[0].canonical_url, "https://registry.example/skill?id=pdf")
        self.assertEqual([item["native_id"] for item in results[0].occurrences], ["pdf"])

    def test_different_spa_fragment_routes_do_not_merge(self):
        rows = [candidate("pdf", "PDF forms", "https://registry.example/#/skills/pdf"),
                candidate("react", "React performance", "https://registry.example/#/skills/react")]
        self.assertEqual(len(self.finder._merge(rows, "pdf forms")), 2)

    def test_different_url_parameters_do_not_merge(self):
        rows = [candidate("pdf", "PDF forms", "https://registry.example/skill;id=pdf"),
                candidate("react", "React performance", "https://registry.example/skill;id=react")]
        self.assertEqual(len(self.finder._merge(rows, "pdf forms")), 2)

    def test_identical_url_still_merges_across_reporting_sources(self):
        url = "https://registry.example/skill;view=full?id=pdf#/forms"
        rows = [candidate("native-one", "PDF forms", url, source="registry-one"),
                candidate("native-two", "PDF forms", url, source="registry-two")]
        results = self.finder._merge(rows, "pdf forms")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source_ids, ["registry-one", "registry-two"])
        self.assertEqual(len(results[0].occurrences), 2)

    def test_parameter_order_is_not_guessed_to_be_equivalent(self):
        rows = [candidate("one", "PDF forms", "https://registry.example/skill?id=pdf&variant=one"),
                candidate("two", "PDF forms", "https://registry.example/skill?variant=one&id=pdf")]
        self.assertEqual(len(self.finder._merge(rows, "pdf forms")), 2)

    def test_strong_repository_identity_still_dominates_listing_urls(self):
        rows = [candidate("one", "PDF forms", "https://registry.example/skill?id=one"),
                candidate("two", "PDF forms", "https://other.example/#/skills/two", source="registry-two")]
        for row in rows:
            row.repository = "example/skills"
            row.skill_path = "skills/pdf"
            row.ref = "main"
        results = self.finder._merge(rows, "pdf forms")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].repository, "example/skills")
        self.assertEqual(results[0].skill_path, "skills/pdf")


if __name__ == "__main__":
    unittest.main()
