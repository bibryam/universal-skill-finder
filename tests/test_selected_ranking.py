from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cache import Cache
from universal_skill_finder.adapters.repositories import _repo_candidate
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate
from universal_skill_finder.ranking import ALGORITHM_VERSION, compatible_match_percent


def candidate(native_id: str, name: str, description: str, *, source: str = "catalog", path: str | None = None) -> Candidate:
    return Candidate(
        native_id=native_id, name=name, description=description, source_id=source,
        source_kind="registry", adapter="stub", native_rank=999,
        repository="example/skills", skill_path=path or native_id, ref="main",
    )


class SelectedRankingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        config = EffectiveConfig(settings={}, packs=[], sources=[], overlay_path=root / "sources.json", overlay={})
        self.finder = UniversalSkillFinder(config, cache=Cache(root / "cache"))

    def test_equivalent_word_family_and_description_each_count_once(self):
        row = candidate("humanizer", "Humanizer", "Humanize, humanizer, and humanize prose")
        result = self.finder._merge([row], "humanize")[0]
        trace = result.ranking
        self.assertEqual(trace["algorithm_version"], ALGORITHM_VERSION)
        evidence = trace["evidence"]["lexical_occurrence"]
        self.assertEqual(evidence["name_groups"], ["humanize"])
        self.assertEqual(evidence["description_groups"], ["humanize"])
        self.assertEqual(trace["components"]["lexical"], 0.95)
        self.assertEqual(compatible_match_percent("humanize", "Humanizer"), 100)

    def test_description_corroboration_is_recorded_as_a_tie_break(self):
        corroborated = candidate("one", "Humanizer", "Humanize text while preserving your voice")
        plain = candidate("two", "Humanizer", "A general writing tool")
        results = self.finder._merge([plain, corroborated], "humanize")
        self.assertTrue(results[0].ranking["tie_breaks"]["description_corroboration"])

    def test_only_typed_skills_sh_skill_installs_are_rankable(self):
        skills = candidate("skills", "PDF forms", "Fill PDF forms", source="skills-sh")
        skills.metrics = {"installs": 50_000, "github_stars": 0}
        skills.metric_observations = [{
            "provider": "skills-sh", "name": "installs", "value": 50_000,
            "scope": "skill", "provenance": "source_provided",
        }]
        other = candidate("other", "PDF forms", "Fill PDF forms", source="skillsmp")
        other.metrics = {"github_stars": 10_000_000, "installs": 10_000_000}
        other.metric_observations = [{
            "provider": "skillsmp", "name": "installs", "value": 10_000_000,
            "scope": "skill", "provenance": "source_provided",
        }]
        results = self.finder._merge([other, skills], "pdf forms")
        traces = {row.skill_path: row.ranking for row in results}
        self.assertEqual(traces["skills"]["components"]["skill_adoption"], 0.1)
        self.assertEqual(traces["other"]["components"]["skill_adoption"], 0.0)
        self.assertIn("repository_stars", traces["other"]["evidence"]["excluded"])

    def test_native_rank_and_repository_stars_do_not_change_selected_order(self):
        alpha = candidate("alpha", "Alpha PDF forms", "Fill PDF forms")
        beta = candidate("beta", "Beta PDF forms", "Fill PDF forms")
        expected = [row.id for row in self.finder._merge([alpha, beta], "pdf forms")]
        alpha.native_rank, beta.native_rank = 1_000_000, 1
        alpha.metrics = {"github_stars": 0}
        beta.metrics = {"github_stars": 10_000_000}
        self.assertEqual([row.id for row in self.finder._merge([alpha, beta], "pdf forms")], expected)

    def test_bounded_repository_content_proof_survives_the_merge(self):
        source = {"id": "repo", "kind": "repository", "adapter": "github-repo", "repository": "example/skills", "ref": "main"}
        row = _repo_candidate(source, "tools/humanizer/SKILL.md", "---\nname: Humanizer\ndescription: Humanize prose\n---\n", "a" * 64)
        merged = self.finder._merge([row], "humanize")[0]
        self.assertEqual(merged.target_proof["status"], "eligible")
        self.assertEqual(merged.target_proof["content_sha256"], "a" * 64)
        self.assertEqual(
            [(proof["role"], proof["status"]) for proof in merged.link_proofs],
            [("skill_destination", "not_checked")],
        )
        self.assertEqual(merged.attributions, [])


if __name__ == "__main__":
    unittest.main()
