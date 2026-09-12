"""Explicit, bounded public live-source checks.

This module is never part of unittest discovery. The parent starts one child
per source only for ``scripts/test_sources.py --live``; each child receives an
empty temporary overlay/cache and reports a small status envelope.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


PASS, FAIL, SKIP, INCONCLUSIVE = "PASS", "FAIL", "SKIP", "INCONCLUSIVE"
MAX_CONCURRENT_SOURCES = 2
MAX_QUERIES_PER_SOURCE = 3
CHILD_TIMEOUT_SECONDS = 90
GLOBAL_TIMEOUT_SECONDS = 600
_ROOT = Path(__file__).resolve().parents[2]
_SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}\Z")


@dataclass(frozen=True)
class LivePlan:
    source_id: str
    queries: tuple[str, ...]
    auth_mode: str
    public_only: bool
    requires_auth: bool = False
    credential_name: str | None = None
    use_credential: bool = False


@dataclass(frozen=True)
class LiveOutcome:
    source_id: str
    status: str
    reason: str
    duration_ms: int
    request_count: int = 0
    query_count: int = 0
    returned_count: int = 0
    cap: int = 0
    partial: bool = False


LIVE_QUERIES: dict[str, tuple[str, str, str]] = {
    "skills-sh": ("pdf", "humanizer", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "skillsmp": ("pdf", "humanize", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "clawhub": ("pdf", "writing", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "skillhub-public": ("pdf", "humanize", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "tessl": ("pdf", "humanize", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "polyskill": ("pdf", "coding", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "skills-directory": ("pdf", "code", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "skillhub-pro": ("pdf", "writing", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "github-code-search": ("pdf", "react", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "openai-skills": ("pdf", "create", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "anthropic-skills": ("pdf", "document", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "google-skills": ("google", "create", "universal_skill_finder_probe_no_skill_7d29e46a"),
    "vercel-agent-skills": ("react", "web", "universal_skill_finder_probe_no_skill_7d29e46a"),
}


def classify(status_code: int | None, reason: str) -> str:
    text = reason.lower()
    if "credential" in text or "auth_missing" in text or "not enabled" in text:
        return SKIP
    if status_code == 429 or status_code in {502, 503, 504} or "timeout" in text or "network" in text:
        return INCONCLUSIVE
    if status_code == 0:
        return PASS
    return FAIL


def child_environment(*, credential_name: str | None = None, credential_value: str | None = None) -> dict[str, str]:
    """Clear ambient source/proxy auth; inject only an explicit test lane."""
    proxy_keys = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY", "NO_PROXY"}
    safe = {key: value for key, value in os.environ.items()
            if not (key.upper().endswith("_TOKEN") or key.upper().endswith("_API_KEY")
                    or key.upper() in proxy_keys)}
    if credential_name and credential_value:
        safe[credential_name] = credential_value
    return safe


def plan_sources(source_ids: Iterable[str], *, public_only: bool, auth_mode: str,
                 required_auth_sources: set[str] = frozenset(),
                 credential_names: Mapping[str, str] | None = None) -> list[LivePlan]:
    if auth_mode not in {"auto", "authenticated"}:
        raise ValueError("unsupported auth mode")
    names = dict(credential_names or {})
    plans: list[LivePlan] = []
    for source_id in sorted(set(source_ids)):
        required_auth = source_id in required_auth_sources
        if public_only and required_auth:
            continue
        credential_name = names.get(source_id)
        use_credential = bool(credential_name) and (required_auth or auth_mode == "authenticated")
        # The first two source-specific seeds form the positive-provider lane;
        # the third is a nonsecret nonsense probe.
        queries = LIVE_QUERIES.get(
            source_id, ("pdf", "humanize", "universal_skill_finder_probe_no_skill_7d29e46a"),
        )[:MAX_QUERIES_PER_SOURCE]
        plans.append(LivePlan(
            source_id, queries, auth_mode, public_only, required_auth,
            credential_name=credential_name, use_credential=use_credential,
        ))
    return plans


def _write_overlay(path: Path, source_id: str) -> None:
    """Select exactly one source in a disposable overlay.

    This may exercise a normally disabled source only after the caller chose it
    explicitly with ``--source``.  The overlay lives in the child temp directory
    and is removed with it; no user or bundled enablement state is changed.
    """
    if not _SOURCE_ID.fullmatch(source_id):
        raise ValueError("unsafe live source identifier")
    path.write_text(json.dumps({"schema_version": 1, "source_overrides": {source_id: {"enabled": True}}}),
                    encoding="utf-8")


def _child_summary(raw: str, source_id: str) -> tuple[str, int, str, int, int, int, bool] | None:
    if len(raw) > 4096:
        return None
    for line in reversed(raw.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (isinstance(value, dict) and value.get("source_id") == source_id
                and value.get("status") in {PASS, FAIL, SKIP, INCONCLUSIVE}
                and type(value.get("queries")) is int and 0 <= value["queries"] <= MAX_QUERIES_PER_SOURCE):
            reason = value.get("reason", "completed")
            if reason not in {"completed", "transient_outcome", "contract_failure", "credential_unavailable"}:
                return None
            counts = (value.get("requests"), value.get("returned"), value.get("cap"))
            if any(type(item) is not int or item < 0 for item in counts) or type(value.get("partial")) is not bool:
                return None
            return value["status"], value["queries"], reason, counts[0], counts[1], counts[2], value["partial"]
    return None


def _run_child(plan: LivePlan, command: list[str], *, deadline: float) -> LiveOutcome:
    started = time.monotonic()
    remaining = max(0.0, deadline - started)
    if remaining <= 0:
        return LiveOutcome(plan.source_id, INCONCLUSIVE, "global live budget exhausted", 0)
    credential_value = os.environ.get(plan.credential_name, "") if plan.use_credential and plan.credential_name else ""
    if plan.use_credential and not credential_value:
        return LiveOutcome(plan.source_id, SKIP, "required credential is unavailable", 0)
    with tempfile.TemporaryDirectory(prefix="universal-skill-finder-live-") as state:
        root = Path(state)
        overlay = root / "sources.json"
        cache = root / "cache"
        _write_overlay(overlay, plan.source_id)
        environment = child_environment(
            credential_name=plan.credential_name if plan.use_credential else None,
            credential_value=credential_value if plan.use_credential else None,
        )
        environment.update({
            "UNIVERSAL_SKILL_FINDER_LIVE_TEMP_STATE": state,
            "UNIVERSAL_SKILL_FINDER_LIVE_SOURCE": plan.source_id,
            "UNIVERSAL_SKILL_FINDER_LIVE_QUERIES": "\n".join(plan.queries),
            "UNIVERSAL_SKILL_FINDER_LIVE_CONFIG": str(overlay),
            "UNIVERSAL_SKILL_FINDER_CACHE": str(cache),
        })
        try:
            completed = subprocess.run(command, cwd=_ROOT, env=environment, capture_output=True, text=True,
                                       timeout=min(CHILD_TIMEOUT_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            return LiveOutcome(plan.source_id, INCONCLUSIVE, "child timeout", int((time.monotonic() - started) * 1000))
    summary = _child_summary(completed.stdout, plan.source_id)
    if summary is not None:
        status, query_count, reason, requests, returned, cap, partial = summary
        return LiveOutcome(
            plan.source_id, status, reason.replace("_", " "), int((time.monotonic() - started) * 1000),
            request_count=requests, query_count=query_count, returned_count=returned, cap=cap, partial=partial,
        )
    reason = "child completed" if completed.returncode == 0 else "child assertion failure"
    return LiveOutcome(plan.source_id, classify(completed.returncode, reason), reason,
                       int((time.monotonic() - started) * 1000))


def run_live_children(plans: Iterable[LivePlan], command: list[str], *, execute: bool = False) -> list[LiveOutcome]:
    """Run at most two isolated children, only when the caller explicitly opts in."""
    planned = list(plans)
    if not execute:
        return [LiveOutcome(item.source_id, SKIP, "live execution not requested", 0) for item in planned]
    deadline = time.monotonic() + GLOBAL_TIMEOUT_SECONDS
    outcomes: list[LiveOutcome] = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SOURCES) as pool:
        futures = [pool.submit(_run_child, item, command, deadline=deadline) for item in planned]
        for future in as_completed(futures):
            outcomes.append(future.result())
    return sorted(outcomes, key=lambda item: item.source_id)


def _child_main() -> int:
    """Run the real CLI with one selected source and isolated temporary state."""
    source_id = os.environ.get("UNIVERSAL_SKILL_FINDER_LIVE_SOURCE", "")
    config = os.environ.get("UNIVERSAL_SKILL_FINDER_LIVE_CONFIG", "")
    cache = os.environ.get("UNIVERSAL_SKILL_FINDER_CACHE", "")
    queries = tuple(query for query in os.environ.get("UNIVERSAL_SKILL_FINDER_LIVE_QUERIES", "").splitlines() if query)[:MAX_QUERIES_PER_SOURCE]
    if not _SOURCE_ID.fullmatch(source_id) or not config or not cache or not queries:
        print(json.dumps({"source_id": source_id, "status": FAIL, "queries": 0}, separators=(",", ":")))
        return 2
    sys.path.insert(0, str(_ROOT / "skills" / "find" / "scripts"))
    from universal_skill_finder.cli import main as cli_main

    exit_codes: list[int] = []
    source_statuses: list[str] = []
    request_count = 0
    returned_count = 0
    partial = False
    malformed = False
    with open(os.devnull, "w", encoding="utf-8") as sink:
        for query in queries:
            # ``--refresh`` plus a new cache root forbids a source cache or
            # stale fallback from masking a failed live request.
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(sink):
                exit_codes.append(cli_main(["--config", config, "--cache-dir", cache, "search", query,
                                            "--source", source_id, "--per-source-limit", "3", "--count", "1",
                                            "--refresh", "--no-installed-check", "--json", "--progress", "off"]))
            try:
                document = json.loads(output.getvalue())
                rows = document.get("coverage", []) if isinstance(document, dict) else []
                row = next(item for item in rows if isinstance(item, dict) and item.get("source_id") == source_id)
                source_statuses.append(str(row.get("status", "unknown")))
                results = document.get("results", [])
                if not isinstance(results, list) or any(
                    not isinstance(item, dict) or item.get("validation_status") != "eligible" for item in results
                ):
                    raise TypeError("invalid materialized result envelope")
                returned_count += len(results)
                partial = partial or bool(row.get("incomplete_results"))
                budget = document.get("timings", {}).get("request_budget", {}).get("request", {})
                used = budget.get("used")
                if type(used) is not int or used < 0:
                    raise TypeError("missing request counter")
                request_count += used
            except (json.JSONDecodeError, StopIteration, TypeError, ValueError):
                malformed = True
    transient = {"rate_limited", "rate_cooldown", "timeout", "failed", "network_error", "deadline_exceeded",
                 "not_started_budget", "preempted", "circuit_open", "half_open_wait"}
    credentials = {"auth_missing"}
    hard = {"auth_failed", "schema_mismatch", "invalid_config", "invalid_query", "not_found", "archive_limit"}
    observed = set(source_statuses)
    if malformed or observed & hard or any(code not in {0, 2} for code in exit_codes):
        status, reason = FAIL, "contract_failure"
    elif observed & credentials:
        status, reason = SKIP, "credential_unavailable"
    elif observed & transient or any(code == 2 for code in exit_codes):
        status, reason = INCONCLUSIVE, "transient_outcome"
    elif returned_count == 0:
        # A provider that cannot materialize any positive destination has not
        # passed the live integration gate, even if its search envelope parsed.
        status, reason = FAIL, "contract_failure"
    else:
        status, reason = PASS, "completed"
    print(json.dumps({
        "source_id": source_id, "status": status, "queries": len(exit_codes), "reason": reason,
        "requests": request_count, "returned": returned_count, "cap": len(exit_codes), "partial": partial,
    }, separators=(",", ":")))
    return 0 if status == PASS else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--child", action="store_true")
    arguments = parser.parse_args(argv)
    return _child_main() if arguments.child else 2


if __name__ == "__main__":
    raise SystemExit(main())
