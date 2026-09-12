from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.repositories import _repo_candidate
from universal_skill_finder.adapters.base import AdapterContext
from universal_skill_finder.adapters.registries import PolySkillAdapter
from universal_skill_finder.cli import _snapshot_destinations
from universal_skill_finder.cache import Cache
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate
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


class _PayloadHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


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
        })
