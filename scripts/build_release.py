#!/usr/bin/env python3
"""Build a deterministic, allowlisted plugin archive without publishing it."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
# Fixed, read-only Git metadata commands below.
import subprocess  # nosec B404
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))
from universal_skill_finder.frontmatter import parse_frontmatter
from universal_skill_finder.versioning import VERSION, release_metadata

PROJECT = "universal-skill-finder"
PLUGIN = "skill"
SKILL = "find"
MARKETPLACE = "skill"
REQUIRED_FILES = (
    "README.md", "img.png", "ARCHITECTURE.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md",
    "pyproject.toml", "requirements-dev.txt", ".gitignore", ".gitattributes",
    ".codex-plugin/plugin.json", ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
    ".agents/plugins/marketplace.json",
)
SOURCE_DIRECTORIES = (f"skills/{SKILL}", "scripts", "tests", ".github", "docs")
EXCLUDED_PARTS = {"__pycache__", ".git", ".idea", ".vscode", ".venv", "build", "dist", "audit-output",
                  ".pytest_cache", ".universal-skill-finder",
                  ".cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox"}
ALLOWED_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".sh", ".ps1"}
ALLOWED_SKILL_FILES = {f"skills/{SKILL}/LICENSE"}
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_FILES = 2000
MAX_GIT_TREE_BYTES = 16 * 1024 * 1024


def unknown_git_metadata() -> dict[str, Any]:
    return {"commit": None, "dirty": None, "tags": []}


def collect_files(root: Path) -> dict[str, bytes]:
    """Read only release-owned regular files. Never follow links or ship caches."""
    paths = {root / relative for relative in REQUIRED_FILES}
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            raise ValueError(f"required release file missing: {relative}")
    for relative in SOURCE_DIRECTORIES:
        directory = root / relative
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"release directory missing or symlinked: {relative}")
        for path in directory.rglob("*"):
            parts = path.relative_to(root).parts
            if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") or part == ".env" or part.startswith(".env.") for part in parts):
                continue
            if path.is_symlink():
                raise ValueError(f"release payload contains a symlink: {path.relative_to(root)}")
            relative_path = path.relative_to(root).as_posix()
            if path.is_file() and (path.suffix in ALLOWED_SUFFIXES or relative_path in ALLOWED_SKILL_FILES):
                paths.add(path)
    if (root / "LICENSE").exists() or (root / "LICENSE").is_symlink():
        paths.add(root / "LICENSE")
    if len(paths) > MAX_FILES:
        raise ValueError("release payload exceeds file-count limit")
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise ValueError(f"release payload contains a symlink: {relative}")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError(f"release file is not regular or exceeds size limit: {relative}")
        with path.open("rb") as stream:
            content = stream.read(MAX_FILE_BYTES + 1)
        total += len(content)
        if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("release payload exceeds size limit")
        files[relative] = content
    return files


def git_metadata(root: Path) -> dict[str, Any]:
    git = shutil.which("git")
    unknown = unknown_git_metadata()
    if not git:
        return unknown

    def run(*arguments: str) -> str:
        # Resolved system Git, fixed read-only subcommands, no shell or remote.
        argv = [git, "-C", str(root), *arguments]
        process = subprocess.run(argv, check=True, capture_output=True,  # nosec B603
                                 text=True, encoding="utf-8", timeout=10)
        return process.stdout.strip()

    try:
        if Path(run("rev-parse", "--show-toplevel")).resolve() != root.resolve():
            return unknown
        commit = run("rev-parse", "--verify", "HEAD")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
            return unknown
        dirty = bool(run("status", "--porcelain=v1", "--untracked-files=all"))
        tags = sorted(run("tag", "--points-at", "HEAD").splitlines())
        return {"commit": commit, "dirty": dirty, "tags": tags}
    except (OSError, ValueError, subprocess.SubprocessError):
        return unknown


def verify_tracked_payload(root: Path, files: dict[str, bytes], commit: str) -> None:
    """Require exact captured bytes from the verified commit, including ignored files.

    Git status alone cannot prove this: global excludes and assume-unchanged
    entries can hide untracked or modified files. Hash blobs directly without
    running clean filters, hooks, external diff tools, or writing Git objects.
    """
    git = shutil.which("git")
    if not git or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise ValueError("cannot verify release files against a Git commit")

    def run(*arguments: str) -> bytes:
        # Fixed read-only subcommands and a validated hexadecimal commit; no shell.
        argv = [git, "-C", str(root), *arguments]
        process = subprocess.run(argv, check=True, capture_output=True, timeout=10)  # nosec B603
        return process.stdout

    try:
        algorithm = run("rev-parse", "--show-object-format").strip().decode("ascii")
        if algorithm not in {"sha1", "sha256"}:
            raise ValueError("unsupported Git object format")
        tree = run("ls-tree", "-rz", "--full-tree", commit)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ValueError("cannot read Git tree for release verification") from exc
    if len(tree) > MAX_GIT_TREE_BYTES or (tree and not tree.endswith(b"\0")):
        raise ValueError("invalid or oversized Git tree for release verification")
    object_length = 40 if algorithm == "sha1" else 64
    entries: dict[bytes, tuple[bytes, bytes, bytes]] = {}
    for row in tree.split(b"\0"):
        if not row:
            continue
        header, separator, path = row.partition(b"\t")
        fields = header.split(b" ")
        if not separator or not path or len(fields) != 3 or path in entries:
            raise ValueError("invalid Git tree entry for release verification")
        mode, kind, object_id = fields
        if not re.fullmatch(rb"[0-9a-f]{" + str(object_length).encode("ascii") + rb"}", object_id):
            raise ValueError("invalid Git object ID for release verification")
        entries[path] = (mode, kind, object_id)
    for path, content in files.items():
        entry = entries.get(os.fsencode(path))
        if entry is None:
            raise ValueError(f"captured release file is not tracked at the verified Git commit: {path}")
        mode, kind, object_id = entry
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError(f"captured release file is not a regular Git blob: {path}")
        # SHA-1 is used only for Git object identity, never a security signature.
        header = b"blob " + str(len(content)).encode("ascii") + b"\0"
        actual = hashlib.new(algorithm, header + content, usedforsecurity=False).hexdigest().encode("ascii")
        if actual != object_id:
            raise ValueError(f"captured release bytes differ from the verified Git commit: {path}")


def validate_skill_metadata(markdown: str) -> None:
    """Validate this package's skill identity and conventional metadata mapping."""
    skill_path = f"skills/{SKILL}/SKILL.md"
    skill = parse_frontmatter(markdown)
    if skill.get("name") != SKILL or not skill.get("description"):
        raise ValueError(f"skill identity differs from canonical skill: {skill_path}")
    # The discovery parser intentionally reads top-level scalar fields only.
    # Reuse it for our indented metadata block without requiring a YAML package.
    nested_lines = []
    in_metadata = False
    for line in markdown.splitlines()[1:]:
        if line.strip() == "---":
            break
        if not line.strip():
            continue
        if not line[:1].isspace():
            in_metadata = line.strip() == "metadata:"
        elif in_metadata:
            nested_lines.append(line)
    nested = parse_frontmatter("---\n" + dedent("\n".join(nested_lines)) + "\n---\n")
    if nested.get("version") != VERSION:
        raise ValueError(f"skill metadata.version differs from canonical version: {skill_path}")


def release_blockers(files: dict[str, bytes], git: dict[str, Any]) -> list[str]:
    validate_skill_metadata(files.get(f"skills/{SKILL}/SKILL.md", b"").decode("utf-8"))
    for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        manifest = json.loads(files[relative])
        if manifest.get("name") != PLUGIN or manifest.get("version") != VERSION:
            raise ValueError(f"plugin name/version differs from canonical version: {relative}")
    for relative in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
        marketplace = json.loads(files[relative])
        plugins = marketplace.get("plugins", [])
        if marketplace.get("name") != MARKETPLACE or len(plugins) != 1 or plugins[0].get("name") != PLUGIN:
            raise ValueError(f"marketplace identity differs from canonical project: {relative}")
        expected = plugins[0].get("version") if relative.startswith(".claude") else plugins[0].get("version", VERSION)
        if expected != VERSION:
            raise ValueError(f"marketplace version differs from canonical version: {relative}")
        if plugins[0].get("source") not in ("./", {"source": "local", "path": "./"}):
            raise ValueError(f"marketplace source differs from plugin payload: {relative}")
    blockers = []
    if not files.get("LICENSE", b"").strip():
        blockers.append("license not selected")
    elif files.get(f"skills/{SKILL}/LICENSE") != files["LICENSE"]:
        raise ValueError("standalone skill license is missing or differs from root LICENSE")
    if not git.get("commit"):
        blockers.append("no Git commit is available")
    if git.get("dirty") is not False:
        blockers.append("clean Git state is not verified")
    if f"v{VERSION}" not in git.get("tags", []):
        blockers.append(f"HEAD is not tagged v{VERSION}")
    changelog = files["CHANGELOG.md"].decode("utf-8")
    if not re.search(rf"^## {re.escape(VERSION)} - \d{{4}}-\d{{2}}-\d{{2}}\s*$", changelog, re.MULTILINE):
        blockers.append("changelog release date is not finalized")
    return blockers


def build_release(root: Path, output: Path, *, final: bool = False) -> dict[str, Any]:
    root = root.resolve()
    output = output.resolve()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", VERSION):
        raise ValueError("canonical release version must be MAJOR.MINOR.PATCH")
    git_before = git_metadata(root)
    files = collect_files(root)
    git = git_metadata(root)
    unstable_git = git_before != git
    if unstable_git:
        git = unknown_git_metadata()
    blockers = release_blockers(files, git)
    if unstable_git:
        blockers.append("Git state changed while capturing release files")
    if final and blockers:
        raise ValueError("final release blocked: " + "; ".join(blockers))
    if final:
        try:
            verify_tracked_payload(root, files, git["commit"])
        except ValueError as exc:
            raise ValueError(f"final release blocked: {exc}") from exc
        if git_metadata(root) != git:
            raise ValueError("final release blocked: Git state changed during payload verification")
    # Fingerprint the captured payload, never reread a changing checkout.
    with TemporaryDirectory(prefix="universal-skill-finder-release-snapshot-") as temporary:
        snapshot = Path(temporary)
        for relative, data in files.items():
            if relative.startswith(f"skills/{SKILL}/"):
                destination = snapshot / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        metadata = release_metadata(snapshot / "skills" / SKILL / "scripts" / "universal_skill_finder")
    inventory = [{"path": path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                 for path, content in sorted(files.items())]
    inventory_bytes = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest = {
        "manifest_version": 1, "project": PROJECT, "release_status": "final" if final else "candidate",
        "provenance": metadata, "source_revision": "sha256:" + hashlib.sha256(inventory_bytes).hexdigest(),
        "git": git, "promotion_blockers": blockers, "files": inventory,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    name = f"{PROJECT}-{VERSION}" + ("" if final else "-candidate")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, content in sorted({**files, "release-manifest.json": manifest_bytes}.items()):
            info = zipfile.ZipInfo(f"{name}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content, compresslevel=9)
    content = buffer.getvalue()
    archive_name = name + ".zip"
    checksum = hashlib.sha256(content).hexdigest()
    artifacts = {archive_name: content, name + ".manifest.json": manifest_bytes,
                 archive_name + ".sha256": f"{checksum}  {archive_name}\n".encode("ascii")}
    output.mkdir(parents=True, exist_ok=True)
    for filename in artifacts:
        destination = output / filename
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"refusing to overwrite existing release artifact: {filename}")
    for filename, data in artifacts.items():
        with (output / filename).open("xb") as stream:
            stream.write(data)
    return {"archive": str(output / archive_name), "checksum": checksum,
            "manifest": str(output / (name + ".manifest.json")), "source_revision": manifest["source_revision"],
            "release_status": manifest["release_status"], "promotion_blockers": blockers}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="New artifact directory, preferably outside the source tree")
    parser.add_argument("--final", action="store_true", help="Require a license, dated changelog, stable clean Git commit, matching tag and exact tracked payload")
    args = parser.parse_args()
    try:
        print(json.dumps(build_release(ROOT, args.output_dir, final=args.final), indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Release build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
