from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import AdapterContext, SourceUnavailable
from universal_skill_finder.adapters.github_search import (
    API_ORIGIN,
    MAX_FILE_BYTES,
    MAX_FILE_REQUESTS,
    MAX_METADATA_REQUESTS,
    MAX_SECONDS,
    SEARCH_SCOPE_NOTE,
    GitHubCodeSearchAdapter,
    _blob_bytes,
    _search_query,
)
from universal_skill_finder.cache import Cache
from universal_skill_finder.http import FinderHttpError, HttpResponse


COMMIT = "a" * 40
TOKEN_ENV = "UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN"


def source(**changes):
    return {"id": "github-discovery", "kind": "registry", "adapter": "github-code-search",
            "base_url": API_ORIGIN, "auth_env": TOKEN_ENV, "public_only": True, **changes}


def skill(name="pdf-tools", description="Read and fill PDF forms"):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Instructions\n".encode("utf-8")


def blob(raw):
    sha = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw, usedforsecurity=False).hexdigest()
    return {"sha": sha, "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode("ascii")}


def row(raw=None, *, repository="example/skills", path="pdf/SKILL.md", **changes):
    raw = skill() if raw is None else raw
    return {"repository": {"full_name": repository, "private": False}, "path": path, "sha": blob(raw)["sha"],
            "html_url": f"https://github.com/{repository}/blob/{COMMIT}/{path}", **changes}


class PayloadHttp:
    def __init__(self, rows=(), *, raw=None, incomplete=False, total=None, search_payload=None):
        raw = skill() if raw is None else raw
        self.responses = {API_ORIGIN + "/search/code": search_payload if search_payload is not None else {
            "items": list(rows), "incomplete_results": incomplete, "total_count": len(rows) if total is None else total,
        }}
        for item in rows:
            repository = item.get("repository", {}).get("full_name", "example/skills")
            if isinstance(repository, str):
                self.responses[f"{API_ORIGIN}/repos/{repository}/git/blobs/{item.get('sha')}"] = blob(raw)
                self.responses[f"{API_ORIGIN}/repos/{repository}"] = {
                    "full_name": repository, "private": False, "stargazers_count": 42,
                }
        self.calls = []
        self.after_request = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses[url]
        if self.after_request:
            self.after_request(url)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, HttpResponse):
            return value
        data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        return HttpResponse(data, 200, {}, url)


class GitHubSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {TOKEN_ENV: "fixture-token"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def context(self, http, **settings):
        return AdapterContext(http, Cache(Path(self.temp.name) / "cache"), settings)

    def search(self, http, *, configuration=None, query="PDF forms", limit=10, **settings):
        context = self.context(http, **settings)
        result = GitHubCodeSearchAdapter().search(configuration or source(), query, limit, context)
        return result, context

    def test_missing_explicit_token_does_not_use_ambient_credentials(self):
        http = PayloadHttp()
        with patch.dict(os.environ, {"GH_TOKEN": "ambient-value", "GITHUB_TOKEN": "ambient-value"}, clear=True):
            with self.assertRaises(SourceUnavailable) as caught:
                self.search(http)
        self.assertEqual(caught.exception.status, "auth_missing")
        self.assertEqual(http.calls, [])

    def test_source_cannot_override_origin_credentials_headers_or_public_scope(self):
        for changes in (
            {"base_url": "https://attacker.example"}, {"base_url": API_ORIGIN + "/"},
            {"base_url": "http://api.github.com"}, {"base_url": "https://api.github.com:444"},
            {"endpoint": API_ORIGIN + "/search/code"}, {"headers": {}},
            {"auth_optional": True}, {"auth_optional": 0}, {"public_only": False}, {"public_only": "true"},
        ):
            http = PayloadHttp()
            with self.subTest(changes=changes), self.assertRaises(SourceUnavailable) as caught:
                self.search(http, configuration=source(**changes))
            self.assertEqual(caught.exception.status, "invalid_config")
            self.assertEqual(http.calls, [])

    def test_invalid_token_is_never_sent_or_echoed(self):
        http = PayloadHttp()
        with patch.dict(os.environ, {TOKEN_ENV: "private-value\r\nX-Injected: yes"}):
            with self.assertRaises(SourceUnavailable) as caught:
                self.search(http)
        self.assertNotIn("private-value", str(caught.exception))
        self.assertEqual(http.calls, [])

    def test_offline_direct_call_never_contacts_github(self):
        http = PayloadHttp()
        context = self.context(http)
        context.offline = True
        with self.assertRaises(SourceUnavailable) as caught:
            GitHubCodeSearchAdapter().search(source(), "PDF", 10, context)
        self.assertEqual(caught.exception.status, "offline_miss")
        self.assertEqual(http.calls, [])

    def test_query_words_cannot_inject_qualifiers_or_boolean_operators(self):
        value, shortened = _search_query('PDF repo:private/secrets OR filename:token "NOT" org:private')
        self.assertFalse(shortened)
        self.assertEqual(value.count(":"), 1)
        self.assertTrue(value.endswith("filename:SKILL.md"))
        self.assertIn('"or"', value)
        self.assertIn('"not"', value)
        self.assertNotIn("repo:", value)
        self.assertNotIn("org:", value)

    def test_long_query_is_bounded_and_shortening_is_disclosed(self):
        query = " ".join("capability" + str(index) for index in range(60))
        value, shortened = _search_query(query)
        self.assertLessEqual(len(value), 256)
        self.assertTrue(shortened)
        _, context = self.search(PayloadHttp(), query=query)
        self.assertTrue(context.incomplete_results)
        self.assertIn("shortened", context.detail)

    def test_symbol_only_query_fails_before_request(self):
        http = PayloadHttp()
        with self.assertRaises(SourceUnavailable) as caught:
            self.search(http, query='"/::***')
        self.assertEqual(caught.exception.status, "invalid_query")
        self.assertEqual(http.calls, [])

    def test_native_rank_filepath_hash_and_commit_are_preserved(self):
        private = row(repository="private/hidden", repository_override=None)
        private["repository"]["private"] = True
        public = row()
        http = PayloadHttp([private, public])
        results, context = self.search(http)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.native_rank, 2)
        self.assertEqual(result.native_id, "example/skills:pdf/SKILL.md")
        self.assertEqual(result.skill_path, "pdf")
        self.assertEqual(result.ref, COMMIT)
        self.assertNotEqual(result.ref, public["sha"])
        self.assertEqual(result.content_sha256, hashlib.sha256(skill()).hexdigest())
        self.assertEqual(result.metrics, {})
        self.assertTrue(context.incomplete_results)
        self.assertNotIn("private/hidden", context.detail)
        self.assertFalse(any("private/hidden" in url for _, url, _ in http.calls))

    def test_no_blob_hash_is_promoted_to_a_commit_ref(self):
        for url in (None, "https://github.com/example/skills/blob/main/pdf/SKILL.md",
                    f"https://github.com/other/repo/blob/{COMMIT}/pdf/SKILL.md",
                    f"https://github.com/example/skills/blob/{COMMIT}/wrong/SKILL.md",
                    f"https://github.com/example/skills/blob/{COMMIT}/pdf/SKILL.md?unexpected=1"):
            item = row(html_url=url)
            with self.subTest(url=url):
                results, _ = self.search(PayloadHttp([item]))
                self.assertIsNone(results[0].ref)
                self.assertEqual(results[0].canonical_url, "https://github.com/example/skills")
                self.assertEqual(results[0].install, {})

    def test_returned_urls_never_determine_authenticated_request_destinations(self):
        item = row(url="https://attacker.example/content", git_url="https://attacker.example/blob")
        item["repository"]["url"] = "https://attacker.example/repository"
        http = PayloadHttp([item])
        results, _ = self.search(http)
        self.assertEqual(len(results), 1)
        for method, url, options in http.calls:
            self.assertEqual(method, "GET")
            self.assertEqual(urlparse(url).netloc, "api.github.com")
            if url.endswith("/search/code"):
                self.assertEqual(options["headers"]["Authorization"], "Bearer fixture-token")
            else:
                self.assertNotIn("Authorization", options["headers"])
            self.assertGreater(options["max_bytes"], 0)
            self.assertGreater(options["timeout"], 0)
            self.assertLessEqual(options["timeout"], MAX_SECONDS)

    def test_visibility_change_cannot_use_token_to_fetch_now_private_content(self):
        item = row()
        for status in (403, 404):
            http = PayloadHttp([item])
            http.responses[f"{API_ORIGIN}/repos/example/skills/git/blobs/{item['sha']}"] = FinderHttpError(
                "public content unavailable", status=status,
            )
            with self.subTest(status=status):
                results, context = self.search(http)
                self.assertEqual(results, [])
                self.assertTrue(context.incomplete_results)
                self.assertEqual(len(http.calls), 2)
                self.assertNotIn("Authorization", http.calls[1][2]["headers"])

    def test_private_invalid_and_unresolved_rows_never_trigger_content_fetches(self):
        rows = [row() for _ in range(7)]
        rows[0]["repository"]["private"] = True
        rows[1]["repository"].pop("private")
        rows[2]["repository"]["full_name"] = "--option/repo"
        rows[3]["path"] = "../pdf/SKILL.md"
        rows[4]["path"] = "pdf/README.md"
        rows[5]["sha"] = "not-a-sha"
        rows[6]["repository"] = "not-an-object"
        http = PayloadHttp()
        http.responses[API_ORIGIN + "/search/code"] = {"items": [*rows, None], "total_count": 8, "incomplete_results": False}
        results, context = self.search(http)
        self.assertEqual(results, [])
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(context.incomplete_results)

    def test_blob_base64_size_encoding_and_identity_are_checked(self):
        original = blob(skill())
        for change in (
            {"encoding": "utf-8"}, {"size": True}, {"size": MAX_FILE_BYTES + 1},
            {"size": original["size"] + 1}, {"content": "!not-base64!"},
            {"sha": "b" * 40}, {"content": base64.b64encode(b"x" * original["size"]).decode("ascii")},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                _blob_bytes({**original, **change}, original["sha"], MAX_FILE_BYTES)
        self.assertEqual(_blob_bytes(original, original["sha"], MAX_FILE_BYTES), skill())

    def test_line_wrapped_base64_and_root_skill_are_supported(self):
        item = row(path="SKILL.md")
        http = PayloadHttp([item])
        response = http.responses[f"{API_ORIGIN}/repos/example/skills/git/blobs/{item['sha']}"]
        content = response["content"]
        response["content"] = "\n".join(content[index:index + 60] for index in range(0, len(content), 60)) + "\n"
        results, _ = self.search(http)
        self.assertEqual(results[0].skill_path, ".")

    def test_required_frontmatter_is_verified_not_guessed_from_filename(self):
        for raw in (b"# PDF skill without metadata", skill(name="Invalid Name"), skill(description="[]"),
                    skill(description="true"), skill(description="123"), skill(name="true"),
                    b"---\nname: pdf-tools\ndescription: Read PDFs\n# Missing closing delimiter",
                    b"---\nname: pdf-tools\n---\n# No description", b"\xff\xfe"):
            http = PayloadHttp([row(raw)], raw=raw)
            with self.subTest(raw=raw):
                results, context = self.search(http)
                self.assertEqual(results, [])
                self.assertTrue(context.incomplete_results)

    def test_text_descriptions_can_be_quoted_or_folded(self):
        for raw in (skill(description="'[PDF] process files'"), skill(description="'123'"),
                    skill(description=">\n  Read PDF files\n  and fill forms"),
                    skill(description="Read PDFs --- including forms")):
            with self.subTest(raw=raw):
                results, _ = self.search(PayloadHttp([row(raw)], raw=raw))
                self.assertEqual(len(results), 1)

    def test_provider_incomplete_flag_survives_even_when_there_are_no_matches(self):
        results, context = self.search(PayloadHttp(incomplete=True))
        self.assertEqual(results, [])
        self.assertTrue(context.incomplete_results)
        self.assertIn("GitHub reported incomplete", context.detail)

    def test_reaching_requested_top_n_is_not_itself_incomplete(self):
        rows = [row(path=f"pdf-{index}/SKILL.md") for index in range(10)]
        results, context = self.search(PayloadHttp(rows, total=1000), limit=3)
        self.assertEqual(len(results), 3)
        self.assertFalse(context.incomplete_results)
        self.assertEqual(context.detail, SEARCH_SCOPE_NOTE)

    def test_empty_complete_search_discloses_token_scope_without_marking_partial(self):
        results, context = self.search(PayloadHttp())
        self.assertEqual(results, [])
        self.assertFalse(context.incomplete_results)
        self.assertEqual(context.detail, SEARCH_SCOPE_NOTE)

    def test_fixed_file_fetch_cap_marks_unfulfilled_larger_request_partial(self):
        rows = [row(path=f"pdf-{index}/SKILL.md") for index in range(20)]
        http = PayloadHttp(rows, total=1000)
        results, context = self.search(http, limit=20)
        self.assertEqual(len(results), MAX_FILE_REQUESTS)
        self.assertEqual(sum("/git/blobs/" in url for _, url, _ in http.calls), MAX_FILE_REQUESTS)
        self.assertTrue(context.incomplete_results)
        self.assertIn("file-fetch limit", context.detail)

    def test_duplicate_rows_do_not_consume_file_fetch_budget_twice(self):
        item = row()
        http = PayloadHttp([item, deepcopy(item)])
        results, _ = self.search(http)
        self.assertEqual(len(results), 1)
        self.assertEqual(sum("/git/blobs/" in url for _, url, _ in http.calls), 1)

    def test_cached_metadata_is_optional_and_never_fetched_in_foreground(self):
        rows = [row(repository=f"example/skills-{index}") for index in range(5)]
        http = PayloadHttp(rows)
        cache = Cache(Path(self.temp.name) / "cache")
        for index in range(MAX_METADATA_REQUESTS):
            repository = f"example/skills-{index}"
            key = cache.key("github-repository-metrics-v1", repository.casefold())
            cache.write("repository-metadata", key, {
                "github_stars": index, "github_stars_scope": "repository",
                "github_stars_repository": repository,
                "github_stars_observed_at": "2026-09-10T00:00:00+00:00",
            })
        results, context = self.search(http)
        metadata_calls = [url for _, url, _ in http.calls if "/repos/" in url and "/git/blobs/" not in url]
        self.assertEqual(metadata_calls, [])
        self.assertEqual(len(results), 5)
        self.assertFalse(context.incomplete_results)
        self.assertEqual(results[0].metrics["github_stars"], 0)
        self.assertEqual(results[2].metrics["github_stars"], 2)
        self.assertEqual(results[-1].metrics, {})

    def test_invalid_private_or_other_repository_star_payloads_are_omitted(self):
        for payload in ({"private": True, "full_name": "example/skills", "stargazers_count": 500},
                        {"private": False, "full_name": "other/skills", "stargazers_count": 500},
                        {"private": False, "full_name": "example/skills", "stargazers_count": True}):
            http = PayloadHttp([row()])
            http.responses[API_ORIGIN + "/repos/example/skills"] = payload
            with self.subTest(payload=payload):
                results, context = self.search(http)
                self.assertEqual(results[0].metrics, {})
                self.assertFalse(context.incomplete_results)

    def test_malformed_search_schema_is_failure_not_empty_success(self):
        for payload in ([], {"items": {}}, {"items": [], "incomplete_results": "false", "total_count": 0},
                        {"items": [], "incomplete_results": False, "total_count": True}):
            http = PayloadHttp(search_payload=payload)
            with self.subTest(payload=payload), self.assertRaises(SourceUnavailable) as caught:
                self.search(http)
            self.assertEqual(caught.exception.status, "schema_mismatch")
            self.assertEqual(len(http.calls), 1)

    def test_excessive_json_depth_is_rejected_even_in_unknown_fields(self):
        nested = []
        for _ in range(101):
            nested = [nested]
        payload = {"items": [], "total_count": 0, "incomplete_results": False, "unknown": nested}
        http = PayloadHttp(search_payload=payload)
        with self.assertRaises(SourceUnavailable) as caught:
            self.search(http)
        self.assertEqual(caught.exception.status, "schema_mismatch")
        self.assertEqual(len(http.calls), 1)

    def test_excessive_blob_json_depth_skips_content_and_preserves_partial_status(self):
        item = row()
        payload = blob(skill())
        nested = []
        for _ in range(101):
            nested = [nested]
        payload["unknown"] = nested
        http = PayloadHttp([item])
        http.responses[f"{API_ORIGIN}/repos/example/skills/git/blobs/{item['sha']}"] = payload
        results, context = self.search(http)
        self.assertEqual(results, [])
        self.assertTrue(context.incomplete_results)
        self.assertEqual(len(http.calls), 2)

    def test_substituted_response_origin_is_rejected(self):
        http = PayloadHttp(search_payload=HttpResponse(
            b'{"items":[],"total_count":0,"incomplete_results":false}', 200, {}, "https://attacker.example/search/code",
        ))
        with self.assertRaises(SourceUnavailable) as caught:
            self.search(http)
        self.assertEqual(caught.exception.status, "schema_mismatch")
        self.assertEqual(len(http.calls), 1)

    def test_missing_file_is_partial_and_does_not_discard_other_valid_skills(self):
        rows = [row(path="missing/SKILL.md"), row(skill(name="pdf-reader"), path="reader/SKILL.md")]
        http = PayloadHttp(rows)
        http.responses[f"{API_ORIGIN}/repos/example/skills/git/blobs/{rows[0]['sha']}"] = FinderHttpError("not found", status=404)
        http.responses[f"{API_ORIGIN}/repos/example/skills/git/blobs/{rows[1]['sha']}"] = blob(skill(name="pdf-reader"))
        results, context = self.search(http)
        self.assertEqual([candidate.name for candidate in results], ["pdf-reader"])
        self.assertTrue(context.incomplete_results)

    def test_file_rate_limit_preserves_prior_candidates_and_stops_requests(self):
        rows = [row(), row(skill(name="pdf-reader"), path="reader/SKILL.md")]
        http = PayloadHttp(rows)
        http.responses[f"{API_ORIGIN}/repos/example/skills/git/blobs/{rows[1]['sha']}"] = FinderHttpError(
            "quota", status=403, rate_limited=True,
        )
        results, context = self.search(http)
        self.assertEqual(len(results), 1)
        self.assertTrue(context.incomplete_results)
        self.assertIn("rate-limited", context.detail)
        self.assertEqual(context.rate_limit_headers, {})
        self.assertEqual(len(http.calls), 3)

    def test_total_time_budget_is_forwarded_without_extending_it(self):
        clock = SimpleNamespace(now=0.0)
        http = PayloadHttp([row()])
        http.after_request = lambda url: setattr(clock, "now", clock.now + (1 if url.endswith("/search/code") else MAX_SECONDS))
        with patch("universal_skill_finder.adapters.github_search.time.monotonic", side_effect=lambda: clock.now):
            results, context = self.search(http)
        self.assertEqual(results, [])
        self.assertTrue(context.incomplete_results)
        self.assertIn("time budget", context.detail)
        self.assertEqual([options["timeout"] for _, _, options in http.calls], [MAX_SECONDS, MAX_SECONDS - 1])

    def test_configured_response_and_skill_limits_are_not_bypassed(self):
        http = PayloadHttp([row()])
        with self.assertRaises(SourceUnavailable):
            self.search(http, max_response_bytes=8)
        self.assertEqual(http.calls[0][2]["max_bytes"], 8)
        results, context = self.search(PayloadHttp([row()]), max_skill_file_bytes=8)
        self.assertEqual(results, [])
        self.assertTrue(context.incomplete_results)

    def test_total_bytes_budget_stops_before_following_requests(self):
        http = PayloadHttp([row()])
        search_size = len(json.dumps(http.responses[API_ORIGIN + "/search/code"]).encode("utf-8"))
        with patch("universal_skill_finder.adapters.github_search.MAX_TOTAL_BYTES", search_size):
            results, context = self.search(http)
        self.assertEqual(results, [])
        self.assertTrue(context.incomplete_results)
        self.assertEqual(len(http.calls), 1)


if __name__ == "__main__":
    unittest.main()
