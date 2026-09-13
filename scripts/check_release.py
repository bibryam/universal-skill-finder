#!/usr/bin/env python3
"""Offline manifest, documentation, and copied-skill release smoke checks."""
from __future__ import annotations

import json
import os
import re
import shutil
# Release checks invoke only this interpreter and the bundled CLI.
import subprocess  # nosec B404
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "skill"
SKILL_NAME = "find"
MARKETPLACE = "skill"
SKILL = ROOT / "skills" / SKILL_NAME
sys.path.insert(0, str(SKILL / "scripts"))
from universal_skill_finder import __version__
from universal_skill_finder.frontmatter import parse_frontmatter
from universal_skill_finder.config import import_source_pack, load_config
from universal_skill_finder.versioning import SEARCH_REPORT_SCHEMA_VERSION, release_metadata
from build_release import validate_skill_metadata
from update_readme_sources import updated_readme


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def main() -> int:
    for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        manifest = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        check(manifest["name"] == PLUGIN and manifest["version"] == __version__, f"version/name mismatch: {relative}")
    for relative in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"):
        manifest = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        check(manifest["name"] == MARKETPLACE, f"marketplace name mismatch: {relative}")
        check(len(manifest["plugins"]) == 1 and manifest["plugins"][0]["name"] == PLUGIN, "unexpected marketplace entries")
        entry = manifest["plugins"][0]
        check(entry.get("version", __version__) == __version__, "marketplace version mismatch")
        source = entry["source"]
        check(source == "./" or source == {"source": "local", "path": "./"}, "unexpected plugin source")
    skill_markdown = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    validate_skill_metadata(skill_markdown)
    metadata = parse_frontmatter(skill_markdown)
    check(metadata.get("name") == SKILL_NAME and bool(metadata.get("description")), "invalid skill metadata")
    check(len(list((ROOT / "skills").glob("*/SKILL.md"))) == 1, "expected one canonical skill")
    check(not (ROOT / "SKILL.md").exists(), "root duplicate skill must not return")
    for launcher in ("run.sh", "run.ps1"):
        check((SKILL / "scripts" / launcher).is_file(), f"missing prerequisite launcher: {launcher}")
    license_bytes = (ROOT / "LICENSE").read_bytes()
    check(bool(license_bytes.strip()), "empty project license")
    check((SKILL / "LICENSE").read_bytes() == license_bytes, "standalone skill license differs from root LICENSE")
    for path in [ROOT / "README.md", ROOT / "ARCHITECTURE.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md", *SKILL.rglob("*.md"), *(ROOT / "docs").glob("*.md")]:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#"):
                continue
            check((path.parent / target.split("#", 1)[0]).exists(), f"broken relative link: {path.name} -> {target}")
    for path in SKILL.rglob("*"):
        check(not path.is_symlink(), f"skill must be portable without symlinks: {path}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check(updated_readme(readme) == readme, "README source defaults differ; run python3 scripts/update_readme_sources.py")

    with TemporaryDirectory(prefix="universal-skill-finder-portable-") as temporary:
        work = Path(temporary)
        copied = work / "portable skill"
        shutil.copytree(SKILL, copied, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        expected_revision = release_metadata(SKILL / "scripts" / "universal_skill_finder")
        check(release_metadata(copied / "scripts" / "universal_skill_finder") == expected_revision,
              "copied payload changed release provenance")
        example_config = load_config(str(work / "example-overlay.json"))
        pack, _ = import_source_pack(example_config, str(copied / "config" / "source-packs" / "example-json-registry.json"))
        check(pack["enabled"] is False, "example registry pack must remain disabled")
        fixture = work / "fixture"
        fixture.mkdir()
        (fixture / "SKILL.md").write_text("---\nname: portable-pdf\ndescription: Fill PDF forms\n---\n", encoding="utf-8")
        base = [sys.executable, str(copied / "scripts" / "skill_finder.py")]
        configuration_path = (work / "sources.json").resolve()
        configuration = ["--config", str(configuration_path), "--cache-dir", str(work / "cache")]

        def run(*args: str) -> str:
            # Fixed argv, no shell or discovered code.
            options = [] if args == ("--check",) else configuration
            return subprocess.run([*base, *options, *args], cwd=work, check=True, capture_output=True, text=True, encoding="utf-8").stdout  # nosec B603

        check("Prerequisites OK" in run("--check"), "copied preflight failed")
        launchers = []
        shell = shutil.which("sh")
        if shell and os.name != "nt":
            launchers.append([shell, str(copied / "scripts" / "run.sh")])
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell:
            launchers.append([powershell, "-NoProfile", "-File", str(copied / "scripts" / "run.ps1")])
        for launcher in launchers:
            # Exercise the installed host entry point, including full engine loading.
            result = subprocess.run([*launcher, *configuration, "doctor"], cwd=work, check=True,  # nosec B603
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            check(expected_revision["code_revision"] in result.stdout, "copied launcher failed to load its engine")
        check(run("sources", "config-path").strip() == str(configuration_path), "incorrect source configuration path")
        check("Configured sources" in run("sources", "list", "--markdown"), "source listing missing")
        check(not configuration_path.exists() and not (work / "cache").exists(), "configuration reads created state")
        run("sources", "init")
        choices = json.loads(configuration_path.read_text(encoding="utf-8"))
        check(set(choices) == {"sources"}, "initial configuration is not a simple source list")
        expected_ids = {source["id"] for source in load_config(str(configuration_path)).sources}
        check({item["id"] for item in choices["sources"]} == expected_ids, "initial source list is incomplete")
        check(all(set(item) == {"id", "enabled"} and type(item["enabled"]) is bool for item in choices["sources"]),
              "initial source flags are not simple booleans")
        initial_bytes = configuration_path.read_bytes()
        run("sources", "init")
        check(configuration_path.read_bytes() == initial_bytes, "repeat initialization changed source choices")
        run("sources", "disable", "tessl")
        check(not load_config(str(configuration_path)).source("tessl")["effective_enabled"], "source disable did not persist")
        check(expected_revision["code_revision"] in run("doctor"), "doctor missing code revision")
        run("sources", "add-local", str(fixture), "--id", "fixture")
        sources = run("sources", "list", "--markdown")
        check("[openai-skills](https://github.com/openai/skills)" in sources and "Disable fixture" in sources,
              "copied source-management table missing links/actions")
        check(run("repositories", "list", "--markdown") == sources, "source list alias differs")
        check(not load_config(str(configuration_path)).source("tessl")["effective_enabled"], "adding a source reset existing choices")
        report = json.loads(run("pdf forms", "--source", "fixture", "--json"))
        check(report["schema_version"] == SEARCH_REPORT_SCHEMA_VERSION, "report missing search schema version")
        check(all(report["provenance"].get(key) == value for key, value in expected_revision.items()),
              "report release provenance differs from copied payload")
        check(re.fullmatch(r"sha256:[0-9a-f]{64}", report["provenance"]["effective_configuration_revision"]) is not None,
              "report missing effective configuration revision")
        check(len(report["results"]) == 1 and report["results"][0]["name"] == "portable-pdf", "copied skill search failed")
        handoff = report["results"][0]["install"]
        check(handoff.get("kind") == "local" and Path(handoff["path"]).resolve() == fixture.resolve(), "local handoff lost identity")
        check(handoff.get("requires_approval") is True, "handoff lacks approval boundary")
        snapshot = work / "search-snapshot.json"
        markdown = run(
            "pdf forms", "--source", "fixture", "--markdown", "--assistant", "codex",
            "--no-installed-check", "--report-json", str(snapshot),
        )
        check("**1 unique candidate**" in markdown and "1. portable-pdf · portable-pdf" in markdown,
              "compact discovery result missing")
        check("fixture · Fill PDF forms" in markdown and "Inspect #N" in markdown
              and "npx skills@" not in markdown, "compact discovery fields or action missing")
        check("## Source coverage" not in markdown and "\n---\n" not in markdown
              and "<details" not in markdown, "default search is not compact terminal output")
        details = run("details", "--report", str(snapshot), "--markdown")
        check("| Source | Search status | Candidates in pool | Globally shown | Enabled |" in details,
              "saved search details are missing source coverage")
        check("Searched" in details and "Not searched" in details, "source status indicators missing")
        plain = run("pdf forms", "--source", "fixture", "--assistant", "codex", "--no-installed-check")
        check("1. portable-pdf ; portable-pdf" in plain and "fixture ; Fill PDF forms" in plain
              and "<!doctype" not in plain and "```" not in plain, "plain CLI compact output missing")
        html_report = run("pdf forms", "--source", "fixture", "--html", "--assistant", "codex", "--no-installed-check")
        check(html_report.lower().startswith("<!doctype html>") and "<details" in html_report
              and "<summary>Description</summary>" in html_report and "portable-pdf" in html_report,
              "copied skill collapsible HTML report missing")
        check("Not ready:" in html_report and "npx skills@" not in html_report,
              "HTML report bypassed the exact installer proof gate")
    print("Release smoke passed: manifests, links, portable license, copied provenance/preflight/launchers, editable source config, source aliases, example registry pack, compact discovery search, saved details, exact handoff.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"Release smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
