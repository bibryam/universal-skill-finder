from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from live.source_checks import (
    CHILD_TIMEOUT_SECONDS, GLOBAL_TIMEOUT_SECONDS, INCONCLUSIVE, MAX_CONCURRENT_SOURCES,
    MAX_QUERIES_PER_SOURCE, PASS, SKIP, _valid_discovery_result_envelope,
    child_environment, classify, plan_sources, run_live_children,
)
from live.source_checks import _write_overlay


class LiveRunnerTests(unittest.TestCase):
    def test_live_contract_accepts_unchecked_discovery_results_with_new_ranking_trace(self):
        result = {
            "id": "skill:video", "validation_status": "not_checked",
            "ranking": {
                "algorithm_version": "discovery-70-20-10-v1",
                "components": {
                    "query_relevance": 1.0,
                    "source_signal": 0.5,
                    "corroboration": 0.0,
                },
            },
        }
        self.assertTrue(_valid_discovery_result_envelope({
            "discovery_mode": True, "results": [result],
        }))
        self.assertFalse(_valid_discovery_result_envelope({
            "discovery_mode": False, "results": [result],
        }))
        result["ranking"]["components"] = {"lexical": 1.0}
        self.assertFalse(_valid_discovery_result_envelope({
            "discovery_mode": True, "results": [result],
        }))

    def test_status_classifier_keeps_transient_failures_distinct(self):
        self.assertEqual(classify(0, "complete"), PASS)
        self.assertEqual(classify(429, "rate limit"), INCONCLUSIVE)
        self.assertEqual(classify(503, "outage"), INCONCLUSIVE)
        self.assertEqual(classify(1, "auth_missing"), SKIP)
        self.assertEqual(classify(1, "schema drift"), "FAIL")

    def test_planner_is_bounded_and_public_only_excludes_required_credentials(self):
        plans = plan_sources(["public", "private", "third"], public_only=True, auth_mode="auto",
                             required_auth_sources={"private"})
        self.assertEqual([item.source_id for item in plans], ["public", "third"])
        self.assertTrue(all(len(item.queries) <= MAX_QUERIES_PER_SOURCE for item in plans))
        self.assertEqual((MAX_CONCURRENT_SOURCES, CHILD_TIMEOUT_SECONDS, GLOBAL_TIMEOUT_SECONDS), (2, 90, 600))

    def test_authenticated_mode_names_only_the_selected_sources_credential(self):
        plans = plan_sources(
            ["public", "other"], public_only=False, auth_mode="authenticated",
            credential_names={"public": "PUBLIC_API_KEY"},
        )
        self.assertEqual(
            [(item.source_id, item.credential_name, item.use_credential) for item in plans],
            [("other", None, False), ("public", "PUBLIC_API_KEY", True)],
        )

    def test_child_environment_removes_source_auth_and_does_not_invent_credentials(self):
        environment = child_environment()
        self.assertNotIn("VERCEL_OIDC_TOKEN", environment)
        self.assertNotIn("UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN", environment)

    def test_anonymous_child_removes_ambient_proxy_routing_and_credentials(self):
        proxy_keys = ("HTTP_PROXY", "https_proxy", "ALL_PROXY", "ftp_proxy", "NO_PROXY")
        with patch.dict("os.environ", {key: "http://fixture.invalid:8080" for key in proxy_keys}, clear=True):
            environment = child_environment()
        self.assertFalse(any(key in environment for key in proxy_keys))

    def test_explicit_execution_uses_isolated_child_protocol_and_sanitized_summary(self):
        plan = plan_sources(["public"], public_only=True, auth_mode="auto")[0]
        child_output = ('{"source_id":"public","status":"PASS","queries":3,"reason":"completed",'
                        '"requests":5,"returned":2,"cap":3,"partial":false}\n')
        with patch("live.source_checks.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=child_output)) as run:
            outcomes = run_live_children([plan], [sys.executable, "child.py", "--child"], execute=True)
        self.assertEqual(
            [(item.status, item.request_count, item.query_count, item.returned_count, item.cap, item.partial)
             for item in outcomes],
            [(PASS, 5, 3, 2, 3, False)],
        )
        self.assertEqual(outcomes[0].reason, "completed")
        kwargs = run.call_args.kwargs
        environment = kwargs["env"]
        self.assertIn("UNIVERSAL_SKILL_FINDER_LIVE_CONFIG", environment)
        self.assertIn("UNIVERSAL_SKILL_FINDER_CACHE", environment)
        self.assertNotIn("UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN", environment)
        self.assertLessEqual(kwargs["timeout"], CHILD_TIMEOUT_SECONDS)

    def test_required_auth_is_skipped_without_spawning_child_when_named_credential_is_missing(self):
        plan = plan_sources(
            ["private"], public_only=False, auth_mode="auto", required_auth_sources={"private"},
            credential_names={"private": "PRIVATE_API_KEY"},
        )[0]
        with patch("live.source_checks.subprocess.run") as run:
            outcomes = run_live_children([plan], [sys.executable, "child.py"], execute=True)
        self.assertEqual([(item.status, item.request_count) for item in outcomes], [(SKIP, 0)])
        run.assert_not_called()

    def test_authenticated_child_receives_only_the_explicit_named_credential(self):
        plan = plan_sources(
            ["public"], public_only=False, auth_mode="authenticated",
            credential_names={"public": "PUBLIC_API_KEY"},
        )[0]
        child_output = ('{"source_id":"public","status":"PASS","queries":3,"reason":"completed",'
                        '"requests":4,"returned":1,"cap":3,"partial":false}\n')
        with patch.dict("os.environ", {
            # Synthetic isolation fixture, never an external credential.
            "PUBLIC_API_KEY": "selected-secret",  # pragma: allowlist secret
            "UNRELATED_TOKEN": "do-not-forward",
        }, clear=False), patch(
            "live.source_checks.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=child_output),
        ) as run:
            outcome = run_live_children([plan], [sys.executable, "child.py", "--child"], execute=True)[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["PUBLIC_API_KEY"], "selected-secret")
        self.assertNotIn("UNRELATED_TOKEN", environment)
        self.assertNotIn("selected-secret", repr(outcome))

    def test_temp_overlay_selects_only_explicit_source_and_is_not_persistent_enablement(self):
        with TemporaryDirectory() as temporary:
            overlay = Path(temporary) / "sources.json"
            _write_overlay(overlay, "normally-disabled")
            self.assertEqual(json.loads(overlay.read_text(encoding="utf-8")), {
                "schema_version": 1,
                "source_overrides": {"normally-disabled": {"enabled": True}},
            })
        self.assertFalse(overlay.exists())
