"""Selection rules for the explicit source-test runner.

The manifest is evidence metadata.  Runtime source configuration remains the
bundled catalogue and is not read or modified by this helper.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "source_test_manifest.json"
GIT_TIMEOUT_SECONDS = 5
_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})")


@dataclass(frozen=True)
class Selection:
    sources: tuple[str, ...]
    cases: tuple[str, ...]
    reason: str


def load_manifest(path: Path = MANIFEST) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 2 or not isinstance(value.get("sources"), dict):
        raise ValueError("unsupported source test manifest")
    if not isinstance(value.get("generated_cases"), list) or not all(isinstance(item, str) for item in value["generated_cases"]):
        raise ValueError("manifest generated_cases must be an array of strings")
    return value


def all_sources(manifest: dict | None = None) -> tuple[str, ...]:
    return tuple(sorted((manifest or load_manifest())["sources"]))


def cases_for_source(source: str, manifest: dict | None = None) -> tuple[str, ...]:
    manifest = manifest or load_manifest()
    detail = manifest["sources"][source]
    cases = {"contract", *manifest["generated_cases"], *detail.get("extra_cases", [])}
    not_applicable = manifest.get("not_applicable", {}).get(detail["adapter"], {})
    return tuple(sorted(cases - set(not_applicable)))


def explicit_selection(sources: list[str], cases: list[str], manifest: dict | None = None) -> Selection:
    manifest = manifest or load_manifest()
    known = set(manifest["sources"])
    unknown = sorted(set(sources) - known)
    if unknown:
        raise ValueError("unknown source(s): " + ", ".join(unknown))
    selected = tuple(sorted(set(sources))) if sources else all_sources(manifest)
    available = set.intersection(*(set(cases_for_source(source, manifest)) for source in selected)) if selected else set()
    unknown_cases = sorted(set(cases) - available)
    if unknown_cases:
        raise ValueError("case is not selectable for every requested source: " + ", ".join(unknown_cases))
    return Selection(selected, tuple(sorted(set(cases))) or tuple(sorted(available)), "explicit")


def _git(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a bounded, read-only Git inspection through its resolved binary."""
    located = shutil.which("git")
    if not located:
        raise ValueError("git executable is unavailable")
    executable = str(Path(located).resolve())
    try:
        # ``executable`` is resolved from PATH, arguments are fixed by this
        # helper or a verified object id, and no shell is involved.
        return subprocess.run(  # nosec B603
            [executable, *arguments], cwd=ROOT, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("git inspection timed out") from exc
    except OSError as exc:
        raise ValueError("git inspection failed") from exc


def changed_selection(base_ref: str, manifest: dict | None = None) -> Selection:
    manifest = manifest or load_manifest()
    if not base_ref:
        raise ValueError("--changed-since requires a supplied base ref")
    verified = _git(["rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"])
    commit = verified.stdout.strip()
    if verified.returncode or not _COMMIT.fullmatch(commit):
        raise ValueError("base ref is not a commit: " + base_ref)
    changed_result = _git(["diff", "--no-ext-diff", "--no-textconv", "--name-only", commit, "--"])
    status_result = _git(["status", "--porcelain"])
    if changed_result.returncode or status_result.returncode:
        raise ValueError("git inspection failed")
    changed = changed_result.stdout.splitlines()
    changed += status_result.stdout.splitlines()
    paths = "\n".join(changed)
    if not paths:
        return Selection((), (), "no relevant changes")
    shared = ("universal_skill_finder/http.py", "universal_skill_finder/cache.py", "universal_skill_finder/config.py", "universal_skill_finder/models.py",
              "universal_skill_finder/federation.py", "source_contract_support.py", "source_test_manifest.json")
    if any(item in paths for item in shared):
        return Selection(all_sources(manifest), ("contract",), "shared integration boundary changed")
    selected: set[str] = set()
    for source in manifest["sources"]:
        if source in paths or f"fixtures/sources/{source}/" in paths:
            selected.add(source)
    if "adapters/registries.py" in paths:
        selected.update(source for source, item in manifest["sources"].items()
                        if item["adapter"] not in {"github-repo", "github-code-search", "tessl"})
    elif "adapters/repositories.py" in paths:
        selected.update(source for source, item in manifest["sources"].items() if item["adapter"] == "github-repo")
    elif "adapters/github_search.py" in paths:
        selected.add("github-code-search")
    elif "adapters/tessl.py" in paths:
        selected.add("tessl")
    if not selected:
        return Selection(all_sources(manifest), ("contract",), "unclassified relevant change")
    return Selection(tuple(sorted(selected)), ("contract",), "changed source boundary")
