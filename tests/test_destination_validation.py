from __future__ import annotations

import sys
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))

from universal_skill_finder.validation import (
    AnonymousPublicTransport,
    AnonymousResponse,
    Destination,
    MAX_TARGET_BYTES,
    ProviderProfile,
    SKILLSMP_PROFILE,
    github_repository_destination,
    github_skill_destination,
    reviewed_destination,
    resolve_github_target,
    skillhub_destination,
    TargetResolutionCache,
    _bounded_dns,
    _supervised_https_get,
    target_proof_link,
    validate_destination,
    validate_ranked,
)
from universal_skill_finder.runtime import Deadline, NetworkPolicy, PermitPool, RequestBudget
from universal_skill_finder.snapshot import validate_frozen_result_record


@dataclass
class Lease:
    status: str = "ok"
    released: bool = False
    def release(self): self.released = True


class Budget:
    def __init__(self, exhausted=False): self.exhausted, self.calls = exhausted, []
    def reserve(self, kind, amount, deadline):
        self.calls.append((kind, amount))
        return "budget_exhausted" if self.exhausted else Lease()


class Permits:
    def __init__(self): self.calls = []
    def acquire(self, origin, phase, deadline): self.calls.append((origin, phase)); return Lease()


class Transport:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class RawResponse:
    def __init__(self, status=200, body=b"ok", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}
    def read(self, _limit): return self._body


class PinnedConnection:
    def __init__(self, peer="8.8.8.8", response=None):
        self.peer_address, self.response, self.calls, self.closed = peer, response or RawResponse(), [], False
    def request(self, method, target, headers): self.calls.append((method, target, headers))
    def getresponse(self): return self.response
    def close(self): self.closed = True


def resolver(host, port): return ["8.8.8.8"]
def identity(response, expected): return response.body == expected


def _blocked_dns_worker(_host, _port, _output):
    """Spawn-safe test worker: no resolver or network access occurs here."""
    time.sleep(10)


def _blocked_https_worker(_url, _addresses, _headers, _max_bytes, _expires_at, _output):
    """Spawn-safe test worker: simulates a header/body trickle that never ends."""
    time.sleep(10)


class DestinationValidationTests(unittest.TestCase):
    def destination(self, url="https://skills.sh/example", expected=b"skill"):
        return Destination("one", "listing", url, expected, ProviderProfile("fixture", identity))

    def test_reviewed_skills_sh_redirect_and_actual_connection_address_can_be_eligible(self):
        transport = Transport([
            AnonymousResponse(302, {"location": "https://www.skills.sh/example"}, connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"skill", connection_address="8.8.8.8"),
        ])
        proof = validate_destination(self.destination(), transport=transport, resolver=resolver, budget=Budget(),
                                     permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual((proof.status, proof.final_url), ("eligible", "https://www.skills.sh/example"))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0][2]["headers"], {"Accept": "text/html,application/xhtml+xml"})

    def test_unreviewed_redirect_and_actual_address_mismatch_do_not_become_eligible(self):
        redirect = validate_destination(self.destination(), transport=Transport([
            AnonymousResponse(302, {"location": "https://evil.example/x"}, connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(redirect.status, "unavailable")
        wrong_address = validate_destination(self.destination(), transport=Transport([
            AnonymousResponse(200, body=b"skill", connection_address="127.0.0.1")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(wrong_address.status, "inconclusive")

    def test_generic_not_found_text_and_http_200_are_not_a_soft_error_rule(self):
        no_identity = Destination("one", "listing", "https://skills.sh/example", b"skill", None)
        proof = validate_destination(no_identity, transport=Transport([
            AnonymousResponse(200, body=b"not found", connection_address="8.8.8.8")]), resolver=resolver,
            budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(proof.status, "not_checked")

    def test_missing_budget_is_not_checked_and_explicit_404_is_unavailable(self):
        skipped = validate_destination(self.destination(), transport=Transport([]), resolver=resolver, budget=Budget(True),
                                       permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(skipped.status, "not_checked")
        missing = validate_destination(self.destination(), transport=Transport([
            AnonymousResponse(404, connection_address="8.8.8.8")]), resolver=resolver, budget=Budget(), permits=Permits(),
            deadline=time.monotonic() + 5)
        self.assertEqual(missing.status, "unavailable")

    def test_expired_deadline_does_not_start_injected_resolution_or_a_network_request(self):
        calls = []

        def should_not_resolve(host, port):
            calls.append((host, port))
            return ["8.8.8.8"]

        proof = validate_destination(self.destination(), transport=Transport([]), resolver=should_not_resolve,
                                     budget=Budget(), permits=Permits(), deadline=time.monotonic() - 0.01)
        self.assertEqual((proof.status, calls), ("not_checked", []))

    def test_bounded_dns_timeout_terminates_and_reaps_a_blocked_child_without_network(self):
        children = []
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, "resolution deadline elapsed"):
            _bounded_dns("no-network.test", 443, time.monotonic() + 0.05,
                         _worker=_blocked_dns_worker, _on_start=children.append)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(children), 1)
        self.assertFalse(children[0].is_alive())
        self.assertIsNotNone(children[0].exitcode)

    def test_supervised_https_request_reaps_a_blocked_header_or_body_worker_without_network(self):
        children = []
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "request deadline elapsed"):
            _supervised_https_get(
                "https://provider.example/slow", ("8.8.8.8",), {"Accept": "text/plain"}, timeout=0.05,
                max_bytes=1024, _worker=_blocked_https_worker, _on_start=children.append,
            )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(len(children), 1)
        self.assertFalse(children[0].is_alive())
        self.assertIsNotNone(children[0].exitcode)

    def test_provisional_is_bounded_to_three_ranked_identities_and_final_replenishes(self):
        destinations = [Destination(str(index), "listing", f"https://skills.sh/{index}", b"skill", ProviderProfile("fixture", identity))
                        for index in range(5)]
        provisional = validate_ranked(destinations, phase="provisional", transport=Transport([
            AnonymousResponse(200, body=b"skill", connection_address="8.8.8.8") for _ in range(3)]), resolver=resolver,
            budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(list(provisional), ["0", "1", "2"])
        final = validate_ranked(destinations, phase="final", transport=Transport([
            AnonymousResponse(200, body=b"skill", connection_address="8.8.8.8") for _ in range(5)]), resolver=resolver,
            budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5, max_identities=5)
        self.assertEqual(list(final), ["0", "1", "2", "3", "4"])

    def test_final_validation_uses_bounded_parallelism_but_returns_rank_order(self):
        class ParallelTransport:
            def __init__(self):
                self.barrier = threading.Barrier(2)
                self.lock = threading.Lock()
                self.active = 0
                self.maximum = 0

            def request(self, *_args, **_kwargs):
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                self.barrier.wait(timeout=1)
                with self.lock:
                    self.active -= 1
                return AnonymousResponse(200, body=b"skill", connection_address="8.8.8.8")

        destinations = [self.destination(url=f"https://skills.sh/{identity}") for identity in ("one", "two")]
        destinations[0] = Destination("one", destinations[0].role, destinations[0].url,
                                      destinations[0].expected_identity, destinations[0].profile)
        destinations[1] = Destination("two", destinations[1].role, destinations[1].url,
                                      destinations[1].expected_identity, destinations[1].profile)
        transport = ParallelTransport()
        proofs = validate_ranked(
            destinations, phase="final", transport=transport, resolver=resolver,
            budget=Budget(), permits=Permits(), deadline=time.monotonic() + 2,
            workers=2,
        )
        self.assertEqual(list(proofs), ["one", "two"])
        self.assertTrue(all(proof.status == "eligible" for proof in proofs.values()))
        self.assertEqual(transport.maximum, 2)

    def test_bounded_alternate_can_repair_a_missing_primary_without_losing_rank_identity(self):
        destinations = [
            Destination("one", "listing", "https://skills.sh/missing", b"skill", ProviderProfile("fixture", identity)),
            Destination("one", "repository", "https://skills.sh/working", b"skill", ProviderProfile("fixture", identity)),
        ]
        proofs = validate_ranked(destinations, phase="final", transport=Transport([
            AnonymousResponse(404, connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"skill", connection_address="8.8.8.8"),
        ]), resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(proofs["one"].status, "eligible")

    def test_shared_runtime_budget_permit_deadline_and_json_safe_model_proof_are_consumed(self):
        policy = NetworkPolicy.for_mode()
        deadline = Deadline.start(policy)
        budget, permits = RequestBudget(requests=2), PermitPool(policy)
        proof = validate_destination(self.destination(), transport=Transport([
            AnonymousResponse(200, body=b"skill", connection_address="8.8.8.8")]), resolver=resolver,
            budget=budget, permits=permits, deadline=deadline)
        self.assertEqual(proof.status, "eligible")
        self.assertIsInstance(proof.redirect_chain, list)
        self.assertIsInstance(proof.checked_at, str)
        self.assertEqual(budget.snapshot()["request"]["used"], 1)
        self.assertEqual(permits._active, 0)

    def test_production_transport_pins_public_peer_preserves_host_path_and_sends_no_auth(self):
        connection = PinnedConnection(response=RawResponse(body=b"ok", headers={"X-Test": "yes"}))
        created = []
        transport = AnonymousPublicTransport(lambda host, port, addresses, timeout: created.append((host, port, addresses, timeout)) or connection)
        response = transport.request("GET", "https://provider.example/a/b?q=1", timeout=2, max_bytes=20,
                                     allowed_addresses=("8.8.8.8",), headers={"Accept": "text/html"})
        self.assertEqual(created[0][:3], ("provider.example", 443, ("8.8.8.8",)))
        self.assertEqual(connection.calls, [("GET", "/a/b?q=1", {"Accept": "text/html"})])
        self.assertEqual((response.connection_address, response.headers), ("8.8.8.8", {"x-test": "yes"}))
        self.assertTrue(connection.closed)

    def test_production_transport_refuses_http_auth_headers_and_unapproved_actual_peer(self):
        called = []
        transport = AnonymousPublicTransport(lambda *_: called.append(True) or PinnedConnection())
        with self.assertRaises(ValueError):
            transport.request("GET", "http://provider.example/x", timeout=1, max_bytes=10,
                              allowed_addresses=("8.8.8.8",), headers={"Accept": "text/html"})
        with self.assertRaises(ValueError):
            transport.request("GET", "https://provider.example/x", timeout=1, max_bytes=10,
                              allowed_addresses=("8.8.8.8",), headers={"Authorization": "Bearer no"})
        wrong_peer = AnonymousPublicTransport(lambda *_: PinnedConnection(peer="1.1.1.1"))
        with self.assertRaises(ValueError):
            wrong_peer.request("GET", "https://provider.example/x", timeout=1, max_bytes=10,
                               allowed_addresses=("8.8.8.8",), headers={"Accept": "text/html"})
        self.assertEqual(called, [])

    def test_skillsmp_requires_structured_identity_but_never_uses_generic_not_found_text(self):
        destination = reviewed_destination("one", role="listing", url="https://skillsmp.com/creators/owner/repository/blade",
                                           adapter="skillsmp", expected_identity={"id": "blade"})
        proof = validate_destination(destination, transport=Transport([
            AnonymousResponse(200, body=b'{"id":"blade","description":"not found text is ordinary content"}', connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(proof.status, "eligible")

    def test_reviewed_profiles_never_grant_arbitrary_host_port_userinfo_or_unsafe_path_authority(self):
        cases = [
            ("skills-sh", "https://skills.sh.evil/example"),
            ("skills-sh", "https://skills.sh@evil.example/example"),
            ("skills-sh", "https://skills.sh:8443/example"),
            ("skills-sh", "https://skills.sh/%2e%2e/secret"),
            ("skillsmp", "https://skillsmp.com/skill/blade"),
            ("skillsmp", "https://evil.example/skills/blade"),
            ("github-repo", "https://www.github.com/owner/repo/tree/main/skill"),
        ]
        for adapter, url in cases:
            with self.subTest(url=url):
                self.assertIsNone(reviewed_destination("one", role="listing" if adapter not in {"github-repo"} else "repository",
                                                       url=url, adapter=adapter, expected_identity={"id": "one"}).profile)
        calls = Transport([])
        tampered = Destination("one", "listing", "https://evil.example/skills/blade", {"id": "blade"}, SKILLSMP_PROFILE)
        proof = validate_destination(tampered, transport=calls, resolver=resolver, budget=Budget(), permits=Permits(),
                                     deadline=time.monotonic() + 5)
        self.assertEqual((proof.status, calls.calls), ("not_checked", []))

    def test_skills_sh_profile_allows_only_the_reviewed_www_redirect_pair(self):
        destination = reviewed_destination("one", role="listing", url="https://skills.sh/example",
                                           adapter="skills-sh", expected_identity={"id": "example"})
        proof = validate_destination(destination, transport=Transport([
            AnonymousResponse(302, {"location": "https://www.skills.sh/example"}, connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b'{"id":"example"}', connection_address="8.8.8.8"),
        ]), resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual((proof.status, proof.final_url), ("eligible", "https://www.skills.sh/example"))

    def test_skillhub_uses_singular_route_and_structured_missing_code_only(self):
        destination = skillhub_destination("one", base_url="https://skills.palebluedot.live", native_id="owner/blade")
        self.assertEqual(destination.url, "https://skills.palebluedot.live/skill/owner/blade")
        proof = validate_destination(destination, transport=Transport([
            AnonymousResponse(200, body=b'{"error":{"code":"SKILL_NOT_FOUND"}}', connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(proof.status, "unavailable")
        with self.assertRaises(ValueError):
            skillhub_destination("one", base_url="https://skills.palebluedot.live", native_id="owner/../blade")

    def test_github_proof_requires_exact_owner_repository_ref_path_and_metadata(self):
        destination = github_skill_destination("one", repository="owner/repo", ref="main", skill_path="skills/blade")
        valid = validate_destination(destination, transport=Transport([
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">', connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(
            (valid.status, valid.role, destination.url),
            ("eligible", "skill_destination", "https://github.com/owner/repo/tree/main/skills/blade"),
        )
        wrong = validate_destination(destination, transport=Transport([
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/other">', connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(wrong.status, "unavailable")

    def test_exact_target_and_all_reviewed_navigation_destinations_are_checked_separately(self):
        record = {
            "id": "one", "repository": "owner/repo", "ref": "main",
            "skill_path": "skills/humanize", "name": "Humanize",
            "occurrences": [{"adapter": "skills-sh", "ref": "main"}],
        }
        fixture = ProviderProfile("fixture", identity)
        destinations = [
            github_skill_destination("one", repository="owner/repo", ref="main", skill_path="skills/humanize"),
            Destination("one", "listing", "https://one.example/humanize", b"one", fixture),
            Destination("one", "listing", "https://two.example/humanize", b"two", fixture),
        ]
        transport = Transport([
            AnonymousResponse(200, body=b"---\nname: Humanize\n---\nRewrite prose.", connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">',
                              connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">',
                              connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"one", connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"two", connection_address="8.8.8.8"),
        ])

        outcome = validate_frozen_result_record(
            "one", record, destinations=destinations, transport=transport, resolver=resolver,
            budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5,
        )

        self.assertEqual((outcome["status"], outcome["target_proof"]["status"]), ("eligible", "eligible"))
        self.assertEqual(
            [(proof["role"], proof["url"], proof["status"]) for proof in outcome["link_proofs"]],
            [
                ("skill_destination", "https://github.com/owner/repo/tree/main/skills/humanize", "eligible"),
                ("repository", "https://github.com/owner/repo", "eligible"),
                ("listing", "https://one.example/humanize", "eligible"),
                ("listing", "https://two.example/humanize", "eligible"),
            ],
        )
        self.assertFalse(any(
            str(proof.get("url", "")).startswith("https://raw.githubusercontent.com/")
            for proof in outcome["link_proofs"]
        ))

    def test_stale_ref_repair_validates_only_the_resolved_github_tree(self):
        record = {
            "id": "one", "repository": "owner/repo", "ref": "old",
            "skill_path": "skills/humanize", "name": "Humanize",
            "occurrences": [{"adapter": "skills-sh", "ref": "old"}],
        }
        transport = Transport([
            AnonymousResponse(404, connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b'{"full_name":"owner/repo","default_branch":"main"}',
                              connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"---\nname: Humanize\n---\nRewrite prose.", connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">',
                              connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">',
                              connection_address="8.8.8.8"),
        ])

        outcome = validate_frozen_result_record(
            "one", record,
            destinations=[github_skill_destination(
                "one", repository="owner/repo", ref="old", skill_path="skills/humanize",
            )],
            transport=transport, resolver=resolver, budget=Budget(), permits=Permits(),
            deadline=time.monotonic() + 5,
        )

        self.assertEqual(outcome["target_proof"]["resolved"]["ref"], "main")
        self.assertEqual(outcome["link_proofs"][0]["url"],
                         "https://github.com/owner/repo/tree/main/skills/humanize")
        called_urls = [url for _method, url, _kwargs in transport.calls]
        self.assertIn("https://github.com/owner/repo/tree/main/skills/humanize", called_urls)
        self.assertIn("https://github.com/owner/repo", called_urls)
        self.assertNotIn("https://github.com/owner/repo/tree/old/skills/humanize", called_urls)

    def test_github_repository_proof_accepts_only_the_exact_repository_page(self):
        destination = github_repository_destination("repository", repository="owner/repository")
        self.assertEqual(destination.url, "https://github.com/owner/repository")
        valid = validate_destination(destination, transport=Transport([
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repository">', connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(valid.status, "eligible")
        wrong = validate_destination(destination, transport=Transport([
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="attacker/repo">', connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(wrong.status, "unavailable")

    def test_http_is_never_probed_and_access_denial_or_redirect_loops_are_inconclusive(self):
        http_destination = self.destination(url="http://skills.sh/example")
        skipped = validate_destination(http_destination, transport=Transport([]), resolver=resolver, budget=Budget(),
                                       permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(skipped.status, "not_checked")
        denied = validate_destination(self.destination(), transport=Transport([
            AnonymousResponse(403, connection_address="8.8.8.8")]), resolver=resolver, budget=Budget(), permits=Permits(),
            deadline=time.monotonic() + 5)
        self.assertEqual(denied.status, "inconclusive")
        redirects = validate_destination(self.destination(), transport=Transport([
            AnonymousResponse(302, {"location": "https://skills.sh/example"}, connection_address="8.8.8.8")
            for _ in range(4)]), resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual(redirects.status, "inconclusive")

    def test_frozen_record_helper_persists_link_evidence_without_elevating_target_proof(self):
        record = {"id": "one", "target_proof": {"kind": "github", "status": "not_checked",
                                                     "reported": {"repository": "owner/repo"}}}
        destination = github_skill_destination("one", repository="owner/repo", ref="main", skill_path="skills/blade")
        outcome = validate_frozen_result_record("one", record, destinations=[destination], transport=Transport([
            AnonymousResponse(200, body=b'<meta name="octolytics-dimension-repository_nwo" content="owner/repo">', connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual((outcome["status"], outcome["link_proofs"][0]["status"]), ("eligible", "eligible"))
        self.assertNotIn("target_proof", outcome)

    def test_exact_target_resolution_keeps_raw_content_separate_from_browser_proof(self):
        body = b"---\nname: Humanize\n---\nRewrite prose.\n"
        proof = resolve_github_target(
            "one", {"repository": "owner/repo", "ref": "main", "skill_path": "skills/humanize",
                    "name": "humanize", "path_is_exact": True, "ref_pinned": True},
            transport=Transport([AnonymousResponse(200, body=body, connection_address="8.8.8.8")]),
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5,
        )
        self.assertEqual((proof["kind"], proof["status"], proof["ref"], proof["skill_path"]),
                         ("github", "eligible", "main", "skills/humanize"))
        self.assertEqual(proof["resolved"]["url"],
                         "https://raw.githubusercontent.com/owner/repo/main/skills/humanize/SKILL.md")
        self.assertEqual(proof["resolved"]["browser_url"],
                         "https://github.com/owner/repo/tree/main/skills/humanize")
        self.assertEqual(proof["resolved"]["browse_url"],
                         "https://github.com/owner/repo/tree/main/skills/humanize")
        self.assertEqual(proof["url"], proof["resolved"]["url"])
        self.assertEqual(proof["identity_basis"], "github-exact-skill-md-v1")
        self.assertEqual(proof["resolved"]["actual_name"], "Humanize")
        link = target_proof_link(proof)
        self.assertEqual((link.status, link.role, link.url), ("not_checked", "skill_destination", None))
        self.assertIn("requires separate validation", link.detail)

    def test_pathless_resolution_uses_one_authoritative_default_then_root_only_with_matching_name(self):
        transport = Transport([
            AnonymousResponse(200, body=b'{"full_name":"owner/repo","default_branch":"trunk"}', connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"---\nname: Humanize\n---\nText", connection_address="8.8.8.8"),
        ])
        proof = resolve_github_target(
            "one", {"repository": "owner/repo", "name": "Humanize"}, transport=transport,
            resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5,
        )
        self.assertEqual((proof["status"], proof["ref"], proof["skill_path"]), ("eligible", "trunk", "."))
        self.assertEqual([url for _method, url, _kwargs in transport.calls], [
            "https://api.github.com/repos/owner/repo",
            "https://raw.githubusercontent.com/owner/repo/trunk/SKILL.md",
        ])

    def test_pathless_pinned_ref_checks_only_pinned_root_and_never_default_branch(self):
        transport = Transport([
            AnonymousResponse(200, body=b"---\nname: Humanize\n---\nText", connection_address="8.8.8.8"),
        ])
        proof = resolve_github_target(
            "one", {"repository": "owner/repo", "ref": "release", "ref_pinned": True, "name": "Humanize"},
            transport=transport, resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5,
        )
        self.assertEqual((proof["status"], proof["ref"], proof["skill_path"]), ("eligible", "release", "."))
        self.assertEqual([url for _method, url, _kwargs in transport.calls], [
            "https://raw.githubusercontent.com/owner/repo/release/SKILL.md",
        ])

    def test_target_repair_is_once_for_explicitly_unpinned_known_path_never_for_pin(self):
        reported = {"repository": "owner/repo", "ref": "old", "skill_path": "skills/humanize",
                    "name": "humanize", "path_is_exact": True, "ref_pinned": False}
        repaired_transport = Transport([
            AnonymousResponse(404, connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b'{"full_name":"owner/repo","default_branch":"main"}', connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"---\nname: humanize\n---\nText", connection_address="8.8.8.8"),
        ])
        repaired = resolve_github_target("one", reported, transport=repaired_transport, resolver=resolver,
                                         budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual((repaired["status"], repaired["ref"]), ("eligible", "main"))
        self.assertEqual(len(repaired_transport.calls), 3)
        pinned = dict(reported, ref_pinned=True)
        pinned_transport = Transport([AnonymousResponse(404, connection_address="8.8.8.8")])
        no_repair = resolve_github_target("one", pinned, transport=pinned_transport, resolver=resolver,
                                          budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5)
        self.assertEqual((no_repair["status"], len(pinned_transport.calls)), ("unavailable", 1))

    def test_target_resolution_rejects_mismatched_content_and_reuses_default_branch_fact(self):
        cache = TargetResolutionCache()
        transport = Transport([
            AnonymousResponse(200, body=b'{"full_name":"owner/repo","default_branch":"main"}', connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"---\nname: Other\n---\nText", connection_address="8.8.8.8"),
            AnonymousResponse(200, body=b"---\nname: Other\n---\nText", connection_address="8.8.8.8"),
        ])
        first = resolve_github_target("one", {"repository": "owner/repo", "ref": "HEAD", "skill_path": "one",
                                                "name": "Humanize", "path_is_exact": True}, transport=transport,
                                      resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5,
                                      cache=cache)
        second = resolve_github_target("two", {"repository": "owner/repo", "ref": "HEAD", "skill_path": "two",
                                                 "name": "Humanize", "path_is_exact": True}, transport=transport,
                                       resolver=resolver, budget=Budget(), permits=Permits(), deadline=time.monotonic() + 5,
                                       cache=cache)
        self.assertEqual((first["status"], second["status"]), ("unavailable", "unavailable"))
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(transport.calls[1][1], "https://raw.githubusercontent.com/owner/repo/main/one/SKILL.md")
        self.assertEqual(transport.calls[2][1], "https://raw.githubusercontent.com/owner/repo/main/two/SKILL.md")

    def test_target_resolution_consumes_shared_validation_and_github_api_budgets_and_releases_permit(self):
        policy = NetworkPolicy.for_mode()
        budget, permits = RequestBudget(requests=2, bytes=MAX_TARGET_BYTES + 64 * 1024,
                                        validation_requests=2, github_api_requests=1), PermitPool(policy)
        proof = resolve_github_target(
            "one", {"repository": "owner/repo", "name": "Humanize"}, transport=Transport([
                AnonymousResponse(200, body=b'{"full_name":"owner/repo","default_branch":"main"}', connection_address="8.8.8.8"),
                AnonymousResponse(200, body=b"---\nname: Humanize\n---\nText", connection_address="8.8.8.8"),
            ]), resolver=resolver, budget=budget, permits=permits, deadline=Deadline.start(policy),
        )
        self.assertEqual(proof["status"], "eligible")
        counters = budget.snapshot()
        self.assertEqual((counters["request"]["used"], counters["validation"]["used"], counters["github_api"]["used"]), (2, 2, 1))
        self.assertEqual(permits._active, 0)
