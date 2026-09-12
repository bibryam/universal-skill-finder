from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]


class SourceWorkflowStatusTests(unittest.TestCase):
    def run_step(self, name: str, command: str, status: int):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("Bash is unavailable for the workflow status check")
        workflow = (ROOT / ".github/workflows/live-sources.yml").read_text(encoding="utf-8")
        block = workflow.split("      - name: " + name + "\n", 1)[1]
        self.assertTrue(block.startswith("        shell: bash\n        run: |\n"))
        lines = []
        for line in block.split("        run: |\n", 1)[1].splitlines():
            if not line.startswith("          "):
                break
            lines.append(line[10:])
        script = "\n".join(lines)
        self.assertEqual(script.count(command), 1)
        # Simulate only the source runner, preserving the real workflow's tee
        # pipeline and branching. No registry or other external process runs.
        script = script.replace(command, f"(exit {status})")
        with TemporaryDirectory() as temporary:
            return subprocess.run([bash, "--noprofile", "--norc", "-e", "-c", script],
                                  cwd=temporary, capture_output=True, text=True, timeout=5)

    def test_offline_pipeline_preserves_runner_failure(self):
        for status in (0, 1, 2, 3):
            with self.subTest(status=status):
                outcome = self.run_step("Run hermetic source contracts",
                                        "python scripts/test_sources.py --all --offline", status)
                self.assertEqual(outcome.returncode, status)

    def test_live_pipeline_fails_contract_errors_and_warns_for_incomplete_coverage(self):
        for status in (0, 1, 2, 3):
            with self.subTest(status=status):
                outcome = self.run_step("Record live coverage status",
                                        "python scripts/test_sources.py --live --all", status)
                self.assertEqual(outcome.returncode, status if status in (1, 2) else 0)
                self.assertEqual("::warning::Live source coverage is incomplete." in outcome.stdout,
                                 status == 3)
