#!/usr/bin/env python3
"""Run detect-secrets on release-owned files; fail on any unreviewed finding."""
from __future__ import annotations

import json
import re
# Invokes an installed development scanner, never discovered code.
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Exclude local artifacts, never production source. Synthetic fixture exceptions
# require a narrow inline pragma with an explanation in the relevant test.
EXCLUDED = r"(^|/)(\.git|\.idea|\.vscode|\.venv|__pycache__|build|dist|audit-output|[^/]+\.egg-info)(/|$)|\.pyc$"
REVIEWED_DIGEST_FIELDS = {
    "content_sha256", "expected_sha256", "sha256", "evaluation_manifest_sha256",
    "source_revision", "code_revision", "catalogue_revision",
    "effective_configuration_revision", "tessl_score_version",
}


def reviewed_digest_finding(filename: str, finding: dict[str, object]) -> bool:
    """Suppress only typed JSON digest/revision fields, never arbitrary entropy."""
    if finding.get("type") != "Hex High Entropy String" or not filename.endswith(".json"):
        return False
    line_number = finding.get("line_number")
    if type(line_number) is not int or line_number < 1:
        return False
    try:
        line = (ROOT / filename).read_text(encoding="utf-8").splitlines()[line_number - 1]
    except (OSError, UnicodeError, IndexError):
        return False
    match = re.fullmatch(r'\s*"([A-Za-z0-9_]+)"\s*:\s*"(?:sha256:)?([0-9a-f]{40}|[0-9a-f]{64})",?\s*', line)
    return bool(match and match.group(1) in REVIEWED_DIGEST_FIELDS)


def main() -> int:
    # Fixed interpreter/module/arguments, no shell.
    process = subprocess.run(  # nosec B603
        [sys.executable, "-m", "detect_secrets", "scan", "--all-files", "--no-verify", "--disable-plugin", "IPPublicDetector", "--exclude-files", EXCLUDED, "."],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    raw_results = json.loads(process.stdout)["results"]
    results = {
        filename: [finding for finding in findings if not reviewed_digest_finding(filename, finding)]
        for filename, findings in raw_results.items()
    }
    results = {filename: findings for filename, findings in results.items() if findings}
    # Never print secret values, even when running in a public CI job.
    for filename, findings in results.items():
        for finding in findings:
            print(f"{filename}:{finding['line_number']}: {finding['type']}")
    print(f"Secret scan: {sum(len(findings) for findings in results.values())} unreviewed findings.")
    return 1 if results else 0


if __name__ == "__main__":
    raise SystemExit(main())
