from __future__ import annotations

import json
import os
import sys
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters import ADAPTER_SPECS, _catalogue, adapters
from universal_skill_finder.adapters.base import AdapterContext, AdapterSpec, SourceUnavailable
from universal_skill_finder.adapters.registries import HttpJsonAdapter
from universal_skill_finder.cache import Cache
from universal_skill_finder.config import (
    SUPPORTED_ADAPTERS,
    ConfigurationError,
    EffectiveConfig,
    default_config_path,
    import_source_pack,
    load_config,
    set_pack_enabled,
    set_source_enabled,
    validate_effective,
)
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.versioning import ADAPTER_CONTRACT_VERSION, SCHEMA_VERSION


def json_source(**changes):
    return {
        "id": "team-catalogue",
        "adapter": "http-json-v1",
        "kind": "registry",
        "enabled": True,
        "effective_enabled": True,
        "endpoint": "https://catalogue.example.invalid/search",
        "mapping": {
            "items": "data.pages.0.skills",
            "id": "identifier",
            "name": "metadata.name",
            "description": "metadata.summary",
            "url": "links.0.url",
            "repository": "location.repository",
            "skill_path": "location.path",
            "ref": "location.ref",
        },
        **changes,
    }


def mapped_payload():
    return {"data": {"pages": [{"skills": [
        {"identifier": "pdf", "metadata": {"name": "pdf-tools", "summary": "Read PDF forms"},
         "links": [{"url": "https://github.com/example/skills/tree/main/pdf"}],
         "location": {"repository": "example/skills", "path": "pdf", "ref": "main"},
         "install": {"command": "never execute remote instructions"}},
        "not a candidate",
        {"identifier": "pdf-reader", "metadata": {"name": "pdf-reader", "summary": "Read PDF files"}},
        {"identifier": "third", "metadata": {"name": "third", "summary": "PDF helper"}},
    ]}]}}


class PayloadHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


class AdapterCatalogueTests(unittest.TestCase):
    def test_factory_validation_and_config_share_one_catalogue(self):
        first, second = adapters(), adapters()
        self.assertEqual(set(first), set(ADAPTER_SPECS))
        self.assertEqual(SUPPORTED_ADAPTERS, frozenset(ADAPTER_SPECS))
        for name, spec in ADAPTER_SPECS.items():
            with self.subTest(adapter=name):
                self.assertIsInstance(first[name], spec.factory)
                self.assertEqual(first[name].name, name)
                self.assertIsNot(first[name], second[name])
                self.assertEqual(spec.contract_version, ADAPTER_CONTRACT_VERSION)
                self.assertTrue(callable(first[name].search))

    def test_catalogue_and_descriptors_are_immutable(self):
        spec = ADAPTER_SPECS["skills-sh"]
        with self.assertRaises(TypeError):
            ADAPTER_SPECS["another"] = spec
        with self.assertRaises(FrozenInstanceError):
            spec.kind = "repository"

    def test_duplicate_registration_fails_instead_of_silently_overwriting(self):
        spec = ADAPTER_SPECS["skills-sh"]
        with self.assertRaisesRegex(ValueError, "duplicate adapter registration"):
            _catalogue((spec, spec))

    def test_invalid_registration_metadata_fails_early(self):
        spec = ADAPTER_SPECS["skills-sh"]
        for changes in (
            {"kind": "shell"}, {"cache_policy": "execute"},
            {"relevance_basis": "alphabetical_padding"},
            {"contract_version": ADAPTER_CONTRACT_VERSION + 1}, {"contract_version": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _catalogue((replace(spec, **changes),))

    def test_registration_requires_search_method(self):
        class MissingSearch:
            name = "missing-search"

        with self.assertRaisesRegex(ValueError, "no search method"):
            _catalogue((AdapterSpec(MissingSearch, "registry", "query"),))

    def test_cache_ownership_matches_existing_connector_semantics(self):
        self.assertEqual(ADAPTER_SPECS["github-repo"].cache_policy, "catalogue")
        self.assertEqual(ADAPTER_SPECS["local-directory"].cache_policy, "none")
        self.assertEqual(ADAPTER_SPECS["github-repo"].relevance_basis, "local_lexical")
        self.assertEqual(ADAPTER_SPECS["local-directory"].relevance_basis, "local_lexical")
        for spec in ADAPTER_SPECS.values():
            if spec.kind == "registry":
                self.assertEqual(spec.cache_policy, "query")
                self.assertEqual(spec.relevance_basis, "provider_query")

    def test_every_source_kind_is_checked_against_registration(self):
        for name, spec in ADAPTER_SPECS.items():
            source = json_source(
                adapter=name, kind="repository" if spec.kind == "registry" else "registry",
                base_url="https://catalogue.example.invalid", repository="example/skills", ref="main", path=".",
            )
            config = EffectiveConfig({}, [], [source], Path("unused.json"), {})
            with self.subTest(adapter=name):
                self.assertTrue(any(f"requires kind={spec.kind}" in error for error in validate_effective(config)))

    def test_named_registries_require_base_url_not_just_endpoint(self):
        for name, spec in ADAPTER_SPECS.items():
            if spec.kind != "registry" or name == "http-json-v1":
                continue
            config = EffectiveConfig({}, [], [json_source(adapter=name)], Path("unused.json"), {})
            with self.subTest(adapter=name):
                self.assertTrue(any("requires base_url" in error for error in validate_effective(config)))

    def test_unknown_or_executable_adapter_names_are_rejected(self):
        for name in ("shell-command", "module:callable", "unknown-registry"):
            config = EffectiveConfig({}, [], [json_source(adapter=name)], Path("unused.json"), {})
            with self.subTest(adapter=name):
                self.assertTrue(any("unsupported adapter" in error for error in validate_effective(config)))


class BundledCatalogueValidationTests(unittest.TestCase):
    def test_bundled_schema_rejects_missing_boolean_and_future_versions(self):
        original = json.loads(default_config_path().read_text(encoding="utf-8"))
        with TemporaryDirectory() as temp:
            fixture = Path(temp) / "bundled.json"
            for version in (None, True, str(SCHEMA_VERSION), SCHEMA_VERSION + 1):
                payload = {**original, "schema_version": version}
                fixture.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(version=version), patch("universal_skill_finder.config.default_config_path", return_value=fixture):
                    with self.assertRaisesRegex(ConfigurationError, "bundled catalogue must declare schema_version"):
                        load_config(str(Path(temp) / "overlay.json"))

    def test_malformed_bundled_shape_fails_before_merging(self):
        original = json.loads(default_config_path().read_text(encoding="utf-8"))
        changes = (
            {"settings": []}, {"packs": {}}, {"sources": [None]},
            {"packs": [{"id": []}]}, {"sources": [{"id": "one", "pack": []}]},
        )
        with TemporaryDirectory() as temp:
            fixture = Path(temp) / "bundled.json"
            for change in changes:
                fixture.write_text(json.dumps({**original, **change}), encoding="utf-8")
                with self.subTest(change=change), patch("universal_skill_finder.config.default_config_path", return_value=fixture):
                    with self.assertRaises(ConfigurationError):
                        load_config(str(Path(temp) / "overlay.json"))


class DeclarativeConnectorContractTests(unittest.TestCase):
    def test_dotted_keys_numeric_indexes_and_exact_limit(self):
        source = json_source(query_param="search", limit_param="count")
        before = deepcopy(source)
        http = PayloadHttp(mapped_payload())
        with TemporaryDirectory() as temp:
            context = AdapterContext(http, Cache(Path(temp) / "cache"), {})
            candidates = HttpJsonAdapter().search(source, "PDF forms", 2, context)
        self.assertEqual(source, before)
        self.assertEqual([item.name for item in candidates], ["pdf-tools", "pdf-reader"])
        self.assertEqual([item.native_rank for item in candidates], [1, 2])
        self.assertEqual(candidates[0].repository, "example/skills")
        self.assertEqual(candidates[0].skill_path, "pdf")
        self.assertEqual(candidates[0].ref, "main")
        self.assertEqual(candidates[0].source_id, source["id"])
        self.assertNotIn("command", candidates[0].install)
        self.assertEqual(http.calls[0][1]["params"], {"search": "PDF forms", "count": 2})

    def test_changed_response_shape_is_explicit_failure_not_empty_success(self):
        with TemporaryDirectory() as temp:
            context = AdapterContext(PayloadHttp({"changed": []}), Cache(Path(temp) / "cache"), {})
            with self.assertRaises(SourceUnavailable) as caught:
                HttpJsonAdapter().search(json_source(), "PDF forms", 2, context)
        self.assertEqual(caught.exception.status, "schema_mismatch")

    def test_missing_required_credentials_prevent_transport(self):
        source = json_source(headers={"Authorization": {"env": "CONNECTOR_FIXTURE_TOKEN", "prefix": "Bearer "}})
        http = PayloadHttp(mapped_payload())
        with TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            context = AdapterContext(http, Cache(Path(temp) / "cache"), {})
            with self.assertRaises(SourceUnavailable) as caught:
                HttpJsonAdapter().search(source, "PDF forms", 2, context)
        self.assertEqual(caught.exception.status, "auth_missing")
        self.assertEqual(http.calls, [])

    def test_imported_json_registry_enablement_search_and_offline_cache(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            overlay = root / "overlay.json"
            pack_file = root / "pack.json"
            source = json_source(enabled=False)
            source.pop("kind")  # Kind inference must come from the registration.
            source.pop("effective_enabled")
            pack_file.write_text(json.dumps({
                "schema_version": SCHEMA_VERSION,
                "pack": {"id": "team-registries", "enabled": True},
                "sources": [source],
            }), encoding="utf-8")
            import_source_pack(load_config(str(overlay)), str(pack_file))
            config = load_config(str(overlay))
            self.assertEqual(config.source(source["id"])["kind"], "registry")
            self.assertFalse(config.source(source["id"])["effective_enabled"])
            set_source_enabled(config, source["id"], True)
            config = load_config(str(overlay))
            http = PayloadHttp(mapped_payload())
            finder = UniversalSkillFinder(config, cache=Cache(root / "cache"), http=http)
            report = finder.search("PDF forms", source_ids=[source["id"]], limit=2)
            self.assertEqual(report.results, [])
            self.assertEqual(len(report.candidate_previews), 2)
            self.assertEqual(report.not_checked_count, 2)
            self.assertEqual(next(item.status for item in report.coverage if item.source_id == source["id"]), "ok")
            cached = finder.search("PDF forms", source_ids=[source["id"]], limit=2, offline=True)
            self.assertEqual(next(item.status for item in cached.coverage if item.source_id == source["id"]), "cached")
            self.assertEqual(cached.results, [])
            self.assertEqual(len(cached.candidate_previews), 2)
            self.assertEqual(len(http.calls), 1)
            set_pack_enabled(config, "team-registries", False)
            config = load_config(str(overlay))
            set_source_enabled(config, source["id"], True)
            config = load_config(str(overlay))
            self.assertTrue(config.source(source["id"])["enabled"])
            self.assertFalse(config.source(source["id"])["effective_enabled"])
            disabled = UniversalSkillFinder(config, cache=Cache(root / "cache"), http=http).search(
                "PDF forms", source_ids=[source["id"]]
            )
            self.assertEqual(next(item.status for item in disabled.coverage if item.source_id == source["id"]), "disabled")
            self.assertEqual(len(http.calls), 1)


if __name__ == "__main__":
    unittest.main()
