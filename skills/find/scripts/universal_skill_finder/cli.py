from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import adapters
from .cache import Cache, default_cache_dir
from .config import (
    ConfigurationError,
    add_github_repository,
    add_local_directory,
    import_source_pack,
    initialize_source_config,
    load_config,
    remove_custom_pack,
    remove_custom_source,
    set_pack_enabled,
    set_source_enabled,
    validate_effective,
)
from .federation import UniversalSkillFinder
from .health import HealthStore
from .installed import annotate_installed
from .models import ProgressEvent, SearchReport, UNSAFE_LOCATION_WARNING
from .presentation import (
    ASSISTANTS, installed_label, render_explanation, render_help, render_markdown,
    render_preview, render_progress, render_report,
)
from .snapshot import (
    MAX_PAGE_SCAN, Page, SnapshotError, create_snapshot, decode_cursor, extend_snapshot,
    load_snapshot, materialize_page, next_safe_cursor, resolve_materialized_result, save_exclusive_path,
    seed_initial_page, snapshot_dict, snapshot_writer_lock, update_snapshot,
    validate_frozen_result_record,
)
from .source_presentation import render_sources_markdown, source_rows
from .text import clean_text, parse_github_repository, safe_web_url
from .terminal import should_style, style_report
from .runtime import Deadline, NetworkPolicy, PermitPool, RequestBudget, emit_progress
from .validation import AnonymousPublicTransport, TargetResolutionCache, github_skill_destination, public_resolver, reviewed_destination
from .versioning import (
    REPORT_FORMAT_VERSION, SCHEMA_VERSION, SEARCH_REPORT_SCHEMA_VERSION,
    effective_config_revision, release_metadata,
)

SUCCESS_STATUSES = {"ok", "cached"}
COMMANDS = {"search", "help", "page", "explain", "repositories", "sources", "packs", "doctor", "cache"}
GLOBAL_VALUE_OPTIONS = {"--config", "--cache-dir"}
GLOBAL_STANDALONE_OPTIONS = {"-h", "--help", "--version"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skill-find", description="Universal Skill Finder: search Agent Skills sources")
    parser.add_argument("--config", help="Source configuration JSON path")
    parser.add_argument("--cache-dir", help="Cache directory override")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    help_parser = sub.add_parser("help", help="Show concise local user help")
    help_parser.add_argument("--assistant", choices=tuple(ASSISTANTS))
    help_parser.add_argument("--invocation", choices=("plugin", "standalone", "cli", "unknown"), default="cli")
    help_parser.add_argument("--markdown", action="store_true")

    search = sub.add_parser("search", help="Search enabled sources")
    search.add_argument("query", nargs="*", help="Capability or task to search for")
    search.add_argument("--per-source-limit", type=int, help="Candidate depth requested from each source")
    search.add_argument("--limit", type=int, help="Legacy alias for --per-source-limit")
    search.add_argument("-n", "--count", type=int, help="Overall result cap (1-100)")
    search.add_argument("--max-results", type=int, help="Legacy overall snapshot cap (1-500)")
    search.add_argument("--page-size", type=int, default=10, help="Checked results per page (1-100)")
    search.add_argument("--source", "--repository", dest="source", metavar="SOURCE_ID", action="append", default=[], help="Search only this source ID; repeatable")
    search.add_argument("--exclude", action="append", default=[], help="Skip this source ID; repeatable")
    search.add_argument("--offline", action="store_true", help="Use only cached and local data")
    search.add_argument("--refresh", action="store_true", help="Ignore fresh caches")
    search.add_argument("--dry-run", action="store_true", help="Show which sources would receive the query")
    output = search.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    output.add_argument("--markdown", action="store_true", help="Emit compact Markdown result cards for the terminal")
    output.add_argument("--html", action="store_true", help="Explicitly export an optional HTML report with collapsible details")
    search.add_argument("--assistant", choices=tuple(ASSISTANTS), help="Current plugin host for display-only install commands")
    search.add_argument("--no-installed-check", action="store_true", help="Skip the read-only check of standard local skill directories")
    search.add_argument("--strict", action="store_true", help="Return failure when any selected source fails or returns incomplete coverage")
    search.add_argument("--thorough", action="store_true", help="Use the longer measured network budget")
    search.add_argument("--preview", action="store_true", help="Emit up to three early verified previews on stderr")
    search.add_argument("--progress", choices=("auto", "plain", "off"), default="auto", help="Interim stderr progress")
    search.add_argument("--report-file", type=Path, help="Exclusively create the rendered page artifact")
    search.add_argument("--report-json", type=Path, help="Exclusively create the bounded snapshot artifact")

    page = sub.add_parser("page", help="Render the next page from a saved snapshot")
    page.add_argument("--report", type=Path, required=True)
    page.add_argument("--cursor", help="Saved cursor; omit to resume the latest unfinished page")
    page.add_argument("--page-size", type=int)
    extension = page.add_mutually_exclusive_group()
    extension.add_argument("--extend-count", type=int, help="Explicitly raise this saved snapshot's overall cap (1-100)")
    extension.add_argument("--more", action="store_true", help="Continue, raising the cap by one page only when needed (up to 100)")
    page_output = page.add_mutually_exclusive_group()
    page_output.add_argument("--json", action="store_true")
    page_output.add_argument("--markdown", action="store_true")
    page_output.add_argument("--html", action="store_true", help="Emit collapsible HTML result cards")
    page.add_argument("--assistant", choices=tuple(ASSISTANTS))
    page.add_argument("--progress", choices=("auto", "plain", "off"), default="auto")
    page.add_argument("--report-file", type=Path, help="Exclusively create the complete rendered page artifact")

    explain = sub.add_parser("explain", help="Explain a materialized result from a saved snapshot")
    explain.add_argument("--report", type=Path, required=True)
    explain.add_argument("--result", required=True)
    explain.add_argument("--markdown", action="store_true")

    sources = sub.add_parser("sources", aliases=["repositories"], help="List and configure sources")
    source_sub = sources.add_subparsers(dest="source_command", required=True)
    source_list = source_sub.add_parser("list", help="List effective sources and links without contacting them")
    source_output = source_list.add_mutually_exclusive_group()
    source_output.add_argument("--markdown", action="store_true", help="Show linked sources, effective states and suggested changes")
    source_output.add_argument("--json", action="store_true", help="Emit safe source metadata, never credential values")
    enable = source_sub.add_parser("enable", help="Enable a source")
    enable.add_argument("source_id", metavar="source_id")
    disable = source_sub.add_parser("disable", help="Disable a source")
    disable.add_argument("source_id", metavar="source_id")
    explain = source_sub.add_parser("explain", help="Show safe source state and credential prerequisites")
    explain.add_argument("source_id", metavar="source_id")
    add_repo = source_sub.add_parser("add-repo", help="Add a GitHub repository without writing an adapter")
    add_repo.add_argument("repository")
    add_repo.add_argument("--id", dest="source_id", metavar="SOURCE_ID")
    add_repo.add_argument("--ref", default="HEAD")
    add_repo.add_argument("--include", action="append", default=[])
    add_repo.add_argument("--exclude", action="append", default=[])
    add_repo.add_argument("--pack", dest="pack_id", help="Place the repository in an existing source pack")
    add_local = source_sub.add_parser("add-local", help="Add a local skill directory")
    add_local.add_argument("path")
    add_local.add_argument("--id", dest="source_id", metavar="SOURCE_ID", required=True)
    add_local.add_argument("--include", action="append", default=[])
    add_local.add_argument("--exclude", action="append", default=[])
    add_local.add_argument("--pack", dest="pack_id", help="Place the directory in an existing source pack")
    remove = source_sub.add_parser("remove", help="Remove a user-added source")
    remove.add_argument("source_id", metavar="source_id")
    source_sub.add_parser("validate", help="Validate the merged configuration")
    source_sub.add_parser("init", help="Create one editable source list, preserving existing settings")
    source_sub.add_parser("config-path", help="Print the source configuration path without creating it")
    retry = source_sub.add_parser("retry", help="Arm one foreground outage-breaker retry")
    retry.add_argument("source_id", metavar="source_id")

    packs = sub.add_parser("packs", help="Manage bundled source packs")
    pack_sub = packs.add_subparsers(dest="pack_command", required=True)
    pack_sub.add_parser("list", help="List source packs")
    pack_enable = pack_sub.add_parser("enable", help="Enable a pack")
    pack_enable.add_argument("pack_id")
    pack_disable = pack_sub.add_parser("disable", help="Disable a pack")
    pack_disable.add_argument("pack_id")
    pack_import = pack_sub.add_parser("import", help="Import a declarative source pack from a local JSON file")
    pack_import.add_argument("path")
    pack_remove = pack_sub.add_parser("remove", help="Remove a user-imported pack and its sources")
    pack_remove.add_argument("pack_id")

    sub.add_parser("doctor", help="Check runtime, configuration, adapters, and credentials")

    cache_parser = sub.add_parser("cache", help="Inspect cached registry queries")
    cache_sub = cache_parser.add_subparsers(dest="cache_command", required=True)
    cache_list = cache_sub.add_parser("list", help="List exact registry queries available offline")
    cache_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def _expand_search_shorthand(argv: list[str]) -> list[str]:
    """Treat a bare query as `search` without changing named subcommands."""
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in GLOBAL_VALUE_OPTIONS:
            index += 2
            continue
        if any(argument.startswith(f"{option}=") for option in GLOBAL_VALUE_OPTIONS):
            index += 1
            continue
        break

    if index >= len(argv):
        return argv
    if argv[index] in COMMANDS or argv[index] in GLOBAL_STANDALONE_OPTIONS:
        return argv
    return [*argv[:index], "search", *argv[index:]]


def _location(result: Any) -> str:
    if result.repository:
        return result.repository + (f"/{result.skill_path}" if result.skill_path else "")
    if result.install.get("kind") == "local":
        return clean_text(result.install.get("path"))
    return "registry listing"


def _source_link(result: Any) -> str:
    if safe_web_url(result.canonical_url):
        return result.canonical_url
    if result.install.get("kind") == "local" and result.install.get("path"):
        path = Path(result.install["path"])
        if path.is_absolute():
            return path.as_uri()
    repository = parse_github_repository(result.repository)
    if repository:
        return f"https://github.com/{repository}"
    for occurrence in result.occurrences:
        link = safe_web_url(occurrence.get("canonical_url"))
        if link:
            return link
    return "Unavailable"


def _install_option(result: Any, index: int) -> str:
    install = result.install
    if not install or UNSAFE_LOCATION_WARNING in result.warnings:
        return f"Inspect #{index} (installation target unavailable)"
    kind = install.get("kind")
    if kind == "github":
        if not install.get("skill_path"):
            return f"Inspect #{index} (skill path unresolved)"
        if install.get("repository"):
            return f"Install #{index} (review required)"
    elif kind == "local" and install.get("path"):
        return f"Install #{index} (review required)"
    elif kind in {"clawhub", "polyskill", "skillhub"} and install.get("reference"):
        return f"Install #{index} (review required)"
    return f"Inspect #{index} (installation target unavailable)"


def _print_report(report: SearchReport, *, dry_run: bool) -> None:
    print(f"Query: {clean_text(report.query)}")
    completed = sum(1 for item in report.coverage if item.status in SUCCESS_STATUSES and not item.incomplete_results)
    planned = sum(1 for item in report.coverage if item.status == "planned")
    enabled = [item for item in report.coverage if item.enabled]
    runnable = [item for item in enabled if item.status not in {"not_selected", "excluded"}]
    if dry_run:
        print(f"Planned sources: {planned}")
    else:
        print(f"Coverage: {completed} of {len(runnable)} selected enabled sources completed")
        print()
        print(f"Results ({len(report.results)})")
        if not report.results:
            if any(item.incomplete_results for item in report.coverage):
                print("No verified matches were returned; search coverage is incomplete.")
            elif completed:
                print("No matches were found in the sources that completed.")
            else:
                print("No sources completed. Search coverage is unavailable.")
        for index, result in enumerate(report.results, 1):
            summary = clean_text(result.description, 180)
            sources = ", ".join(clean_text(source) for source in result.source_ids)
            print(f"  {index:>2}. {clean_text(result.name)}")
            if summary:
                print(f"      {summary}")
            print(f"      Skill link: {_source_link(result)}")
            print(f"      Found in: {clean_text(_location(result))}; reported by {sources}")
            print(f"      Install: {_install_option(result, index)}")
            if installed_label(result):
                print(f"      Local check: {installed_label(result)}")
            if result.warnings:
                print(f"      Warnings: {'; '.join(clean_text(warning) for warning in result.warnings)}")
    print()
    print("Enabled sources")
    if not enabled:
        print("  None")
    for item in enabled:
        count = f"{item.result_count} results" if item.status in SUCCESS_STATUSES else ""
        target = f" [{clean_text(item.target or item.host)}]" if item.target or item.host else ""
        detail = f" - {clean_text(item.detail)}" if item.detail else ""
        cache = f" cache={item.cache_age_seconds}s" if item.cache_age_seconds is not None else ""
        status = f"{item.status} (partial)" if item.incomplete_results else item.status
        print(f"  {clean_text(item.source_id):<24} {clean_text(status):<16} {count}{cache}{target}{detail}")
    disabled = [clean_text(item.source_id) for item in report.coverage if not item.enabled]
    if disabled:
        print("Disabled sources: " + ", ".join(disabled))
    if report.results and not dry_run:
        print('\nNext: ask your agent "Install #N" or "Inspect #N" for a result above. The CLI does not install skills.')


def _load(args: argparse.Namespace):
    config = load_config(args.config)
    cache = Cache(Path(args.cache_dir)) if args.cache_dir else Cache()
    return config, cache


def _resolve_search_args(args: argparse.Namespace) -> None:
    """Resolve aliases once, before any configuration/cache/network activity."""
    if getattr(args, "_search_args_resolved", False):
        return
    if args.per_source_limit is not None and args.limit is not None:
        raise ConfigurationError("use only one of --per-source-limit or --limit")
    if args.count is not None and args.max_results is not None:
        raise ConfigurationError("use only one of --count/-n or --max-results")
    depth = args.per_source_limit if args.per_source_limit is not None else args.limit
    legacy_cap = args.max_results is not None
    cap = args.count if args.count is not None else args.max_results
    args.limit = 10 if depth is None else depth
    args.count = 10 if cap is None else cap
    args.max_results = args.count
    args.legacy_max_results = legacy_cap
    if not 1 <= args.limit <= 200:
        raise ConfigurationError("--per-source-limit/--limit must be 1-200")
    if not 1 <= args.max_results <= (500 if legacy_cap else 100):
        raise ConfigurationError("--count must be 1-100; legacy --max-results must be 1-500")
    # The parser no longer tells us which alias supplied the resolved cap. The
    # raw legacy range is checked in main before this function.
    if not 1 <= args.page_size <= 100:
        raise ConfigurationError("--page-size must be 1-100")
    if (args.preview or args.thorough) and (args.offline or args.dry_run):
        raise ConfigurationError("--preview/--thorough cannot be used with --offline or --dry-run")
    args._search_args_resolved = True


def _progress_callback(args: argparse.Namespace):
    enabled = args.progress == "plain" or (args.progress == "auto" and sys.stderr.isatty())
    if not enabled:
        return None

    def emit(event: object) -> None:
        text = render_preview(event, format="plain") if getattr(event, "type", None) == "early_verified" and getattr(args, "preview", False) else render_progress(event, format="plain")
        if text:
            print(text, file=sys.stderr, flush=True)

    return emit


def _write_exclusive_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")


def _emit_rendered_report(rendered: str, args: argparse.Namespace) -> None:
    """Style interactive plain output only, after clean artifacts are saved."""
    if not (args.json or args.markdown or args.html) and should_style(sys.stdout):
        rendered = style_report(rendered)
    print(rendered)


def _check_artifact_paths(*paths: Path | None) -> None:
    """Reject existing/conflicting artifact paths before discovery or page writes."""
    selected = [Path(path).absolute() for path in paths if path is not None]
    if len(set(selected)) != len(selected):
        raise ConfigurationError("report and snapshot artifacts need different fresh paths")
    for path in selected:
        if path.exists() or path.is_symlink():
            raise FileExistsError("report artifact already exists; choose a fresh path")


def _stored_validation(record: dict[str, Any]) -> dict[str, Any]:
    status = record.get("validation_status")
    if status not in {"eligible", "unavailable", "inconclusive", "not_checked"}:
        statuses = [item.get("status") for item in record.get("link_proofs", []) if isinstance(item, dict)]
        status = "eligible" if "eligible" in statuses else "unavailable" if statuses and all(item == "unavailable" for item in statuses) else "inconclusive" if "inconclusive" in statuses else "not_checked"
    return {"status": status, "detail": "stored destination proof"}


def _snapshot_destinations(candidate_id: str, record: Mapping[str, Any]) -> list[Any]:
    """Rebuild only reviewed proof requests from a frozen result record."""
    destinations: list[Any] = []
    seen: set[tuple[str, str]] = set()
    occurrences = record.get("occurrences", [])
    if not isinstance(occurrences, (list, tuple)):
        occurrences = ()
    for occurrence in occurrences[:100]:
        if not isinstance(occurrence, Mapping):
            continue
        adapter = occurrence.get("adapter")
        evidence = occurrence.get("source_evidence")
        expected = evidence.get("expected_identity") if isinstance(evidence, Mapping) else None
        listing_url = occurrence.get("listing_url")
        listing_role = occurrence.get("listing_role")
        # Match the live federation path: some reviewed public contracts derive
        # identity from their audited route rather than a source JSON field
        # (notably ClawHub and Tessl).  A snapshot is frozen source evidence,
        # not permission to skip that same anonymous destination check.
        if (isinstance(adapter, str) and isinstance(listing_url, str)
                and listing_role in {"listing", "bundle_listing", "source_page", "repository"}):
            destination = reviewed_destination(
                candidate_id, role=listing_role, url=listing_url, adapter=adapter,
                expected_identity=expected,
            )
            key = (destination.role, destination.url or "")
            if destination.profile is not None and key not in seen:
                seen.add(key)
                destinations.append(destination)
        repository = occurrence.get("repository")
        ref = occurrence.get("ref")
        skill_path = occurrence.get("skill_path")
        if all(isinstance(value, str) and value for value in (repository, ref, skill_path)):
            try:
                destination = github_skill_destination(
                    candidate_id, repository=repository, ref=ref, skill_path=skill_path,
                )
            except ValueError:
                continue
            key = (destination.role, destination.url or "")
            if key not in seen:
                seen.add(key)
                destinations.append(destination)
    # Older additive schema-1 records and compact integrations may omit the
    # occurrence array while retaining the frozen exact repository identity.
    # Rebuild the same reviewed GitHub request from those top-level fields.
    repository, ref, skill_path = record.get("repository"), record.get("ref"), record.get("skill_path")
    if all(isinstance(value, str) and value for value in (repository, ref, skill_path)):
        try:
            destination = github_skill_destination(
                candidate_id, repository=repository, ref=ref, skill_path=skill_path,
            )
        except ValueError:
            pass
        else:
            key = (destination.role, destination.url or "")
            if key not in seen:
                destinations.append(destination)
    return destinations


def _persist_report_snapshot(report: SearchReport, path: Path, args: argparse.Namespace) -> None:
    internal = report.snapshot if isinstance(report.snapshot, dict) else {}
    ordered = internal.get("ordered_pool")
    records = internal.get("result_records")
    traces = internal.get("ranking_traces")
    if not isinstance(ordered, list) or not isinstance(records, dict):
        raise SnapshotError("search did not expose a complete frozen result pool")
    records = {identity: dict(record) for identity, record in records.items()}
    for result in report.results:
        if result.id in records:
            # Inventory is read once before persistence. Preserve its per-result
            # annotation for later pages instead of scanning again.
            records[result.id]["installed"] = dict(result.installed)
    # Federation already selected this display page from the fully validated
    # ranked pool. Do not replay the separate 30-item snapshot scan here: a
    # valid selected result may rank just beyond that window. Retain only
    # identities whose persisted record still says eligible; this is a safety
    # filter, never replenishment from elsewhere in the frozen pool.
    live_cap = min(args.count, args.page_size)
    initial_ids: list[str] = []
    retained_results = []
    for result in report.results:
        if len(initial_ids) >= live_cap:
            break
        record = records.get(result.id)
        if (record is not None and result.id not in initial_ids
                and _stored_validation(record).get("status") == "eligible"):
            initial_ids.append(result.id)
            retained_results.append(result)
    report.results = retained_results
    for row in report.coverage:
        row.shown = sum(row.source_id in result.source_ids for result in report.results)
    report.page_start = 1 if report.results else 0
    report.page_shown = len(report.results)
    report.materialized_total = len(report.results)
    report.can_explain = bool(report.results)
    snapshot = create_snapshot(
        query=report.query,
        options={"depth": args.limit, "sources": list(args.source), "excluded": list(args.exclude), "thorough": args.thorough},
        config_revision=str(report.provenance.get("effective_configuration_revision", "unavailable")),
        ordered_pool=ordered,
        requested_cap=args.count,
        page_size=args.page_size,
        ranking_traces=traces if isinstance(traces, dict) else {},
        inventory_evidence=report.installed_scan,
        result_records=records,
        report_metadata={
            "coverage": [asdict(row) for row in report.coverage],
            "accepted_occurrences": report.accepted_occurrences,
            "unique_count": report.unique_count,
            "merged_duplicates": max(0, report.accepted_occurrences - report.unique_count),
            "timings": dict(report.timings),
            "mode": report.mode,
            "provenance": dict(report.provenance),
            "notes": list(report.notes),
            "page_incomplete": report.page_incomplete,
            "validation_checked_count": report.validation_checked_count,
            "validation_deferred_count": report.validation_deferred_count,
            "validation_stop_reason": report.validation_stop_reason,
            "validation_stopped_reason": report.validation_stopped_reason,
        },
    )
    page = seed_initial_page(snapshot, initial_ids)
    save_exclusive_path(path, page.snapshot)
    numbers = dict(page.snapshot.result_numbers)
    for result in report.results:
        result.result_number = numbers.get(result.id)
    report.snapshot = {"status": "saved", "path": str(path), "snapshot_id": page.snapshot.snapshot_id}
    report.continuation = {"available": page.next_cursor is not None, "cursor": page.next_cursor}
    report.show_more_available = (page.next_cursor is None and page.resume_cursor is not None
                                  and page.snapshot.requested_cap < 100)
    report.show_more_cursor = page.resume_cursor if report.show_more_available else None


def _page(args: argparse.Namespace) -> int:
    started_at = time.monotonic()
    if args.page_size is not None and not 1 <= args.page_size <= 100:
        raise ConfigurationError("--page-size must be 1-100")
    if args.extend_count is not None and not 1 <= args.extend_count <= 100:
        raise ConfigurationError("--extend-count must be 1-100")
    _check_artifact_paths(args.report_file)
    progress = _progress_callback(args)
    with snapshot_writer_lock(args.report):
        snapshot = load_snapshot(args.report)
        if snapshot.report_metadata.get("mode", "online") != "online":
            raise SnapshotError("only a saved online search can be continued")
        if args.extend_count is not None:
            if args.extend_count < snapshot.requested_cap:
                raise ConfigurationError("--extend-count cannot lower the saved result cap")
            if args.extend_count > snapshot.requested_cap:
                snapshot = extend_snapshot(snapshot, args.extend_count)
        elif args.more and next_safe_cursor(snapshot) is not None and len(snapshot.result_numbers) >= snapshot.requested_cap:
            if snapshot.requested_cap < 100:
                snapshot = extend_snapshot(snapshot, min(100, snapshot.requested_cap + (args.page_size or snapshot.page_size)))
        thawed = snapshot_dict(snapshot)
        records = thawed.get("result_records", {})
        cursor = args.cursor or next_safe_cursor(snapshot)
        emit_progress(progress, ProgressEvent("page_started", query=snapshot.query, status="started"))
        policy = NetworkPolicy.for_mode(bool(snapshot.options.get("thorough", False)))
        parent_deadline = Deadline.start(policy)
        validation_deadline = Deadline(
            parent_deadline.started_at,
            parent_deadline.collection_cutoff_at,
            parent_deadline.validation_deadline(time.monotonic()),
            parent_deadline.validation_cap_seconds,
        )
        budget = RequestBudget(
            requests=policy.validation_requests,
            validation_requests=policy.validation_requests,
            github_api_requests=policy.github_api_requests,
        )
        permits = PermitPool(policy)
        transport = AnonymousPublicTransport()
        target_cache = TargetResolutionCache()

        def validate(candidate_id: str, frozen_record: dict[str, Any] | None) -> dict[str, Any]:
            # Snapshot construction recursively freezes lists/mappings. Work
            # from the already-thawed record, not a shallow dict of tuples.
            record = records.get(candidate_id, {})
            return validate_frozen_result_record(
                candidate_id, record,
                destinations=_snapshot_destinations(candidate_id, record),
                transport=transport, resolver=public_resolver, budget=budget,
                permits=permits, deadline=validation_deadline,
                target_cache=target_cache,
            )

        page = Page((), None, None, snapshot, is_exhausted=True) if cursor is None else materialize_page(
            snapshot, cursor,
            validate=validate,
            page_size=args.page_size,
            can_validate=lambda _identity, _record: validation_deadline.remaining() > 0
            and budget.snapshot()["validation"]["used"] < policy.validation_requests,
        )
        update_snapshot(args.report, page.snapshot)
        state = snapshot_dict(page.snapshot)
    results = []
    for identity in page.ids:
        record = dict(state["result_records"].get(identity, {}))
        record["result_number"] = page.snapshot.result_numbers.get(identity)
        results.append(record)
    metadata = state.get("report_metadata", {})
    statuses = [proof.get("status") for proof in state["validation_ledger"].values() if isinstance(proof, dict)]
    numbers = [value for value in page.snapshot.result_numbers.values() if type(value) is int]
    current_numbers = [
        result.get("result_number") for result in results
        if type(result.get("result_number")) is int
    ]
    materialized_before = (
        max(0, min(current_numbers) - 1)
        if current_numbers else max(numbers, default=0)
    )
    page_target = min(
        args.page_size or page.snapshot.page_size,
        max(0, page.snapshot.requested_cap - materialized_before),
    )
    scan_limited = (
        page.scanned_count >= MAX_PAGE_SCAN
        and not page.is_exhausted
        and len(results) < page_target
    )
    page_incomplete = bool(results) and len(results) < page_target and (page.has_pending or scan_limited)
    validation_budget = budget.snapshot().get("validation", {})
    validation_budget_reached = (
        type(validation_budget.get("used")) is int
        and type(validation_budget.get("limit")) is int
        and validation_budget["used"] >= validation_budget["limit"]
    )
    if page.has_pending and validation_deadline.remaining() <= 0:
        validation_stop_reason = "deadline_reached"
        validation_stopped_reason = (
            f"{policy.validation_seconds:g}-second destination-verification deadline reached after final validation "
            f"completed for {page.scanned_count} candidates on this page"
        )
    elif page.has_pending and validation_budget_reached:
        validation_stop_reason = "request_budget_reached"
        validation_stopped_reason = (
            "destination-verification request budget reached after final validation "
            f"completed for {page.scanned_count} candidates on this page"
        )
    elif page.has_pending:
        validation_stop_reason = "validation_deferred"
        validation_stopped_reason = page.pending_detail or "destination verification stopped before dispatch"
    elif scan_limited:
        validation_stop_reason = "scan_limit_reached"
        validation_stopped_reason = f"{MAX_PAGE_SCAN}-candidate destination-verification scan limit reached"
    elif page.is_exhausted:
        validation_stop_reason = "pool_exhausted"
        validation_stopped_reason = None
    else:
        validation_stop_reason = "page_full"
        validation_stopped_reason = None
    validation_deferred_count = 0
    if page_incomplete:
        original_deferred = metadata.get("validation_deferred_count")
        if materialized_before == 0 and metadata.get("page_incomplete") is True \
                and type(original_deferred) is int and original_deferred >= 0:
            validation_deferred_count = original_deferred
        elif page.resume_cursor is not None:
            resume_position, _materialized = decode_cursor(page.resume_cursor, page.snapshot)
            validation_deferred_count = max(0, len(page.snapshot.ordered_pool) - resume_position)
        else:
            validation_deferred_count = max(
                0, len(page.snapshot.ordered_pool) - len(page.snapshot.validation_ledger),
            )
    coverage = metadata.get("coverage", [])
    if not isinstance(coverage, list):
        coverage = []
    coverage = [dict(row) for row in coverage if isinstance(row, dict)]
    for row in coverage:
        row["shown"] = sum(row.get("source_id") in result.get("source_ids", []) for result in results)
    accepted = metadata.get("accepted_occurrences")
    if type(accepted) is not int or accepted < len(page.snapshot.ordered_pool):
        accepted = sum(max(1, len(record.get("occurrences", []))) for record in records.values())
    report = {
        "schema_version": SEARCH_REPORT_SCHEMA_VERSION,
        "report_format_version": REPORT_FORMAT_VERSION,
        "mode": "online",
        "continuation_page": True,
        "coverage_context": "saved" if coverage else "unavailable",
        "query": page.snapshot.query,
        "results": results,
        "coverage": coverage,
        "accepted_occurrences": accepted,
        "unique_count": len(page.snapshot.ordered_pool),
        "eligible_count": statuses.count("eligible"),
        "unavailable_count": statuses.count("unavailable"),
        "inconclusive_count": statuses.count("inconclusive"),
        "not_checked_count": len(page.snapshot.ordered_pool) - len(statuses) + statuses.count("not_checked"),
        "page_start": min((page.snapshot.result_numbers[item] for item in page.ids), default=0),
        "page_shown": len(results),
        "materialized_total": max(numbers, default=0),
        "requested_count": page.snapshot.requested_cap,
        "page_size": args.page_size or page.snapshot.page_size,
        "can_explain": bool(page.snapshot.result_numbers),
        "installed_scan": state.get("inventory_evidence", {}),
        "provenance": metadata.get("provenance", {}),
        "search_timings": metadata.get("timings", {}),
        "notes": ["Original search: " + note for note in metadata.get("notes", [])[:10]
                  if isinstance(note, str)] if isinstance(metadata.get("notes", []), list) else [],
        "timings": {"page_ms": int((time.monotonic() - started_at) * 1000),
                    "request_budget": budget.snapshot()},
        "validation_stopped_reason": validation_stopped_reason,
        "validation_stop_reason": validation_stop_reason,
        "validation_checked_count": page.scanned_count,
        "validation_deferred_count": validation_deferred_count,
        "page_incomplete": page_incomplete,
        "pool_exhausted": page.is_exhausted,
        "has_pending": page.has_pending,
        "continuation": {"available": page.next_cursor is not None, "cursor": page.next_cursor},
        "show_more_available": (page.next_cursor is None and page.resume_cursor is not None
                                and page.snapshot.requested_cap < 100),
        "show_more_cursor": (page.resume_cursor if page.next_cursor is None and page.resume_cursor is not None
                               and page.snapshot.requested_cap < 100 else None),
        "snapshot": {"status": "saved", "path": str(args.report), "snapshot_id": page.snapshot.snapshot_id},
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False) if args.json else render_report(
        report, assistant=args.assistant, format="html" if args.html else "markdown" if args.markdown else "plain"
    )
    if args.report_file:
        _write_exclusive_text(args.report_file, rendered)
    emit_progress(progress, ProgressEvent("page_finished", query=page.snapshot.query,
                  candidate_count=len(results), completed=len(results),
                  elapsed_ms=int((time.monotonic() - started_at) * 1000), status="complete"))
    _emit_rendered_report(rendered, args)
    return 0


def _explain(args: argparse.Namespace) -> int:
    snapshot = load_snapshot(args.report)
    state = snapshot_dict(snapshot)
    if args.result == "all":
        numbers = sorted(snapshot.result_numbers.values())
    else:
        try:
            numbers = [int(args.result)]
        except (TypeError, ValueError):
            raise ConfigurationError("--result must be a positive number or all") from None
    rendered: list[str] = []
    for number in numbers:
        record_view = resolve_materialized_result(snapshot, number)
        identity = next(item for item, value in snapshot.result_numbers.items() if value == number)
        record = dict(state["result_records"].get(identity, {}))
        if not record_view or not record:
            raise SnapshotError("stored explanation is unavailable")
        record["result_number"] = number
        rendered.append(render_explanation(state, record, format="markdown" if args.markdown else "plain"))
    if not rendered:
        raise SnapshotError("no materialized results are available to explain")
    print("\n\n".join(rendered))
    return 0


def _search(args: argparse.Namespace) -> int:
    started_at = time.monotonic()
    _resolve_search_args(args)
    if not clean_text(" ".join(args.query), 500):
        print(render_help(assistant=args.assistant, invocation="cli", format="markdown" if args.markdown else "plain"))
        return 0
    _check_artifact_paths(args.report_file, args.report_json)
    config, cache = _load(args)
    report = UniversalSkillFinder(config, cache=cache).search(
        " ".join(args.query), limit=args.limit, max_results=args.max_results,
        count=args.count, page_size=args.page_size, thorough=args.thorough,
        progress_callback=_progress_callback(args), preview=args.preview,
        source_ids=args.source, exclude_ids=args.exclude, offline=args.offline,
        refresh=args.refresh, dry_run=args.dry_run,
    )
    inventory_started_at = time.monotonic()
    if args.assistant and not args.dry_run and not args.no_installed_check:
        annotate_installed(report, args.assistant)
    else:
        reason = "preview" if args.dry_run else "opted_out" if args.no_installed_check else "assistant_unknown"
        report.installed_scan = {"status": "not_checked", "reason": reason, "assistant": args.assistant}
    report.timings["inventory_ms"] = int((time.monotonic() - inventory_started_at) * 1000)
    report.timings["cli_before_artifacts_ms"] = int((time.monotonic() - started_at) * 1000)
    if args.report_json:
        _persist_report_snapshot(report, args.report_json, args)
    if args.json:
        payload = report.to_dict()
        for key in ("show_more_available", "show_more_cursor"):
            if hasattr(report, key):
                payload[key] = getattr(report, key)
        rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        rendered = render_report(report, assistant=args.assistant,
                                 format="html" if args.html else "markdown" if args.markdown else "plain",
                                 dry_run=args.dry_run)
    if args.report_file:
        _write_exclusive_text(args.report_file, rendered)
    _emit_rendered_report(rendered, args)
    if args.dry_run:
        return 0
    successful = [item for item in report.coverage if item.status in SUCCESS_STATUSES]
    if not successful:
        return 2
    if args.strict:
        selected = set(args.source)
        failures = [
            item for item in report.coverage
            if (not selected or item.source_id in selected)
            and (item.incomplete_results or item.status not in SUCCESS_STATUSES | {"disabled", "not_selected", "excluded"})
        ]
        if failures:
            return 2
    return 0


def _sources(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    command = args.source_command
    if command == "list":
        if args.json:
            print(json.dumps({"schema_version": SCHEMA_VERSION, "sources": source_rows(config)}, indent=2, ensure_ascii=False))
            return 0
        print(render_sources_markdown(config))
        return 0
    if command == "enable":
        source = config.source(args.source_id)
        set_source_enabled(config, args.source_id, True)
        if source and not source.get("pack_enabled", True):
            print(f"Enabled {args.source_id} directly, but pack {source.get('pack')} remains disabled in {config.overlay_path}.")
            print(f"The source will not be searched. Ask to enable pack {source.get('pack')} explicitly; that also affects its other enabled sources.")
        else:
            print(f"Enabled {args.source_id} in {config.overlay_path}")
        return 0
    if command == "disable":
        set_source_enabled(config, args.source_id, False)
        print(f"Disabled {args.source_id} in {config.overlay_path}")
        return 0
    if command == "explain":
        source = config.source(args.source_id)
        if source is None:
            raise ConfigurationError(f"unknown source: {args.source_id}")
        row = next(item for item in source_rows(config) if item["id"] == args.source_id)
        print(json.dumps({"schema_version": SCHEMA_VERSION, "source": {**row, "adapter": source["adapter"]}},
                         indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    if command == "add-repo":
        source = add_github_repository(
            config, args.repository, source_id=args.source_id, ref=args.ref,
            include=args.include or None, exclude=args.exclude or None, pack_id=args.pack_id,
        )
        print(f"Added {source['id']} ({source['repository']}) to {config.overlay_path}")
        return 0
    if command == "add-local":
        source = add_local_directory(
            config, args.path, source_id=args.source_id,
            include=args.include or None, exclude=args.exclude or None, pack_id=args.pack_id,
        )
        print(f"Added {source['id']} ({source['path']}) to {config.overlay_path}")
        return 0
    if command == "remove":
        remove_custom_source(config, args.source_id)
        print(f"Removed {args.source_id} from {config.overlay_path}")
        return 0
    if command == "validate":
        errors = validate_effective(config)
        if errors:
            raise ConfigurationError("; ".join(errors))
        print(f"Configuration is valid: {len(config.sources)} sources, {len(config.packs)} packs")
        return 0
    if command == "init":
        changed = initialize_source_config(config)
        print(f"{'Created or updated' if changed else 'Existing'} source configuration: {config.overlay_path}")
        print('Edit the sources list: set "enabled" to true or false for each source.')
        disabled_packs = [clean_text(pack["id"]) for pack in config.packs if not pack.get("enabled", True)]
        if disabled_packs:
            print("Disabled packs still block their sources: " + ", ".join(disabled_packs) + ".")
            print('Ask "Enable pack PACK-ID" explicitly to change a pack; this also affects its other enabled sources.')
        return 0
    if command == "config-path":
        print(config.overlay_path)
        return 0
    if command == "retry":
        source = config.source(args.source_id)
        if source is None:
            raise ConfigurationError(f"unknown source: {args.source_id}")
        scope = UniversalSkillFinder._health_scope(source)
        if scope is None:
            raise ConfigurationError(f"source has no remote search operation: {args.source_id}")
        cache = Cache(Path(args.cache_dir)) if args.cache_dir else Cache()
        decision = HealthStore(cache).arm_retry(scope)
        if not decision.allowed:
            raise ConfigurationError(decision.reason or f"cannot arm retry for {args.source_id}")
        print(f"Armed one foreground retry for {args.source_id}; no request was sent.")
        return 0
    raise ConfigurationError(f"unknown sources command: {command}")


def _packs(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.pack_command == "list":
        for pack in config.packs:
            state = "enabled" if pack.get("enabled", True) else "disabled"
            count = sum(1 for source in config.sources if source.get("pack") == pack["id"])
            print(f"{pack['id']:<28} {state:<9} sources={count:<3} {pack.get('description', '')}")
        return 0
    if args.pack_command == "import":
        pack, sources = import_source_pack(config, args.path)
        noun = "source" if len(sources) == 1 else "sources"
        print(f"Imported {pack['id']} with {len(sources)} {noun} into {config.overlay_path}")
        return 0
    if args.pack_command == "remove":
        removed = remove_custom_pack(config, args.pack_id)
        noun = "source" if removed == 1 else "sources"
        print(f"Removed pack {args.pack_id} and {removed} {noun} from {config.overlay_path}")
        return 0
    enabled = args.pack_command == "enable"
    set_pack_enabled(config, args.pack_id, enabled)
    print(f"{'Enabled' if enabled else 'Disabled'} pack {args.pack_id} in {config.overlay_path}")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    config, cache = _load(args)
    print(f"universal-skill-finder {__version__}")
    metadata = release_metadata()
    print(f"Code revision ({metadata['revision_scope']}): {metadata['code_revision']}")
    print(f"Catalogue revision: {metadata['catalogue_revision']}")
    print(f"Configuration revision: {effective_config_revision(config.settings, config.packs, config.sources)}")
    print(f"Contracts: schema={SCHEMA_VERSION}, adapter={metadata['adapter_contract_version']}, cache={metadata['cache_format_version']}")
    print(f"Python: {platform.python_version()} ({sys.executable})")
    print(f"Configuration: {config.overlay_path}")
    print(f"Cache: {cache.root}")
    print("Adapters: " + ", ".join(sorted(adapters())))
    print(f"Sources: {sum(1 for s in config.sources if s['effective_enabled'])} enabled, {len(config.sources)} total")
    missing = [
        f"{source['id']}:{credential['environment_variable']}"
        for source in source_rows(config) if source["enabled"]
        for credential in source["credentials"]
        if credential["required"] and not credential["present"]
    ]
    if missing:
        print("Missing required source credentials: " + ", ".join(missing))
        return 2
    return 0


def _cache(args: argparse.Namespace) -> int:
    _, cache = _load(args)
    records = cache.metadata("queries")
    if args.cache_command != "list":
        raise ConfigurationError(f"unknown cache command: {args.cache_command}")
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    print(f"Cache: {cache.root}")
    if not records:
        print("No indexed registry query metadata is available.")
        return 0
    for item in records:
        print(
            f"{item['source_id']:<24} age={item['cache_age_seconds']:<6}s "
            f"limit={item['limit']:<3} query={item['query']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if not raw_argv:
        print(render_help(invocation="unknown", format="plain"))
        return 0
    args = parser.parse_args(_expand_search_shorthand(raw_argv))
    try:
        if args.command is None:
            print(render_help(invocation="unknown", format="plain"))
            return 0
        if args.command == "search":
            _resolve_search_args(args)
            return _search(args)
        if args.command == "help":
            print(render_help(assistant=args.assistant, invocation=args.invocation,
                              format="markdown" if args.markdown else "plain"))
            return 0
        if args.command == "page":
            return _page(args)
        if args.command == "explain":
            return _explain(args)
        if args.command in {"repositories", "sources"}:
            return _sources(args)
        if args.command == "packs":
            return _packs(args)
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "cache":
            return _cache(args)
        raise ConfigurationError(f"unknown command: {args.command}")
    except ConfigurationError as exc:
        print(f"configuration error: {clean_text(exc)}", file=sys.stderr)
        return 3
    except SnapshotError as exc:
        print(f"snapshot error: {clean_text(exc)}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"invalid data or plugin payload: {clean_text(exc)}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"file or network error: {clean_text(exc)}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
