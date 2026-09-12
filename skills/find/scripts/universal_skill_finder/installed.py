"""Bounded, read-only evidence about instruction files in known local skill roots.

This is not a host inventory or an enabled-skill check. Matching SKILL.md bytes
prove matching instructions, not matching companion files, origin or permissions.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

from .frontmatter import parse_frontmatter
from .models import Result, SearchReport
from .text import clean_text

MAX_ROOTS = 16
MAX_ENTRIES = 5000
MAX_SKILLS = 1000
MAX_DEPTH = 8
MAX_SKILL_BYTES = 524_288
MAX_TOTAL_BYTES = 8_388_608
_IGNORED_DIRECTORIES = frozenset({".git", ".venv", "node_modules", "__pycache__"})
_LIMITATIONS = [
    "Only the current directory's project roots and selected user skill roots are checked; ancestor, admin, plugin and bundled roots are not checked.",
    "Symlinks are not followed; configured enablement, host precedence and companion files are not checked.",
    "Matching instruction bytes do not establish matching repository, bundle or permissions.",
]


@dataclass
class _Skill:
    scope: str
    directory: Path
    names: set[str]
    sha256: str


@dataclass
class _Budget:
    entries: int = 0
    skills: int = 0
    bytes: int = 0
    exhausted: bool = False
    deadline: float | None = None

    def time_exhausted(self) -> bool:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.exhausted = True
        return self.exhausted


def _absolute(value: str | Path, cwd: Path, home: Path) -> Path:
    text = str(value)
    if text == "~":
        return home
    if text.startswith("~/") or text.startswith("~\\"):
        return Path(os.path.abspath(home / text[2:]))
    path = Path(text)
    return Path(os.path.abspath(path if path.is_absolute() else cwd / path))


def _roots(assistant: str, cwd: Path, home: Path, environ: Mapping[str, str]) -> list[tuple[str, Path]]:
    if assistant == "claude-code":
        return [("project", cwd / ".claude" / "skills"), ("user", home / ".claude" / "skills")]
    codex_home = _absolute(environ.get("CODEX_HOME") or home / ".codex", cwd, home)
    return [("project", cwd / ".agents" / "skills"), ("user", home / ".agents" / "skills"),
            ("user_legacy", codex_home / "skills")]


def _anchored_supported() -> bool:
    return os.open in os.supports_dir_fd and os.scandir in os.supports_fd and hasattr(os, "O_NOFOLLOW")


def _open_root(path: Path) -> int | None:
    """Refuse symlinks in root components; anchor all POSIX descendant reads."""
    if not _anchored_supported():
        for part in (*reversed(path.parents), path):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError("unsafe skill root")
        return None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_skill(directory: Path, descriptor: int | None, scope: str, budget: _Budget) -> _Skill:
    if budget.skills >= MAX_SKILLS or budget.bytes >= MAX_TOTAL_BYTES:
        budget.exhausted = True
        raise ValueError("scan_limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    filename = directory / "SKILL.md"
    if descriptor is None and filename.is_symlink():
        raise ValueError("symlink_skipped")
    opened = os.open("SKILL.md", flags, dir_fd=descriptor) if descriptor is not None else os.open(filename, flags)
    with os.fdopen(opened, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("special_file_skipped")
        if before.st_size > MAX_SKILL_BYTES:
            raise ValueError("file_limit")
        raw = stream.read(min(MAX_SKILL_BYTES, MAX_TOTAL_BYTES - budget.bytes) + 1)
        budget.bytes += len(raw)
        if len(raw) > MAX_SKILL_BYTES:
            raise ValueError("file_limit")
        if budget.bytes > MAX_TOTAL_BYTES:
            budget.exhausted = True
            raise ValueError("scan_limit")
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("changed_during_scan")
    text = raw.decode("utf-8")
    metadata = parse_frontmatter(text)
    names = {clean_text(directory.name, 300).casefold()}
    if isinstance(metadata.get("name"), str) and metadata["name"].strip():
        names.add(clean_text(metadata["name"], 300).casefold())
    budget.skills += 1
    return _Skill(scope, directory, names, hashlib.sha256(raw).hexdigest())


def _scan_root(path: Path, scope: str, budget: _Budget) -> tuple[list[_Skill], dict]:
    skills: list[_Skill] = []
    issues: set[str] = set()

    def walk(directory: Path, descriptor: int | None, depth: int) -> None:
        if budget.time_exhausted():
            issues.add("time_limit")
            return
        children: list[str] = []
        found = False
        try:
            with os.scandir(descriptor if descriptor is not None else directory) as entries:
                for entry in entries:
                    if budget.time_exhausted():
                        issues.add("time_limit")
                        return
                    budget.entries += 1
                    if budget.entries > MAX_ENTRIES:
                        budget.exhausted = True
                        issues.add("scan_limit")
                        return
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        issues.add("symlink_skipped")
                        continue
                    if entry.name == "SKILL.md":
                        found = True
                        if stat.S_ISREG(info.st_mode):
                            try:
                                skills.append(_read_skill(directory, descriptor, scope, budget))
                            except UnicodeError:
                                issues.add("invalid_text")
                            except ValueError as exc:
                                issues.add(str(exc))
                            except OSError:
                                issues.add("unreadable_skill")
                        else:
                            issues.add("special_file_skipped")
                    elif stat.S_ISDIR(info.st_mode) and entry.name not in _IGNORED_DIRECTORIES:
                        children.append(entry.name)
            # Companion files are not skill inventory. Never descend into a skill.
            if found or budget.exhausted:
                return
            for name in sorted(children):
                if budget.exhausted:
                    return
                if depth >= MAX_DEPTH:
                    issues.add("depth_limit")
                    return
                child = directory / name
                child_descriptor = None
                try:
                    if descriptor is not None:
                        child_descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    elif child.is_symlink() or not child.is_dir():
                        issues.add("symlink_skipped")
                        continue
                    walk(child, child_descriptor, depth + 1)
                except OSError:
                    issues.add("unreadable_directory")
                finally:
                    if child_descriptor is not None:
                        os.close(child_descriptor)
        except OSError:
            issues.add("unreadable_directory")

    descriptor = None
    try:
        descriptor = _open_root(path)
        walk(path, descriptor, 0)
    except FileNotFoundError:
        return [], {"scope": scope, "status": "missing", "skills": 0, "issues": []}
    except OSError:
        issues.add("unavailable_root")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return skills, {"scope": scope, "status": "partial" if issues else "scanned", "skills": len(skills), "issues": sorted(issues)}


def _local_identity(result: Result) -> Path | None:
    # Only a configured local-directory occurrence can establish local authority.
    # An arbitrary install.kind/path from remote metadata is never sufficient.
    if result.repository or not any(item.get("adapter") == "local-directory" and item.get("source_kind") == "repository"
                                    for item in result.occurrences):
        return None
    try:
        parsed = urlparse(result.canonical_url or "")
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            return None
        location = unquote(parsed.path)
        if os.name == "nt" and re.match(r"^/[A-Za-z]:/", location):
            location = location[1:]
        path = Path(location)
        return Path(os.path.abspath(path)) if path.is_absolute() else None
    except (TypeError, ValueError):
        return None


def annotate_installed(
    report: SearchReport, assistant: str | None, *, cwd: str | Path | None = None,
    environ: Mapping[str, str] | None = None, home: str | Path | None = None,
    roots: Sequence[tuple[str, str | Path]] | None = None,
    max_seconds: float = 0.250,
) -> None:
    """Annotate results only; never change discovery order, cache or source state."""
    snapshot_records = report.snapshot.get("result_records", {}) if isinstance(report.snapshot, dict) else {}
    snapshot_records = snapshot_records if isinstance(snapshot_records, dict) else {}
    visible_ids = {result.id for result in report.results}
    targets: list[tuple[object, dict | None]] = [(result, snapshot_records.get(result.id)) for result in report.results]
    for identity, record in snapshot_records.items():
        if identity in visible_ids or not isinstance(record, dict):
            continue
        targets.append((SimpleNamespace(
            id=identity,
            name=record.get("name", ""),
            content_sha256=record.get("content_sha256"),
            canonical_url=record.get("canonical_url"),
            repository=record.get("repository"),
            occurrences=record.get("occurrences", []),
            installed={},
        ), record))
    scan = {"status": "not_checked", "assistant": assistant if assistant in {"codex", "claude-code"} else None,
            "scopes": [], "roots": [], "skills_read": 0, "limitations": list(_LIMITATIONS)}
    report.installed_scan = scan
    if assistant not in {"codex", "claude-code"}:
        scan["reason"] = "assistant_unknown"
        for result, record in targets:
            result.installed = {"status": "unknown", "evidence": ["assistant_unknown"], "scopes": []}
            if isinstance(record, dict):
                record["installed"] = dict(result.installed)
        return
    if not targets:
        scan["reason"] = "no_results"
        return
    try:
        current = Path(os.path.abspath(cwd if cwd is not None else Path.cwd()))
        personal = Path(os.path.abspath(home if home is not None else Path.home()))
        environment = os.environ if environ is None else environ
        selected = list(roots) if roots is not None else _roots(assistant, current, personal, environment)
    except (OSError, RuntimeError, ValueError):
        # A removed current directory or unavailable home must not discard
        # successful discovery results, or leak private paths in its exception.
        scan.update(status="partial", reason="root_resolution_failed")
        for result, record in targets:
            result.installed = {"status": "unknown", "evidence": ["scan_incomplete"], "scopes": []}
            if isinstance(record, dict):
                record["installed"] = dict(result.installed)
        return
    records: list[_Skill] = []
    if type(max_seconds) not in {int, float} or max_seconds <= 0:
        raise ValueError("installed inventory allowance must be positive")
    budget = _Budget(deadline=time.monotonic() + min(float(max_seconds), 0.250))
    seen: set[Path] = set()
    for index, (scope, location) in enumerate(selected):
        if budget.time_exhausted():
            scan["roots"].append({"scope": clean_text(scope, 64), "status": "partial", "skills": 0,
                                  "issues": ["time_limit"]})
            break
        scope = clean_text(scope, 64)
        try:
            path = _absolute(location, current, personal)
        except (OSError, RuntimeError, ValueError):
            scan["roots"].append({"scope": scope, "status": "partial", "skills": 0, "issues": ["root_resolution_failed"]})
            continue
        if path in seen:
            continue
        seen.add(path)
        if index >= MAX_ROOTS or budget.exhausted:
            scan["roots"].append({"scope": scope, "status": "not_checked", "skills": 0, "issues": ["scan_limit"]})
            continue
        found, coverage = _scan_root(path, scope, budget)
        records.extend(found)
        scan["roots"].append(coverage)
    scan["scopes"] = sorted({root["scope"] for root in scan["roots"]})
    scan["skills_read"] = len(records)
    complete = bool(scan["roots"]) and all(root["status"] in {"scanned", "missing"} for root in scan["roots"])
    scan["status"] = "complete" if complete else "partial"
    if not _anchored_supported():
        scan["limitations"].append("This platform refuses static symlinks but has weaker protection against concurrent filesystem changes.")
    for result, record in targets:
        digest = result.content_sha256
        digest = digest.lower() if isinstance(digest, str) and re.fullmatch(r"[A-Fa-f0-9]{64}", digest) else None
        local = _local_identity(result)
        exact = [item for item in records if local is not None and item.directory == local]
        matching = [item for item in records if digest and item.sha256 == digest]
        name = clean_text(result.name, 300).casefold()
        names = [item for item in records if name in item.names]
        collisions = [item for item in names if digest and item.sha256 != digest]
        if exact:
            status, evidence, matches = "exact_local", ["canonical_local_directory"], exact
        elif matching:
            status, evidence, matches = "matching_instructions", ["skill_md_sha256"], matching
        elif collisions:
            status, evidence, matches = "name_collision", ["same_name_different_instructions"], collisions
        elif names:
            status, evidence, matches = "unknown", ["name_only"], names
        else:
            status, evidence, matches = ("not_found", ["no_match_in_checked_roots"], []) if complete else ("unknown", ["scan_incomplete"], [])
        annotation = {"status": status, "evidence": evidence, "scopes": sorted({item.scope for item in matches})}
        if collisions and status != "name_collision":
            annotation["evidence"].append("same_name_different_instructions")
            annotation["collision_scopes"] = sorted({item.scope for item in collisions})
        if not complete and "scan_incomplete" not in annotation["evidence"]:
            annotation["evidence"].append("scan_incomplete")
        result.installed = annotation
        if isinstance(record, dict):
            record["installed"] = dict(annotation)
