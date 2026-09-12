from __future__ import annotations

import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.adapters.base import SourceUnavailable
from universal_skill_finder.adapters.registries import ClawHubAdapter, HttpJsonAdapter, SkillsShAdapter, _github_install
from universal_skill_finder.config import EffectiveConfig
from universal_skill_finder.http import FinderHttpError, HttpClient, HttpResponse, SafeRedirectHandler
from universal_skill_finder.federation import UniversalSkillFinder
from universal_skill_finder.models import Candidate, Coverage, SearchReport
from universal_skill_finder.presentation import render_report
from universal_skill_finder.text import clean_text, parse_github_repository, parse_github_tree_url, safe_web_url, text_match_percent


class PayloadHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


def registry(adapter, **kwargs):
    return {"id": "test-source", "kind": "registry", "adapter": adapter, "base_url": "https://catalog.example", **kwargs}


class Response(io.BytesIO):
    status = 200
    headers = {}

    def geturl(self):
        return "https://catalog.example/search"


class NetworkBoundaryTests(unittest.TestCase):
    def test_redirect_cannot_contact_unplanned_origin_or_private_service(self):
        request = Request("https://catalog.example/search?q=private", headers={"Authorization": "Bearer secret"})
        for target in ("https://other.example/", "https://127.0.0.1/admin", "https://169.254.169.254/", "https://catalog.example:444/"):
            with self.subTest(target=target), self.assertRaisesRegex(FinderHttpError, "cross-origin"):
                SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)

    def test_same_origin_redirect_preserves_auth_and_normalizes_default_port(self):
        request = Request("https://catalog.example/search", headers={"Authorization": "Bearer secret"})
        result = SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, "https://catalog.example:443/api/search")
        self.assertEqual(result.get_header("Authorization"), "Bearer secret")

    def test_redirect_rejects_unsafe_scheme_and_credentials(self):
        request = Request("https://catalog.example/search")
        for target in ("http://catalog.example/", "file:///etc/passwd", "https://user:secret@catalog.example/", "https://catalog.example/\nadmin"):  # pragma: allowlist secret - synthetic URL rejection fixture
            with self.subTest(target=target), self.assertRaises(FinderHttpError):
                SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)

    def test_invalid_network_urls_are_rejected_before_request(self):
        for url in ("https://user:secret@example.com", "https://example.com/#secret", "https://example.com:bad/", "https://[", "https://example.com/\r\nheader", "file:///etc/passwd"):  # pragma: allowlist secret - synthetic URL rejection fixture
            with self.subTest(url=url), patch("universal_skill_finder.http.build_opener") as opener, self.assertRaises(FinderHttpError) as raised:
                HttpClient().get_bytes(url)
            opener.assert_not_called()
            self.assertNotIn("secret", str(raised.exception))

    def test_explicit_user_configured_private_https_remains_supported(self):
        HttpClient._validate_url("https://10.1.2.3:8443/catalog")
        HttpClient._validate_url("http://127.0.0.1:8080/catalog")

    def test_header_injection_does_not_echo_secret(self):
        with patch("universal_skill_finder.http.build_opener") as opener, self.assertRaises(FinderHttpError) as raised:
            HttpClient().get_bytes("https://catalog.example", headers={"Authorization": "Bearer secret\r\nX-Injected: yes"})
        opener.assert_not_called()
        self.assertNotIn("secret", str(raised.exception))

    def test_response_byte_limit_is_enforced(self):
        with patch("universal_skill_finder.http.build_opener") as opener:
            opener.return_value.open.return_value = Response(b"12345")
            with self.assertRaisesRegex(FinderHttpError, "exceeded 4 bytes"):
                HttpClient(max_bytes=4).get_bytes("https://catalog.example/search")

    def test_response_read_deadline_bounds_drip_feeding(self):
        with patch("universal_skill_finder.http.build_opener") as opener, patch("universal_skill_finder.http.time.monotonic", side_effect=[0, 0, 11]):
            opener.return_value.open.return_value = Response(b"hello")
            with self.assertRaisesRegex(FinderHttpError, "timeout"):
                HttpClient(timeout=10).get_bytes("https://catalog.example/search")

    def test_http_and_transport_errors_do_not_expose_query_strings(self):
        url = "https://catalog.example/search?token=private-value"
        for error in (HTTPError(url, 401, "Unauthorized", {}, io.BytesIO()), URLError("private-value")):
            with self.subTest(error=type(error).__name__), patch("universal_skill_finder.http.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(FinderHttpError) as raised:
                    HttpClient().get_bytes(url)
                self.assertNotIn("private-value", str(raised.exception))

    def test_deep_json_is_reported_as_invalid_json(self):
        response = HttpResponse(b"[" * 3000 + b"]" * 3000, 200, {}, "https://catalog.example")
        with patch.object(HttpClient, "request", return_value=response), self.assertRaisesRegex(FinderHttpError, "invalid JSON"):
            HttpClient().get_json("https://catalog.example")


class UntrustedRegistryTests(unittest.TestCase):
    def test_remote_candidate_cannot_supply_its_own_eligible_link_state(self):
        source = registry("skills-sh")
        raw = Candidate(
            "owner/repo/skill", "Example", "Example skill", source["id"],
            source["kind"], source["adapter"],
            link_proofs=[{
                "role": "listing", "url": "https://skills.sh/owner/repo/skill",
                "status": "eligible", "identity_basis": "skills-sh-html-route-v2",
            }],
        )
        candidate = UniversalSkillFinder._validated_candidates([raw.to_dict()], source)[0]
        self.assertEqual(candidate.link_proofs[0]["status"], "not_checked")
        self.assertIn("reviewed destination validation", candidate.link_proofs[0]["detail"])

    def test_remote_candidate_actionable_status_spellings_cannot_gain_click_authority(self):
        source = registry("skills-sh")
        forged_url = "https://attacker.example/forged-skill"
        legitimate_url = "https://skills.sh/owner/repo/skill"
        for status in ("eligible", "verified", "reachable", "ELIGIBLE", "Verified", "ReAcHaBlE"):
            with self.subTest(status=status):
                raw = Candidate(
                    "owner/repo/skill", "Example", "Example skill", source["id"],
                    source["kind"], source["adapter"],
                    link_proofs=[{
                        "role": "listing", "url": forged_url, "status": status,
                        "identity_basis": "attacker-supplied",
                    }],
                )
                candidate = UniversalSkillFinder._validated_candidates([raw.to_dict()], source)[0]
                self.assertEqual(candidate.link_proofs[0]["status"], "not_checked")

                finder = UniversalSkillFinder(
                    EffectiveConfig({}, [], [source], Path("unused.json"), {}),
                    http=PayloadHttp({}),
                )
                result = finder._merge([candidate], "Example")[0]
                result.link_proofs.append({
                    "role": "listing", "url": legitimate_url, "status": "eligible",
                    "identity_basis": "reviewed-destination",
                })
                result.validation_status = finder._validation_status(result)
                result.result_number = 1
                report = SearchReport(
                    query="Example", results=[result], coverage=[Coverage(source["id"], "ok", 1)],
                    generated_at="2026-09-12T00:00:00+00:00", configuration_path="unused.json",
                    accepted_occurrences=1, unique_count=1, eligible_count=1,
                    page_shown=1, materialized_total=1,
                )
                outputs = [render_report(report, assistant="codex", format=kind) for kind in ("markdown", "plain", "html")]
                self.assertEqual(result.validation_status, "eligible")
                self.assertTrue(all(legitimate_url in output for output in outputs))
                self.assertTrue(all(forged_url not in output for output in outputs))

    def test_legacy_archive_link_claim_is_demoted_when_read_from_cache(self):
        candidate = Candidate.from_dict({
            "native_id": "owner/repo:skills/example", "name": "Example", "description": "Example skill",
            "source_id": "repo", "source_kind": "repository", "adapter": "github-repo",
            "repository": "owner/repo", "skill_path": "skills/example", "ref": "main",
            "target_proof": {"kind": "github_archive", "status": "eligible"},
            "link_proofs": [{
                "role": "skill_destination", "url": "https://github.com/owner/repo/tree/main/skills/example",
                "status": "eligible", "method": "bounded_archive",
            }],
        })
        self.assertEqual(candidate.target_proof["status"], "eligible")
        self.assertEqual(candidate.link_proofs[0]["status"], "not_checked")

    def test_ambient_oidc_is_not_sent_to_custom_skills_sh_endpoint(self):
        for base_url in ("https://attacker.example", "https://skills.sh.attacker.example", "http://skills.sh", "https://skills.sh:444"):
            http = PayloadHttp({"skills": []})
            with self.subTest(base_url=base_url), patch.dict(os.environ, {"VERCEL_OIDC_TOKEN": "secret"}):
                SkillsShAdapter().search(registry("skills-sh", base_url=base_url), "pdf", 3, SimpleNamespace(http=http))
            self.assertNotIn("Authorization", http.calls[0][1].get("headers", {}))

    def test_official_skills_sh_origin_ignores_ambient_oidc(self):
        http = PayloadHttp({"skills": []})
        with patch.dict(os.environ, {"VERCEL_OIDC_TOKEN": "secret"}):
            SkillsShAdapter().search(registry("skills-sh", base_url="https://skills.sh"), "pdf", 3, SimpleNamespace(http=http))
        self.assertTrue(http.calls[0][0].endswith("/api/search"))
        self.assertNotIn("Authorization", http.calls[0][1].get("headers", {}))

    def test_install_handoff_rejects_shell_option_and_path_injection(self):
        for repository, path, ref, slug in (
            ("--help/repo", "skills/pdf", "main", "pdf"),
            ("acme/repo;whoami", "skills/pdf", "main", "pdf"),
            ("acme/repo", "../../outside", "main", "pdf"),
            ("acme/repo", "/tmp/outside", "main", "pdf"),
            ("acme/repo", "skills/pdf", "--upload-pack=evil", "pdf"),
            ("acme/repo", "skills/pdf", "main", "$(touch /tmp/pwned)"),
            ("acme/repo", "skills/pdf", "main", "--all"),
        ):
            with self.subTest(repository=repository, path=path, ref=ref, slug=slug):
                self.assertEqual(_github_install(repository, path, ref, slug), {})
        self.assertEqual(_github_install("acme/repo", "skills/pdf", "main", "pdf")["repository"], "acme/repo")

    def test_clawhub_does_not_emit_registry_supplied_shell_command(self):
        http = PayloadHttp({"results": [{"ownerHandle": "alice", "slug": "pdf", "install": {"reference": "$(touch /tmp/pwned)"}}]})
        result = ClawHubAdapter().search(registry("clawhub"), "pdf", 3, SimpleNamespace(http=http))[0]
        self.assertEqual(result.install, {})

    def test_generic_mapping_missing_items_is_schema_mismatch(self):
        http = PayloadHttp({"changed_contract": []})
        source = registry("http-json-v1", mapping={"items": "results", "name": "title"})
        with self.assertRaises(SourceUnavailable) as raised:
            HttpJsonAdapter().search(source, "pdf", 3, SimpleNamespace(http=http))
        self.assertEqual(raised.exception.status, "schema_mismatch")

    def test_registry_javascript_urls_do_not_become_clickable_results(self):
        http = PayloadHttp({"results": [{"title": "PDF", "url": "javascript:alert(1)"}]})
        source = registry("http-json-v1", mapping={"items": "results", "name": "title", "url": "url"})
        result = HttpJsonAdapter().search(source, "pdf", 3, SimpleNamespace(http=http))[0]
        self.assertIsNone(result.canonical_url)

    def test_github_identity_parser_rejects_deceptive_or_malformed_locations(self):
        for value in ("../repo", "acme/..", "https://github.com/acme/../evil", "https://github.com/acme/repo;evil", "https://user:secret@github.com/acme/repo", "https://github.com:444/acme/repo", "https://evil.example/acme/repo", "http://github.com/acme/repo", "acme/repo\n"):  # pragma: allowlist secret - synthetic URL rejection fixture
            with self.subTest(value=value):
                self.assertIsNone(parse_github_repository(value))
        self.assertEqual(parse_github_repository("https://github.com/acme/repo.git"), "acme/repo")

    def test_github_tree_parser_rejects_encoded_traversal(self):
        self.assertEqual(parse_github_tree_url("https://github.com/acme/repo/tree/main/skills/%2e%2e/%2e%2e/outside"), (None, None, None))
        self.assertEqual(parse_github_tree_url("https://github.com/acme/repo/blob/main/skills/pdf/SKILL.md"), ("acme/repo", "skills/pdf", "main"))

    def test_terminal_metadata_cannot_hide_text_with_bidi_controls(self):
        self.assertEqual(clean_text("safe\u202eevil\u202c\x1b\ntext"), "safeevil text")
        self.assertIsNone(safe_web_url("https://github.com/\u202eevil"))

    def test_short_queries_do_not_match_unrelated_substrings(self):
        for query, description in (("go", "Django tools"), ("rag", "Storage helper"), ("ai", "Maintain packages")):
            with self.subTest(query=query):
                self.assertEqual(text_match_percent(query, description), 0)
        self.assertEqual(text_match_percent("pdf", "pdf-reader"), 100)
        self.assertEqual(text_match_percent("skill", "Find skills"), 100)
        self.assertEqual(text_match_percent("policy", "Review policies"), 100)


if __name__ == "__main__":
    unittest.main()
