from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.presentation import render_explanation, render_help, render_preview, render_progress, render_report
from universal_skill_finder.models import Coverage, Result, SearchReport
from universal_skill_finder.source_presentation import render_sources_table


def proof(role: str, status: str, url: str | None = None, detail: str | None = None):
    return SimpleNamespace(role=role, status=status, url=url, detail=detail)


def report(mode: str = "online"):
    first = SimpleNamespace(
        number=4, name="humanizer", description="Remove AI-writing patterns without changing meaning.",
        link_proofs=[
            proof("skill_destination", "eligible", "https://github.com/example/humanizer/tree/main/skills/humanizer"),
            proof("repository", "eligible", "https://github.com/example/humanizer"),
        ],
        location=[
            SimpleNamespace(label="example/humanizer", status="reachable", url="https://example.test/repo"),
            SimpleNamespace(label="skills/humanizer", status="eligible", url="https://example.test/repo/tree/main/skills/humanizer"),
        ],
        found_on=[
            SimpleNamespace(source_id="skillsmp", label="SkillsMP", role="listing", status="eligible", url="https://example.test/listing"),
            SimpleNamespace(source_id="tessl", label="Tessl", role="source_page", status="reachable", url="https://example.test/tessl", suffix="source page; listing unavailable"),
        ],
        metrics_text="Not available", target_proof=SimpleNamespace(
            kind="github", status="eligible", actual_name="humanizer", content_sha256="0" * 64,
            url="https://raw.githubusercontent.com/example/humanizer/main/skills/humanizer/SKILL.md",
            method="anonymous_exact_skill_md_get", identity_basis="github-exact-skill-md-v1",
            reported={"repository": "example/humanizer", "ref": "main", "skill_path": "skills/humanizer"},
            resolved={"repository": "example/humanizer", "ref": "main", "skill_path": "skills/humanizer"},
        ),
        install={"kind": "github", "repository": "example/humanizer", "skill_path": "skills/humanizer", "ref": "main"},
        repository="example/humanizer", skill_path="skills/humanizer", ref="main", warnings=["Review API access"], source_ids=["skillsmp", "tessl"],
    )
    second = SimpleNamespace(
        number=9, name="audit", description="Audit text only.",
        link_proofs=[proof("skill", "eligible", "https://example.test/skills/audit")],
        location=[SimpleNamespace(label="audit/source", status="unverified", reason="repository not checked")],
        found_on=[SimpleNamespace(source_id="skillhub", label="SkillHub", role="listing", status="unavailable", reason="listing returned 404")],
        metrics_text="Not available", target_proof=SimpleNamespace(status="inconclusive", detail="exact SKILL.md proof is missing"),
        install={}, repository=None, skill_path=None, ref=None, warnings=[], source_ids=["skillhub"],
    )
    return SimpleNamespace(
        report_format_version=2, mode=mode, query="humanize text", results=[first, second],
        accepted_candidates=5, unique_skills=4, duplicates_merged=1, eligible=2, unavailable=1, inconclusive=1, not_checked=0,
        page_range="4–9", page_shown_count=2, materialized_count=2, requested_count=3, page_size=2,
        coverage=[
            SimpleNamespace(source_id="skillsmp", status="searched", candidates_returned=3, shown=1, enabled=True, link_proof=proof("source_page", "inconclusive", "https://example.test/unchecked")),
            SimpleNamespace(source_id="tessl", status="cached", candidates_returned=0, shown=1, enabled=True, link_proof=proof("source_page", "reachable", "https://example.test/tessl")),
        ],
        notes=["Coverage is partial."], installed_scan={}, can_explain=True,
        continuation=SimpleNamespace(available=True), footer_link_proof=proof("footer", "eligible", "https://example.test/universal-skill-finder"),
    )


class PresentationV2Tests(unittest.TestCase):
    def test_checked_cards_preserve_field_order_stable_numbers_and_link_gates(self):
        text = render_report(report(), assistant="codex")
        heading = "### 4. [humanizer](https://github.com/example/humanizer/tree/main/skills/humanizer)"
        self.assertIn(heading, text)
        self.assertIn("### 9. [audit](https://example.test/skills/audit)", text)
        first_card = text.split(heading, 1)[1].split("### 9.", 1)[0]
        fields = ["Remove AI-writing patterns without changing meaning.", "Location:",
                  "Found on:", "Signals:", "Inspect and install:"]
        self.assertEqual([first_card.index(field) for field in fields],
                         sorted(first_card.index(field) for field in fields))
        self.assertIn("listing returned 404", text)
        self.assertNotIn("https://example.test/unchecked", text)
        self.assertNotIn("[SkillHub]", text)
        self.assertIn("Installation unavailable: exact SKILL.md proof is missing", text)
        self.assertIn("with verified destinations (not necessarily install-ready)", text)
        self.assertNotIn("Install #9", text)
        self.assertIn("Next page is available", text)
        self.assertIn("Explain #N is available", text)

    def test_untrusted_bare_urls_in_descriptions_and_notes_are_inert_in_markdown_and_plain(self):
        found = report()
        found.results[0].description = "Remote says https://evil.example/path and [click](http://bad.example/x)."
        found.notes = ["Do not open file:///tmp/unchecked or https://other.example/note."]
        markdown = render_report(found, assistant="codex")
        plain = render_report(found, assistant="codex", format="plain")
        for text in (markdown, plain):
            self.assertIn("https[:]//evil.example/path", text)
            self.assertIn("http[:]//bad.example/x", text)
            self.assertIn("file[:]///tmp/unchecked", text)
            self.assertIn("https[:]//other.example/note", text)
            self.assertNotIn("](http://bad.example/x)", text)
        # The separate, proof-backed primary destination remains an explicit link.
        self.assertIn("](https://github.com/example/humanizer/tree/main/skills/humanizer)", markdown)

    def test_plain_uses_same_numbers_without_active_markdown_or_unicode_states(self):
        text = render_report(report(), assistant="codex", format="plain")
        self.assertIn("4. humanizer", text)
        self.assertIn("9. audit", text)
        self.assertNotIn("](https://", text)
        self.assertIn("[enabled]", text)
        self.assertNotIn("✅", text)

    def test_offline_previews_are_unnumbered_and_nonexecutable(self):
        found = report(mode="offline_preview")
        found.results = []
        found.candidate_previews = [SimpleNamespace(name="humanizer", repository="example/humanizer", sources=["skillsmp"], description="Remove AI-writing patterns.", reason="offline cached candidate")]
        text = render_report(found)
        self.assertIn("Offline preview", text)
        self.assertIn("Candidate previews (1)", text)
        self.assertNotIn("### 1.", text)
        self.assertNotIn("```sh", text)
        self.assertNotIn("Install #", text)
        self.assertNotIn("Next page", text)

    def test_partial_coverage_words_cached_and_zero_result_states_precisely(self):
        found = report()
        found.results = []
        found.coverage = [
            SimpleNamespace(source_id="cached", status="cached", cache_age_seconds=12, incomplete_results=True, candidates_returned=2, shown=0, enabled=True, link_proof=proof("source_page", "inconclusive")),
            SimpleNamespace(source_id="live", status="searched", incomplete_results=True, candidates_returned=0, shown=0, enabled=True, link_proof=proof("source_page", "inconclusive")),
        ]
        text = render_report(found)
        self.assertIn("Partial cached (not contacted; age 12s)", text)
        self.assertIn("Partial search", text)
        self.assertIn("No verified matches were returned; search coverage is incomplete.", text)

    def test_checked_listing_and_bundle_listing_are_inspection_only_primary_destinations(self):
        found = report()
        listing = found.results[0]
        listing.name = "registry skill"
        listing.link_proofs = [proof("listing", "eligible", "https://example.test/listing")]
        listing.target_proof = SimpleNamespace(status="inconclusive", detail="exact target is unresolved")
        listing.install = {}
        listing.repository = listing.skill_path = listing.ref = None
        listing.location = []
        found.results = [listing]
        text = render_report(found, assistant="codex")
        self.assertIn("### 4. [registry skill](https://example.test/listing)", text)
        self.assertIn("**Location:** [Checked skill listing](https://example.test/listing) · exact skill location unavailable", text)
        self.assertIn("Open checked destination", text)
        self.assertNotIn("```sh", text)
        listing.link_proofs = [proof("bundle_listing", "eligible", "https://example.test/bundle")]
        text = render_report(found, assistant="codex")
        self.assertIn("### 4. [registry skill (bundle)](https://example.test/bundle)", text)
        self.assertIn("**Location:** [Checked bundle listing](https://example.test/bundle) · exact skill location unavailable", text)
        self.assertNotIn("```sh", text)

    def test_attribution_keeps_unverified_sources_plain_and_never_labels_github_as_tessl(self):
        found = report()
        row = found.results[0]
        row.source_ids = ["skillsmp", "tessl", "unverified-registry"]
        row.found_on = [
            SimpleNamespace(source_id="skillsmp", label="SkillsMP", role="listing", status="eligible", url="https://example.test/listing"),
            SimpleNamespace(source_id="tessl", label="Tessl", role="repository", status="eligible", url="https://github.com/example/humanizer"),
        ]
        text = render_report(found)
        self.assertIn("[SkillsMP](https://example.test/listing)", text)
        self.assertIn("Tessl (repository source)", text)
        self.assertIn("unverified-registry (listing not verified)", text)
        self.assertNotIn("[Tessl](https://github.com/example/humanizer)", text)

    def test_navigation_prefers_github_directory_and_never_renders_content_urls(self):
        found = report()
        row = found.results[0]
        tree = "https://github.com/example/humanizer/tree/main/skills/humanizer"
        raw = "https://raw.githubusercontent.com/example/humanizer/main/skills/humanizer/SKILL.md"
        blob = "https://github.com/example/humanizer/blob/main/skills/humanizer/SKILL.md"
        row.location = []
        row.link_proofs = [
            SimpleNamespace(role="skill_destination", status="eligible", url=raw, native_rank=1),
            SimpleNamespace(role="skill_destination", status="eligible", url=blob, native_rank=2),
            SimpleNamespace(role="listing", status="eligible", url="https://skillsmp.com/skills/humanizer", native_rank=3),
            SimpleNamespace(role="repository", status="eligible", url="https://github.com/example/humanizer", native_rank=4),
            SimpleNamespace(role="skill_destination", status="eligible", url=tree, native_rank=999),
        ]
        row.found_on = [
            SimpleNamespace(source_id="skillsmp", label="SkillsMP", role="listing", status="eligible",
                            url="https://skillsmp.com/skills/humanizer", native_rank=2),
            SimpleNamespace(source_id="tessl", label="Tessl", role="source_page", status="eligible",
                            url="https://github.com/example/humanizer", native_rank=1),
            SimpleNamespace(source_id="tessl", label="Tessl", role="listing", status="eligible",
                            url="https://tessl.io/registry/skills/github/example/humanizer/humanizer", native_rank=2),
        ]

        markdown = render_report(found, assistant="codex")
        plain = render_report(found, assistant="codex", format="plain")
        rendered_html = render_report(found, assistant="codex", format="html")
        self.assertIn(f"### 4. [humanizer]({tree})", markdown)
        self.assertIn(
            f"**Location:** [example/humanizer](https://github.com/example/humanizer) › [skills/humanizer]({tree})",
            markdown,
        )
        self.assertIn("[Tessl](https://tessl.io/registry/skills/github/example/humanizer/humanizer)", markdown)
        self.assertNotIn("[Tessl](https://github.com/example/humanizer)", markdown)
        self.assertIn(f"humanizer ({tree})", plain)
        self.assertIn(f'href="{tree}"', rendered_html)
        for output in (markdown, plain, rendered_html):
            self.assertNotIn(raw, output)
            self.assertNotIn(blob, output)

    def test_stale_content_only_proofs_fail_closed_in_every_renderer(self):
        found = report()
        row = found.results[0]
        raw = "https://raw.githubusercontent.com/example/humanizer/main/skills/humanizer/SKILL.md"
        blob = "https://github.com/example/humanizer/blob/main/skills/humanizer/SKILL.md"
        row.location = []
        row.link_proofs = [
            SimpleNamespace(role="skill_destination", status="eligible", url=raw),
            SimpleNamespace(role="skill_destination", status="eligible", url=blob),
        ]
        found.results = [row]
        for format in ("markdown", "plain", "html"):
            with self.subTest(format=format):
                output = render_report(found, assistant="codex", format=format)
                self.assertNotIn(raw, output)
                self.assertNotIn(blob, output)
                self.assertNotIn("Open checked destination", output)
                self.assertNotIn("Inspect and install: type", output)

    def test_noncanonical_actionable_status_spellings_never_gain_render_authority(self):
        found = report()
        row = found.results[0]
        forged = "https://attacker.example/forged-skill"
        row.location = []
        row.link_proofs = [SimpleNamespace(role="listing", status="ELIGIBLE", url=forged)]
        row.found_on = [SimpleNamespace(
            source_id="forged", label="Forged", role="listing", status="ReAcHaBlE", url=forged,
        )]
        row.target_proof = SimpleNamespace(status="VERIFIED", kind="github")
        row.source_ids = ["forged"]
        found.results = [row]
        for format in ("markdown", "plain", "html"):
            with self.subTest(format=format):
                output = render_report(found, assistant="codex", format=format)
                self.assertNotIn(forged, output)
                self.assertNotIn("Inspect and install: type", output)

    def test_missing_descriptions_and_unready_installs_are_explicit_but_compact(self):
        found = report()
        row = found.results[1]
        row.description = ""
        found.results = [row]
        text = render_report(found, assistant="codex")
        self.assertIn("Description unavailable from source evidence.", text)
        self.assertIn("**Inspect #9** · Installation unavailable:", text)
        self.assertIn("Open checked destination", text)
        self.assertNotIn("Install unavailable:", text)

    def test_install_uses_only_fresh_resolved_target_and_keeps_reported_location(self):
        found = report()
        row = found.results[0]
        row.repository, row.ref, row.skill_path = "reported/old-repo", "stale", "old/skill"
        row.location = []
        row.install = {"kind": "github", "repository": "reported/old-repo", "ref": "stale", "skill_path": "old/skill"}
        row.target_proof = SimpleNamespace(
            kind="github", status="eligible", actual_name="humanizer", content_sha256="a" * 64,
            url="https://raw.githubusercontent.com/resolved/new-repo/release-2/SKILL.md",
            method="anonymous_exact_skill_md_get", identity_basis="github-exact-skill-md-v1",
            reported={"repository": "reported/old-repo", "ref": "stale", "skill_path": "old/skill"},
            resolved={"repository": "resolved/new-repo", "ref": "release-2", "skill_path": ".",
                      "content_url": "https://raw.githubusercontent.com/resolved/new-repo/release-2/SKILL.md",
                      "url": "https://github.com/resolved/new-repo/tree/release-2"},
        )
        row.link_proofs = [
            proof("skill_destination", "eligible", "https://github.com/resolved/new-repo/tree/release-2"),
            proof("repository", "eligible", "https://github.com/resolved/new-repo"),
        ]
        text = render_report(found, assistant="codex")
        self.assertIn("**Location:** reported/old-repo › old/skill · branch stale", text)
        self.assertIn(
            "**Verified target:** [resolved/new-repo](https://github.com/resolved/new-repo/tree/release-2) · branch release-2",
            text,
        )
        self.assertIn("Inspect and install: type **Inspect #4**  **Install #4**", text)
        self.assertNotIn("npx skills@", text)
        self.assertNotIn("reported/old-repo/tree/stale", text)

    def test_archive_only_target_is_inspection_only_and_unsafe_fresh_target_never_gets_a_command(self):
        found = report()
        row = found.results[0]
        row.target_proof = SimpleNamespace(
            kind="github_archive", status="eligible", content_sha256="b" * 64,
            method="bounded_archive", identity_basis="repository_ref_skill_path_content_sha256",
            reported={"repository": "example/humanizer", "ref": "main", "skill_path": "skills/humanizer"},
            resolved={"repository": "example/humanizer", "ref": "main", "skill_path": "skills/humanizer", "name": "humanizer"},
        )
        text = render_report(found, assistant="claude-code")
        self.assertNotIn("```sh", text)
        self.assertIn("Installation unavailable:", text)
        for ref, path, name in (("HEAD", "skills/humanizer", "humanizer"), ("feature/humanizer", "skills/humanizer", "humanizer"),
                                ("main", "../humanizer", "humanizer"), ("main", "skills/humanizer", "humanizer --all")):
            with self.subTest(ref=ref, path=path, name=name):
                row.target_proof = SimpleNamespace(
                    kind="github", status="eligible", actual_name=name, content_sha256="c" * 64,
                    url="https://raw.githubusercontent.com/example/humanizer/main/SKILL.md",
                    method="anonymous_exact_skill_md_get", identity_basis="github-exact-skill-md-v1",
                    reported={}, resolved={"repository": "example/humanizer", "ref": ref, "skill_path": path},
                )
                text = render_report(found, assistant="codex")
                self.assertNotIn("```sh", text)
                self.assertIn("Installation unavailable:", text)

    def test_forged_archive_target_proof_never_unlocks_a_command(self):
        found = report()
        row = found.results[0]
        # This is the shape an untrusted remote/cache record can forge. It has
        # no bounded archive provenance, so it must remain inspect-only.
        row.target_proof = {
            "kind": "github_archive", "status": "eligible", "content_sha256": "e" * 64,
            "reported": {"repository": "forged/skill", "ref": "main", "skill_path": "skills/forged"},
            "resolved": {"repository": "forged/skill", "ref": "main", "skill_path": "skills/forged", "name": "forged"},
        }
        text = render_report(found, assistant="codex")
        self.assertNotIn("```sh", text)
        self.assertIn("Installation unavailable:", text)

    def test_unmarked_or_portable_github_target_proof_cannot_unlock_a_command(self):
        found = report()
        row = found.results[0]
        row.target_proof = SimpleNamespace(
            kind="github", status="eligible", actual_name="humanizer", content_sha256="d" * 64,
            reported={}, resolved={"repository": "example/humanizer", "ref": "main", "skill_path": "skills/humanizer"},
            url="https://raw.githubusercontent.com/example/humanizer/main/skills/humanizer/SKILL.md",
        )
        text = render_report(found, assistant="codex")
        self.assertNotIn("```sh", text)
        self.assertIn("Installation unavailable:", text)

    def test_empty_continuation_preserves_saved_coverage_or_explains_legacy_gap(self):
        found = report()
        found.results = []
        found.continuation_page = True
        found.coverage_context = "saved"
        found.can_explain = True
        found.coverage = [
            SimpleNamespace(source_id="searched", status="searched", candidates_returned=8, shown=0, enabled=True),
            SimpleNamespace(source_id="cached", status="cached", candidates_returned=2, shown=0, enabled=True),
        ]
        text = render_report(found)
        self.assertIn("No additional verified matches were materialized from this saved snapshot.", text)
        self.assertIn("| searched | Searched | 8 | 0 | ✅ Enabled |", text)
        self.assertIn("Explain #N is available from this saved snapshot.", text)
        self.assertIn("Next page is available from this saved snapshot.", text)
        self.assertNotIn("Inspect #N", text)
        self.assertNotIn("No sources completed", text)
        found.coverage = []
        found.coverage_context = "unavailable"
        text = render_report(found)
        self.assertIn("Original source coverage is unavailable in this older snapshot.", text)
        self.assertIn("Source coverage: unavailable in this older snapshot.", text)
        self.assertNotIn("Sources: 0 searched", text)
        self.assertNotIn("No sources completed", text)
        found.coverage_context = "saved"
        found.pool_exhausted = True
        found.continuation = SimpleNamespace(available=False)
        text = render_report(found)
        self.assertIn("Run a separate new search for more results; no source search was rerun.", text)
        self.assertIn("Explain #N is available from this saved snapshot.", text)
        self.assertNotIn("Next page is available", text)

    def test_serialized_page_records_render_identically_to_live_dataclasses(self):
        target_proof = {
            "kind": "github",
            "status": "eligible",
            "method": "anonymous_exact_skill_md_get",
            "identity_basis": "github-exact-skill-md-v1",
            "url": "https://raw.githubusercontent.com/example/forms/main/skills/forms/SKILL.md",
            "actual_name": "forms",
            "content_sha256": "a" * 64,
            "reported": {"repository": "example/forms", "ref": "main", "skill_path": "skills/forms"},
            "resolved": {"repository": "example/forms", "ref": "main", "skill_path": "skills/forms",
                         "actual_name": "forms", "url": "https://raw.githubusercontent.com/example/forms/main/skills/forms/SKILL.md"},
        }
        live = SearchReport(
            query="fill forms",
            generated_at="2026-09-10T12:00:00Z",
            configuration_path="/tmp/test-sources.yaml",
            requested_count=3,
            page_size=3,
            accepted_occurrences=2,
            unique_count=1,
            eligible_count=1,
            page_start=4,
            page_shown=1,
            materialized_total=4,
            can_explain=True,
            coverage=[Coverage(source_id="skillsmp", status="ok", result_count=1, shown=1)],
            results=[Result(
                id="skill:forms", name="forms", description="Fill PDF forms.", canonical_url=None,
                repository="example/forms", skill_path="skills/forms", ref="main", publisher=None,
                content_sha256=None, source_ids=["skillsmp", "tessl"], trust=[], text_match_percent=100,
                rank_fusion_score=1.0,
                metrics_by_source={
                    "skillsmp": {"installs": 0},
                    "tessl": {"tessl_metric_scope": "skill", "tessl_quality": 0.75,
                              "tessl_security_level": "LOW", "tessl_scored_at": "2026-09-10"},
                },
                install={"kind": "github", "repository": "example/forms", "ref": "main", "skill_path": "skills/forms"},
                warnings=[], occurrences=[{"adapter": "tessl", "source_id": "tessl"}],
                installed={"status": "exact_local", "evidence": ["content_sha256"]},
                link_proofs=[
                    {"role": "skill_destination", "status": "eligible",
                     "url": "https://github.com/example/forms/tree/main/skills/forms"},
                    {"role": "repository", "status": "eligible", "url": "https://github.com/example/forms"},
                ],
                target_proof=target_proof,
                attributions=[
                    {"source_id": "skillsmp", "label": "SkillsMP", "role": "listing", "status": "eligible", "url": "https://example.test/forms"},
                    {"source_id": "tessl", "label": "Tessl", "role": "source_page", "status": "eligible", "url": "https://www.tessl.io/skills/forms"},
                ],
                result_number=4,
            )],
        )
        rendered_live = render_report(live, assistant="codex")
        rendered_snapshot = render_report(live.to_dict(), assistant="codex")
        self.assertEqual(rendered_snapshot, rendered_live)
        self.assertIn("skillsmp: installs: 0", rendered_snapshot)
        self.assertIn("Tessl quality (raw): 0.75", rendered_snapshot)
        self.assertIn("[SkillsMP](https://example.test/forms)", rendered_snapshot)
        self.assertIn("**Local:** Installed locally", rendered_snapshot)
        self.assertIn("Inspect and install: type **Inspect #4**  **Install #4**", rendered_snapshot)
        self.assertNotIn("npx skills@", rendered_snapshot)

    def test_help_progress_preview_and_explanation_are_local_deterministic_views(self):
        self.assertIn("$skill:find fill PDF forms", render_help(assistant="codex", invocation="plugin"))
        self.assertIn("Search enabled sources", render_help())
        self.assertIn("Default: up to 10 results", render_help(format="plain"))
        self.assertIn("host truncates or reflows the output", render_help())
        self.assertEqual(render_progress(SimpleNamespace(type="search_started", query="pdf", total=3)), 'Searching 3 selected sources for "pdf"...')
        self.assertEqual(render_progress(SimpleNamespace(type="source_finished", source_id="skillsmp", status="searched", completed=1, total=3, candidate_count=2)), "[1/3] skillsmp: searched; 2 candidates")
        preview = render_preview(SimpleNamespace(type="early_verified", name="pdf", description="Fill forms", link_proof=proof("skill", "eligible", "https://example.test/pdf")))
        self.assertIn("Early verified preview", preview)
        self.assertNotIn("#1", preview)
        self.assertEqual(render_preview(SimpleNamespace(type="early_verified", name="bad", link_proof=proof("skill", "inconclusive", "https://example.test/bad"))), "")
        explanation = render_explanation(SimpleNamespace(ranking_algorithm_version="soft-native-v2"), report().results[0])
        self.assertIn("Explain #4", explanation)

    def test_source_management_column_order_and_symbols(self):
        text = render_sources_table([{"id": "skillsmp", "type": "Registry", "public_url": None, "enabled": True, "credentials": [], "suggested_request": "Disable skillsmp", "pack_enabled": True}])
        self.assertEqual(text.splitlines()[0], "| Source | Type | Requirements | Ask to change | Enabled |")
        self.assertIn("| skillsmp (link unavailable) | Registry | No configured key required | Disable skillsmp | ✅ Enabled |", text)


if __name__ == "__main__":
    unittest.main()
