from __future__ import annotations

import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import source_test_selection as selection
import test_sources


class SourceSelectionTests(unittest.TestCase):
    def test_explicit_source_and_case_are_independently_selectable(self):
        result = selection.explicit_selection(["skillsmp"], ["many"])
        self.assertEqual(result.sources, ("skillsmp",))
        self.assertEqual(result.cases, ("many",))

    def test_case_not_shared_by_requested_sources_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not selectable"):
            selection.explicit_selection(["skillsmp", "tessl"], ["invalid-route"])

    def test_unknown_source_is_rejected_before_a_test_or_network_call(self):
        with self.assertRaisesRegex(ValueError, "unknown source"):
            selection.explicit_selection(["not-a-source"], [])

    def test_changed_since_uses_a_verified_commit_and_bounded_read_only_git(self):
        commit = "a" * 40
        git = str(ROOT / "fixture-git")
        completed = [
            CompletedProcess([], 0, commit + "\n", ""),
            CompletedProcess([], 0, "skills/find/scripts/universal_skill_finder/adapters/registries.py\n", ""),
            CompletedProcess([], 0, "", ""),
        ]
        with patch.object(selection.shutil, "which", return_value=git), \
             patch.object(selection.subprocess, "run", side_effect=completed) as run:
            outcome = selection.changed_selection("origin/main")
        self.assertEqual(outcome.reason, "changed source boundary")
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(calls[0], [git, "rev-parse", "--verify", "--end-of-options", "origin/main^{commit}"])
        self.assertEqual(calls[1], [git, "diff", "--no-ext-diff", "--no-textconv", "--name-only", commit, "--"])
        self.assertEqual(calls[2], [git, "status", "--porcelain"])
        self.assertTrue(all(call.kwargs["timeout"] == selection.GIT_TIMEOUT_SECONDS for call in run.call_args_list))

    def test_changed_since_treats_option_like_or_invalid_refs_as_data(self):
        git = str(ROOT / "fixture-git")
        with patch.object(selection.shutil, "which", return_value=git), \
             patch.object(selection.subprocess, "run", return_value=CompletedProcess([], 1, "", "")) as run:
            with self.assertRaisesRegex(ValueError, "base ref is not a commit"):
                selection.changed_selection("--upload-pack=unexpected")
        self.assertEqual(run.call_args.args[0], [
            git, "rev-parse", "--verify", "--end-of-options", "--upload-pack=unexpected^{commit}",
        ])

        with patch.object(selection.shutil, "which", return_value=git), \
             patch.object(selection.subprocess, "run", return_value=CompletedProcess([], 0, "a" * 41 + "\n", "")):
            with self.assertRaisesRegex(ValueError, "base ref is not a commit"):
                selection.changed_selection("main")

    def test_changed_since_reports_missing_or_timed_out_git(self):
        with patch.object(selection.shutil, "which", return_value=None), \
             self.assertRaisesRegex(ValueError, "git executable is unavailable"):
            selection.changed_selection("main")
        with patch.object(selection.shutil, "which", return_value=str(ROOT / "fixture-git")), \
             patch.object(selection.subprocess, "run", side_effect=TimeoutExpired(["git"], 5)), \
             self.assertRaisesRegex(ValueError, "git inspection timed out"):
            selection.changed_selection("main")


class SourceRunnerTests(unittest.TestCase):
    def test_list_needs_no_selection_or_network(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(test_sources.main(["--list"]), 0)

    def test_offline_selected_source_reports_pass_when_hermetic_child_passes(self):
        with patch.object(test_sources, "_run_offline", return_value=test_sources.PASS) as run:
            with redirect_stdout(StringIO()):
                self.assertEqual(test_sources.main(["--source", "skillsmp", "--offline"]), 0)
        self.assertTrue(run.called)

    def test_live_lane_is_explicitly_incomplete_in_wave_one(self):
        with patch.object(test_sources, "_run_offline", return_value=test_sources.PASS):
            with redirect_stdout(StringIO()):
                self.assertEqual(test_sources.main(["--source", "skillsmp"]), 3)

    def test_fail_and_invalid_invocation_have_distinct_exit_codes(self):
        with patch.object(test_sources, "_run_offline", return_value=test_sources.FAIL):
            with redirect_stdout(StringIO()):
                self.assertEqual(test_sources.main(["--all", "--offline"]), 1)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(test_sources.main(["--offline"]), 2)
            self.assertEqual(test_sources.main(["--offline", "--live", "--all"]), 2)

    def test_authenticated_live_mode_reaches_explicit_live_planning(self):
        with patch.object(test_sources, "_run_offline", return_value=test_sources.PASS), patch.object(
            test_sources, "_run_live", return_value=[(test_sources.SKIP, "skillsmp", "credential unavailable")],
        ) as run:
            with redirect_stdout(StringIO()):
                self.assertEqual(test_sources.main([
                    "--live", "--source", "skillsmp", "--auth-mode", "authenticated",
                ]), 3)
        self.assertEqual(run.call_args.args[1].auth_mode, "authenticated")

    def test_offline_child_is_bounded_and_timeout_is_failure(self):
        selected = selection.Selection(("skillsmp",), ("contract",), "fixture")
        with patch.object(test_sources.subprocess, "run", return_value=CompletedProcess([], 0)) as run:
            self.assertEqual(test_sources._run_offline(selected, full=False), test_sources.PASS)
        self.assertEqual(run.call_args.kwargs["timeout"], test_sources.OFFLINE_TEST_TIMEOUT_SECONDS)
        with patch.object(test_sources.subprocess, "run", side_effect=TimeoutExpired(["python"], 120)):
            self.assertEqual(test_sources._run_offline(selected, full=False), test_sources.FAIL)
