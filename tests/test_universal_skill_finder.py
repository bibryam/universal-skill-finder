from __future__ import annotations

import io
import json
import os
import sys
import tarfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import Request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "skills" / "find" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from universal_skill_finder.adapters.base import AdapterContext, SourceUnavailable
from universal_skill_finder.adapters.registries import (
    ClawHubAdapter,
    HttpJsonAdapter,
    PolySkillAdapter,
    SkillHubPublicAdapter,
    SkillsMpAdapter,
    SkillsShAdapter,
)
from universal_skill_finder.adapters.repositories import GitHubRepoAdapter
from universal_skill_finder.cache import Cache
from universal_skill_finder.cli import main as cli_main
from universal_skill_finder.config import (
    ConfigurationError,
    EffectiveConfig,
    add_github_repository,
    default_config_path,
    import_source_pack,
    load_config,
    remove_custom_pack,
    set_pack_enabled,
    set_source_enabled,
)
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.http import FinderHttpError, SafeRedirectHandler
from universal_skill_finder.models import Candidate
from universal_skill_finder.validation import AnonymousResponse


class FakeHttp:
    def __init__(self, *, payload=None, archive: bytes | None = None):
        self.payload = payload
        self.archive = archive
        self.calls: list[tuple[str, str, dict]] = []

    def get_json(self, url, *, params=None, headers=None):
        self.calls.append(("GET", url, {"params": params or {}, "headers": headers or {}}))
        return self.payload

    def post_json(self, url, *, body=None, headers=None):
        self.calls.append(("POST", url, {"body": body or {}, "headers": headers or {}}))
        return self.payload

    def get_bytes(self, url, *, max_bytes=None):
        self.calls.append(("GET_BYTES", url, {"max_bytes": max_bytes}))
        if self.archive is None:
            raise AssertionError("unexpected archive request")
        return self.archive


class FixtureValidationTransport:
    """Affirm exact synthetic GitHub destinations below the real validator."""

    def request(self, _method, url, **_kwargs):
        parts = [part for part in url.split("github.com/", 1)[-1].split("/") if part]
        repository = "/".join(parts[:2])
        body = (
            '<html><head><meta name="octolytics-dimension-repository_nwo" '
            f'content="{repository}"></head></html>'
        ).encode()
        return AnonymousResponse(200, body=body, connection_address="8.8.8.8")


def fixture_finder(config: EffectiveConfig, cache: Cache, *, http=None) -> UniversalSkillFinder:
    return UniversalSkillFinder(
        config, cache=cache, http=http or FakeHttp(),
        validation_transport=FixtureValidationTransport(),
        validation_resolver=lambda _host, _port: ("8.8.8.8",),
    )


def adapter_context(root: Path, http: FakeHttp, **settings) -> AdapterContext:
    defaults = {
        "max_archive_bytes": 1024 * 1024,
        "max_archive_members": 100,
        "max_archive_uncompressed_bytes": 1024 * 1024,
        "max_skill_file_bytes": 64 * 1024,
        "max_skills_per_repository": 100,
        "repository_cache_ttl_seconds": 300,
    }
    defaults.update(settings)
    return AdapterContext(http=http, cache=Cache(root / "cache"), settings=defaults)


def registry_source(source_id: str, adapter: str, **extra):
    return {
        "id": source_id,
        "kind": "registry",
        "adapter": adapter,
        "enabled": True,
        "effective_enabled": True,
        "base_url": "https://example.test",
        "trust": "community-index",
        **extra,
    }


def tar_gz(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body in entries:
            info = tarfile.TarInfo(f"repository-main/{name}")
            info.size = len(body)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def skill_markdown(name: str, description: str) -> bytes:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n".encode()


class ConfigTests(unittest.TestCase):
    def test_packaged_default_config_is_valid(self):
        packaged = json.loads(default_config_path().read_text(encoding="utf-8"))
        self.assertEqual(packaged["schema_version"], 1)
        self.assertEqual(len(packaged["sources"]), 13)

    def test_invalid_boolean_and_unknown_override_are_rejected(self):
        with TemporaryDirectory() as temp:
            overlay = Path(temp) / "sources.json"
            overlay.write_text(json.dumps({
                "schema_version": 1,
                "source_overrides": {"skills-sh": {"enabled": "false"}},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "must be boolean"):
                load_config(str(overlay))

            overlay.write_text(json.dumps({
                "schema_version": 1,
                "source_overrides": {"missing-source": {"enabled": False}},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unknown source id"):
                load_config(str(overlay))

    def test_overlay_source_and_pack_precedence(self):
        with TemporaryDirectory() as temp:
            overlay = Path(temp) / "config" / "sources.json"
            config = load_config(str(overlay))
            added = add_github_repository(
                config,
                "acme/skill-box",
                source_id="acme-skill-box",
                ref="main",
                include=None,
                exclude=None,
                pack_id="official-repositories",
            )
            self.assertEqual(added["include"], ["**/SKILL.md"])

            reloaded = load_config(str(overlay))
            self.assertTrue(reloaded.source("acme-skill-box")["effective_enabled"])
            set_pack_enabled(reloaded, "official-repositories", False)

            reloaded = load_config(str(overlay))
            self.assertFalse(reloaded.source("acme-skill-box")["effective_enabled"])
            set_source_enabled(reloaded, "acme-skill-box", True)
            reloaded = load_config(str(overlay))
            self.assertTrue(reloaded.source("acme-skill-box")["enabled"])
            self.assertFalse(reloaded.source("acme-skill-box")["effective_enabled"])
            if os.name == "posix":
                self.assertEqual(overlay.stat().st_mode & 0o777, 0o600)

    def test_source_pack_import_disable_and_remove_lifecycle(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            overlay = root / "sources.json"
            pack_path = root / "pack.json"
            pack_path.write_text(json.dumps({
                "schema_version": 1,
                "pack": {"id": "team-catalogs", "description": "Team repositories"},
                "sources": [{
                    "id": "team-skills",
                    "kind": "repository",
                    "adapter": "github-repo",
                    "repository": "acme/team-skills",
                    "ref": "main",
                    "include": ["**/SKILL.md"],
                    "exclude": [],
                }],
            }), encoding="utf-8")

            config = load_config(str(overlay))
            imported_pack, imported_sources = import_source_pack(config, str(pack_path))
            self.assertEqual(imported_pack["id"], "team-catalogs")
            self.assertEqual([source["id"] for source in imported_sources], ["team-skills"])

            reloaded = load_config(str(overlay))
            self.assertEqual(reloaded.pack("team-catalogs")["origin"], "user")
            self.assertTrue(reloaded.source("team-skills")["effective_enabled"])
            set_pack_enabled(reloaded, "team-catalogs", False)
            reloaded = load_config(str(overlay))
            self.assertFalse(reloaded.source("team-skills")["effective_enabled"])
            self.assertEqual(remove_custom_pack(reloaded, "team-catalogs"), 1)
            reloaded = load_config(str(overlay))
            self.assertIsNone(reloaded.pack("team-catalogs"))
            self.assertIsNone(reloaded.source("team-skills"))

    def test_source_pack_rejects_unknown_executable_adapter(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            pack_path = root / "unsafe.json"
            pack_path.write_text(json.dumps({
                "schema_version": 1,
                "pack": {"id": "unsafe-pack"},
                "sources": [{
                    "id": "runs-shell",
                    "kind": "registry",
                    "adapter": "shell-command",
                    "base_url": "https://example.test",
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unsupported adapter"):
                import_source_pack(load_config(str(root / "overlay.json")), str(pack_path))


class RepositoryAdapterTests(unittest.TestCase):
    def test_indexes_root_and_nested_skills_and_honors_excludes(self):
        archive = tar_gz([
            ("SKILL.md", skill_markdown("root-helper", "Root helper")),
            ("skills/pdf/SKILL.md", skill_markdown("pdf-tools", "Read PDF files")),
            ("private/secret/SKILL.md", skill_markdown("secret", "Private helper")),
            ("README.md", b"not a skill"),
        ])
        source = {
            "id": "repo",
            "kind": "repository",
            "adapter": "github-repo",
            "repository": "acme/skills",
            "ref": "main",
            "include": ["**/SKILL.md"],
            "exclude": ["private/**"],
            "trust": "user-configured",
        }
        with TemporaryDirectory() as temp:
            http = FakeHttp(archive=archive)
            catalog, cache_age = GitHubRepoAdapter().catalog(source, adapter_context(Path(temp), http))
            self.assertIsNone(cache_age)
            self.assertEqual({item.name for item in catalog}, {"root-helper", "pdf-tools"})
            self.assertEqual({item.skill_path for item in catalog}, {".", "skills/pdf"})
        self.assertEqual([call[1] for call in http.calls], [
            "https://codeload.github.com/acme/skills/tar.gz/main",
        ])

    def test_archive_member_limit_is_reported(self):
        archive = tar_gz([
            ("one/SKILL.md", skill_markdown("one", "One")),
            ("two/SKILL.md", skill_markdown("two", "Two")),
        ])
        source = {
            "id": "repo",
            "kind": "repository",
            "adapter": "github-repo",
            "repository": "acme/skills",
            "ref": "main",
            "include": ["**/SKILL.md"],
            "exclude": [],
        }
        with TemporaryDirectory() as temp:
            context = adapter_context(Path(temp), FakeHttp(archive=archive), max_archive_members=1)
            with self.assertRaises(SourceUnavailable) as raised:
                GitHubRepoAdapter().catalog(source, context)
            self.assertEqual(raised.exception.status, "archive_limit")

    def test_archive_declared_expansion_limit_is_reported(self):
        archive = tar_gz([("one/SKILL.md", skill_markdown("one", "A longer description"))])
        source = {
            "id": "repo", "kind": "repository", "adapter": "github-repo",
            "repository": "acme/skills", "ref": "main", "include": ["**/SKILL.md"], "exclude": [],
        }
        with TemporaryDirectory() as temp:
            context = adapter_context(Path(temp), FakeHttp(archive=archive), max_archive_uncompressed_bytes=8)
            with self.assertRaises(SourceUnavailable) as raised:
                GitHubRepoAdapter().catalog(source, context)
            self.assertEqual(raised.exception.status, "archive_limit")

    def test_oversized_skill_file_is_skipped(self):
        archive = tar_gz([
            ("small/SKILL.md", skill_markdown("small", "Small helper")),
            ("large/SKILL.md", skill_markdown("large", "x" * 300)),
        ])
        source = {
            "id": "repo",
            "kind": "repository",
            "adapter": "github-repo",
            "repository": "acme/skills",
            "ref": "main",
            "include": ["**/SKILL.md"],
            "exclude": [],
        }
        with TemporaryDirectory() as temp:
            context = adapter_context(Path(temp), FakeHttp(archive=archive), max_skill_file_bytes=100)
            catalog, _ = GitHubRepoAdapter().catalog(source, context)
            self.assertEqual([item.name for item in catalog], ["small"])


class RegistryAdapterTests(unittest.TestCase):
    def _context(self, temp: str, payload) -> tuple[AdapterContext, FakeHttp]:
        http = FakeHttp(payload=payload)
        return adapter_context(Path(temp), http), http

    def test_skills_sh_uses_public_contract_and_ignores_ambient_oidc(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            context, http = self._context(temp, {"skills": [{
                "id": "openai/skills/pdf", "skillId": "pdf", "name": "PDF",
                "source": "openai/skills", "installs": 12,
            }]})
            result = SkillsShAdapter().search(registry_source("skills-sh", "skills-sh"), "pdf", 2, context)[0]
            self.assertEqual((result.repository, result.slug), ("openai/skills", "pdf"))
            self.assertIn("legacy API", result.warnings[0])
            self.assertTrue(http.calls[0][1].endswith("/api/search"))

        with TemporaryDirectory() as temp, patch.dict(os.environ, {"VERCEL_OIDC_TOKEN": "token"}, clear=True):
            context, http = self._context(temp, {"data": [{
                "id": "openai/skills/pdf", "skillId": "pdf", "source": "openai/skills",
            }]})
            result = SkillsShAdapter().search(registry_source("skills-sh", "skills-sh", base_url="https://skills.sh"), "pdf", 2, context)[0]
            self.assertEqual(result.slug, "pdf")
            self.assertIn("legacy API", result.warnings[0])
            self.assertTrue(http.calls[0][1].endswith("/api/search"))
            self.assertNotIn("Authorization", http.calls[0][2].get("headers", {}))

    def test_current_registry_payloads_map_to_common_candidates(self):
        cases = [
            (
                SkillsMpAdapter(),
                registry_source("skillsmp", "skillsmp", auth_env="SKILLSMP_API_KEY", auth_optional=True),
                {"success": True, "data": {"skills": [{
                    "id": "mp-1", "name": "PDF Reader", "author": "alice",
                    "description": "Read PDFs", "githubUrl": "https://github.com/acme/tools/tree/main/skills/pdf",
                    "skillUrl": "https://skillsmp.com/skills/mp-1", "stars": 9,
                }]}},
                ("acme/tools", "skills/pdf", "main"),
            ),
            (
                SkillHubPublicAdapter(),
                registry_source("skillhub-public", "skillhub-public"),
                {"skills": [{
                    "id": "pdf-reader", "name": "PDF Reader", "description": "Read PDFs",
                    "githubOwner": "acme", "githubRepo": "tools", "githubStars": 7,
                    "securityStatus": "pass",
                }]},
                ("acme/tools", None, None),
            ),
            (
                PolySkillAdapter(),
                registry_source("polyskill", "polyskill"),
                {"skills": [{
                    "id": "poly-1", "manifest": {"name": "@acme/pdf", "description": "Read PDFs"},
                    "githubUrl": "https://github.com/acme/tools/tree/main/skills/pdf",
                }]},
                ("acme/tools", "skills/pdf", "main"),
            ),
        ]
        with patch.dict(os.environ, {}, clear=True):
            for adapter, source, payload, identity in cases:
                with self.subTest(adapter=adapter.name), TemporaryDirectory() as temp:
                    context, _ = self._context(temp, payload)
                    result = adapter.search(source, "pdf", 3, context)[0]
                    self.assertEqual((result.repository, result.skill_path, result.ref), identity)
                    self.assertEqual(result.native_rank, 1)
                    self.assertTrue(result.name)

    def test_clawhub_preserves_publisher_slug_and_filters_mirrors(self):
        payload = {"results": [
            {"source": "skills-sh", "ownerHandle": "mirror", "slug": "pdf"},
            {
                "source": "clawhub", "ownerHandle": "alice", "slug": "pdf", "displayName": "PDF",
                "canonicalUrl": "/alice/skills/pdf", "native": {"skill": {"stats": {"downloads": 10}}},
            },
        ]}
        with TemporaryDirectory() as temp:
            context, _ = self._context(temp, payload)
            result = ClawHubAdapter().search(
                registry_source("clawhub", "clawhub", native_only=True, non_suspicious_only=True),
                "pdf", 10, context,
            )
            self.assertEqual(len(result), 1)
            self.assertEqual((result[0].publisher, result[0].slug), ("alice", "pdf"))
            self.assertEqual(result[0].install["reference"], "alice/pdf")

    def test_schema_mismatch_is_explicit(self):
        with TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            context, _ = self._context(temp, {"unexpected": []})
            with self.assertRaises(SourceUnavailable) as raised:
                SkillsMpAdapter().search(
                    registry_source("skillsmp", "skillsmp", auth_env="SKILLSMP_API_KEY", auth_optional=True),
                    "pdf", 2, context,
                )
            self.assertEqual(raised.exception.status, "schema_mismatch")

    def test_declarative_http_json_mapping(self):
        payload = {"response": {"items": [{
            "key": "pdf-1", "title": "PDF Reader", "about": "Read PDFs",
            "repo": "acme/tools", "location": "skills/pdf", "branch": "main",
        }]}}
        source = registry_source(
            "custom-json", "http-json-v1", endpoint="https://catalog.test/search",
            mapping={
                "items": "response.items", "id": "key", "name": "title", "description": "about",
                "repository": "repo", "skill_path": "location", "ref": "branch",
            },
        )
        with TemporaryDirectory() as temp:
            context, _ = self._context(temp, payload)
            result = HttpJsonAdapter().search(source, "pdf", 2, context)[0]
            self.assertEqual((result.native_id, result.repository, result.skill_path), ("pdf-1", "acme/tools", "skills/pdf"))
            self.assertEqual(result.install["ref"], "main")


class HttpSafetyTests(unittest.TestCase):
    def test_cross_origin_redirect_and_downgrade_are_rejected(self):
        handler = SafeRedirectHandler()
        request = Request(
            "https://catalog.example/search",
            headers={"Accept": "application/json", "Authorization": "Bearer secret", "X-Catalog-Key": "secret"},
        )
        with self.assertRaises(FinderHttpError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://other.example/search")
        with self.assertRaises(FinderHttpError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://catalog.example/search")


class StaticAdapter:
    def __init__(self, candidates=None, failure: Exception | None = None):
        self.candidates = candidates or []
        self.failure = failure
        self.calls = 0

    def search(self, source, query, limit, context):
        self.calls += 1
        if self.failure:
            raise self.failure
        return [Candidate.from_dict(candidate.to_dict()) for candidate in self.candidates[:limit]]


def candidate(source: str, repository: str, path: str, *, rank: int, name="PDF Reader") -> Candidate:
    canonical_url = f"https://github.com/{repository}/tree/main/{path}"
    return Candidate(
        native_id=f"{source}-native",
        name=name,
        description="Read and extract PDF documents",
        source_id=source,
        source_kind="registry",
        adapter="stub",
        native_rank=rank,
        canonical_url=canonical_url,
        repository=repository,
        skill_path=path,
        ref="main",
        slug="pdf-reader",
        publisher=repository.split("/", 1)[0],
        trust="community-index",
        install={"kind": "github", "repository": repository, "skill_path": path},
        # Static federation fixtures model a candidate whose repository target
        # and exact skill destination were already proved. Real registry
        # adapters deliberately do not manufacture these records.
        target_proof={
            "kind": "fixture", "status": "eligible",
            "reported": {"repository": repository, "ref": "main", "skill_path": path, "name": name},
            "resolved": {"repository": repository, "ref": "main", "skill_path": path, "name": name},
            "ref": "main", "skill_path": path, "actual_name": name,
            "content_sha256": "0" * 64, "checked_at": "2026-09-10T00:00:00+00:00",
        },
        link_proofs=[{
            "role": "skill_destination", "url": canonical_url, "status": "eligible",
            "method": "fixture", "checked_at": "2026-09-10T00:00:00+00:00",
            "identity_basis": "fixture_repository_ref_skill_path_content",
        }],
    )


def finder_config(root: Path, sources: list[dict]) -> EffectiveConfig:
    return EffectiveConfig(
        settings={"cache_ttl_seconds": 300, "max_workers": 1},
        packs=[],
        sources=sources,
        overlay_path=root / "overlay.json",
        overlay={},
    )


class FederationTests(unittest.TestCase):
    def test_partial_failure_is_visible_and_disabled_source_is_never_called(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sources = [
                registry_source("good", "skills-sh"),
                registry_source("limited", "skillsmp"),
                {**registry_source("disabled", "clawhub"), "enabled": False, "effective_enabled": False},
            ]
            good = StaticAdapter([candidate("good", "acme/tools", "skills/pdf", rank=1)])
            limited = StaticAdapter(failure=SourceUnavailable("rate_limited", "quota exhausted"))
            disabled = StaticAdapter([candidate("disabled", "other/tools", "pdf", rank=1)])
            finder_cache = Cache(root / "cache")
            finder = fixture_finder(finder_config(root, sources), finder_cache)
            finder.adapter_map = {"skills-sh": good, "skillsmp": limited, "clawhub": disabled}

            report = finder.search("pdf")
            self.assertEqual([item.status for item in report.coverage], ["ok", "rate_limited", "disabled"])
            self.assertEqual((good.calls, limited.calls, disabled.calls), (1, 1, 0))
            self.assertEqual(len(report.results), 1)
            self.assertEqual(finder_cache.metadata("queries")[0]["query"], "pdf")

            offline = finder.search("pdf forms", source_ids=["good"], offline=True)
            good_coverage = next(item for item in offline.coverage if item.source_id == "good")
            self.assertEqual(good_coverage.status, "offline_miss")
            self.assertIn("cached: 'pdf'", good_coverage.detail)

    def test_dedup_rrf_stable_ids_and_same_name_non_dedup(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            sources = [registry_source("one", "skills-sh"), registry_source("two", "skillsmp")]
            first = candidate("one", "acme/tools", "skills/pdf", rank=1)
            second = candidate("two", "acme/tools", "skills/pdf", rank=2)
            unrelated = candidate("one", "other/tools", "skills/pdf", rank=3)
            adapter_one = StaticAdapter([first, unrelated])
            adapter_two = StaticAdapter([second])
            finder = fixture_finder(finder_config(root, sources), Cache(root / "cache"))
            finder.adapter_map = {"skills-sh": adapter_one, "skillsmp": adapter_two}

            report = finder.search("pdf")
            self.assertEqual(len(report.results), 2)
            merged = report.results[0]
            self.assertEqual(merged.source_ids, ["one", "two"])
            self.assertEqual(merged.rank_fusion_score, round(1 / 61 + 1 / 62, 8))
            self.assertTrue(merged.install["requires_approval"])

            forward = finder._merge([first, second], "pdf")[0].id
            reverse = finder._merge([second, first], "pdf")[0].id
            self.assertEqual(forward, reverse)
            self.assertEqual(forward, merged.id)

    def test_weak_slug_only_merges_when_path_is_unambiguous(self):
        with TemporaryDirectory() as temp:
            finder = UniversalSkillFinder(finder_config(Path(temp), []), cache=Cache(Path(temp) / "cache"), http=FakeHttp())
            first = candidate("repo-a", "acme/tools", "skills/one/pdf-reader", rank=1)
            second = candidate("repo-b", "acme/tools", "examples/two/pdf-reader", rank=1)
            pathless = candidate("catalog", "acme/tools", "placeholder", rank=1)
            pathless.skill_path = None
            pathless.canonical_url = "https://catalog.example/acme/pdf-reader"

            ambiguous = finder._merge([first, second, pathless], "pdf")
            self.assertEqual(len(ambiguous), 3)
            unambiguous = finder._merge([first, pathless], "pdf")
            self.assertEqual(len(unambiguous), 1)
            self.assertEqual(unambiguous[0].id, finder._merge([first], "pdf")[0].id)

    def test_hosted_namespace_merges_and_duplicate_source_does_not_boost_rrf(self):
        with TemporaryDirectory() as temp:
            finder = UniversalSkillFinder(finder_config(Path(temp), []), cache=Cache(Path(temp) / "cache"), http=FakeHttp())
            first = Candidate(
                native_id="one", name="PDF", description="PDF forms", source_id="index-one",
                source_kind="registry", adapter="stub", native_rank=1, slug="pdf", publisher="alice",
                identity_namespace="shared-catalog",
            )
            duplicate = Candidate.from_dict(first.to_dict())
            duplicate.native_id = "duplicate-row"
            duplicate.native_rank = 2
            second = Candidate.from_dict(first.to_dict())
            second.source_id = "index-two"
            second.native_id = "two"
            second.native_rank = 2

            single_source = finder._merge([first, duplicate], "pdf")[0]
            self.assertEqual(single_source.rank_fusion_score, round(1 / 61, 8))
            corroborated = finder._merge([first, duplicate, second], "pdf")[0]
            self.assertEqual(corroborated.source_ids, ["index-one", "index-two"])
            self.assertEqual(corroborated.rank_fusion_score, round(1 / 61 + 1 / 62, 8))


class CliTests(unittest.TestCase):
    def test_bare_query_is_search_shorthand_and_global_options_are_preserved(self):
        with patch("universal_skill_finder.cli._search", return_value=0) as search:
            self.assertEqual(cli_main(["--config", "/tmp/overlay.json", "pdf", "forms", "--offline"]), 0)

        args = search.call_args.args[0]
        self.assertEqual(args.command, "search")
        self.assertEqual(args.query, ["pdf", "forms"])
        self.assertEqual(args.config, "/tmp/overlay.json")
        self.assertTrue(args.offline)

    def test_named_subcommand_is_not_rewritten_as_search(self):
        with patch("universal_skill_finder.cli._doctor", return_value=0) as doctor:
            self.assertEqual(cli_main(["doctor"]), 0)
        doctor.assert_called_once()

    def test_cli_imports_pack_adds_repository_and_lists_sources_without_network(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            overlay = root / "sources.json"
            source_pack = root / "team-pack.json"
            source_pack.write_text(json.dumps({
                "schema_version": 1,
                "pack": {"id": "team-pack", "description": "Team skills"},
                "sources": [{
                    "id": "team-one",
                    "kind": "repository",
                    "adapter": "github-repo",
                    "repository": "acme/team-one",
                    "ref": "main",
                    "include": ["**/SKILL.md"],
                    "exclude": [],
                }],
            }), encoding="utf-8")

            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(cli_main(["--config", str(overlay), "packs", "import", str(source_pack)]), 0)
                self.assertEqual(cli_main(["--config", str(overlay), "sources", "add-repo", "acme/extra", "--pack", "team-pack"]), 0)
                self.assertEqual(cli_main(["--config", str(overlay), "packs", "disable", "team-pack"]), 0)
                self.assertEqual(cli_main(["--config", str(overlay), "sources", "list"]), 0)
                self.assertEqual(cli_main(["--config", str(overlay), "sources", "validate"]), 0)

            self.assertEqual(errors.getvalue(), "")
            text = output.getvalue()
            self.assertIn("Imported team-pack with 1 source", text)
            self.assertIn("team-one", text)
            self.assertIn("acme-extra", text)
            config = load_config(str(overlay))
            self.assertFalse(config.source("team-one")["effective_enabled"])
            self.assertFalse(config.source("acme-extra")["effective_enabled"])


if __name__ == "__main__":
    unittest.main()
