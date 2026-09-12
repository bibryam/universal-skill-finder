from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate
from test_universal_skill_finder import FakeHttp, StaticAdapter, finder_config, fixture_finder, registry_source


def occurrence(source: str, path: str, *, name: str, description: str, rank: int = 1) -> Candidate:
    url = f"https://github.com/example/skills/tree/main/{path}"
    return Candidate(
        native_id=f"{source}:{path}", name=name, description=description,
        source_id=source, source_kind="registry", adapter="stub", native_rank=rank,
        canonical_url=url, repository="example/skills", skill_path=path, ref="main", slug=path,
        target_proof={"kind": "fixture", "status": "eligible", "skill_path": path,
                      "content_sha256": "0" * 64},
        link_proofs=[{"role": "skill_destination", "url": url, "status": "eligible",
                      "method": "fixture", "identity_basis": "fixture_exact_skill"}],
    )


class RankingQualityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.finder = UniversalSkillFinder(finder_config(self.root, []), cache=Cache(self.root / "cache"), http=FakeHttp())

    def test_complete_query_match_outranks_a_partial_match_mirrored_by_two_sources(self):
        rows = [
            occurrence("one", "canvas-design", name="canvas-design", description="Create PDF posters"),
            occurrence("two", "canvas-design", name="canvas-design", description="Create PDF posters"),
            occurrence("three", "form-filler", name="PDF Form Filler", description="Fill PDF forms", rank=10),
        ]
        merged = self.finder._merge(rows, "pdf forms")
        self.assertEqual([row.skill_path for row in merged], ["form-filler", "canvas-design"])
        self.assertEqual([row.text_match_percent for row in merged], [100, 50])
        self.assertLess(merged[0].rank_fusion_score, merged[1].rank_fusion_score)

    def test_rrf_is_diagnostic_and_does_not_override_selected_ties(self):
        rows = [
            occurrence("one", "single", name="PDF forms one", description="Read forms"),
            occurrence("one", "mirrored", name="PDF forms two", description="Read forms", rank=2),
            occurrence("two", "mirrored", name="PDF forms two", description="Read forms", rank=3),
        ]
        merged = self.finder._merge(rows, "pdf forms")
        self.assertEqual([row.skill_path for row in merged], ["single", "mirrored"])
        self.assertEqual(merged[0].text_match_percent, merged[1].text_match_percent)
        self.assertLess(merged[0].rank_fusion_score, merged[1].rank_fusion_score)
        self.assertEqual([row.ranking["components"]["skill_adoption"] for row in merged], [0.0, 0.0])

    def test_merge_stays_lossless_before_public_relevance_admission(self):
        rows = [
            occurrence("registry", "acroform", name="AcroForm assistant", description="Populate interactive documents"),
            occurrence("registry", "pdf-form", name="PDF forms", description="Fill forms", rank=2),
        ]
        merged = self.finder._merge(rows, "pdf forms")
        self.assertEqual([row.skill_path for row in merged], ["pdf-form", "acroform"])
        self.assertEqual(merged[1].text_match_percent, 0)

    def test_ties_and_result_ids_do_not_depend_on_completion_order(self):
        rows = [
            occurrence("one", "zeta", name="Zeta PDF forms", description="Fill forms"),
            occurrence("one", "beta", name="Alpha PDF forms", description="Fill forms"),
            occurrence("two", "alpha", name="Alpha PDF forms", description="Fill forms"),
        ]
        expected = [row.to_dict() for row in self.finder._merge(rows, "pdf forms")]
        self.assertEqual(expected[-1]["name"], "Zeta PDF forms")
        for permutation in itertools.permutations(rows):
            self.assertEqual([row.to_dict() for row in self.finder._merge(list(permutation), "pdf forms")], expected)

    def test_popularity_metrics_do_not_change_ranking(self):
        rows = [
            occurrence("one", "alpha", name="Alpha PDF forms", description="Fill forms"),
            occurrence("two", "zeta", name="Zeta PDF forms", description="Fill forms"),
        ]
        expected = [row.id for row in self.finder._merge(rows, "pdf forms")]
        rows[0].metrics = {"github_stars": 0, "downloads": 0}
        rows[1].metrics = {"github_stars": 1_000_000, "downloads": 1_000_000}
        self.assertEqual([row.id for row in self.finder._merge(rows, "pdf forms")], expected)

    def test_relevance_first_keeps_default_top_ten_and_all_source_coverage(self):
        sources = [registry_source("first", "skills-sh"), registry_source("second", "skillsmp")]
        first = StaticAdapter([occurrence("first", f"partial-{index}", name=f"PDF poster {index}",
                                         description="Design posters", rank=index) for index in range(1, 11)])
        second = StaticAdapter([occurrence("second", f"forms-{index}", name=f"PDF forms {index}",
                                          description="Fill forms", rank=index) for index in range(1, 11)])
        finder = fixture_finder(finder_config(self.root, sources), Cache(self.root / "cache"))
        finder.adapter_map = {"skills-sh": first, "skillsmp": second}
        report = finder.search("pdf forms")
        self.assertEqual(len(report.results), 10)
        self.assertTrue(all(row.text_match_percent == 100 for row in report.results))
        self.assertEqual([row.source_id for row in report.coverage], ["first", "second"])
        self.assertEqual([row.result_count for row in report.coverage], [10, 10])
        self.assertEqual((first.calls, second.calls), (1, 1))


if __name__ == "__main__":
    unittest.main()
