"""Small, dependency-free ANSI decoration for already-sanitized plain reports.

This module deliberately owns no rendering decisions or terminal output.  Callers
choose it only for interactive stdout; report artifacts retain their plain bytes.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping


RESET = "\x1b[0m"
BOLD = "\x1b[1m"
CYAN_BOLD = "\x1b[1;36m"
AMBER = "\x1b[33m"
RED = "\x1b[31m"

_CARD = re.compile(r"^(\d+\.\s+)(.*?)(\s+;\s+.*)?$")
_SUMMARY_STATS = re.compile(r"^\d+ (?:candidates?|matches?|saved skills?)\b", re.IGNORECASE)
_LABEL = re.compile(
    r"\b(Sources|Metrics|Path|Location|Found on|Signals|Install(?: \([^\n)]*\))?|Local|"
    r"Resolved target|Verified target|Search|"
    r"Source coverage|Partial coverage|Notes|Next actions|Candidate previews):"
)
_RED_MARKER = re.compile(r"\b(Not ready|Failed|Unavailable|inconclusive|not checked)\b", re.IGNORECASE)
_AMBER_MARKER = re.compile(
    r"\b(Warning|Partial(?: search| cached)?|Cached|listings? not verified)\b", re.IGNORECASE
)
_COMMAND = re.compile(r"^\s*(?:npx\s+skills@|npm\s|pnpm\s|yarn\s|uvx\s|python(?:3)?\s|git\s|curl\s)")


def _windows_vt_capable(stream: object, environment: Mapping[str, str]) -> bool:
    """Recognize an already VT-capable Windows terminal without mutating it."""
    if environment.get("WT_SESSION") or environment.get("ANSICON"):
        return True
    if environment.get("ConEmuANSI", "").upper() == "ON":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        fileno = getattr(stream, "fileno")
        handle = msvcrt.get_osfhandle(fileno())
        mode = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING.  Do not call SetConsoleMode: a
        # styling preference must not change the caller's console state.
        return bool(mode.value & 0x0004)
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return False


def should_style(stream: object, environment: Mapping[str, str] | None = None) -> bool:
    """Return true only for a conventional interactive ANSI-capable stdout."""
    env = os.environ if environment is None else environment
    isatty = getattr(stream, "isatty", None)
    try:
        if not callable(isatty) or not isatty():
            return False
    except OSError:
        return False
    term = env.get("TERM")
    if not isinstance(term, str) or not term or term.lower() == "dumb":
        return False
    if bool(env.get("NO_COLOR")):
        return False
    # FORCE_COLOR is intentionally ignored.  Redirected stdout must remain
    # clean, and users can opt into an ANSI-capable terminal conventionally.
    return os.name != "nt" or _windows_vt_capable(stream, env)


def _safe_plain_text(text: str) -> str:
    """Discard controls that cannot belong to the plain rendering contract."""
    return "".join(
        character for character in text
        if character in {"\n", "\t"}
        or (ord(character) >= 0x20 and character != "\x7f" and not 0x80 <= ord(character) <= 0x9f)
    )


def _sgr(code: str, value: str) -> str:
    return f"\x1b[{code}m{value}{RESET}"


def _decorate_line(line: str) -> str:
    """Decorate a non-command report line using only fixed SGR sequences."""
    if not line or _COMMAND.match(line):
        return line
    if line == "Universal Skill Finder":
        return _sgr("1;36", line)
    if _SUMMARY_STATS.match(line):
        line = _sgr("1", line)
    card = _CARD.match(line)
    if card:
        number, name, remainder = card.groups()
        return number + _sgr("1;36", name) + (remainder or "")
    line = _LABEL.sub(lambda match: _sgr("1", match.group(1)) + ":", line)
    line = _RED_MARKER.sub(lambda match: _sgr("31", match.group(0)), line)
    return _AMBER_MARKER.sub(lambda match: _sgr("33", match.group(0)), line)


def style_report(text: str) -> str:
    """Apply fixed ANSI emphasis while preserving clean plain-report content.

    The function has no terminal, file, network, or environment side effects.
    It removes unexpected control bytes first, so pre-sanitized renderer output
    stays unchanged after ANSI SGR codes are stripped.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    plain = _safe_plain_text(text)
    lines: list[str] = []
    for line in plain.split("\n"):
        decorated = _decorate_line(line)
        lines.append(decorated)
    return "\n".join(lines)
