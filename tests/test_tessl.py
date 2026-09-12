from __future__ import annotations

import json
import os
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import AdapterContext, SourceUnavailable
from universal_skill_finder.adapters.tessl import API_ORIGIN, MAX_RESPONSE_BYTES, SEARCH_ENDPOINT, TesslAdapter
from universal_skill_finder.cache import Cache
from universal_skill_finder.http import FinderHttpError, HttpClient, HttpResponse
from universal_skill_finder.models import Candidate


def source(**changes):
    return {"id": "tessl", "kind": "registry", "adapter": "tessl", "base_url": API_ORIGIN,
            "trust": "unverified", **changes}


def row(index=1, **changes):
    attributes = {
        "name": "pdf", "description": "Read and fill PDF forms",
        "sourceUrl": "https://github.com/example/skills", "path": "skills/pdf/SKILL.md",
        "isPrivate": False, "updatedAt": "2026-09-05 04:10:11.58+00", "validationPassed": None,
        "scores": {"version": "a" * 40, "aggregate": 0.656, "quality": 0.7875, "impact": None,
                   "security": "HIGH", "securityLevel": "HIGH", "evalAvg": None,
                   "evalBaseline": None, "evalImprovement": None, "evalImprovementMultiplier": None,
                   "evalCount": None, "lastScoredAt": "2026-09-05 04:10:41.243993+00"},
        **changes,
    }
    return {"id": f"019f948e-9997-7013-8ca1-{index:012x}", "type": "skill", "attributes": attributes}


def payload(rows=(), *, size=10, total=None):
    rows = list(rows)
    total = len(rows) if total is None else total
    return {"data": rows, "meta": {"pagination": {
        "total": total, "pages": (total + size - 1) // size, "number": 1, "size": size,
    }}, "links": {"next": "https://attacker.example/next" if total > size else None}}


def bundle(**changes):
    item = row()
    item["type"] = "tile"
    item["attributes"] = {
        "name": "default-skill-review", "fullName": "tessl/default-skill-review", "isPrivate": False,
        "scores": {"version": "0.2.0", "quality": 0.86, "securityLevel": "NONE"},
        "versions": [{"version": "0.2.0", "hasSkills": True, "archived": False, "skills": [],
                      "summary": "Review the quality of agent skill instructions"}], **changes,
    }
    item["relationships"] = {"workspace": {"data": {"type": "workspace", "attributes": {"name": "tessl"}}}}
    return item


class PayloadHttp(HttpClient):
    def __init__(self, value):
        super().__init__()
        self.value = value
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.value, Exception):
            raise self.value
        if isinstance(self.value, HttpResponse):
            return self.value
        data = self.value if isinstance(self.value, bytes) else json.dumps(self.value).encode()
        return HttpResponse(data, 200, {}, SEARCH_ENDPOINT)


class TesslTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def search(self, value=None, *, configuration=None, query="PDF forms", limit=10, offline=False, **settings):
        http = value if isinstance(value, PayloadHttp) else PayloadHttp(payload([row()]) if value is None else value)
        context = AdapterContext(http, Cache(Path(self.temp.name) / "cache"), settings, offline=offline)
        results = TesslAdapter().search(configuration or source(), query, limit, context)
        return results, context, http

    def test_anonymous_fixed_hybrid_skill_request_does_not_read_credentials(self):
        with patch.dict(os.environ, {"TESSL_API_TOKEN": "secret", "TESSL_TOKEN": "secret",
                                     "GH_TOKEN": "secret", "GITHUB_TOKEN": "secret"}, clear=True):
            results, context, http = self.search()
        self.assertEqual(len(results), 1)
        self.assertFalse(context.incomplete_results)
        self.assertEqual((context.source_total, context.total_relation, context.effective_limit), (1, "exact", 10))
        self.assertEqual(len(http.calls), 1)
        method, url, options = http.calls[0]
        self.assertEqual((method, url), ("GET", SEARCH_ENDPOINT))
        self.assertEqual(options["params"], {
            "q": "PDF forms", "searchMode": "hybrid", "filter[hasSkills]": "true", "include": "tile-skills",
            "filter[includePrivate]": "false", "filter[includeInvalid]": "false",
            "page[number]": 1, "page[size]": 10,
        })
        self.assertNotIn("headers", options)
        self.assertNotIn("secret", repr(options))
        self.assertEqual(options["max_bytes"], MAX_RESPONSE_BYTES)

    def test_direct_calls_reject_origin_and_authentication_overrides(self):
        for changes in (
            {"base_url": API_ORIGIN + "/"}, {"base_url": "https://attacker.example"},
            {"base_url": "http://api.tessl.io"}, {"base_url": "https://api.tessl.io:444"},
            {"endpoint": SEARCH_ENDPOINT}, {"headers": {}}, {"headers": {"Authorization": "secret"}},
            {"auth_env": "TESSL_TOKEN"}, {"auth_env": None}, {"auth_optional": True},
            {"auth_optional": False}, {"allow_insecure_local": True}, {"allow_insecure_local": 0},
        ):
            http = PayloadHttp(payload())
            with self.subTest(changes=changes), self.assertRaises(SourceUnavailable) as caught:
                self.search(http, configuration=source(**changes))
            self.assertEqual(caught.exception.status, "invalid_config")
            self.assertEqual(http.calls, [])

    def test_offline_never_contacts_api(self):
        http = PayloadHttp(payload())
        with self.assertRaises(SourceUnavailable) as caught:
            self.search(http, offline=True)
        self.assertEqual(caught.exception.status, "offline_miss")
        self.assertEqual(http.calls, [])

    def test_query_is_value_not_api_parameters(self):
        query = "pdf &filter[includePrivate]=true&filter[type][eq]=tile"
        _, _, http = self.search(query=query)
        params = http.calls[0][2]["params"]
        self.assertEqual(params["q"], query)
        self.assertEqual(params["filter[includePrivate]"], "false")
        self.assertNotIn("filter[type][eq]", params)
        self.assertEqual(params["filter[hasSkills]"], "true")

    def test_long_query_is_shortened_and_marked_partial(self):
        _, context, http = self.search(query="x" * 600)
        self.assertEqual(len(http.calls[0][2]["params"]["q"]), 500)
        self.assertTrue(context.incomplete_results)
        self.assertIn("shortened", context.detail)

    def test_empty_query_and_invalid_limit_fail_before_request(self):
        for query, limit in (("", 10), (" \n", 10), (None, 10), ("pdf", 0), ("pdf", True)):
            http = PayloadHttp(payload())
            with self.subTest(query=query, limit=limit), self.assertRaises(SourceUnavailable) as caught:
                self.search(http, query=query, limit=limit)
            self.assertEqual(caught.exception.status, "invalid_query")
            self.assertEqual(http.calls, [])

    def test_real_source_identity_and_opaque_score_revision_are_preserved(self):
        results, _, _ = self.search()
        result = results[0]
        self.assertEqual(result.repository, "example/skills")
        self.assertEqual(result.skill_path, "skills/pdf")
        self.assertEqual(result.canonical_url, "https://github.com/example/skills")
        self.assertEqual(result.publisher, "example")
        self.assertEqual(result.name, "pdf")
        self.assertEqual(result.native_rank, 1)
        self.assertEqual(result.metrics["tessl_score_version"], "a" * 40)
        self.assertIsNone(result.ref)
        self.assertIsNone(result.content_sha256)
        self.assertEqual(result.install, {})
        self.assertIsNone(Candidate.from_dict(result.to_dict()).ref)

    def test_actual_skill_file_path_becomes_directory_including_hidden_roots(self):
        for path, expected in (("SKILL.md", "."), (".agents/skills/pdf/SKILL.md", ".agents/skills/pdf"),
                               (".claude/skills/pdf/SKILL.md", ".claude/skills/pdf")):
            with self.subTest(path=path):
                results, context, _ = self.search(payload([row(path=path)]))
                self.assertEqual(results[0].skill_path, expected)
                self.assertEqual(Candidate.from_dict(results[0].to_dict()).skill_path, expected)
                self.assertFalse(context.incomplete_results)

    def test_source_urls_and_paths_are_not_repaired_into_install_targets(self):
        for changes in (
            {"path": "../pdf/SKILL.md"}, {"path": "/pdf/SKILL.md"}, {"path": "pdf//SKILL.md"},
            {"path": "pdf/./SKILL.md"}, {"path": "pdf/../SKILL.md"}, {"path": "-args/SKILL.md"},
            {"path": "pdf/README.md"}, {"path": "pdf/SKILL.md?x"}, {"path": "pdf\\SKILL.md"},
            {"path": "pdf/%2e%2e/SKILL.md"}, {"path": None}, {"path": {}},
            {"sourceUrl": "http://github.com/example/skills"}, {"sourceUrl": "javascript:run()"},
            {"sourceUrl": "https://user:secret@github.com/example/skills"},  # pragma: allowlist secret - synthetic credential-bearing URL rejection fixture
            {"sourceUrl": "https://github.com/example/skills?secret=value"},
            {"sourceUrl": "https://github.com/example/skills#wrong"},
            {"sourceUrl": "https://github.com/example/skills/blob/main/pdf/SKILL.md"},
            {"sourceUrl": "https://github.com/example/skills/tree/main/pdf"},
            {"sourceUrl": "https://github.com/example/skills%2fevil"}, {"sourceUrl": ""},
        ):
            with self.subTest(changes=changes):
                results, context, http = self.search(payload([row(**changes)]))
                self.assertEqual(results, [])
                self.assertTrue(context.incomplete_results)
                self.assertEqual(len(http.calls), 1)
                self.assertNotIn("secret", context.detail)

    def test_unsupported_repository_host_is_omitted_to_avoid_false_identity_merges(self):
        results, context, http = self.search(payload([row(sourceUrl="https://gitlab.com/example/skills")]))
        self.assertEqual(results, [])
        self.assertTrue(context.incomplete_results)
        self.assertEqual(len(http.calls), 1)

    def test_private_invalid_or_untyped_rows_are_omitted_without_leaking_metadata(self):
        invalid = [row(index=1, isPrivate=True, name="private-secret"), row(index=2, isPrivate="false"),
                   row(index=3, validationPassed=False), row(index=4, validationPassed="true"),
                   row(index=5, name={}), row(index=6, description=[]), "bad row",
                   {**row(index=7), "type": "tile"}, {**row(index=8), "id": "not-a-uuid"}]
        valid = row(index=10)
        results, context, _ = self.search(payload(invalid + [valid]))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].native_rank, 10)
        self.assertTrue(context.incomplete_results)
        self.assertIn("9 nonpublic, invalid, or unsupported", context.detail)
        self.assertNotIn("private-secret", context.detail)

    def test_duplicate_uuid_is_omitted_with_partial_coverage(self):
        results, context, _ = self.search(payload([row(), row()]))
        self.assertEqual(len(results), 1)
        self.assertTrue(context.incomplete_results)

    def test_malformed_row_type_is_partial_not_an_exception(self):
        for value in ([], {}, None, 1):
            with self.subTest(value=value):
                item = {**row(), "type": value}
                results, context, _ = self.search(payload([item]))
                self.assertEqual(results, [])
                self.assertTrue(context.incomplete_results)

    def test_normal_requested_top_n_is_complete_and_does_not_follow_next_url(self):
        results, context, http = self.search(payload([row(index=1), row(index=2)], size=2, total=20), limit=2)
        self.assertEqual(len(results), 2)
        self.assertFalse(context.incomplete_results)
        self.assertIn("top 2 of 20", context.detail)
        self.assertEqual(len(http.calls), 1)

    def test_response_rows_and_requested_count_are_bounded(self):
        results, context, http = self.search(payload([row(index=i + 1) for i in range(110)], size=100), limit=1000)
        self.assertEqual(len(results), 100)
        self.assertEqual(http.calls[0][2]["params"]["page[size]"], 100)
        self.assertTrue(context.incomplete_results)
        self.assertIn("capped at 100", context.detail)

    def test_response_cap_honours_lower_user_setting_and_rejects_overlong_body(self):
        with self.assertRaises(SourceUnavailable) as caught:
            self.search(b" " * (MAX_RESPONSE_BYTES + 1))
        self.assertEqual(caught.exception.status, "schema_mismatch")
        _, _, http = self.search(max_response_bytes=4096)
        self.assertEqual(http.calls[0][2]["max_bytes"], 4096)

    def test_substituted_response_origin_is_rejected(self):
        for url in ("http://api.tessl.io/experimental/search", "https://attacker.example/search",
                    "https://api.tessl.io:444/search", "https://secret@api.tessl.io/search"):
            with self.subTest(url=url), self.assertRaises(SourceUnavailable) as caught:
                self.search(HttpResponse(json.dumps(payload()).encode(), 200, {}, url))
            self.assertEqual(caught.exception.status, "schema_mismatch")

    def test_empty_array_with_valid_pagination_is_a_complete_empty_search(self):
        results, context, _ = self.search(payload())
        self.assertEqual(results, [])
        self.assertFalse(context.incomplete_results)

    def test_missing_or_malformed_pagination_retains_rows_with_partial_coverage(self):
        for meta in (None, {}, {"pagination": None}, {"pagination": {"total": False}},
                     {"pagination": {"total": 1, "pages": 1, "number": 2, "size": 10}}):
            with self.subTest(meta=meta):
                results, context, _ = self.search({"data": [row()], "meta": meta})
                self.assertEqual(len(results), 1)
                self.assertTrue(context.incomplete_results)

    def test_early_short_page_is_partial_even_with_zero_results(self):
        for rows in ([], [row()]):
            results, context, _ = self.search(payload(rows, total=20))
            self.assertEqual(len(results), len(rows))
            self.assertTrue(context.incomplete_results)
            self.assertIn("response count", context.detail)

    def test_upstream_incomplete_flag_and_malformed_flag_remain_partial(self):
        for value in (True, "false", 1, None):
            data = payload()
            data["meta"]["incomplete_results"] = value
            _, context, _ = self.search(data)
            self.assertTrue(context.incomplete_results)
        data = payload()
        data["incomplete_results"] = False
        _, context, _ = self.search(data)
        self.assertFalse(context.incomplete_results)

    def test_malformed_envelope_fails_instead_of_reporting_zero_matches(self):
        for data in (None, [], {"data": {}}, {"skills": []}, {"errors": [{"detail": "secret"}]}):
            with self.subTest(data=data), self.assertRaises(SourceUnavailable) as caught:
                self.search(PayloadHttp(data))
            self.assertEqual(caught.exception.status, "schema_mismatch")
            self.assertNotIn("secret", str(caught.exception))

    def test_json_errors_and_excessive_depth_fail_safely(self):
        for data in (b"not-json secret", b"\xff", b'{"data":' + b"[" * 110 + b"]" * 110 + b"}"):
            with self.subTest(data=data[:12]), self.assertRaises(FinderHttpError) as caught:
                self.search(data)
            self.assertNotIn("secret", str(caught.exception))

    def test_http_failures_propagate_without_retry_or_credential_fallback(self):
        for status in (401, 403, 429, 500):
            http = PayloadHttp(FinderHttpError(f"HTTP {status} from https://api.tessl.io", status=status))
            with self.subTest(status=status), self.assertRaises(FinderHttpError) as caught:
                self.search(http)
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(len(http.calls), 1)

    def test_score_metrics_retain_provenance_not_popularity_or_safety_certification(self):
        results, _, _ = self.search()
        metrics = results[0].metrics
        self.assertEqual(metrics["tessl_quality"], 0.7875)
        self.assertEqual(metrics["tessl_aggregate"], 0.656)
        self.assertEqual(metrics["tessl_security_level"], "HIGH")
        self.assertIn("tessl_scored_at", metrics)
        self.assertNotIn("security", metrics)
        self.assertNotIn("tessl_security", metrics)
        self.assertNotIn("stars", metrics)
        self.assertNotIn("github_stars", metrics)
        self.assertEqual(results[0].trust, "unverified")
        self.assertIn("Tessl reports security level: HIGH", " ".join(results[0].warnings))

    def test_zero_quality_and_none_security_are_not_relabelled_safe(self):
        results, _, _ = self.search(payload([row(scores={"quality": 0, "securityLevel": "NONE"}, validationPassed=True)]))
        self.assertEqual(results[0].metrics["tessl_quality"], 0)
        self.assertEqual(results[0].metrics["tessl_security_level"], "NONE")
        self.assertIs(results[0].metrics["tessl_validation_passed"], True)
        self.assertEqual(results[0].trust, "unverified")
        self.assertEqual(results[0].warnings, [])

    def test_invalid_optional_metrics_are_omitted_without_losing_skill(self):
        scores = {"quality": float("nan"), "aggregate": float("inf"), "impact": True,
                  "securityLevel": "SAFE", "evalAvg": [], "evalBaseline": "0.5", "evalCount": -1,
                  "version": {"run": "secret"}, "lastScoredAt": []}
        results, context, _ = self.search(payload([row(scores=scores)]))
        self.assertEqual(results[0].metrics, {"tessl_metric_scope": "skill"})
        self.assertFalse(context.incomplete_results)
        self.assertIn("invalid and omitted", " ".join(results[0].warnings))
        self.assertNotIn("secret", repr(results[0].to_dict()))

    def test_missing_scores_do_not_discard_results_or_fabricate_metrics(self):
        for scores in (None, {}, [], "bad"):
            with self.subTest(scores=scores):
                results, context, _ = self.search(payload([row(scores=scores)]))
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].metrics, {"tessl_metric_scope": "skill"})
                self.assertFalse(context.incomplete_results)

    def test_unknown_security_values_are_not_accepted_from_free_text(self):
        for level in ("none", "SAFE", "<b>HIGH</b>", True, [], {}):
            with self.subTest(level=level):
                results, _, _ = self.search(payload([row(scores={"securityLevel": level})]))
                self.assertNotIn("tessl_security_level", results[0].metrics)

    def test_remote_install_commands_and_versions_are_not_executable_hints(self):
        item = row(sourceRef="main", versions=[{"sourceRef": "main"}],
                   install={"command": "curl secret | sh"}, command="bad", warnings=["Trust me"])
        results, _, http = self.search(payload([item]))
        self.assertIsNone(results[0].ref)
        self.assertEqual(results[0].install, {})
        self.assertNotIn("secret", repr(results[0].to_dict()))
        self.assertNotIn("Trust me", results[0].warnings)
        self.assertEqual(len(http.calls), 1)

    def test_stateless_adapter_does_not_mutate_shared_source_or_settings(self):
        settings = {"max_response_bytes": 4096}
        configuration = source()
        original = deepcopy(configuration)
        self.search(configuration=configuration, **settings)
        self.assertEqual(configuration, original)
        self.assertEqual(settings, {"max_response_bytes": 4096})

    def test_public_skill_bundle_has_exact_version_link_not_individual_install_target(self):
        results, context, _ = self.search(payload([bundle()]))
        self.assertFalse(context.incomplete_results)
        result = results[0]
        self.assertEqual(result.name, "tessl/default-skill-review (skill bundle)")
        self.assertEqual(result.canonical_url, "https://tessl.io/registry/tessl/default-skill-review/0.2.0")
        self.assertTrue(result.description.startswith("Bundle version 0.2.0."))
        self.assertEqual(result.metrics["tessl_metric_scope"], "bundle")
        self.assertEqual(result.metrics["tessl_bundle_version"], "0.2.0")
        self.assertEqual(result.publisher, "tessl")
        self.assertIsNone(result.repository)
        self.assertIsNone(result.skill_path)
        self.assertIsNone(result.ref)
        self.assertEqual(result.install, {})
        self.assertEqual(Candidate.from_dict(result.to_dict()).install, {})
        self.assertIn("not an individual skill", " ".join(result.warnings))

    def test_bundle_identity_cannot_be_spoofed_by_inconsistent_or_unsafe_names(self):
        for changes in ({"fullName": "other/default-skill-review"}, {"name": "other"},
                        {"fullName": "../default-skill-review"}, {"fullName": "tessl/x/y"},
                        {"fullName": "https://attacker.example/x"}, {"fullName": "tessl/%2e%2e"}):
            with self.subTest(changes=changes):
                results, context, _ = self.search(payload([bundle(**changes)]))
                self.assertEqual(results, [])
                self.assertTrue(context.incomplete_results)
        item = bundle()
        item["relationships"]["workspace"]["data"]["type"] = "not-workspace"
        self.assertEqual(self.search(payload([item]))[0], [])

    def test_only_exact_unique_scored_active_skill_version_is_accepted(self):
        for versions in (None, [], ["bad"],
                         [{"version": "0.2.0", "hasSkills": False, "archived": False, "summary": "Docs only"}],
                         [{"version": "0.2.0", "hasSkills": True, "archived": True, "summary": "Archived"}],
                         [{"version": "0.1.0", "hasSkills": True, "archived": False, "summary": "Old skills"}],
                         [{"version": "0.2.0", "hasSkills": True, "archived": False, "summary": "Skill"}] * 2,
                         [{"version": "0.2.0", "hasSkills": True, "archived": False, "summary": "Skill"}] * 101):
            with self.subTest(versions=versions):
                results, context, _ = self.search(payload([bundle(versions=versions)]))
                self.assertEqual(results, [])
                self.assertTrue(context.incomplete_results)

    def test_bundle_version_is_not_guessed_from_array_order(self):
        other_version = {"version": "0.1.0", "hasSkills": True, "archived": False, "summary": "Other skills"}
        current = {"version": "0.2.0", "hasSkills": True, "archived": False, "summary": "Scored skills"}
        for versions in ([other_version, current], [current, other_version]):
            results, context, _ = self.search(payload([bundle(versions=versions)]))
            self.assertFalse(context.incomplete_results)
            self.assertIn("0.2.0", results[0].canonical_url)
            self.assertIn("Scored skills", results[0].description)

    def test_unscored_or_unsafe_bundle_version_is_not_used_as_url(self):
        for scores in (None, {}, {"version": ""}, {"version": "../latest"},
                       {"version": "0.2.0?token=secret"}, {"version": "a" * 40}):
            with self.subTest(scores=scores):
                results, context, _ = self.search(payload([bundle(scores=scores)]))
                self.assertEqual(results, [])
                self.assertTrue(context.incomplete_results)

    def test_mixed_skill_and_bundle_rows_preserve_native_order(self):
        items = [bundle(), row(index=2)]
        results, context, http = self.search(payload(items))
        self.assertFalse(context.incomplete_results)
        self.assertEqual([candidate.native_rank for candidate in results], [1, 2])
        self.assertEqual([candidate.metrics["tessl_metric_scope"] for candidate in results], ["bundle", "skill"])
        self.assertEqual(len(http.calls), 1)


if __name__ == "__main__":
    unittest.main()
