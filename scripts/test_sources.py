#!/usr/bin/env python3
"""Run deterministic source contracts and the separately opt-in live lane."""
from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

from source_test_selection import ROOT, all_sources, cases_for_source, changed_selection, explicit_selection, load_manifest


PASS, FAIL, SKIP, INCONCLUSIVE = "PASS", "FAIL", "SKIP", "INCONCLUSIVE"
OFFLINE_TEST_TIMEOUT_SECONDS = 120


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--list", action="store_true")
    value.add_argument("--plan", action="store_true")
    value.add_argument("--source", action="append", default=[])
    value.add_argument("--case", action="append", default=[])
    value.add_argument("--all", action="store_true")
    value.add_argument("--full", action="store_true")
    value.add_argument("--offline", action="store_true")
    value.add_argument("--live", action="store_true")
    value.add_argument("--public-only", action="store_true")
    value.add_argument("--auth-mode", choices=("auto", "authenticated"), default="auto")
    value.add_argument("--changed-since")
    return value


def _emit(status: str, target: str, detail: str) -> None:
    print(f"{status}\t{target}\t{detail}")


def _run_offline(selection, *, full: bool) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.endswith("_TOKEN") and not key.endswith("_API_KEY")}
    environment["UNIVERSAL_SKILL_FINDER_SOURCE_TEST_SOURCES"] = ",".join(selection.sources)
    environment["UNIVERSAL_SKILL_FINDER_SOURCE_TEST_CASES"] = ",".join(selection.cases)
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"] if full else [
        sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_source_contracts.py", "-v",
    ]
    try:
        # ``sys.executable`` and all unittest arguments are fixed locally;
        # source selection is passed only through environment data, never a shell.
        completed = subprocess.run(command, cwd=ROOT, env=environment,  # nosec B603
                                   timeout=OFFLINE_TEST_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return FAIL
    return PASS if completed.returncode == 0 else FAIL


def _run_live(selection, arguments: argparse.Namespace) -> list[tuple[str, str, str]]:
    # Import only in the explicit lane. The module has no import-time network
    # behavior; execution remains gated by the caller's --live flag.
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "skills" / "find" / "scripts"))
    from live.source_checks import plan_sources, run_live_children
    from universal_skill_finder.config import load_config

    config = load_config(str(ROOT / ".source-test-live-overlay.json"))
    required = {source["id"] for source in config.sources
                if source.get("auth_env") and source.get("auth_optional") is not True}
    credential_names = {
        source["id"]: source["auth_env"] for source in config.sources
        if isinstance(source.get("auth_env"), str)
    }
    plans = plan_sources(selection.sources, public_only=arguments.public_only, auth_mode=arguments.auth_mode,
                         required_auth_sources=required, credential_names=credential_names)
    child = ROOT / "tests" / "live" / "source_checks.py"
    outcomes = run_live_children(plans, [sys.executable, str(child), "--child"], execute=arguments.live)
    skipped_by_mode = set(selection.sources) - {item.source_id for item in plans}
    rows = [(
        item.status, item.source_id,
        f"{item.reason}; mode={arguments.auth_mode}; duration_ms={item.duration_ms}; "
        f"requests={item.request_count}; returned={item.returned_count}; cap={item.cap}; "
        f"partial={str(item.partial).lower()}",
    ) for item in outcomes]
    rows += [(SKIP, source_id, "excluded by --public-only credential policy") for source_id in sorted(skipped_by_mode)]
    return rows


def resolve(arguments: argparse.Namespace):
    manifest = load_manifest()
    if arguments.changed_since:
        return changed_selection(arguments.changed_since, manifest)
    if arguments.source:
        return explicit_selection(arguments.source, arguments.case, manifest)
    if arguments.all or arguments.full:
        return explicit_selection([], arguments.case, manifest)
    raise ValueError("select --source, --all, --full, or --changed-since")


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    manifest = load_manifest()
    if arguments.list:
        for source in all_sources(manifest):
            _emit(PASS, source, ",".join(cases_for_source(source, manifest)))
        return 0
    if arguments.offline and arguments.live:
        print("invalid invocation: --offline and --live cannot be combined", file=sys.stderr)
        return 2
    if (arguments.public_only or arguments.auth_mode != "auto") and not arguments.live:
        print("invalid invocation: --public-only and --auth-mode require --live", file=sys.stderr)
        return 2
    try:
        selection = resolve(arguments)
    except ValueError as exc:
        print(f"invalid invocation: {exc}", file=sys.stderr)
        return 2
    if arguments.plan:
        _emit(PASS, "plan", f"{selection.reason}: {', '.join(selection.sources) or 'none'}")
        return 0
    if not selection.sources:
        _emit(SKIP, "selection", selection.reason)
        return 3
    status = _run_offline(selection, full=arguments.full)
    _emit(status, "offline", ",".join(selection.sources))
    if status == FAIL:
        return 1
    if arguments.offline:
        return 0
    rows = _run_live(selection, arguments)
    for live_status, source_id, detail in rows:
        _emit(live_status, source_id, detail)
    if not arguments.live:
        return 3
    statuses = {status for status, _source_id, _detail in rows}
    if FAIL in statuses:
        return 1
    return 0 if statuses == {PASS} else 3


if __name__ == "__main__":
    raise SystemExit(main())
