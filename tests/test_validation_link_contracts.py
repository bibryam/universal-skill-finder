from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.repositories import _repo_candidate
from universal_skill_finder.adapters.base import AdapterContext
from universal_skill_finder.adapters.registries import PolySkillAdapter
from universal_skill_finder.adapters.tessl import API_ORIGIN, SEARCH_ENDPOINT, TesslAdapter
from universal_skill_finder.cli import _snapshot_destinations
from universal_skill_finder.cache import Cache
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.http import HttpClient, HttpResponse
from universal_skill_finder.models import Candidate, Coverage, SearchReport
from universal_skill_finder.presentation import render_report
from universal_skill_finder.runtime import Deadline, NetworkPolicy
from universal_skill_finder.validation import (
    AnonymousResponse,
    reviewed_destination,
    validate_destination,
)


class _Lease:
    status = "ok"

    def release(self) -> None:
        pass


class _Budget:
    def reserve(self, *_args):
        return _Lease()


class _Permits:
    def acquire(self, *_args):
        return _Lease()


class _Transport:
    def __init__(self, *responses: AnonymousResponse):
        self.responses = list(responses)
        self.calls: list[str] = []

    def request(self, _method, url, **_kwargs):
        self.calls.append(url)
        return self.responses.pop(0)


class _UnusedTransport:
    def request(self, *_args, **_kwargs):
        raise AssertionError("expired validation must not make a request")


class _PayloadHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


class _TesslPayloadHttp(HttpClient):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def request(self, _method, _url, **_kwargs):
        return HttpResponse(json.dumps(self.payload).encode("utf-8"), 200, {}, SEARCH_ENDPOINT)


class _TesslSeamTransport:
    repository = "fernandezbaptiste/claude-code-skills"
    listing = (
        "https://tessl.io/registry/skills/github/"
        "fernandezbaptiste/claude-code-skills/pdf-creator"
    )

    def __init__(self, *, listing_status: int):
        self.listing_status = listing_status
        self.calls: list[str] = []

    def request(self, _method, url, **_kwargs):
        self.calls.append(url)
        if url == f"https://api.github.com/repos/{self.repository}":
            body = json.dumps({"full_name": self.repository, "default_branch": "main"}).encode("utf-8")
            return AnonymousResponse(200, {}, body, "8.8.8.8")
        if url == f"https://raw.githubusercontent.com/{self.repository}/main/pdf-creator/SKILL.md":
            return AnonymousResponse(
                200, {}, b"---\nname: pdf-creator\n---\nCreate professional PDFs.", "8.8.8.8",
            )
        if url in {
            f"https://github.com/{self.repository}",
            f"https://github.com/{self.repository}/tree/main/pdf-creator",
        }:
            body = (
                '<meta name="octolytics-dimension-repository_nwo" '
                f'content="{self.repository}">'
            ).encode("utf-8")
            return AnonymousResponse(200, {}, body, "8.8.8.8")
        if url == self.listing:
            if self.listing_status == 404:
                return AnonymousResponse(404, {}, b"", "8.8.8.8")
            body = (
                f'<meta property="og:url" content="{self.listing}">'
                '<h1>pdf-creator</h1>'
                f'<a href="https://github.com/{self.repository}">Repository</a>'
            ).encode("utf-8")
            return AnonymousResponse(200, {"content-type": "text/html"}, body, "8.8.8.8")
        raise AssertionError(f"unexpected validation URL: {url}")


class _RouteTransport:
    def __init__(self):
        self.calls: list[str] = []

    def request(self, _method, url, **_kwargs):
        self.calls.append(url)
        bodies = {
            "https://clawhub.ai/artur-zhdan/skills/humanize": (
                "<h1>Humanize</h1><code>openclaw skills install @artur-zhdan/humanize</code>"
            ),
            "https://tessl.io/registry/acme/pdf-tools/1.2.3": (
                '<link rel="canonical" href="https://tessl.io/registry/acme/pdf-tools/1.2.3">'
                "<h1>acme/pdf-tools</h1><p>1.2.3</p>"
            ),
            "https://github.com/acme/repository-skill": (
                '<meta name="octolytics-dimension-repository_nwo" content="acme/repository-skill">'
            ),
            "https://github.com/acme/repository-skill/tree/main": (
                '<meta name="octolytics-dimension-repository_nwo" content="acme/repository-skill">'
            ),
            "https://api.github.com/repos/acme/repository-skill": (
                '{"full_name":"acme/repository-skill","default_branch":"main"}'
            ),
            "https://raw.githubusercontent.com/acme/repository-skill/main/SKILL.md": (
                "---\nname: Repository skill\n---\nRepository-backed skill."
            ),
        }
        return AnonymousResponse(
            200, {"content-type": "text/html"}, bodies[url].encode("utf-8"), "8.8.8.8",
        )


def _resolver(_host: str, _port: int):
    return ["8.8.8.8"]


def _proof(destination, body: str, *, content_type: str = "text/html"):
    transport = _Transport(AnonymousResponse(
        200, {"content-type": content_type}, body.encode("utf-8"), "8.8.8.8",
    ))
    proof = validate_destination(
        destination, transport=transport, resolver=_resolver, budget=_Budget(), permits=_Permits(),
        deadline=time.monotonic() + 2,
    )
    return proof, transport


class ProviderLinkContractTests(unittest.TestCase):
    def test_expired_first_page_validation_reports_the_deferred_tail(self) -> None:
        candidates = [
            Candidate(
                f"video-{index}", f"Video editor {index}", "Edit video", "fixture", "registry", "skills-sh",
                native_rank=index + 1,
            )
            for index in range(52)
        ]
        with TemporaryDirectory() as temporary:
            config = EffectiveConfig(
                settings={}, packs=[],
                sources=[{"id": "fixture", "adapter": "skills-sh", "kind": "registry"}],
                overlay_path=Path(temporary) / "sources.json", overlay={},
            )
            finder = UniversalSkillFinder(
                config, cache=Cache(Path(temporary) / "cache"),
                validation_transport=_UnusedTransport(), validation_resolver=lambda *_args: ("8.8.8.8",),
            )
            results = finder._merge(candidates, "video editor")
            now = time.monotonic()
            expired = Deadline(now - 9, now - 8, now - 1, 8.0)
            run = finder._validate_ranked_pool(
                results, expired, NetworkPolicy.for_mode(), validation_deadline=expired,
            )

        self.assertEqual(run.stop_reason, "deadline_reached")
        self.assertEqual((run.checked_count, run.deferred_count), (0, 52))
        self.assertEqual(
            run.stopped_reason,
            "8-second destination-verification deadline reached after final validation completed for 0 of 52 candidates",
        )

    def test_exhausted_request_budget_stops_before_burning_the_ranked_pool(self) -> None:
        candidates = [
            Candidate(
                f"video-{index}", f"Video editor {index}", "Edit video", "fixture", "registry", "skills-sh",
                native_rank=index + 1,
            )
            for index in range(5)
        ]
        with TemporaryDirectory() as temporary:
            config = EffectiveConfig(
                settings={}, packs=[],
                sources=[{"id": "fixture", "adapter": "skills-sh", "kind": "registry"}],
                overlay_path=Path(temporary) / "sources.json", overlay={},
            )
            finder = UniversalSkillFinder(
                config, cache=Cache(Path(temporary) / "cache"),
                validation_transport=_UnusedTransport(), validation_resolver=lambda *_args: ("8.8.8.8",),
            )
            results = finder._merge(candidates, "video editor")
            policy = NetworkPolicy(1.0, 2.0, 1.0, validation_requests=0)
            run = finder._validate_ranked_pool(results, Deadline.start(policy), policy)

        self.assertEqual(run.stop_reason, "request_budget_reached")
        self.assertEqual((run.checked_count, run.deferred_count), (0, 5))
        self.assertIn("0 of 5", run.stopped_reason)

    def test_eligible_identity_is_not_deferred_by_a_later_bounded_proof(self) -> None:
        candidate = Candidate(
            "video", "Video editor", "Edit video", "fixture", "registry", "skills-sh",
        )
        with TemporaryDirectory() as temporary:
            config = EffectiveConfig(
                settings={}, packs=[],
                sources=[{"id": "fixture", "adapter": "skills-sh", "kind": "registry"}],
                overlay_path=Path(temporary) / "sources.json", overlay={},
            )
            finder = UniversalSkillFinder(
                config, cache=Cache(Path(temporary) / "cache"),
                validation_transport=_UnusedTransport(), validation_resolver=lambda *_args: ("8.8.8.8",),
            )
            results = finder._merge([candidate], "video editor")
            outcome = {
                "status": "eligible",
                "link_proofs": [
                    {"role": "listing", "url": "https://skills.sh/example/video", "status": "eligible"},
                    {"role": "source_page", "url": "https://skills.sh/example", "status": "not_checked",
                     "detail": "validation deadline exhausted"},
                ],
            }
            with patch("universal_skill_finder.federation.validate_record", return_value=outcome):
                run = finder._validate_ranked_pool(
                    results, Deadline.start(NetworkPolicy.for_mode()), NetworkPolicy.for_mode(),
                    requested_size=2,
                )

        self.assertEqual(run.stop_reason, "pool_exhausted")
        self.assertEqual((run.checked_count, run.deferred_count), (1, 0))
        self.assertIsNone(run.stopped_reason)

    def test_archive_content_proof_never_authorizes_github_web_link(self) -> None:
        source = {"id": "repo", "kind": "repository", "adapter": "github-repo", "repository": "owner/repo", "ref": "main"}
        candidate = _repo_candidate(source, "skills/example/SKILL.md", "---\nname: Example\n---\nUseful.", "a" * 64)
        self.assertEqual(candidate.target_proof["status"], "eligible")
        self.assertEqual(candidate.link_proofs[0]["status"], "not_checked")
        self.assertEqual(candidate.link_proofs[0]["method"], "bounded_archive")
        self.assertIn("anonymous GET", candidate.link_proofs[0]["detail"])

    def test_skills_sh_html_requires_matching_breadcrumb_and_h1(self) -> None:
        destination = reviewed_destination(
            "one", role="listing", url="https://skills.sh/blader/humanizer/humanizer",
            adapter="skills-sh", expected_identity={"id": "ignored-by-html"},
        )
        valid, _ = _proof(destination, "<nav>blader/humanizer</nav><h1>humanizer</h1>")
        self.assertEqual(valid.status, "eligible")
        generic, _ = _proof(destination, "<h1>humanizer</h1>")
        self.assertEqual(generic.status, "inconclusive")

    def test_skillsmp_html_contract_matches_public_creator_route_not_json_endpoint(self) -> None:
        destination = reviewed_destination(
            "one", role="listing", url="https://skillsmp.com/creators/blader/humanizer/skill",
            adapter="skillsmp", expected_identity={"id": "github.com/blader/humanizer"},
        )
        valid, _ = _proof(destination, "<h1>humanizer</h1><p>Repository blader/humanizer</p>")
        self.assertEqual(valid.status, "eligible")
        wrong, _ = _proof(destination, "<h1>humanizer</h1><p>Repository attacker/repo</p>")
        self.assertEqual(wrong.status, "inconclusive")

    def test_clawhub_html_requires_typed_install_identity_not_status_200(self) -> None:
        destination = reviewed_destination(
            "one", role="listing", url="https://clawhub.ai/artur-zhdan/skills/humanize",
            adapter="clawhub", expected_identity=None,
        )
        self.assertIsNotNone(destination.profile)
        valid, _ = _proof(destination, "<h1>Humanize</h1><code>openclaw skills install @artur-zhdan/humanize</code>")
        self.assertEqual(valid.status, "eligible")
        wrong, _ = _proof(destination, "<h1>Humanize</h1><code>openclaw skills install @attacker/humanize</code>")
        self.assertEqual(wrong.status, "inconclusive")

    def test_skillhub_accepts_rendered_identity_but_not_an_html_shell(self) -> None:
        destination = reviewed_destination(
            "one", role="listing", url="https://skills.palebluedot.live/skill/openclaw/skills/humanize",
            adapter="skillhub-public", expected_identity={"id": "openclaw/skills/humanize"},
        )
        valid, _ = _proof(destination, "<h1>Humanize</h1><p>openclaw/skills/humanize</p>")
        self.assertEqual(valid.status, "eligible")
        shell, _ = _proof(destination, "<h1>Humanize</h1>")
        self.assertEqual(shell.status, "inconclusive")

    def test_polyskill_uses_the_reviewed_singular_namespaced_route_and_exact_identity(self) -> None:
        destination = reviewed_destination(
            "one", role="listing", url="https://polyskill.ai/skill/@anthropic/pdf",
            adapter="polyskill", expected_identity={"name": "@anthropic/pdf"},
        )
        self.assertIsNotNone(destination.profile)
        valid, _ = _proof(destination, (
            '<link rel="canonical" href="https://polyskill.ai/skill/@anthropic/pdf">'
            '<meta property="og:url" content="https://polyskill.ai/skill/@anthropic/pdf"><h1>@anthropic/pdf</h1>'
        ))
        self.assertEqual(valid.status, "eligible")
        wrong, _ = _proof(destination, (
            '<link rel="canonical" href="https://polyskill.ai/skill/@anthropic/pdf"><h1>@attacker/pdf</h1>'
        ))
        self.assertEqual(wrong.status, "inconclusive")
        plural = reviewed_destination(
            "one", role="listing", url="https://polyskill.ai/skills/@anthropic%2Fpdf",
            adapter="polyskill", expected_identity={"name": "@anthropic/pdf"},
        )
        self.assertIsNone(plural.profile)

    def test_polyskill_adapter_preserves_the_reviewed_singular_listing_route(self) -> None:
        http = _PayloadHttp({"skills": [{
            "id": "public-id", "name": "@anthropic/pdf", "manifest": {
                "name": "@anthropic/pdf", "description": "PDF tools", "author": "anthropic",
            },
        }]})
        with TemporaryDirectory() as temporary:
            context = AdapterContext(http=http, cache=Cache(Path(temporary) / "cache"), settings={})
            rows = PolySkillAdapter().search(
                {"id": "polyskill", "kind": "registry", "adapter": "polyskill", "base_url": "https://polyskill.ai"},
                "pdf", 3, context,
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].listing_url, rows[0].listing_role, rows[0].listing_derivation), (
            "https://polyskill.ai/skill/@anthropic/pdf", "listing", "connector_reviewed",
        ))
        self.assertEqual(rows[0].source_evidence["expected_identity"], {"name": "@anthropic/pdf"})

    def test_snapshot_revalidation_rebuilds_route_derived_provider_destinations(self) -> None:
        records = {
            "claw": {
                "occurrences": [{
                    "adapter": "clawhub", "listing_url": "https://clawhub.ai/artur-zhdan/skills/humanize",
                    "listing_role": "listing", "source_evidence": {},
                }],
            },
            "bundle": {
                "occurrences": [{
                    "adapter": "tessl", "listing_url": "https://tessl.io/registry/acme/pdf-tools/1.2.3",
                    "listing_role": "bundle_listing", "source_evidence": {},
                }],
            },
            "repository": {
                "occurrences": [{
                    "adapter": "tessl", "listing_url": "https://github.com/acme/repository-skill",
                    "listing_role": "repository", "source_evidence": {},
                }],
            },
        }
        rebuilt = {identity: _snapshot_destinations(identity, record) for identity, record in records.items()}
        self.assertEqual(
            {identity: [(item.role, item.url, item.profile is not None) for item in destinations]
             for identity, destinations in rebuilt.items()},
            {
                "claw": [("listing", "https://clawhub.ai/artur-zhdan/skills/humanize", True)],
                "bundle": [("bundle_listing", "https://tessl.io/registry/acme/pdf-tools/1.2.3", True)],
                "repository": [("repository", "https://github.com/acme/repository-skill", True)],
            },
        )

    def test_tessl_bundle_requires_exact_registry_canonical_package_and_version(self) -> None:
        url = "https://tessl.io/registry/acme/humanize/1.2.3"
        destination = reviewed_destination("one", role="bundle_listing", url=url, adapter="tessl", expected_identity=None)
        self.assertIsNotNone(destination.profile)
        valid, _ = _proof(destination, f'<link rel="canonical" href="{url}"><h1>acme/humanize</h1><p>1.2.3</p>')
        self.assertEqual(valid.status, "eligible")
        wrong, _ = _proof(destination, '<link rel="canonical" href="https://tessl.io/registry/acme/humanize/9.9.9"><h1>acme/humanize</h1><p>1.2.3</p>')
        self.assertEqual(wrong.status, "inconclusive")

    def test_tessl_individual_listing_requires_exact_page_repository_and_skill(self) -> None:
        url = "https://tessl.io/registry/skills/github/acme/toolbox/pdf-creator"
        destination = reviewed_destination(
            "one", role="listing", url=url, adapter="tessl", expected_identity=None,
        )
        self.assertIsNotNone(destination.profile)
        self.assertEqual(destination.expected_identity, {
            "repository": "acme/toolbox", "name": "pdf-creator", "url": url,
        })
        valid, _ = _proof(destination, (
            f'<meta property="og:url" content="{url}"><h1>pdf-creator</h1>'
            '<a href="https://github.com/acme/toolbox">Repository</a>'
        ))
        self.assertEqual(valid.status, "eligible")
        wrong, _ = _proof(destination, (
            f'<link rel="canonical" href="{url}"><h1>pdf-creator</h1>'
            '<a href="https://github.com/attacker/toolbox">Repository</a>'
        ))
        self.assertNotEqual(wrong.status, "eligible")

        missing = validate_destination(
            destination,
            transport=_Transport(AnonymousResponse(404, {}, b"", "8.8.8.8")),
            resolver=_resolver, budget=_Budget(), permits=_Permits(),
            deadline=time.monotonic() + 2,
        )
        self.assertEqual(missing.status, "unavailable")

    def test_tessl_candidate_renders_github_navigation_and_only_a_live_native_listing(self) -> None:
        repository = _TesslSeamTransport.repository
        listing = _TesslSeamTransport.listing
        tree = f"https://github.com/{repository}/tree/main/pdf-creator"
        root = f"https://github.com/{repository}"
        raw = f"https://raw.githubusercontent.com/{repository}/main/pdf-creator/SKILL.md"
        payload = {
            "data": [{
                "id": "019f948e-9997-7013-8ca1-000000000001",
                "type": "skill",
                "attributes": {
                    "name": "pdf-creator",
                    "description": "Create professional PDFs.",
                    "sourceUrl": root,
                    "path": "pdf-creator/SKILL.md",
                    "isPrivate": False,
                    "validationPassed": None,
                    "scores": None,
                },
            }],
            "meta": {"pagination": {"total": 1, "pages": 1, "number": 1, "size": 1}},
        }
        source = {
            "id": "tessl", "kind": "registry", "adapter": "tessl",
            "base_url": API_ORIGIN, "trust": "unverified",
        }
        policy = NetworkPolicy.for_mode()

        for listing_status in (200, 404):
            with self.subTest(listing_status=listing_status), TemporaryDirectory() as temporary:
                cache = Cache(Path(temporary) / "cache")
                context = AdapterContext(_TesslPayloadHttp(payload), cache, {})
                candidates = TesslAdapter().search(source, "pdf creator", 1, context)
                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0].listing_url, listing)

                config = EffectiveConfig(
                    settings={}, packs=[], sources=[source],
                    overlay_path=Path(temporary) / "sources.json", overlay={},
                )
                transport = _TesslSeamTransport(listing_status=listing_status)
                finder = UniversalSkillFinder(
                    config, cache=cache, validation_transport=transport,
                    validation_resolver=_resolver,
                )
                results = finder._merge(candidates, "pdf creator")
                finder._validate_ranked_pool(
                    results, Deadline.start(policy), policy, requested_size=1,
                )
                result = results[0]
                result.validation_status = finder._validation_status(result)
                result.result_number = 1
                report = SearchReport(
                    query="pdf creator", results=[result],
                    coverage=[Coverage("tessl", "ok", 1)],
                    generated_at="2026-09-12T00:00:00+00:00",
                    configuration_path=str(Path(temporary) / "sources.json"),
                    accepted_occurrences=1, unique_count=1, eligible_count=1,
                    page_shown=1, materialized_total=1,
                )
                rendered = {
                    format: render_report(report, assistant="codex", format=format)
                    for format in ("markdown", "plain", "html")
                }

                self.assertEqual(result.validation_status, "eligible")
                self.assertEqual(result.target_proof["url"], raw)
                self.assertFalse(any(proof.get("url") == raw for proof in result.link_proofs))
                self.assertIn(f"### 1. [pdf-creator]({tree})", rendered["markdown"])
                self.assertIn(f"[{repository}]({root})", rendered["markdown"])
                self.assertIn(f"pdf-creator ({tree})", rendered["plain"])
                self.assertIn(f"{repository} ({root})", rendered["plain"])
                self.assertIn(f'href="{tree}"', rendered["html"])
                self.assertIn(f'href="{root}"', rendered["html"])
                for output in rendered.values():
                    self.assertNotIn(raw, output)
                    self.assertNotIn("/blob/main/pdf-creator/SKILL.md", output)
                if listing_status == 200:
                    self.assertTrue(all(listing in output for output in rendered.values()))
                    self.assertIn(
                        ("tessl", listing),
                        {(entry["source_id"], entry["url"]) for entry in result.attributions},
                    )
                else:
                    self.assertTrue(all(listing not in output for output in rendered.values()))
                    self.assertIn("tessl (listing not verified)", rendered["markdown"])

    def test_tessl_repository_uses_github_metadata_contract(self) -> None:
        destination = reviewed_destination(
            "one", role="repository", url="https://github.com/acme/humanize", adapter="tessl", expected_identity=None,
        )
        valid, transport = _proof(destination, '<meta name="octolytics-dimension-repository_nwo" content="acme/humanize">')
        self.assertEqual(valid.status, "eligible")
        self.assertEqual(transport.calls, ["https://github.com/acme/humanize"])
        wrong, _ = _proof(destination, '<meta name="octolytics-dimension-repository_nwo" content="attacker/humanize">')
        self.assertEqual(wrong.status, "unavailable")

    def test_non_html_public_pages_do_not_become_eligible_from_generic_200(self) -> None:
        destination = reviewed_destination(
            "one", role="listing", url="https://clawhub.ai/artur-zhdan/skills/humanize",
            adapter="clawhub", expected_identity=None,
        )
        proof, _ = _proof(destination, '{"ok": true}', content_type="application/json")
        self.assertEqual(proof.status, "inconclusive")

    def test_federation_materializes_clawhub_and_tessl_reviewed_destinations(self) -> None:
        candidates = [
            Candidate(
                "artur-zhdan/humanize", "Humanize", "Humanize prose", "clawhub", "registry", "clawhub",
                native_rank=1, slug="humanize", publisher="artur-zhdan",
                listing_url="https://clawhub.ai/artur-zhdan/skills/humanize", listing_role="listing",
                listing_derivation="connector_reviewed",
            ),
            Candidate(
                "bundle-id", "PDF tools bundle", "PDF tools", "tessl", "registry", "tessl",
                native_rank=1, listing_url="https://tessl.io/registry/acme/pdf-tools/1.2.3",
                listing_role="bundle_listing", listing_derivation="source_provided",
            ),
            Candidate(
                "repository-id", "Repository skill", "Repository-backed skill", "tessl", "registry", "tessl",
                native_rank=2, repository="acme/repository-skill",
                listing_url="https://github.com/acme/repository-skill", listing_role="repository",
                listing_derivation="source_provided",
            ),
        ]
        sources = [
            {"id": "clawhub", "adapter": "clawhub", "kind": "registry"},
            {"id": "tessl", "adapter": "tessl", "kind": "registry"},
        ]
        policy = NetworkPolicy.for_mode()
        with TemporaryDirectory() as temporary:
            config = EffectiveConfig(
                settings={}, packs=[], sources=sources,
                overlay_path=Path(temporary) / "sources.json", overlay={},
            )
            transport = _RouteTransport()
            finder = UniversalSkillFinder(
                config, cache=Cache(Path(temporary) / "cache"), validation_transport=transport,
                validation_resolver=_resolver,
            )
            results = finder._merge(candidates, "skills")
            finder._validate_ranked_pool(results, Deadline.start(policy), policy)
            for result in results:
                result.validation_status = finder._validation_status(result)

        self.assertEqual({result.validation_status for result in results}, {"eligible"})
        self.assertEqual(set(transport.calls), {
            "https://clawhub.ai/artur-zhdan/skills/humanize",
            "https://tessl.io/registry/acme/pdf-tools/1.2.3",
            "https://api.github.com/repos/acme/repository-skill",
            "https://raw.githubusercontent.com/acme/repository-skill/main/SKILL.md",
            "https://github.com/acme/repository-skill/tree/main",
            "https://github.com/acme/repository-skill",
        })
