from __future__ import annotations

import io
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder.cli import _emit_rendered_report, main
from test_cli_v2 import frozen_report


class TtyOutput(io.StringIO):
    def isatty(self):
        return True


class CliTerminalStyleTests(unittest.TestCase):
    def test_machine_and_markdown_formats_never_receive_terminal_styling(self):
        for selected in ("json", "markdown", "html"):
            args = SimpleNamespace(json=False, markdown=False, html=False)
            setattr(args, selected, True)
            output = TtyOutput()
            with self.subTest(format=selected), redirect_stdout(output), \
                 patch("universal_skill_finder.cli.style_report") as style:
                _emit_rendered_report("unmodified artifact", args)
                style.assert_not_called()
            self.assertEqual(output.getvalue(), "unmodified artifact\n")

    def test_search_styles_terminal_but_preserves_clean_saved_plain_report(self):
        with TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "page.txt"
            output, error = TtyOutput(), io.StringIO()
            finder = SimpleNamespace(search=Mock(return_value=frozen_report()))
            with patch("universal_skill_finder.cli._load", return_value=(object(), object())), \
                 patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder), \
                 patch.dict(os.environ, {"TERM": "xterm-256color", "WT_SESSION": "fixture"}, clear=True), \
                 redirect_stdout(output), redirect_stderr(error):
                code = main(["search", "pdf", "--report-file", str(artifact), "--progress", "off"])
            self.assertEqual(code, 0)
            displayed = output.getvalue()
            saved = artifact.read_text(encoding="utf-8")
            self.assertIn("\x1b[", displayed)
            self.assertNotIn("\x1b", saved)
            self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", displayed), saved)

    def test_page_uses_terminal_emitter_without_coloring_its_artifact(self):
        with TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "snapshot.json"
            artifact = Path(temporary) / "page.txt"
            finder = SimpleNamespace(search=Mock(return_value=frozen_report()))
            with patch("universal_skill_finder.cli._load", return_value=(object(), object())), \
                 patch("universal_skill_finder.cli.UniversalSkillFinder", return_value=finder), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(["search", "pdf", "--count", "2", "--page-size", "1", "--report-json", str(snapshot), "--progress", "off"])
            self.assertEqual(code, 0)
            proof = {"role": "repository", "url": "https://github.com/anthropics/skills/tree/main/skills/pdf",
                     "status": "eligible", "final_url": "https://github.com/anthropics/skills/tree/main/skills/pdf"}
            output = TtyOutput()
            with patch("universal_skill_finder.cli.validate_frozen_result_record", return_value={"status": "eligible", "link_proofs": [proof]}), \
                 patch.dict(os.environ, {"TERM": "xterm-256color", "WT_SESSION": "fixture"}, clear=True), \
                 redirect_stdout(output), redirect_stderr(io.StringIO()):
                code = main(["page", "--report", str(snapshot), "--report-file", str(artifact), "--progress", "off"])
            self.assertEqual(code, 0)
            self.assertIn("\x1b[", output.getvalue())
            self.assertNotIn("\x1b", artifact.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
