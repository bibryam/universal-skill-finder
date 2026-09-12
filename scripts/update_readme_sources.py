#!/usr/bin/env python3
"""Keep the README's source table aligned with bundled defaults, offline."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))
from universal_skill_finder.config import load_config
from universal_skill_finder.source_presentation import source_rows

START = "<!-- source-defaults:start -->"
END = "<!-- source-defaults:end -->"
DISPLAY_NAMES = {
    "skills-sh": "skills.sh", "skillsmp": "SkillsMP", "clawhub": "ClawHub",
    "skillhub-public": "SkillHub Public", "tessl": "Tessl", "polyskill": "Polyskill",
    "skills-directory": "Skills Directory", "skillhub-pro": "SkillHub Pro",
    "github-code-search": "GitHub code search", "openai-skills": "openai/skills",
    "anthropic-skills": "anthropics/skills", "google-skills": "google/skills",
    "vercel-agent-skills": "vercel-labs/agent-skills",
}


def render_default_sources() -> str:
    # An explicit missing overlay and empty credential environment ensure the
    # README describes shipped defaults, never the maintainer's personal setup.
    with TemporaryDirectory(prefix="universal-skill-finder-readme-sources-") as temporary:
        rows = source_rows(load_config(str(Path(temporary) / "sources.json")), environ={})
    enabled = sum(row["enabled"] for row in rows)
    lines = ["| Source | Type | Default state | API key |", "|---|---|:---:|---|"]
    for row in rows:
        name = DISPLAY_NAMES.get(row["id"], row["id"])
        if row["public_url"]:
            name = f"[{name}]({row['public_url']})"
        credentials = row["credentials"]
        key = "Required" if any(item["required"] for item in credentials) else "Optional" if credentials else "Not required"
        state = "Enabled" if row["enabled"] else "Disabled"
        lines.append(f"| {name} | {row['type']} | {state} | {key} |")
    lines.extend(["", f"**{len(rows)} bundled sources · {enabled} enabled by default.**"])
    return "\n".join(lines)


def updated_readme(markdown: str) -> str:
    if markdown.count(START) != 1 or markdown.count(END) != 1:
        raise ValueError("README must contain exactly one pair of source-defaults markers")
    before, remaining = markdown.split(START, 1)
    _, after = remaining.split(END, 1)
    return before + START + "\n\n" + render_default_sources() + "\n\n" + END + after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the README differs from bundled defaults")
    args = parser.parse_args()
    path = ROOT / "README.md"
    original = path.read_text(encoding="utf-8")
    updated = updated_readme(original)
    if args.check:
        if original != updated:
            print("README source defaults are out of date; run python3 scripts/update_readme_sources.py", file=sys.stderr)
            return 1
        print("README source defaults match the bundled catalogue.")
    elif original != updated:
        path.write_text(updated, encoding="utf-8")
        print("Updated README source defaults.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
