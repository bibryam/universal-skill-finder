from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find" / "scripts"))

from universal_skill_finder import terminal


_SGR = re.compile(r"\x1b\[(?:0|1|1;36|31|33)m")


class Stream:
    def __init__(self, tty: bool = True):
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


class TerminalStyleTests(unittest.TestCase):
    def test_style_is_only_enabled_for_conventional_interactive_terminals(self):
        tty = Stream()
        self.assertTrue(terminal.should_style(tty, {"TERM": "xterm-256color", "WT_SESSION": "fixture"}))
        self.assertFalse(terminal.should_style(Stream(False), {"TERM": "xterm-256color"}))
        self.assertFalse(terminal.should_style(tty, {}))
        self.assertFalse(terminal.should_style(tty, {"TERM": "dumb"}))
        self.assertFalse(terminal.should_style(tty, {"TERM": "xterm", "NO_COLOR": "0"}))
        # FORCE_COLOR never overrides a pipe or a missing terminal declaration.
        self.assertFalse(terminal.should_style(Stream(False), {"TERM": "xterm", "FORCE_COLOR": "1"}))
        self.assertFalse(terminal.should_style(tty, {"FORCE_COLOR": "1"}))

    def test_windows_requires_known_or_existing_vt_capability(self):
        with patch.object(terminal.os, "name", "nt"):
            self.assertTrue(terminal.should_style(Stream(), {"TERM": "xterm", "WT_SESSION": "fixture"}))
            self.assertFalse(terminal.should_style(Stream(), {"TERM": "xterm"}))

    def test_fixed_sgr_emphasis_preserves_clean_plain_bytes_and_commands(self):
        command = "npx skills@1.5.23 add owner/repo --skill humanizer"
        plain = "\n".join((
            "Universal Skill Finder",
            "",
            "Search: anti-slop",
            "",
            "75 candidates ; 10 shown",
            "Sources: 5 searched ; 4 cached",
            "1. humanizer",
            "Location: owner/repo › skills/humanizer",
            "Found on: registry (listing not verified)",
            "Signals: registry: stars: 3",
            "Inspect and install: type Inspect #1  Install #1",
            command,
            "Installation unavailable: destination inconclusive",
        ))
        styled = terminal.style_report(plain)
        self.assertEqual(_SGR.sub("", styled), plain)
        self.assertIn("\x1b[1;36mUniversal Skill Finder\x1b[0m", styled)
        self.assertIn("1. \x1b[1;36mhumanizer\x1b[0m", styled)
        self.assertIn("\x1b[1mSearch\x1b[0m: anti-slop", styled)
        self.assertIn("\x1b[1m75 candidates ; 10 shown\x1b[0m", styled)
        self.assertIn("\x1b[1mSources\x1b[0m: 5 searched ; 4 ", styled)
        self.assertIn("\x1b[33mcached\x1b[0m", styled)
        self.assertIn("\x1b[1mLocation\x1b[0m:", styled)
        self.assertIn("\x1b[1mFound on\x1b[0m:", styled)
        self.assertIn("\x1b[33mlisting not verified\x1b[0m", styled)
        self.assertIn("Installation \x1b[31munavailable\x1b[0m", styled)
        self.assertIn("\x1b[31minconclusive\x1b[0m", styled)
        self.assertEqual(next(line for line in styled.splitlines() if line.startswith("npx skills@")), command)
        self.assertGreater(styled.count(terminal.RESET), 0)

    def test_unexpected_controls_are_not_reemitted_as_terminal_sequences(self):
        payload = "Universal Skill Finder\x1b]8;;https://evil.example\x07link\x1b[31m\n1. safe ; owner/repo"
        styled = terminal.style_report(payload)
        stripped = _SGR.sub("", styled)
        self.assertNotIn("\x1b]", styled)
        self.assertNotIn("\x07", styled)
        self.assertTrue(all(character in "\n\t" or ord(character) >= 0x20 for character in stripped))
        self.assertNotIn("\x1b", stripped)
        # Styling uses only the fixed palette; it never emits hyperlinks, OSC,
        # or a success/safety green badge.
        self.assertTrue(all(match.group(0) in {terminal.RESET, terminal.BOLD, terminal.CYAN_BOLD, terminal.AMBER, terminal.RED}
                            for match in _SGR.finditer(styled)))
        self.assertNotIn("\x1b[32m", styled)


if __name__ == "__main__":
    unittest.main()
