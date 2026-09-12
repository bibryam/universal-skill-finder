from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters import ADAPTER_SPECS
from universal_skill_finder.adapters.base import AdapterContext
from universal_skill_finder.adapters.registries import HttpJsonAdapter, SkillHubPublicAdapter
from universal_skill_finder.cache import Cache
from universal_skill_finder.config import load_config
from universal_skill_finder.http import HttpClient
try:  # Discovery adds tests/ to sys.path; module-qualified unittest does not.
    from source_contract_support import (  # type: ignore[import-not-found]
        GENERATED_CASES, ScriptedResponse, deterministic_tar, execute_generated_case, fixture, scripted_http,
    )
except ModuleNotFoundError:
    from tests.source_contract_support import (
        GENERATED_CASES, ScriptedResponse, deterministic_tar, execute_generated_case, fixture, scripted_http,
    )


MANIFEST = ROOT / "tests" / "source_test_manifest.json"
FIXTURES = ROOT / "tests" / "fixtures" / "sources"


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def requested_sources() -> set[str]:
    import os
    value = os.environ.get("UNIVERSAL_SKILL_FINDER_SOURCE_TEST_SOURCES", "")
    return set(filter(None, value.split(",")))


def requested_cases() -> set[str]:
    import os
    value = os.environ.get("UNIVERSAL_SKILL_FINDER_SOURCE_TEST_CASES", "")
    return set(filter(None, value.split(",")))


def source_cases(value: dict, source_id: str) -> set[str]:
    detail = value["sources"][source_id]
    unsupported = set(value.get("not_applicable", {}).get(detail["adapter"], {}))
    return {"contract", *value["generated_cases"], *detail.get("extra_cases", [])} - unsupported


class SourceContractInventoryTests(unittest.TestCase):
    def test_manifest_covers_bundled_source_inventory_and_registered_adapters(self):
        value = manifest()
        config = load_config(str(Path(self.id()).with_suffix(".overlay.json")))
        bundled = {source["id"]: source["adapter"] for source in config.sources if source["origin"] == "bundled"}
        self.assertEqual(set(value["sources"]), set(bundled))
        for source_id, adapter in bundled.items():
            with self.subTest(source=source_id):
                self.assertEqual(value["sources"][source_id]["adapter"], adapter)
                self.assertIn(adapter, ADAPTER_SPECS)
                self.assertIn("contract", source_cases(value, source_id))
                self.assertTrue((FIXTURES / source_id / "contract.json").is_file())
        covered = {item["adapter"] for item in value["sources"].values()} | set(value["adapter_cases"])
        self.assertEqual(covered, set(ADAPTER_SPECS))

    def test_every_declared_fixture_and_generated_case_is_sanitized_and_selectable(self):
        value = manifest()
        for source_id, detail in value["sources"].items():
            with self.subTest(source=source_id):
                data = fixture(FIXTURES / source_id / detail["fixture"])
                self.assertEqual(data["source"], source_id)
                self.assertEqual(data["case"], "contract")
                self.assertEqual(set(value["generated_cases"]), GENERATED_CASES)
                not_applicable = set(value.get("not_applicable", {}).get(detail["adapter"], {}))
                self.assertEqual(set(value["generated_cases"]) - not_applicable,
                                 set(value["generated_cases"]) & source_cases(value, source_id))
        self.assertEqual(fixture(FIXTURES / "skillhub-public" / "invalid-route.json")["status"], 404)

    def test_runner_selected_source_cases_remain_independently_addressable(self):
        value = manifest()
        selected = requested_sources() or set(value["sources"])
        self.assertTrue(selected <= set(value["sources"]))
        for source_id in selected:
            with self.subTest(source=source_id):
                self.assertIn("contract", source_cases(value, source_id))
                self.assertEqual(fixture(FIXTURES / source_id / "contract.json")["source"], source_id)


class GeneratedSourceCaseTests(unittest.TestCase):
    def test_selected_cases_execute_source_specific_generated_assertions(self):
        value = manifest()
        selected_sources = requested_sources() or set(value["sources"])
        selected_cases = requested_cases()
        for source_id in sorted(selected_sources):
            detail = value["sources"][source_id]
            cases = selected_cases or source_cases(value, source_id)
            for case in sorted(cases):
                with self.subTest(source=source_id, case=case):
                    self.assertIn(case, source_cases(value, source_id))
                    if case in GENERATED_CASES:
                        execute_generated_case(
                            source_id, case, adapter=detail["adapter"], source_cap=detail["source_cap"],
                            contract_fixture=fixture(FIXTURES / source_id / detail["fixture"]),
                        )
                    elif case == "contract":
                        self.assertEqual(fixture(FIXTURES / source_id / detail["fixture"])["source"], source_id)
                    elif case == "invalid-route":
                        route = fixture(FIXTURES / source_id / "invalid-route.json")
                        self.assertEqual(route["status"], 404)
                    else:
                        self.fail("manifest case has no executable harness")

    def test_registered_adapter_generated_cases_execute_or_name_an_na(self):
        value = manifest()
        selected_cases = requested_cases()
        selected_sources = requested_sources()
        selected_adapters = ({value["sources"][source]["adapter"] for source in selected_sources}
                             if selected_sources else set(value["adapter_cases"]))
        for adapter, definition in value["adapter_cases"].items():
            if adapter not in selected_adapters:
                continue
            unavailable = set(definition.get("na", {})) | set(value.get("not_applicable", {}).get(adapter, {}))
            cases = selected_cases or set(value["generated_cases"])
            for case in sorted(cases):
                with self.subTest(adapter=adapter, case=case):
                    if case not in GENERATED_CASES:
                        continue
                    if case in unavailable:
                        self.assertIsInstance((definition.get("na", {}) | value.get("not_applicable", {}).get(adapter, {}))[case], str)
                    else:
                        self.assertIn(case, GENERATED_CASES)
                        execute_generated_case(adapter + "-fixture", case, adapter=adapter)


class ScriptedTransportTests(unittest.TestCase):
    def test_real_http_client_runs_against_scripted_opener_and_consumes_exact_request(self):
        data = fixture(FIXTURES / "http-json-v1" / "contract.json")
        source = {
            "id": "fixture", "kind": "registry", "adapter": "http-json-v1", "base_url": "https://fixture.invalid",
            "endpoint": "https://fixture.invalid/search", "mapping": {
                "items": "data", "id": "id", "name": "name", "description": "description"
            },
        }
        responses = [ScriptedResponse.from_fixture(item) for item in data["requests"]]
        with TemporaryDirectory() as temporary, scripted_http(responses) as opener:
            context = AdapterContext(HttpClient(timeout=1), Cache(Path(temporary) / "cache"), {})
            rows = HttpJsonAdapter().search(source, "pdf", 1, context)
        self.assertEqual([row.name for row in rows], ["PDF"])
        self.assertEqual(opener.calls[0]["method"], "GET")

    def test_unplanned_transport_fails_closed(self):
        with self.assertRaisesRegex(AssertionError, "unplanned request"):
            with scripted_http([]):
                HttpClient(timeout=1).get_json("https://fixture.invalid/not-scripted")

    def test_deterministic_archive_has_no_clock_or_platform_variance(self):
        files = {"root/SKILL.md": "---\nname: PDF\n---\n", "root/nested/x": "x"}
        self.assertEqual(deterministic_tar(files), deterministic_tar(files))

    def test_skillhub_uses_the_supported_singular_route(self):
        source = {"id": "skillhub-public", "kind": "registry", "adapter": "skillhub-public",
                  "base_url": "https://skills.palebluedot.live", "trust": "community-index"}
        response = ScriptedResponse("GET", "https://skills.palebluedot.live/api/skills?limit=1&q=pdf",
                                    data=b'{"skills":[{"id":"example","name":"Example"}]}')
        with TemporaryDirectory() as temporary, scripted_http([response]):
            rows = SkillHubPublicAdapter().search(source, "pdf", 1, AdapterContext(HttpClient(), Cache(Path(temporary)), {}))
        evidence = fixture(FIXTURES / "skillhub-public" / "invalid-route.json")
        self.assertEqual(rows[0].canonical_url, source["base_url"] + evidence["supported_route"])
        self.assertEqual(evidence["status"], 404)
        self.assertNotEqual(evidence["unsupported_route"], evidence["supported_route"])

    def test_skills_sh_public_search_never_adopts_ambient_oidc(self):
        from os import environ
        from unittest.mock import patch
        from universal_skill_finder.adapters.registries import SkillsShAdapter

        source = {"id": "skills-sh", "kind": "registry", "adapter": "skills-sh", "base_url": "https://skills.sh"}
        response = ScriptedResponse(
            "GET", "https://skills.sh/api/search?limit=1&q=pdf",
            data=b'{"skills":[{"id":"openai/skills/pdf","name":"PDF","source":"openai/skills"}]}'
        )
        with TemporaryDirectory() as temporary, patch.dict(environ, {"VERCEL_OIDC_TOKEN": "do-not-send"}), scripted_http([response]) as opener:
            rows = SkillsShAdapter().search(source, "pdf", 1, AdapterContext(HttpClient(), Cache(Path(temporary)), {}))
        self.assertEqual(rows[0].source_evidence["native"]["request_mode"], "public_legacy")
        self.assertNotIn("authorization", opener.calls[0]["headers"])
