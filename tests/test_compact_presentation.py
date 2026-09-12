from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.models import Coverage, Result, SearchReport
from universal_skill_finder.presentation import render_report


def _result(*, number: int, name: str = "humanizer", repository: str = "owner/repository",
            path: str = "skills/humanizer", ref: str = "main", description: str = "Full description") -> Result:
    tree_url = f"https://github.com/{repository}/tree/{ref}/{path}"
    raw_url = f"https://raw.githubusercontent.com/{repository}/{ref}/{path}/SKILL.md"
    return Result(
        id=f"skill:{number}", name=name, description=description, canonical_url=None,
        repository=repository, skill_path=path, ref=ref, publisher="fixture",
        content_sha256="a" * 64, source_ids=["skillsmp", "tessl"], trust=["registry"],
        text_match_percent=100, rank_fusion_score=0.1, metrics_by_source={
            "skillsmp": {"stars": 30_563}, "tessl": {"downloads": 28},
        },
        install={"kind": "github", "repository": repository, "skill_path": path,
                 "ref": ref, "requires_approval": True}, warnings=[], occurrences=[],
        result_number=number, validation_status="eligible",
        link_proofs=[
            {"role": "skill_destination", "url": tree_url, "status": "eligible"},
            {"role": "repository", "url": f"https://github.com/{repository}", "status": "eligible"},
        ],
        target_proof={
            "kind": "github", "status": "eligible",
            "method": "anonymous_exact_skill_md_get", "identity_basis": "github-exact-skill-md-v1",
            "url": raw_url, "content_sha256": "a" * 64, "actual_name": name,
            "reported": {"repository": repository, "ref": ref, "skill_path": path, "name": name},
            "resolved": {"repository": repository, "ref": ref, "skill_path": path},
        },
        attributions=[
            {"source_id": "skillsmp", "label": "SkillsMP", "role": "listing",
             "url": "https://www.skillsmp.com/skills/humanizer", "status": "eligible"},
            {"source_id": "tessl", "label": "Tessl", "role": "source_page",
             "url": "https://tessl.io/skills/humanizer", "status": "eligible"},
        ],
    )


def _report(rows: list[Result]) -> SearchReport:
    report = SearchReport(
        query="humanize prose", results=rows,
        coverage=[Coverage("skillsmp", "ok", result_count=len(rows), shown=len(rows))],
        generated_at="2026-09-10T00:00:00+00:00", configuration_path="fixture.json",
        accepted_occurrences=len(rows), unique_count=len(rows), eligible_count=len(rows),
        requested_count=len(rows), page_size=10, page_shown=len(rows), materialized_total=len(rows),
        can_explain=True,
    )
    return report


class CompactPresentationTests(unittest.TestCase):
    def test_scan_first_card_keeps_proofs_provenance_and_bounded_description(self):
        marker = " DESCRIPTION-TAIL"
        description = ("descriptive words " * 120) + marker
        first = _result(number=1, description=description)
        second = _result(number=2, name="rewriter", path="skills/rewriter")
        text = render_report(_report([first, second]), assistant="codex")

        heading = "### 1. [humanizer](https://github.com/owner/repository/tree/main/skills/humanizer)"
        self.assertIn(heading, text)
        first_card = text.split(heading, 1)[1].split("### 2.", 1)[0]
        self.assertIn("**Location:** [owner/repository](https://github.com/owner/repository) › [skills/humanizer](https://github.com/owner/repository/tree/main/skills/humanizer)", first_card)
        self.assertIn("**Found on:** [SkillsMP](https://www.skillsmp.com/skills/humanizer) · [Tessl](https://tessl.io/skills/humanizer)", first_card)
        self.assertIn("**Signals:** skillsmp: stars: 30,563; tessl: downloads: 28", first_card)
        self.assertIn("descriptive words", first_card)
        self.assertIn("...", first_card)
        self.assertNotIn(marker, first_card)
        self.assertIn("Inspect and install: type **Inspect #1**  **Install #1**", first_card)
        command = "npx skills@1.5.23 add https://github.com/owner/repository/tree/main/skills/humanizer --skill humanizer --agent codex --copy"
        self.assertNotIn(command, text)
        self.assertNotIn("\\@", text)
        cards = text.split("## Source coverage", 1)[0]
        self.assertNotIn("---", cards)
        for legacy_label in ("**Sources:**", "**Metrics:**", "**Path:**", "**What it does:**",
                             "**Install (Codex, project):**"):
            self.assertNotIn(legacy_label, text)
        fields = ["descriptive words", "**Location:**", "**Found on:**", "**Signals:**", "Inspect and install:"]
        self.assertEqual([first_card.index(field) for field in fields], sorted(first_card.index(field) for field in fields))
        for preserved in ("## Source coverage", "## Notes", "## Next actions", "Star Universal Skill Finder on GitHub"):
            self.assertIn(preserved, text)

    def test_compact_card_keeps_resolved_correction_and_unavailable_inspection_link(self):
        row = _result(number=1, repository="reported/repository", path="reported/skill", ref="main")
        row.target_proof["resolved"] = {
            "repository": "resolved/repository", "skill_path": "resolved/skill", "ref": "release",
        }
        row.target_proof["actual_name"] = "humanizer"
        resolved = render_report(_report([row]), assistant="codex")
        self.assertIn("**Location:** [reported/repository](https://github.com/reported/repository) › reported/skill", resolved)
        self.assertIn("**Verified target:** resolved/repository › resolved/skill · branch release", resolved)

        row.target_proof = {"status": "not_checked", "detail": "exact target needs review"}
        unavailable = render_report(_report([row]), assistant="codex")
        self.assertIn("**Inspect #1** · Installation unavailable: exact target needs review. [Open checked destination](https://github.com/reported/repository/tree/main/reported/skill)", unavailable)
        self.assertNotIn("**Install #1**", unavailable.split("## Source coverage", 1)[0])
        self.assertNotIn("```sh", unavailable)

    def test_compact_plain_remains_copyable_and_non_markdown(self):
        row = _result(number=1)
        plain = render_report(_report([row]), assistant="claude-code", format="plain")
        self.assertIn("1. humanizer (https://github.com/owner/repository/tree/main/skills/humanizer)", plain)
        self.assertIn("Location: owner/repository (https://github.com/owner/repository) › skills/humanizer (https://github.com/owner/repository/tree/main/skills/humanizer)", plain)
        self.assertIn("Found on: SkillsMP (https://www.skillsmp.com/skills/humanizer)", plain)
        self.assertIn("Signals: skillsmp: stars: 30,563", plain)
        self.assertIn("Inspect and install: type Inspect #1  Install #1", plain)
        self.assertNotIn("npx skills@", plain)
        self.assertNotIn("###", plain)
        self.assertNotIn("```", plain)

    def test_compact_long_locations_and_line_boundaries_are_preserved(self):
        row = _result(number=1, path="skills/" + "p" * 220, ref="release-" + "r" * 140)
        text = render_report(_report([row]), assistant="codex")
        self.assertIn(row.skill_path, text)
        self.assertIn(" · branch " + row.ref + "  \n**Found on:**", text)
        row.skill_path = None
        self.assertIn("**Location:** [owner/repository](https://github.com/owner/repository) (skill folder unavailable) · branch " + row.ref,
                      render_report(_report([row])))

    def test_reported_path_cannot_link_to_a_different_resolved_repository_or_ref(self):
        row = _result(number=1)
        row.target_proof["resolved"]["repository"] = "different/repository"
        row.target_proof["resolved"]["ref"] = "release"
        text = render_report(_report([row]), assistant="codex")
        self.assertIn("**Location:** [owner/repository](https://github.com/owner/repository) › skills/humanizer", text)
        self.assertNotIn("**Location:** owner/repository › [skills/humanizer]", text)

    def test_location_and_action_fallbacks_cover_missing_source_fields(self):
        repository_only = _result(number=1, path=None, ref="release")
        repository_only.link_proofs = []
        repository_only.target_proof = {"status": "inconclusive", "detail": "skill folder unresolved"}
        text = render_report(_report([repository_only]), assistant="codex")
        self.assertIn("**Location:** owner/repository (skill folder unavailable) · branch release", text)
        self.assertIn("Installation unavailable: skill folder unresolved", text)

        path_only = _result(number=2, repository=None, path="skills/humanizer", ref=None)
        path_only.link_proofs = []
        path_only.target_proof = {"status": "not_checked", "detail": "repository unresolved"}
        text = render_report(_report([path_only]), assistant="codex")
        self.assertIn("**Location:** skills/humanizer (repository unavailable)", text)

        listing_only = _result(number=3, repository=None, path=None, ref=None)
        listing_only.link_proofs = [{
            "role": "listing", "status": "eligible", "url": "https://catalog.example/humanizer",
        }]
        listing_only.target_proof = {"status": "inconclusive", "detail": "exact target unresolved"}
        text = render_report(_report([listing_only]), assistant="codex")
        self.assertIn("**Location:** [Checked skill listing](https://catalog.example/humanizer) · exact skill location unavailable", text)
        self.assertIn("[Open checked destination](https://catalog.example/humanizer)", text)

        unavailable = _result(number=4, repository=None, path=None, ref=None)
        unavailable.link_proofs = []
        unavailable.target_proof = {"status": "not_checked"}
        text = render_report(_report([unavailable]), assistant="codex")
        self.assertIn("**Location:** Exact skill location unavailable", text)
        self.assertIn("Installation unavailable: exact skill target proof is missing.", text)

    def test_main_is_hidden_root_is_not_rendered_and_common_unverified_sources_group(self):
        row = _result(number=1, path=".")
        row.link_proofs = []
        row.attributions = []
        text = render_report(_report([row]), assistant="codex")
        self.assertIn("**Location:** owner/repository", text)
        self.assertNotIn("branch main", text)
        self.assertNotIn("› .", text)
        self.assertIn("**Found on:** skillsmp, tessl (listings not verified)", text)


if __name__ == "__main__":
    unittest.main()
