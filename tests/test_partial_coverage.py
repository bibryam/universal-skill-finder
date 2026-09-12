from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import AdapterContext
from universal_skill_finder.cache import Cache
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.http import FinderHttpError, HttpClient
from universal_skill_finder.models import Candidate, SearchReport
from test_universal_skill_finder import StaticAdapter, candidate, finder_config, fixture_finder, registry_source
from test_security_network import Response


class PartialAdapter(StaticAdapter):
    def __init__(self, candidates=None, *, incomplete=True, detail=None):
        super().__init__(candidates)
        self.incomplete = incomplete
        self.detail = detail

    def search(self, source, query, limit, context):
        context.incomplete_results = self.incomplete
        context.detail = self.detail
        return super().search(source, query, limit, context)


class PartialCoverageTests(unittest.TestCase):
    def finder(self, root: Path, adapter: StaticAdapter):
        source = registry_source("one", "skills-sh")
        finder = fixture_finder(finder_config(root, [source]), Cache(root / "cache"))
        finder.adapter_map = {"skills-sh": adapter}
        return finder, source

    def test_partial_results_preserved_live_fresh_cache_and_offline(self):
        row = candidate("one", "acme/skills", "pdf", rank=1)
        adapter = PartialAdapter([row], detail="Provider stopped before all results were returned.")
        with TemporaryDirectory() as temporary:
            finder, _ = self.finder(Path(temporary), adapter)
            reports = [finder.search("pdf"), finder.search("pdf"), finder.search("pdf", offline=True)]
            self.assertEqual([report.coverage[0].status for report in reports], ["ok", "cached", "cached"])
            self.assertEqual(adapter.calls, 1)
            self.assertEqual([len(report.results) for report in reports], [1, 1, 0])
            self.assertEqual(len(reports[-1].candidate_previews), 1)
            for report in reports:
                self.assertTrue(report.coverage[0].incomplete_results)
                self.assertEqual(report.coverage[0].detail, adapter.detail)
                self.assertTrue(report.to_dict()["coverage"][0]["incomplete_results"])
                self.assertEqual(report.to_dict()["schema_version"], 2)

    def test_zero_partial_results_never_become_complete_no_match(self):
        with TemporaryDirectory() as temporary:
            finder, _ = self.finder(Path(temporary), PartialAdapter([]))
            for offline in (False, True):
                report = finder.search("pdf", offline=offline)
                self.assertEqual(report.results, [])
                self.assertTrue(report.coverage[0].incomplete_results)
                self.assertIn("incomplete", report.coverage[0].detail)

    def test_complete_zero_results_and_legacy_context_defaults_remain_complete(self):
        with TemporaryDirectory() as temporary:
            finder, _ = self.finder(Path(temporary), StaticAdapter([]))
            for offline in (False, True):
                coverage = finder.search("pdf", offline=offline).coverage[0]
                self.assertFalse(coverage.incomplete_results)
                self.assertIsNone(coverage.detail)
            context = AdapterContext(finder.http, finder.cache, {})
            self.assertFalse(context.incomplete_results)
            self.assertIsNone(context.detail)

    def test_refresh_can_replace_partial_cache_with_complete_results(self):
        adapter = PartialAdapter([])
        with TemporaryDirectory() as temporary:
            finder, _ = self.finder(Path(temporary), adapter)
            self.assertTrue(finder.search("pdf").coverage[0].incomplete_results)
            adapter.incomplete = False
            complete = finder.search("pdf", refresh=True)
            self.assertFalse(complete.coverage[0].incomplete_results)
            self.assertFalse(finder.search("pdf", offline=True).coverage[0].incomplete_results)
            self.assertEqual(adapter.calls, 2)

    def test_partial_detail_is_bounded_and_control_characters_removed(self):
        detail = "provider\n\x1b\u202elimit " + "x" * 1000
        with TemporaryDirectory() as temporary:
            finder, _ = self.finder(Path(temporary), PartialAdapter([], detail=detail))
            for offline in (False, True):
                coverage = finder.search("pdf", offline=offline).coverage[0]
                self.assertLessEqual(len(coverage.detail), 500)
                for control in ("\n", "\x1b", "\u202e"):
                    self.assertNotIn(control, coverage.detail)

    def test_malformed_partial_cache_metadata_is_a_miss(self):
        bad_fields = [{"incomplete_results": "false"}, {"incomplete_results": 1},
                      {"incomplete_results": None}, {"detail": {"instruction": "untrusted"}}]
        for bad in bad_fields:
            with self.subTest(bad=bad), TemporaryDirectory() as temporary:
                adapter = StaticAdapter([])
                finder, source = self.finder(Path(temporary), adapter)
                key = finder._cache_key(source, "pdf", 10)
                finder.cache.write("queries", key, {"candidates": [], **bad})
                report = finder.search("pdf", offline=True)
                self.assertEqual(report.coverage[0].status, "offline_miss")
                self.assertEqual(adapter.calls, 0)
                self.assertEqual(finder.search("pdf").coverage[0].status, "ok")
                self.assertEqual(adapter.calls, 1)

    def test_old_complete_cache_payloads_remain_compatible(self):
        with TemporaryDirectory() as temporary:
            finder, source = self.finder(Path(temporary), StaticAdapter([]))
            key = finder._cache_key(source, "pdf", 10)
            finder.cache.write("queries", key, {"candidates": []})
            coverage = finder.search("pdf", offline=True).coverage[0]
            self.assertEqual(coverage.status, "cached")
            self.assertFalse(coverage.incomplete_results)

    def test_invalid_live_context_metadata_is_isolated_as_schema_failure(self):
        with TemporaryDirectory() as temporary:
            finder, _ = self.finder(Path(temporary), PartialAdapter([], incomplete="true"))
            coverage = finder.search("pdf").coverage[0]
            self.assertEqual(coverage.status, "schema_mismatch")
            self.assertIn("partial-result", coverage.detail)

    def test_installed_annotations_are_never_taken_from_remote_or_cached_candidates(self):
        raw = candidate("one", "acme/skills", "pdf", rank=1).to_dict()
        raw["installed"] = {"state": "installed", "path": "/forged/local/path"}
        raw["installed_scan"] = {"complete": True}
        parsed = Candidate.from_dict(raw)
        self.assertNotIn("installed", parsed.to_dict())
        with TemporaryDirectory() as temporary:
            finder, source = self.finder(Path(temporary), StaticAdapter([parsed]))
            finder.cache.write("queries", finder._cache_key(source, "pdf", 10), {"candidates": [raw]})
            report = finder.search("pdf", offline=True)
            self.assertEqual(report.results, [])
            self.assertNotIn("installed", report.candidate_previews[0])
            self.assertEqual(report.installed_scan, {})
            live = finder.search("pdf")
            live.results[0].installed = {"state": "installed"}
            report.installed_scan = {"complete": True}
            self.assertEqual(live.to_dict()["results"][0]["installed"], {"state": "installed"})
            self.assertEqual(report.to_dict()["installed_scan"], {"complete": True})
            self.assertEqual(SearchReport("pdf", [], [], "now", "config").installed_scan, {})


class RateLimitAndTimeoutTests(unittest.TestCase):
    def http_error(self, status: int, headers: dict, *, url="https://api.github.com/search/code?token=synthetic-private"):
        body = io.BytesIO(b'{"message":"synthetic private body"}')
        error = HTTPError(url, status, "untrusted status text", headers, body)
        with patch("universal_skill_finder.http.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(FinderHttpError) as raised:
                HttpClient().request("GET", url)
        self.assertTrue(body.closed)
        rendered = str(raised.exception) + json.dumps(raised.exception.headers)
        self.assertNotIn("synthetic-private", rendered)
        self.assertNotIn("private body", rendered)
        self.assertNotIn("untrusted status text", rendered)
        return raised.exception

    def test_github_primary_and_secondary_rate_limits_are_not_auth_failures(self):
        for headers in ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"},
                        {"Retry-After": "60", "X-RateLimit-Remaining": "99"}):
            with self.subTest(headers=headers):
                error = self.http_error(403, headers)
                self.assertTrue(error.rate_limited)
                coverage = UniversalSkillFinder._failure_coverage(registry_source("one", "skills-sh"), error, 0)
                self.assertEqual(coverage.status, "rate_limited")

    def test_401_and_nonquota_403_remain_auth_failures(self):
        for status, headers in ((401, {"X-RateLimit-Remaining": "0", "Retry-After": "60"}),
                                (403, {}), (403, {"X-RateLimit-Remaining": "42"})):
            with self.subTest(status=status, headers=headers):
                error = self.http_error(status, headers)
                self.assertFalse(error.rate_limited)
                self.assertEqual(UniversalSkillFinder._failure_coverage(registry_source("one", "skills-sh"), error, 0).status, "auth_failed")

    def test_non_github_403_headers_do_not_imply_github_quota(self):
        for url in ("https://api.github.com.example/search", "https://api.github.com:444/search", "https://registry.example/search"):
            with self.subTest(url=url):
                self.assertFalse(self.http_error(403, {"X-RateLimit-Remaining": "0"}, url=url).rate_limited)
        self.assertTrue(self.http_error(429, {}, url="https://registry.example/search").rate_limited)

    def test_error_headers_are_allowlisted_bounded_and_sanitized(self):
        error = self.http_error(429, {
            "Retry-After": "60\r\nprivate", "X-RateLimit-Remaining": "9" * 500,
            "X-RateLimit-Reset": "synthetic-private", "Authorization": "synthetic-private",
            "Set-Cookie": "synthetic-private", "Location": "https://private.example/token",
        })
        self.assertEqual(error.headers, {})
        self.assertIsNone(error.retry_after)
        date = "Wed, 21 Oct 2015 07:28:00 GMT"
        dated = self.http_error(403, {"Retry-After": date})
        self.assertTrue(dated.rate_limited)
        self.assertIn("21 Oct 2015", dated.retry_after)
        numeric = self.http_error(403, {"Retry-After": "00060"})
        self.assertEqual(numeric.retry_after, "60")
        coverage = UniversalSkillFinder._failure_coverage(registry_source("one", "skills-sh"), numeric, 0)
        self.assertIn("retry after 60", coverage.detail)

    def test_request_timeout_override_is_clamped_and_does_not_mutate_client(self):
        client = HttpClient(timeout=5)
        for override, expected in ((None, 5), (1, 1), (100, 5)):
            with self.subTest(override=override), patch("universal_skill_finder.http.build_opener") as opener:
                opener.return_value.open.return_value = Response(b"{}")
                self.assertEqual(client.request("GET", "https://catalog.example/search", timeout=override).data, b"{}")
                self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], expected)
                self.assertEqual(client.timeout, 5)

    def test_invalid_timeout_overrides_fail_before_network(self):
        for invalid in (0, -1, float("nan"), float("inf"), True, "1", 1 << 3000):
            with self.subTest(kind=type(invalid).__name__), patch("universal_skill_finder.http.build_opener") as opener:
                with self.assertRaisesRegex(FinderHttpError, "finite and positive"):
                    HttpClient().request("GET", "https://catalog.example/search", timeout=invalid)
                opener.assert_not_called()

    def test_override_controls_body_read_deadline(self):
        with patch("universal_skill_finder.http.build_opener") as opener, patch("universal_skill_finder.http.time.monotonic", side_effect=[0, 0, 1.1]):
            opener.return_value.open.return_value = Response(b"hello")
            with self.assertRaisesRegex(FinderHttpError, "timeout"):
                HttpClient(timeout=10).request("GET", "https://catalog.example/search", timeout=1)


if __name__ == "__main__":
    unittest.main()
