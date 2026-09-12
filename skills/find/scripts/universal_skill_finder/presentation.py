"""Deterministic plugin reports and display-only, locally constructed commands."""
from __future__ import annotations

import html
import math
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlparse

from .models import Coverage, Result, SearchReport, UNSAFE_LOCATION_WARNING
from .text import clean_text, parse_github_repository, safe_skill_path, safe_web_url

ASSISTANTS = {"codex": "Codex", "claude-code": "Claude Code"}
SKILLS_CLI_VERSION = "1.5.23"
COMPLETED = {"ok", "cached"}
NOT_SEARCHED = {"disabled", "not_selected", "excluded", "offline_miss", "planned"}
COUNT_METRICS = (("github_stars", "GitHub repo stars"), ("stars", "stars"), ("installs", "installs"),
                 ("downloads", "downloads"), ("bookmarks", "bookmarks"), ("votes", "votes"))
_UNTRUSTED_URL_SCHEME = re.compile(r"\b(https?|file)://", re.IGNORECASE)


def cell(value: object, limit: int = 2000) -> str:
    """Render untrusted data as text, never as Markdown/HTML instructions."""
    text = html.escape(clean_text(value, limit), quote=False)
    text = re.sub(r"([\\`*_\[\]~])", r"\\\1", text)
    text = text.replace("|", "&#124;")
    # Apply this after Markdown escaping so the inert marker itself remains
    # readable rather than acquiring backslashes around its brackets.
    return _UNTRUSTED_URL_SCHEME.sub(lambda match: match.group(1) + "[:]//", text)


def _link(label: str, url: str) -> str:
    # Parentheses, brackets, quotes and table delimiters cannot break out of the
    # destination. Preserve existing percent escapes and URL query semantics.
    destination = quote(url, safe="/:?#@!$&*+,;=%~._-")
    return f"[{cell(label)}]({destination})"


def skill_link(result: Result) -> str:
    repository = parse_github_repository(result.repository)
    path = safe_skill_path(result.skill_path)
    if repository and path and result.ref:
        # A clickable GitHub URL can represent refs unsupported by the installer.
        ref = quote(result.ref, safe="")
        location = "" if path == "." else "/" + quote(path, safe="/")
        return _link(result.name, f"https://github.com/{repository}/tree/{ref}{location}")
    link = safe_web_url(result.canonical_url)
    if link:
        return _link(result.name, link)
    if result.install.get("kind") == "local":
        path_value = result.install.get("path")
        if isinstance(path_value, str) and Path(path_value).is_absolute():
            return _link(result.name, Path(path_value).as_uri())
    if repository:
        return _link(result.name, f"https://github.com/{repository}")
    for occurrence in result.occurrences:
        link = safe_web_url(occurrence.get("canonical_url"))
        if link:
            return _link(result.name, link)
    return f"{cell(result.name)} (skill link unavailable)"


def installation_fallback(result: Result, reason: str) -> str:
    """A failed command proposal still offers a useful, validated destination."""
    repository = parse_github_repository(result.repository)
    if repository:
        url = f"https://github.com/{repository}"
        path = safe_skill_path(result.skill_path)
        if path and result.ref:
            url += "/tree/" + quote(result.ref, safe="")
            if path != ".":
                url += "/" + quote(path, safe="/")
        action = _link("Open repository", url)
    else:
        url = safe_web_url(result.canonical_url)
        if not url:
            url = next((safe_web_url(item.get("canonical_url")) for item in result.occurrences
                        if safe_web_url(item.get("canonical_url"))), None)
        action = _link("Open listing", url) if url else "Listing link unavailable"
        if not url and result.install.get("kind") == "local":
            local = result.install.get("path")
            if isinstance(local, str) and Path(local).is_absolute():
                action = _link("Open local directory", Path(local).as_uri())
    return f"{action} · {cell(reason)}"


def _count(value: object) -> int | None:
    # Counts are not booleans, percentages, estimates such as '1.2k', or arbitrary
    # strings. Preserve zero while refusing implausibly large/unbounded output.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    if type(value) is float and value.is_integer():
        value = int(value)
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _value(item: object, name: str, default: object = None) -> object:
    """Read renderer evidence without coupling to dataclass or JSON envelopes."""
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _items(value: object) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _metric_entries(result: object) -> list[tuple[str, Mapping[object, object]]]:
    """Return source-scoped metrics shared by live and serialized reports."""
    metrics_by_source = _value(result, "metrics_by_source", {})
    source_ids = {str(source) for source in _items(_value(result, "source_ids", []))}
    if not isinstance(metrics_by_source, Mapping):
        return []
    entries = []
    for source_id, metrics in metrics_by_source.items():
        source = str(source_id)
        if source in source_ids and isinstance(metrics, Mapping):
            entries.append((source, metrics))
    return sorted(entries, key=lambda entry: entry[0])


def metrics_text(result: object) -> str:
    entries: list[str] = []
    # Preserve conflicting observations rather than adding mirrored counts or
    # treating source agreement as a skill-level popularity score.
    for source_id, metrics in _metric_entries(result):
        counts: list[str] = []
        for key, label in COUNT_METRICS:
            value = _count(metrics.get(key))
            if value is None:
                continue
            if key == "github_stars" and metrics.get("github_stars_repository"):
                identity = parse_github_repository(metrics["github_stars_repository"])
                repository = parse_github_repository(_value(result, "repository"))
                if not identity or not repository or identity.lower() != repository.lower():
                    continue
            count = f"{label}: {value:,}"
            checked = metrics.get("github_stars_observed_at")
            if key == "github_stars" and isinstance(checked, str):
                count += f" (observed {cell(checked, 40)})"
            counts.append(count)
        counts.extend(_tessl_assessment(result, source_id, metrics))
        if counts:
            entries.append(f"{cell(source_id)}: " + ", ".join(counts))
    return "; ".join(entries) if entries else "Not available"


def _tessl_assessment(result: object, source_id: str, metrics: Mapping[object, object]) -> list[str]:
    """Keep named-provider assessments separate from popularity and ranking."""
    if metrics.get("tessl_metric_scope") != "skill" or not any(
        _value(item, "adapter") == "tessl" and _value(item, "source_id") == source_id
        for item in _items(_value(result, "occurrences", []))
    ):
        return []
    assessments = []
    quality = metrics.get("tessl_quality")
    if type(quality) in {int, float} and 0 <= quality <= 1 and math.isfinite(quality):
        assessments.append(f"Tessl quality (raw): {quality:.3g}")
    level = metrics.get("tessl_security_level")
    if isinstance(level, str) and level in {"NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"}:
        assessments.append(f"Tessl security level: {level}")
    scored_at = metrics.get("tessl_scored_at")
    if assessments and isinstance(scored_at, str) and scored_at:
        assessments.append(f"scored {cell(scored_at, 40)}")
    return assessments


def _github_install_command(repository: object, skill_path: object, ref: object, name: object,
                            assistant: str | None) -> tuple[str | None, str]:
    """Build one shell-safe command from an already exact target identity."""
    if assistant not in ASSISTANTS:
        return None, "current assistant unknown"
    repository = parse_github_repository(repository)
    if not repository:
        return None, "repository unresolved"
    path = safe_skill_path(skill_path)
    if not path:
        return None, "skill directory unresolved"
    # The pinned Skills CLI splits /tree/ URLs at the first ref segment and uses
    # git clone --branch: neither HEAD, raw commits nor slash refs are reliable.
    if (not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", ref)
            or ref == "HEAD" or re.fullmatch(r"[A-Fa-f0-9]{7,64}", ref)
            or ".." in ref or ref.endswith((".", ".lock"))):
        return None, "compatible branch or tag unresolved"
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", name):
        return None, "exact skill name needs verification"
    # This ASCII whitelist is safe unquoted in POSIX shells and PowerShell.
    # Never slugify a display name or drop --skill, which could install siblings.
    location = "" if path == "." else "/" + path
    url = f"https://github.com/{repository}/tree/{ref}{location}"
    return (f"npx skills@{SKILLS_CLI_VERSION} add {url} --skill {name} "
            f"--agent {assistant} --copy"), ""


def install_command(result: Result, assistant: str | None) -> tuple[str | None, str]:
    """Propose one explicit host/path/name, without trusting remote command hints.

    No shell, subprocess, dependency lookup or installation happens here. Missing
    or ambiguous metadata must be inspected, not repaired into a broad install.
    """
    if UNSAFE_LOCATION_WARNING in result.warnings:
        return None, "unsafe installation target"
    install = result.install
    if install.get("kind") != "github":
        return None, "no verified installer target for this assistant"
    repository = parse_github_repository(result.repository)
    path = safe_skill_path(result.skill_path)
    if not repository or install.get("repository") != repository:
        return None, "repository unresolved"
    if not path or install.get("skill_path") != path:
        return None, "skill directory unresolved"
    if install.get("ref") != result.ref:
        return None, "compatible branch or tag unresolved"
    return _github_install_command(repository, path, result.ref, result.name, assistant)


def _coverage_status(item: Coverage) -> str:
    if item.incomplete_results and item.status in COMPLETED:
        age = f"; age {item.cache_age_seconds}s" if item.cache_age_seconds is not None else ""
        label = f"Partial cached (not contacted{age})" if item.status == "cached" else "Partial search"
    elif item.status == "ok":
        label = "Searched"
    elif item.status == "cached":
        age = f"; age {item.cache_age_seconds}s" if item.cache_age_seconds is not None else ""
        label = f"Cached (not contacted{age})"
    elif item.status in NOT_SEARCHED:
        label = f"Not searched ({cell(item.status)})"
    else:
        label = f"Failed ({cell(item.status)})"
    if item.detail:
        label += ": " + cell(item.detail, 300)
    return label


def installed_label(result: object) -> str:
    """Labels describe local evidence, never source identity or bundle safety."""
    state = _value(result, "installed", {})
    state = state if isinstance(state, Mapping) else {}
    labels = {
        "exact_local": "Installed locally",
        "matching_instructions": "Matching instructions found",
        "name_collision": "Name collision; different instructions",
    }
    label = labels.get(state.get("status"), "") if isinstance(state.get("status"), str) else ""
    evidence = state.get("evidence", [])
    if state.get("status") == "unknown" and isinstance(evidence, list) and "name_only" in evidence:
        label = "Name match; repository unverified"
    if label and state.get("status") != "name_collision" and isinstance(evidence, list) and "same_name_different_instructions" in evidence:
        label += "; conflicting local version also found"
    return label


def render_markdown(report: SearchReport, *, assistant: str | None = None, dry_run: bool = False) -> str:
    # Schema-1 records predate destination/target proofs. Retain their useful
    # text and coverage, but never synthesize a link or command from metadata.
    if _is_schema2(report):
        return render_report(report, assistant=assistant, format="markdown", dry_run=dry_run)
    title = f"Search: {cell(report.query)}"
    title += " · Destination preview" if dry_run else f" · Showing {len(report.results)} results"
    lines = [title, "", "| Source | Enabled | Search status | Matches |",
             "|---|---|---|---|"]
    for item in report.coverage:
        target = f" ({cell(item.target or item.host)})" if item.target or item.host else ""
        count = str(item.result_count) if item.status in COMPLETED else "-"
        enabled = "Yes" if item.enabled else "No"
        source = cell(item.source_id)
        lines.append(f"| {source}{target} | {enabled} | {_coverage_status(item)} | {count} |")
    if not report.coverage:
        lines.extend(["", "No configured sources."])
    if dry_run:
        metadata_hosts = sorted({host for item in report.coverage if item.status == "planned" for host in item.metadata_hosts})
        if metadata_hosts:
            lines.extend(["", "Source refreshes may also contact " + ", ".join(cell(host) for host in metadata_hosts)
                          + " for repository star counts. No search text is sent to these metadata endpoints."])
        lines.extend(["", "Preview only. No sources were searched."])
        return "\n".join([*lines, "", "Legacy report: no destination proof is available, so links and commands are omitted."])
    lines.extend(["", "| # | Skill | Install | Found in | Metrics | What it does |",
                  "|---|---|---|---|---|---|"])
    for index, result in enumerate(report.results, 1):
        action = "Install unavailable: legacy report lacks current target proof"
        location = result.repository or "registry listing"
        if result.repository and result.skill_path:
            location += " → " + result.skill_path
        found = cell(location) + "; reported by " + ", ".join(cell(source) for source in result.source_ids)
        description = cell(result.description, 180)
        label = installed_label(result)
        skill = cell(result.name) + (f" · {label}" if label else "")
        lines.append(f"| {index} | {skill} | {action} | {found} | {metrics_text(result)} | {description} |")
    if not report.results:
        completed = any(item.status in COMPLETED for item in report.coverage)
        partial = any(item.incomplete_results for item in report.coverage)
        message = ("No verified matches were returned; search coverage is incomplete." if partial else
                   "No matches were found in the sources that completed." if completed else
                   "No sources completed. Search coverage is unavailable; retry after addressing the source statuses above.")
        lines.extend(["", message])
    for index, result in enumerate(report.results, 1):
        if result.warnings:
            lines.extend(["", f"Warnings for #{index}: " + "; ".join(cell(warning) for warning in result.warnings)])
    if report.results:
        if any(metrics_text(result) != "Not available" for result in report.results):
            lines.extend(["", "Popularity counts are source-reported snapshots, not quality or safety scores. GitHub stars count the entire repository, not the individual skill. Cached counts may be stale; counts from different sources are not combined."])
        if any(_tessl_assessment(result, source, metrics) for result in report.results
               for source, metrics in result.metrics_by_source.items() if source in result.source_ids and isinstance(metrics, dict)):
            lines.extend(["", "Tessl assessments are source-reported and version-specific, not this finder's safety verdict. Quality is the raw normalized value, not a popularity count. Security level NONE is not a guarantee of safety; missing assessments mean unknown. Scores do not change federation ranking and cached assessments may be stale."])
        lines.extend(["", "Legacy result metadata is informational only; run a current checked search before inspecting or installing."])
    scan = report.installed_scan
    if isinstance(scan, dict) and scan.get("status") in {"complete", "partial"}:
        lines.extend(["", "Installed-skill check covers the current project's and user's standard skill directories only, not parent projects, admin directories or plugin caches. Matching instructions means identical SKILL.md bytes, not the same repository or complete bundle. Name collisions need review; installation commands remain proposals."])
        if scan["status"] == "partial":
            lines.extend(["", "Local skill inventory is incomplete: unreadable, symlinked or over-limit entries may have been skipped. An unmatched result is not proof that a skill is absent."])
        if "This platform refuses static symlinks but has weaker protection against concurrent filesystem changes." in scan.get("limitations", []):
            lines.extend(["", "On this platform the local check has weaker protection against concurrent filesystem changes."])
    return "\n".join([*lines, "", "Legacy report: no destination proof is available, so links and commands are omitted."])


# Schema-2 rendering intentionally depends on structural fields only. This lets
# renderer tests use small records and keeps the renderer decoupled from model
# release order. It never validates, ranks, opens files, or contacts a network.
_ELIGIBLE = {"eligible", "verified"}
_CHECKED = _ELIGIBLE | {"reachable"}
_ROLE_PRIORITY = {"listing": 0, "bundle_listing": 1, "source_page": 2, "repository": 3}


def _text(value: object, limit: int = 400) -> str:
    raw = clean_text(value, limit + 1)
    if len(raw) <= limit:
        return cell(raw, limit)
    shortened = raw[:limit].rsplit(" ", 1)[0].rstrip() or raw[:limit]
    return cell(shortened + "…", limit + 1)


def _status(item: object) -> str:
    return str(_value(item, "status", "")).lower()


def _proof_url(proof: object, *, accepted: set[str] = _ELIGIBLE) -> str | None:
    if _status(proof) not in accepted:
        return None
    url = _value(proof, "url") or _value(proof, "final_url")
    return safe_web_url(url)


def _proofs(item: object) -> list[object]:
    for field in ("link_proofs", "links", "destination_proofs"):
        found = _items(_value(item, field, []))
        if found:
            return found
    primary = _value(item, "primary_destination")
    return [primary] if primary is not None else []


def _proof_for(item: object, roles: set[str], *, accepted: set[str] = _ELIGIBLE) -> object | None:
    candidates = []
    for proof in _proofs(item):
        role = str(_value(proof, "role", "")).lower()
        if role in roles and _proof_url(proof, accepted=accepted):
            candidates.append(proof)
    candidates.sort(key=lambda proof: (int(_value(proof, "native_rank", 1_000_000) or 1_000_000),
                                        str(_proof_url(proof, accepted=accepted) or "")))
    return candidates[0] if candidates else None


def _is_schema2(report: object) -> bool:
    version = _value(report, "report_format_version", _value(report, "report_format", 1))
    try:
        if int(version) < 2:
            return False
    except (TypeError, ValueError):
        return False
    return True


def _number(result: object, fallback: int) -> int:
    value = _value(result, "number", _value(result, "result_number", fallback))
    return value if type(value) is int and value > 0 else fallback


def _primary_skill_link(result: object) -> str:
    proof = _proof_for(result, {"skill", "skill_destination", "listing", "bundle_listing"}, accepted=_ELIGIBLE)
    url = _proof_url(proof, accepted=_ELIGIBLE) if proof else None
    name = _text(_value(result, "name", "unnamed skill"), 160)
    if proof is not None and str(_value(proof, "role", "")).lower() == "bundle_listing":
        name += " (bundle)"
    return _link(name, url) if url else name


def _coverage_rows(report: object) -> list[object]:
    """Use preserved source coverage for pages without inventing a new search."""
    coverage = _value(report, "coverage", None)
    if isinstance(coverage, (list, tuple)):
        return list(coverage)
    # These additive names keep renderer compatibility while the coordinator
    # migrates old snapshot/page envelopes.  They are evidence supplied by the
    # caller, never reconstructed from the current page's results.
    for field in ("original_coverage", "coverage_rows"):
        rows = _value(report, field, None)
        if isinstance(rows, (list, tuple)):
            return list(rows)
    return []


def _is_continuation_page(report: object) -> bool:
    return (_value(report, "continuation_page", False) is True
            or str(_value(report, "mode", "online")) == "continuation")


def _location(result: object) -> str:
    entries = _items(_value(result, "location", _value(result, "location_links", [])))
    rendered: list[str] = []
    for entry in entries:
        label = _text(_value(entry, "label", _value(entry, "path", "location")), 200)
        url = _proof_url(entry, accepted=_CHECKED)
        rendered.append(_link(label, url) if url else f"{label} ({_text(_value(entry, 'reason', _status(entry) or 'not verified'), 160)})")
    if rendered:
        return " / ".join(rendered)
    repository = _value(result, "repository")
    path = _value(result, "skill_path")
    proof = _proof_for(result, {"skill", "skill_destination", "repository"}, accepted=_CHECKED)
    proof_url = _proof_url(proof, accepted=_CHECKED) if proof else None
    role = str(_value(proof, "role", "")).lower() if proof else ""
    resolved_target = _resolved_target(_value(result, "target_proof"))
    resolved_path = resolved_target[1] if resolved_target else None
    parts = [(_link(repository, proof_url) if repository and proof_url and role == "repository" else cell(repository, 200))] if repository else []
    if path:
        path_is_resolved = resolved_path is None or path == resolved_path
        parts.append(_link(path, proof_url) if proof_url and path_is_resolved and role in {"skill", "skill_destination"} else cell(path, 200))
    return " / ".join(parts) if parts else "Location unavailable"


def _canonical_compact_text(value: object) -> str:
    """Escape an already canonical aggregate without imposing a new display cap."""
    raw = html.unescape("" if value is None else str(value))
    raw = re.sub(r"\\([\\`*_\[\]~])", r"\1", raw)
    return cell(raw, max(2000, len(raw)))


def _branch_suffix(ref: object) -> str:
    """Hide the conventional main branch while retaining meaningful refs."""
    return "" if not ref or ref == "main" else " · branch " + cell(ref, 400)


def _is_local_result(result: object) -> bool:
    proof = _value(result, "target_proof", {})
    if _status(proof) in _ELIGIBLE and _value(proof, "kind") == "local_directory":
        return True
    return any(_value(item, "adapter") == "local-directory" for item in _items(_value(result, "occurrences", [])))


def _is_local_source(result: object, source_id: str) -> bool:
    return any(
        _value(item, "source_id") == source_id and _value(item, "adapter") == "local-directory"
        for item in _items(_value(result, "occurrences", []))
    )


def _compact_location(result: object) -> str:
    """Render one useful location line across repository, registry and local results."""
    repository = _value(result, "repository")
    path = _value(result, "skill_path")
    ref = _value(result, "ref")
    if _is_local_result(result):
        location = "Local directory"
        if path and path != ".":
            location += " › " + cell(path, 400)
        return "**Location:** " + location
    entries = _items(_value(result, "location", _value(result, "location_links", [])))
    if entries:
        locations = []
        for entry in entries:
            label = _value(entry, "label", _value(entry, "path", "location"))
            url = _proof_url(entry, accepted=_CHECKED)
            reason = _value(entry, "reason", _status(entry) or "not verified")
            locations.append(_link(str(label), url) if url else f"{cell(label, 400)} ({_text(reason, 160)})")
        return "**Location:** " + " › ".join(locations) + _branch_suffix(ref)

    proof = _proof_for(result, {"skill", "skill_destination", "repository"}, accepted=_CHECKED)
    url = _proof_url(proof, accepted=_CHECKED) if proof else None
    role = str(_value(proof, "role", "")).lower() if proof else ""
    resolved_target = _resolved_target(_value(result, "target_proof"))
    identity_matches = resolved_target is None or (
        _value(result, "repository"), path, ref) == (resolved_target[0], resolved_target[1], resolved_target[2])

    parts: list[str] = []
    if repository:
        repository_url = url if role == "repository" and parse_github_repository(url) == parse_github_repository(repository) else None
        parts.append(_link(str(repository), repository_url) if repository_url else cell(repository, 400))
    if path and path != ".":
        path_url = url if url and identity_matches and role in {"skill", "skill_destination"} else None
        parts.append(_link(str(path), path_url) if path_url else cell(path, 400))
    if parts:
        location = " › ".join(parts)
        if repository and not path:
            location += " (skill folder unavailable)"
        elif path and not repository:
            location += " (repository unavailable)"
        return "**Location:** " + location + _branch_suffix(ref)

    inspection = _proof_for(
        result,
        {"skill", "skill_destination", "listing", "bundle_listing", "repository", "source_page"},
        accepted=_CHECKED,
    )
    inspection_url = _proof_url(inspection, accepted=_CHECKED) if inspection else None
    inspection_role = str(_value(inspection, "role", "")).lower() if inspection else ""
    labels = {
        "skill": "Checked skill destination",
        "skill_destination": "Checked skill destination",
        "listing": "Checked skill listing",
        "bundle_listing": "Checked bundle listing",
        "repository": "Checked repository",
        "source_page": "Checked source page",
    }
    if inspection_url:
        label = labels.get(inspection_role, "Checked destination")
        return "**Location:** " + _link(label, inspection_url) + " · exact skill location unavailable"
    return "**Location:** Exact skill location unavailable"


def _compact_resolved_target(result: object) -> str | None:
    """Retain a material target correction in the same human-readable syntax."""
    proof = _value(result, "target_proof")
    target = _resolved_target(proof)
    reported = _value(proof, "reported")
    if target is None or not isinstance(reported, Mapping):
        return None
    repository, skill_path, ref, _name = target
    resolved = {"repository": repository, "ref": ref, "skill_path": skill_path}
    if not any(key in reported and reported.get(key) != value for key, value in resolved.items()):
        return None
    parts = [cell(repository, 400)]
    if skill_path != ".":
        parts.append(cell(skill_path, 400))
    return "**Verified target:** " + " › ".join(parts) + _branch_suffix(ref)


def _attributions(result: object) -> str:
    explicit = _items(_value(result, "found_on", _value(result, "attributions", [])))
    if not explicit:
        explicit = _items(_value(result, "source_attributions", []))
    by_source: dict[str, list[object]] = {}
    for entry in explicit:
        source = str(_value(entry, "source_id", _value(entry, "id", "unknown")))
        by_source.setdefault(source, []).append(entry)
    # A failed or absent listing must not erase a source that contributed to a
    # merged result.  It remains plain provenance, not a guessed destination.
    for source in _items(_value(result, "source_ids", [])):
        source_id = str(source)
        by_source.setdefault(source_id, [
            {"source_id": source_id, "label": source_id, "role": "unavailable", "status": "not_checked",
             "reason": "listing not verified"}
        ])
    if not by_source:
        return " · ".join(cell(value, 120) for value in sorted(set(_items(_value(result, "source_ids", []))))) or "Source unavailable"
    rendered: list[str] = []
    unverified_listing_labels: list[str] = []
    all_are_unverified_listings = True
    for source, entries in sorted(by_source.items()):
        entries.sort(key=lambda entry: (_ROLE_PRIORITY.get(str(_value(entry, "role", "")).lower(), 99),
                                        str(_value(entry, "url", ""))))
        entry = entries[0]
        label = _text(_value(entry, "label", source), 120)
        url = _proof_url(entry, accepted=_CHECKED)
        role = str(_value(entry, "role", "")).replace("_", " ")
        suffix = _value(entry, "suffix") or ("bundle listing" if role == "bundle listing" else None)
        parsed = urlparse(url) if url else None
        source_is_tessl = source.casefold() == "tessl"
        # A Tessl occurrence can carry a checked GitHub repository proof.  That
        # verifies a repository role, not a Tessl listing, so never turn the
        # Tessl attribution itself into a misleading GitHub link.
        eligible_source_role = role in {"listing", "bundle listing", "source page"}
        tessl_origin = parsed is not None and parsed.hostname in {"tessl.io", "www.tessl.io"}
        if url and eligible_source_role and (not source_is_tessl or tessl_origin):
            text = _link(label, url)
            if suffix:
                text += f" ({_text(suffix, 120)})"
            all_are_unverified_listings = False
        else:
            fallback = ("local directory" if _is_local_source(result, source) else
                        "repository source" if role == "repository" else "listing not verified")
            reason = _text(_value(entry, "reason", fallback), 160)
            text = f"{label} ({reason})"
            if reason == "listing not verified":
                unverified_listing_labels.append(label)
            else:
                all_are_unverified_listings = False
        rendered.append(text)
    if len(unverified_listing_labels) > 1 and all_are_unverified_listings:
        return ", ".join(unverified_listing_labels) + " (listings not verified)"
    return " · ".join(rendered)


def _resolved_target(proof: object) -> tuple[object, object, object, object] | None:
    """Return only the exact identity established by a fresh target proof."""
    kind = str(_value(proof, "kind", ""))
    if _status(proof) not in _ELIGIBLE or kind != "github":
        return None
    if (_value(proof, "method") != "anonymous_exact_skill_md_get"
            or _value(proof, "identity_basis") != "github-exact-skill-md-v1"
            or not safe_web_url(_value(proof, "url"))):
        return None
    resolved = _value(proof, "resolved")
    if not isinstance(resolved, Mapping):
        return None
    repository = _value(resolved, "repository")
    ref = _value(resolved, "ref")
    skill_path = _value(resolved, "skill_path")
    actual_name = _value(proof, "actual_name")
    content_hash = _value(proof, "content_sha256")
    if not (isinstance(content_hash, str) and re.fullmatch(r"[a-fA-F0-9]{64}", content_hash)):
        return None
    if not all(isinstance(value, str) and value for value in (repository, ref, skill_path, actual_name)):
        return None
    return repository, skill_path, ref, actual_name


def _target_is_proved(result: object) -> bool:
    return _resolved_target(_value(result, "target_proof")) is not None


def _resolved_target_note(result: object) -> str | None:
    proof = _value(result, "target_proof")
    target = _resolved_target(proof)
    reported = _value(proof, "reported")
    if target is None or not isinstance(reported, Mapping):
        return None
    repository, skill_path, ref, _name = target
    resolved = {"repository": repository, "ref": ref, "skill_path": skill_path}
    if not any(key in reported and reported.get(key) != value for key, value in resolved.items()):
        return None
    location = f"{cell(repository, 160)} / {cell(skill_path, 180)} @ {cell(ref, 120)}"
    return f"**Resolved target:** {location}"


def _install_v2(result: object, assistant: str | None) -> tuple[str | None, str]:
    proof = _value(result, "target_proof")
    if _status(proof) in _ELIGIBLE and _value(proof, "kind") == "local_directory":
        return None, "local directory installation requires manual review"
    target = _resolved_target(proof)
    if target is None:
        detail = _value(proof, "detail")
        return None, _text(detail or "exact skill target proof is missing", 180)
    return _github_install_command(*target, assistant)


def _compact_actions(result: object, number: int, assistant: str | None) -> str:
    """Expose safe next steps without repeating a long command in every card."""
    command, reason = _install_v2(result, assistant)
    if command:
        return f"Inspect and install: type **Inspect #{number}**  **Install #{number}**"
    inspection = _proof_for(
        result,
        {"skill", "skill_destination", "listing", "bundle_listing", "repository", "source_page"},
        accepted=_CHECKED,
    )
    inspection_url = _proof_url(inspection, accepted=_CHECKED) if inspection else None
    text = f"**Inspect #{number}** · Installation unavailable: {_text(reason, 180)}"
    if inspection_url:
        text += ". " + _link("Open checked destination", inspection_url)
    else:
        text += "."
    return text


def _coverage_status_v2(item: object) -> str:
    status = str(_value(item, "status", _value(item, "live_status", "unknown"))).lower()
    incomplete = _value(item, "incomplete_results", False) is True
    if status in {"ok", "searched", "complete"}:
        label = "Partial search" if incomplete else "Searched"
    elif status == "cached":
        age = _value(item, "cache_age_seconds")
        if incomplete:
            label = f"Partial cached (not contacted; age {age}s)" if type(age) is int and age >= 0 else "Partial cached (not contacted)"
        else:
            label = f"Cached (not contacted; age {age}s)" if type(age) is int and age >= 0 else "Cached (not contacted)"
    elif status in NOT_SEARCHED:
        label = "Not searched"
    else:
        label = "Failed"
    detail = _value(item, "detail")
    return label + (f": {_text(detail, 200)}" if detail else "")


def _coverage_table(report: object) -> list[str]:
    rows = _coverage_rows(report)
    lines = ["## Source coverage", "", "| Source | Search status | Candidates returned | Shown | Enabled |", "|---|---|---:|---:|---|"]
    for item in rows:
        source = _text(_value(item, "source_id", "unknown"), 120)
        proof = _value(item, "link_proof", _value(item, "public_link"))
        url = _proof_url(proof, accepted=_CHECKED) if proof is not None else None
        source = _link(source, url) if url else source
        status = _coverage_status_v2(item)
        raw_count = _value(item, "candidates_returned", _value(item, "result_count"))
        count = str(raw_count) if type(raw_count) is int and raw_count >= 0 and "Failed" not in status and "Not searched" not in status else "-"
        shown = _value(item, "shown", 0)
        shown_text = str(shown) if type(shown) is int and shown >= 0 else "0"
        enabled = "✅ Enabled" if _value(item, "enabled", True) else "❌ Disabled"
        lines.append(f"| {source} | {status} | {count} | {shown_text} | {enabled} |")
    if len(rows) > 1:
        lines.extend(["", "Shown counts unique results on this page per source and are not additive for merged results."])
    return lines


def _footer(report: object) -> list[str]:
    if str(_value(report, "mode", "online")) != "online":
        return []
    if not any(str(_value(item, "status", "")).lower() in COMPLETED
               for item in _coverage_rows(report)):
        return []
    proof = _value(report, "footer_link_proof")
    url = _proof_url(proof, accepted=_ELIGIBLE) if proof is not None else None
    if url:
        return ["", _link("⭐ Star Universal Skill Finder on GitHub", url)]
    return ["", "⭐ Star Universal Skill Finder on GitHub (repository link not verified for this report)."]


def _summary_data(report: object, results: list[object]) -> dict[str, object]:
    """Collect bounded presentation facts once for the header and Notes."""
    def count(value: object, default: int = 0) -> int:
        return value if type(value) is int and value >= 0 else default

    accepted = count(_value(report, "accepted_candidates", _value(report, "accepted_occurrences", 0)))
    unique = count(_value(report, "unique_skills", _value(report, "unique_count", len(results))), len(results))
    merged = count(_value(report, "duplicates_merged", max(0, accepted - unique)))
    checks = {
        name: count(_value(report, name, _value(report, name + "_count", 0)))
        for name in ("eligible", "unavailable", "inconclusive", "not_checked")
    }
    current = count(_value(report, "page_shown_count", _value(report, "page_shown", len(results))), len(results))
    cumulative = count(_value(report, "materialized_count", _value(
        report, "cumulative_materialized_count", _value(report, "materialized_total", current))), current)
    page_start = count(_value(report, "page_start", 1), 1)
    page_range = _value(report, "page_range", f"{page_start}–{page_start + current - 1}" if current else "0")
    requested = count(_value(report, "requested_count", 10), 10)
    page_size = count(_value(report, "page_size", 10), 10)
    coverage = _coverage_rows(report)
    searched = sum(1 for item in coverage if str(_value(item, "status", "")).lower() in {"ok", "searched", "complete"})
    cached = sum(1 for item in coverage if str(_value(item, "status", "")).lower() == "cached")
    not_searched = sum(1 for item in coverage if str(_value(item, "status", "")).lower() in NOT_SEARCHED)
    failed = max(0, len(coverage) - searched - cached - not_searched)
    partial_sources = sorted(
        str(_value(item, "source_id", "unknown")) for item in coverage
        if (_value(item, "incomplete_results", False) is True
            and str(_value(item, "status", "")).lower() not in NOT_SEARCHED)
    )
    verification_all_zero = unique > 0 and current == 0 and checks["eligible"] == 0 and (
        checks["inconclusive"] > 0 or checks["not_checked"] > 0 or sum(checks.values()) == 0
    )
    return {
        "accepted": accepted, "unique": unique, "merged": merged, "checks": checks,
        "current": current, "cumulative": cumulative, "page_range": page_range,
        "requested": requested, "page_size": page_size, "coverage": coverage,
        "searched": searched, "cached": cached, "not_searched": not_searched,
        "failed": failed, "partial_sources": partial_sources,
        "coverage_unavailable": str(_value(report, "coverage_context", "")) == "unavailable" and not coverage,
        "partial": bool(failed or partial_sources or verification_all_zero),
    }


def _counted(value: int, noun: str) -> str:
    plural = "matches" if noun == "match" else noun + "s"
    return f"{value} " + (noun if value == 1 else plural)


def _summary_header(report: object, results: list[object], summary: Mapping[str, object] | None = None) -> str:
    """A scan-first heading; continuation pages never claim a fresh source search."""
    data = summary or _summary_data(report, results)
    query = _text(_value(report, "query", ""), 400)
    current = int(data["current"])
    if _is_continuation_page(report):
        return "\n".join([
            f"Search: **{query}**",
            "",
            f"**{_counted(int(data['unique']), 'saved skill')}** · **{current} shown** · **no new search**",
        ])
    result_line = f"**{_counted(int(data['unique']), 'match')}** · **{current} shown**"
    if data["partial"]:
        result_line += " · **partial**"
    return "\n".join([
        f"Search: **{query}**",
        "",
        result_line,
        f"Sources: **{data['searched']} searched** · **{data['cached']} cached**",
    ])


def _summary_notes(report: object, results: list[object], summary: Mapping[str, object] | None = None) -> list[str]:
    """Retain the detailed former header below the cards instead of deleting it."""
    data = summary or _summary_data(report, results)
    checks = data["checks"]
    if not isinstance(checks, Mapping):
        raise ValueError("summary destination checks must be a mapping")
    lines = [
        f"- Candidates: {data['accepted']} accepted · {data['unique']} unique skills · {data['merged']} duplicate entries merged.",
        ("- Destination checks: "
         f"{checks['eligible']} with verified destinations (not necessarily install-ready) · "
         f"{checks['unavailable']} unavailable · {checks['inconclusive']} inconclusive · "
         f"{checks['not_checked']} not checked."),
        "- Page: " + _page_position_line(data["page_range"], data["current"], data["cumulative"]) + ".",
        f"- Requested up to {data['requested']} results · Page size {data['page_size']}.",
    ]
    if data["coverage_unavailable"]:
        lines.append("- Source coverage: unavailable in this older snapshot.")
    else:
        lines.append("- Sources: " + f"{data['searched']} searched · {data['cached']} cached · "
                     f"{data['failed']} failed · {data['not_searched']} not searched.")
    if data["partial"]:
        lines.append("- Status: partial coverage or destination verification; counts above are not a confirmed zero-match result.")
    return lines


def _notes(report: object, results: list[object]) -> list[str]:
    lines = ["## Notes", ""]
    summary = _summary_data(report, results)
    lines.extend(_summary_notes(report, results, summary))
    warnings: dict[str, list[int]] = {}
    for fallback, result in enumerate(results, 1):
        for warning in _items(_value(result, "warnings", [])):
            warnings.setdefault(_text(warning, 260), []).append(_number(result, fallback))
    for warning, numbers in sorted(warnings.items()):
        labels = ", ".join(f"#{number}" for number in numbers)
        lines.append(f"- {labels}: {warning}")
    for note in _items(_value(report, "notes", [])):
        lines.append(f"- {_text(note, 300)}")
    partial_sources = sorted(
        str(_value(item, "source_id", "unknown"))
        for item in _coverage_rows(report)
        if _value(item, "incomplete_results", False) is True
    )
    if partial_sources:
        lines.append("- Partial coverage: " + ", ".join(cell(source, 120) for source in partial_sources) + ".")
    if any(metrics_text(result) != "Not available" for result in results):
        lines.append("- Popularity counts are source-reported snapshots, not quality or safety scores. GitHub stars count the entire repository, not the individual skill. Counts from different sources are not combined.")
    if any(_tessl_assessment(result, source, metrics) for result in results
           for source, metrics in _metric_entries(result)):
        lines.append("- Tessl assessments are source-reported and version-specific, not this finder's safety verdict. Scores do not change federation ranking.")
    if any(_target_is_proved(result) for result in results):
        lines.append("- The Inspect and install prompt appears only when Universal Skill Finder can generate an exact local proposal after selection. Search does not display or execute it, and it is not a safety endorsement.")
    if any(not _target_is_proved(result) for result in results):
        lines.append("- Installation unavailable means the checked destination does not establish the exact target required for a command.")
    inventory = _value(report, "installed_scan", _value(report, "inventory"))
    limitation = _value(inventory, "limitations") if inventory is not None else None
    if _value(inventory, "status") in {"complete", "partial"}:
        lines.append("- Installed-skill evidence covers only the configured standard directories. Matching instructions means identical SKILL.md bytes, not the same repository or complete bundle.")
    for item in _items(limitation):
        lines.append(f"- Inventory: {_text(item, 260)}")
    if _value(inventory, "status") == "partial":
        lines.append("- Local skill inventory is incomplete. An unmatched result is not proof that a skill is absent.")
    if len(lines) == 2:
        lines.append("- No additional notes.")
    return lines


def _actions(report: object, results: list[object]) -> list[str]:
    lines = ["## Next actions", ""]
    mode = str(_value(report, "mode", "online"))
    if mode == "offline_preview":
        lines.append("List sources or run a checked online search.")
        return lines
    if not results:
        if _is_continuation_page(report):
            if _value(report, "pool_exhausted", False) is True:
                lines.append("This saved snapshot is exhausted. Run a separate new search for more results; no source search was rerun.")
            elif _value(report, "has_pending", False) is True:
                detail = _text(_value(report, "validation_stopped_reason", "destination checks are incomplete"), 200)
                lines.append(f"No additional verified results were materialized; {detail}.")
            else:
                lines.append("This saved snapshot has no additional verified results on this page.")
            if _value(report, "can_explain", False):
                lines.append("Explain #N is available from this saved snapshot.")
            continuation = _value(report, "continuation", _value(report, "continuation_available", False))
            if continuation is True or _value(continuation, "available", False):
                lines.append("Next page is available from this saved snapshot.")
            if _value(report, "show_more_available", False):
                lines.append("Show more can extend this saved snapshot.")
        else:
            lines.append("List sources or run a checked online search.")
            inconclusive = _value(report, "inconclusive", _value(report, "inconclusive_count", 0))
            if type(inconclusive) is int and inconclusive > 0:
                lines.append("Retry this checked search after resolving the reported access or destination-verification errors.")
        return lines
    lines.append("Say **Inspect #N**, **Install #N**, or **List sources**.")
    if _value(report, "can_explain", False):
        lines.append("Explain #N is available from this saved snapshot.")
    continuation = _value(report, "continuation", _value(report, "continuation_available", False))
    if continuation is True or _value(continuation, "available", False):
        lines.append("Next page is available from this saved snapshot.")
    if _value(report, "show_more_available", False):
        lines.append("Show more can extend this saved snapshot.")
    return lines


def _empty_results_message(report: object, coverage_rows: list[object]) -> str:
    """Keep empty online pages precise across every deterministic renderer."""
    incomplete = any(_value(item, "incomplete_results", False) is True for item in coverage_rows)
    completed = any(str(_value(item, "status", "")).lower() in COMPLETED for item in coverage_rows)
    unique = _value(report, "unique_skills", _value(report, "unique_count", 0))
    inconclusive = _value(report, "inconclusive", _value(report, "inconclusive_count", 0))
    not_checked = _value(report, "not_checked", _value(report, "not_checked_count", 0))
    context = str(_value(report, "coverage_context", ""))
    if _is_continuation_page(report) and context == "unavailable":
        return "No additional verified matches were materialized from this saved snapshot. Original source coverage is unavailable in this older snapshot."
    if _is_continuation_page(report) and _value(report, "pool_exhausted", False) is True:
        return "This saved snapshot is exhausted; no additional verified matches were materialized."
    if _is_continuation_page(report) and _value(report, "has_pending", False) is True:
        return f"No additional verified matches were materialized; {_text(_value(report, 'validation_stopped_reason', 'destination checks are incomplete'), 200)}."
    if _is_continuation_page(report):
        return "No additional verified matches were materialized from this saved snapshot."
    if incomplete:
        return "No verified matches were returned; search coverage is incomplete."
    # A non-empty ranked pool is not a zero-match search.  Keep the distinction
    # explicit when every candidate failed or has yet to complete destination
    # verification, so users do not retry a different query unnecessarily.
    if type(unique) is int and unique > 0:
        if type(inconclusive) is int and inconclusive > 0:
            return "No verified matches were materialized; destination verification is inconclusive for one or more candidates."
        if type(not_checked) is int and not_checked > 0:
            return "No verified matches were materialized; destination verification did not complete for one or more candidates."
        return "No verified matches were materialized from the ranked candidates."
    if completed:
        return "No matches were found in the sources that completed."
    return "No sources completed. Search coverage is unavailable."


def _page_position_line(page_range: object, current: object, cumulative: object) -> str:
    """Render a useful page position when no verified card received a number."""
    if type(current) is int and current == 0:
        return f"No verified results materialized on this page · {cumulative} materialized so far"
    return f"{page_range} · {current} on this page · {cumulative} materialized so far"


def _offline_previews(report: object) -> list[str]:
    previews = _items(_value(report, "candidate_previews", []))[:3]
    lines = ["Candidate previews (" + str(len(previews)) + ")", ""]
    for preview in previews:
        name = _text(_value(preview, "name", "unnamed candidate"), 160)
        repository = _value(preview, "repository")
        sources = ", ".join(cell(item, 120) for item in _items(_value(preview, "sources", _value(preview, "source_ids", []))))
        lines.append(f"- {name}" + (f" · {cell(repository, 200)}" if repository else ""))
        lines.append(f"  Availability unverified: {_text(_value(preview, 'reason', _value(preview, 'availability', 'not checked')), 200)}")
        if sources:
            lines.append(f"  Sources: {sources}")
        lines.append(f"  {_text(_value(preview, 'description', ''), 400)}")
    return lines


def render_report(report: object, *, assistant: str | None = None, format: str = "markdown", dry_run: bool = False) -> str:
    """Render a trusted schema-2 report from already selected, validated evidence.

    ``report`` may be a live ``SearchReport`` or its serialized JSON-shaped
    mapping, but mappings must come from the report pipeline or from snapshot
    materialization after its proof-demotion/validation boundary. Rendering is
    deliberately not a validator: it performs no I/O, wall-clock freshness
    decision, or source-origin authentication. It only renders already checked
    proof roles and keeps all other URL-like source text inert.
    """
    if format not in {"markdown", "plain", "html"}:
        raise ValueError("format must be markdown, plain, or html")
    if format == "html":
        return render_html_report(report, assistant=assistant, dry_run=dry_run)
    if dry_run:
        lines = ["Destination preview", "", "No sources were searched."]
        hosts = sorted({str(host) for item in _coverage_rows(report)
                        for host in _items(_value(item, "metadata_hosts", [])) if host})
        if hosts:
            lines.extend(["", "A live refresh may also contact: " + ", ".join(cell(host, 200) for host in hosts)
                          + ". No search text is sent to these optional metadata endpoints."])
        if _coverage_rows(report):
            lines.extend(["", *_coverage_table(report)])
        return _plain(lines) if format == "plain" else "\n".join(lines)
    mode = str(_value(report, "mode", "online"))
    results = _items(_value(report, "results", []))
    if mode == "offline_preview":
        lines = ["## Universal Skill Finder", "", f"Search: **{_text(_value(report, 'query', ''), 400)}**", "", "Offline preview", "", "0 checked results", "", *_offline_previews(report)]
        if _coverage_rows(report):
            lines.extend(["", *_coverage_table(report)])
        return _plain(lines) if format == "plain" else "\n".join(lines)
    summary = _summary_data(report, results)
    coverage_rows = list(summary["coverage"])
    lines = ["## Universal Skill Finder", "", _summary_header(report, results, summary), ""]
    for fallback, result in enumerate(results, 1):
        number = _number(result, fallback)
        metric_value = _value(result, "metrics_text")
        if metric_value is None:
            metric_value = metrics_text(result)
        description = _text(_value(result, "description", ""), 400)
        if not description:
            description = "Description unavailable from source evidence."
        card = [
            f"### {number}. {_primary_skill_link(result)}",
            "",
            description + "  ",
            _compact_location(result) + "  ",
            f"**Found on:** {_attributions(result)}  ",
            f"**Signals:** {_canonical_compact_text(metric_value)}  ",
        ]
        resolved_note = _compact_resolved_target(result)
        if resolved_note:
            card.append(resolved_note + "  ")
        card.append(_compact_actions(result, number, assistant))
        local_label = installed_label(result)
        if local_label:
            card[-1] += "  "
            card.append(f"**Local:** {_text(local_label, 200)}")
        lines.extend(card)
        lines.append("")
    if not results:
        lines.extend([_empty_results_message(report, coverage_rows), ""])
    lines.extend(_coverage_table(report))
    lines.extend([""] + _notes(report, results) + [""] + _actions(report, results))
    lines.extend(_footer(report))
    return _plain(lines) if format == "plain" else "\n".join(lines)


_HTML_STYLE = """body{font:14px/1.45 system-ui,-apple-system,sans-serif;color:#18212b;background:#fff;margin:0 auto;padding:20px;max-width:960px;box-sizing:border-box}
h1{font-size:1.35rem;margin:0}h2{font-size:1.05rem;margin:24px 0 8px}
.summary,.muted{color:#53616d}.card{border:1px solid #d7dde3;border-radius:8px;padding:12px;margin:10px 0;overflow-wrap:anywhere}
.card-heading{display:flex;align-items:baseline;flex-wrap:wrap;gap:0 12px}.card h2{margin:0;font-size:1rem}
.repo{font:.9em ui-monospace,SFMono-Regular,monospace;color:#53616d;overflow-wrap:anywhere}
.facts{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:2px 8px;margin:7px 0}.facts dt{font-weight:600}.facts dd{margin:0;overflow-wrap:anywhere}
details{border-top:1px solid #e5e9ed;margin-top:6px;padding-top:5px}summary{cursor:pointer;font-weight:600}details p{margin:10px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f7f8;padding:10px;border-radius:5px;font-size:13px}
table{border-collapse:collapse;table-layout:fixed;width:100%;font-size:.85rem}th,td{border:1px solid #d7dde3;padding:6px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
footer{border-top:1px solid #d7dde3;margin-top:24px;padding-top:12px}a{color:#075b9c}a:focus-visible,summary:focus-visible{outline:2px solid #075b9c;outline-offset:3px}
@media(max-width:480px){body{padding:12px}.card{padding:10px}}
"""


def _html_text(value: object, limit: int = 1500) -> str:
    """Escape all report-originated text for the static HTML view."""
    return html.escape(clean_text(value, limit), quote=True)


def _html_existing_text(value: object) -> str:
    """Escape a canonical text leaf without rendering its existing entities twice."""
    # These leaves have already passed the canonical per-field bounds. Do not
    # truncate them again after combining source metrics or warning references.
    raw = str(value)
    raw = html.unescape(clean_text(raw, len(raw)))
    raw = re.sub(r"\\([\\`*_\[\]~])", r"\1", raw)
    return _html_text(raw, len(raw))


def _html_document(title: object, body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<title>Universal Skill Finder: " + _html_text(title, 400) + "</title><style>" + _HTML_STYLE
            + "</style></head><body>" + body + "</body></html>")


def _html_link(label: object, url: str | None) -> str:
    safe = safe_web_url(url)
    text = _html_text(label, 400)
    return f'<a href="{html.escape(safe, quote=True)}">{text}</a>' if safe else text


def _html_primary(result: object) -> str:
    proof = _proof_for(result, {"skill", "skill_destination", "listing", "bundle_listing"}, accepted=_ELIGIBLE)
    role = str(_value(proof, "role", "")).lower() if proof is not None else ""
    name = clean_text(_value(result, "name", "unnamed skill"), 160)
    if role == "bundle_listing":
        name += " (bundle)"
    return _html_link(name, _proof_url(proof, accepted=_ELIGIBLE) if proof else None)


def _html_attributions(result: object) -> str:
    explicit = _items(_value(result, "found_on", _value(result, "attributions", [])))
    if not explicit:
        explicit = _items(_value(result, "source_attributions", []))
    grouped: dict[str, list[object]] = {}
    for entry in explicit:
        source = str(_value(entry, "source_id", _value(entry, "id", "unknown")))
        grouped.setdefault(source, []).append(entry)
    for source in _items(_value(result, "source_ids", [])):
        source_id = str(source)
        grouped.setdefault(source_id, [{"source_id": source_id, "label": source_id,
                                        "role": "unavailable", "status": "not_checked",
                                        "reason": "listing not verified"}])
    rendered: list[str] = []
    for source, entries in sorted(grouped.items()):
        entries.sort(key=lambda entry: (_ROLE_PRIORITY.get(str(_value(entry, "role", "")).lower(), 99),
                                        str(_value(entry, "url", ""))))
        entry = entries[0]
        label = _value(entry, "label", source)
        role = str(_value(entry, "role", "")).replace("_", " ")
        url = _proof_url(entry, accepted=_CHECKED)
        parsed = urlparse(url) if url else None
        tessl_ok = source.casefold() != "tessl" or (parsed is not None and parsed.hostname in {"tessl.io", "www.tessl.io"})
        if url and role in {"listing", "bundle listing", "source page"} and tessl_ok:
            value = _html_link(label, url)
            suffix = _value(entry, "suffix") or ("bundle listing" if role == "bundle listing" else None)
            if suffix:
                value += " (" + _html_text(suffix, 120) + ")"
        else:
            reason = _value(entry, "reason", "repository source" if role == "repository" else "listing not verified")
            value = _html_text(label, 120) + " (" + _html_text(reason, 160) + ")"
        rendered.append(value)
    return " · ".join(rendered) if rendered else "Source unavailable"


def _html_metrics(result: object) -> str:
    value = _value(result, "metrics_text")
    return _html_existing_text(metrics_text(result) if value is None else value)


def _html_location(result: object) -> str:
    repository, path, ref = (_value(result, "repository"), _value(result, "skill_path"), _value(result, "ref"))
    entries = _items(_value(result, "location", _value(result, "location_links", [])))
    if entries:
        rendered = []
        for entry in entries:
            label = _value(entry, "label", _value(entry, "path", "location"))
            url = _proof_url(entry, accepted=_CHECKED)
            if url:
                rendered.append(_html_link(label, url))
            else:
                reason = _value(entry, "reason", _status(entry) or "not verified")
                rendered.append(_html_text(label, 400) + " (" + _html_text(reason, 160) + ")")
        value = " / ".join(rendered)
        return value + (" @ " + _html_text(ref, 400) if ref else "")
    proof = _proof_for(result, {"skill", "skill_destination", "repository"}, accepted=_CHECKED)
    url = _proof_url(proof, accepted=_CHECKED) if proof else None
    role = str(_value(proof, "role", "")).lower() if proof else ""
    parts = []
    if repository:
        parts.append(_html_link(repository, url if role == "repository" else None))
    if path:
        parts.append(_html_link(path, url if role in {"skill", "skill_destination"} else None))
    value = " / ".join(parts) if parts else "Location unavailable"
    return value + (" @ " + _html_text(ref, 400) if ref else "")


def _html_repository(result: object) -> str:
    return _html_text(_value(result, "repository") or "Repository unavailable", 200)


def _html_exact_target(result: object) -> str:
    target = _resolved_target(_value(result, "target_proof"))
    if target is None:
        return "Exact target not verified."
    repository, path, ref, name = target
    return " / ".join(_html_text(value, 400) for value in (repository, path)) + " @ " + _html_text(ref, 400) + " · " + _html_text(name, 160)


def _html_install(result: object, assistant: str | None) -> str:
    command, reason = _install_v2(result, assistant)
    host = ASSISTANTS.get(assistant, "current assistant")
    if command:
        value = f"<p><strong>Install ({_html_text(host, 80)}, current project)</strong></p><pre><code>{html.escape(command, quote=True)}</code></pre>"
    else:
        proof = _proof_for(result, {"skill", "skill_destination", "listing", "bundle_listing", "repository", "source_page"}, accepted=_CHECKED)
        url = _proof_url(proof, accepted=_CHECKED) if proof else None
        inspection = " " + _html_link("Inspect checked destination", url) if url else " Inspect an available source before installing."
        value = f"<p><strong>Install ({_html_text(host, 80)}, current project):</strong> Not ready: {_html_text(reason, 180)}.{inspection}</p>"
    local = installed_label(result)
    if local:
        value += "<p><strong>Local check:</strong> " + _html_text(local, 200) + "</p>"
    return value


def _html_coverage(report: object, *, links: bool = True) -> str:
    rows = _coverage_rows(report)
    body = []
    for item in rows:
        source = _value(item, "source_id", "unknown")
        proof = _value(item, "link_proof", _value(item, "public_link"))
        source_value = _html_link(source, _proof_url(proof, accepted=_CHECKED) if links and proof is not None else None)
        status = _coverage_status_v2(item)
        count = _value(item, "candidates_returned", _value(item, "result_count"))
        count_value = str(count) if type(count) is int and count >= 0 and "Failed" not in status and "Not searched" not in status else "-"
        shown = _value(item, "shown", 0)
        body.append("<tr><td>" + source_value + "</td><td>" + _html_existing_text(status) + "</td><td>" + count_value + "</td><td>" + (str(shown) if type(shown) is int and shown >= 0 else "0") + "</td><td>" + ("Enabled" if _value(item, "enabled", True) else "Disabled") + "</td></tr>")
    if not body:
        return "<p class=\"muted\">Source coverage unavailable.</p>"
    suffix = ("<p class=\"muted\">Shown counts unique results on this page per source and are not additive for merged results.</p>"
              if len(rows) > 1 else "")
    return "<table><thead><tr><th>Source</th><th>Search status</th><th>Candidates returned</th><th>Shown</th><th>Enabled</th></tr></thead><tbody>" + "".join(body) + "</tbody></table>" + suffix


def _html_notes(report: object, results: list[object]) -> str:
    items = [line[2:] for line in _notes(report, results)[2:] if line.startswith("- ")]
    return "<ul>" + "".join("<li>" + _html_existing_text(item) + "</li>" for item in items) + "</ul>"


def _html_actions(report: object, results: list[object]) -> str:
    items = [line.replace("**", "") for line in _actions(report, results)[2:] if line]
    return "<ul>" + "".join("<li>" + _html_existing_text(item) + "</li>" for item in items) + "</ul>"


def _html_footer(report: object) -> str:
    if str(_value(report, "mode", "online")) != "online" or not any(
        str(_value(item, "status", "")).lower() in COMPLETED for item in _coverage_rows(report)
    ):
        return ""
    proof = _value(report, "footer_link_proof")
    url = _proof_url(proof, accepted=_ELIGIBLE) if proof is not None else None
    action = _html_link("⭐ Star Universal Skill Finder on GitHub", url) if url else "⭐ Star Universal Skill Finder on GitHub (repository link not verified for this report)."
    return "<footer>" + action + "</footer>"


def _html_summary_header(report: object, results: list[object], summary: Mapping[str, object]) -> str:
    """Use the same compact facts as terminal output without interpreting Markdown."""
    def strong(value: object) -> str:
        return "<strong>" + _html_text(value, 400) + "</strong>"

    current = int(summary["current"])
    if _is_continuation_page(report):
        parts = [
            strong(_value(report, "query", "")), strong(_counted(int(summary['unique']), "saved skill")),
            strong(f"showing {current}"), strong("no new search"),
        ]
    else:
        completed = int(summary["searched"]) + int(summary["cached"])
        parts = [
            strong(_value(report, "query", "")),
            strong(f"{_counted(completed, 'source')} ({summary['searched']} searched, {summary['cached']} cached)"),
            strong(f"{summary['unique']} found"), strong(f"showing {current}"),
        ]
    if summary["partial"]:
        parts.append(strong("partial"))
    return '<p class="summary">' + " · ".join(parts) + "</p>"


def render_html_report(report: object, *, assistant: str | None = None, dry_run: bool = False) -> str:
    """Render a self-contained, no-script HTML view of a trusted schema-2 report."""
    if not _is_schema2(report):
        body = "<h1>Universal Skill Finder</h1><p>Legacy report cannot be rendered as verified HTML.</p>"
        return _html_document("Universal Skill Finder", body)
    query = _html_text(_value(report, "query", ""), 400)
    if dry_run:
        hosts = sorted({str(host) for item in _coverage_rows(report)
                        for host in _items(_value(item, "metadata_hosts", [])) if host})
        metadata = ("<p>A live refresh may also contact: " + ", ".join(_html_text(host, 200) for host in hosts)
                    + ". No search text is sent to these optional metadata endpoints.</p>" if hosts else "")
        body = ("<h1>Universal Skill Finder</h1><p>Destination preview. No sources were searched.</p>"
                + metadata + "<h2>Source coverage</h2>" + _html_coverage(report, links=False))
        return _html_document("Universal Skill Finder", body)
    mode = str(_value(report, "mode", "online"))
    if mode == "offline_preview":
        previews = "".join("<p>" + _html_existing_text(line.removeprefix("- ")) + "</p>"
                           for line in _offline_previews(report) if line)
        body = (f"<h1>Universal Skill Finder</h1><p>Search: {query}</p><p>Offline preview. 0 checked results.</p>"
                + previews + "<h2>Source coverage</h2>" + _html_coverage(report, links=False))
        return _html_document(_value(report, "query", ""), body)
    results = _items(_value(report, "results", []))
    summary = _summary_data(report, results)
    coverage = list(summary["coverage"])
    cards = []
    for fallback, result in enumerate(results, 1):
        number = _number(result, fallback)
        description = _value(result, "description", "") or "Description unavailable from source evidence."
        cards.append("<article class=\"card\"><header class=\"card-heading\"><h2>" + str(number) + ". " + _html_primary(result) + "</h2>"
                     + "<div class=\"repo\">" + _html_repository(result) + "</div></header>"
                     + "<dl class=\"facts\"><dt>Found on</dt><dd>" + _html_attributions(result) + "</dd><dt>Metrics</dt><dd>" + _html_metrics(result) + "</dd></dl>"
                     + "<details><summary>Description</summary><p>" + _html_text(description, 2000) + "</p></details>"
                     + "<details><summary>Exact location and installation command</summary><p><strong>Reported location:</strong> " + _html_location(result) + "</p><p><strong>Exact target:</strong> " + _html_exact_target(result) + "</p>" + _html_install(result, assistant) + "</details></article>")
    empty_message = ("<p class=\"muted\">" + _html_existing_text(_empty_results_message(report, coverage)) + "</p>"
                     if not results else "")
    body = ("<h1>Universal Skill Finder</h1>" + _html_summary_header(report, results, summary)
            + empty_message + "".join(cards) + "<h2>Source coverage</h2>" + _html_coverage(report)
            + "<h2>Notes</h2>" + _html_notes(report, results)
            + "<h2>Next actions</h2>" + _html_actions(report, results) + _html_footer(report))
    return _html_document(_value(report, "query", ""), body)


def _plain(lines: list[str]) -> str:
    """Plain-text view retaining the Markdown field order without markup or ANSI."""
    def replace_link(match: re.Match[str]) -> str:
        return f"{match.group(1)} ({match.group(2)})"

    text = "\n".join(lines)
    text = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", replace_link, text)
    text = re.sub(r"^###?\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "")
    text = re.sub(r"^```(?:text|sh)?\n?", "", text, flags=re.MULTILINE)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return (text.replace("✅", "[enabled]").replace("❌", "[disabled]").replace("…", "...")
            .replace("·", ";").replace("→", "->").replace("—", "-").replace("–", "-"))


def render_help(*, assistant: str | None = None, invocation: str = "unknown", format: str = "markdown") -> str:
    if format not in {"markdown", "plain"}:
        raise ValueError("format must be markdown or plain")
    prefix = {"plugin": "$skill:find" if assistant == "codex" else "/skill:find", "standalone": "$find" if assistant == "codex" else "/find", "cli": "skill-find"}.get(invocation, "Find a skill to")
    examples = ([f"{prefix} fill PDF forms", f"{prefix} humanizer", f"{prefix} React performance --count 3", f"{prefix} database migration --source tessl", f"{prefix} list sources"] if invocation != "unknown" else ["Find a skill to fill PDF forms", "Find a skill named humanizer", "Show 3 skills for React performance", "Find a database migration skill from Tessl", "List sources"])
    lines = ["## Universal Skill Finder", "", "Find, inspect and install verified skills.", "Search enabled sources for a task or skill name.", "", "Examples:", *[f"- {item}" for item in examples], "", "Default: up to 10 results, 10 per page. Choose an overall 1–100 with --count N or -n N; use --page-size N for the page size, or ask for “3 skills for PDF forms.”", "", "Inspect #N / Install #N after a search; Explain #N and Next page need its saved snapshot. Show more can extend the same snapshot up to 100.", "", "Pages are bounded. If a host truncates or reflows the output, relay the saved page artifact; host rendering is outside Universal Skill Finder's control."]
    return _plain(lines) if format == "plain" else "\n".join(lines)


def render_progress(event: object, *, format: str = "plain") -> str:
    if format not in {"plain", "markdown"}:
        raise ValueError("format must be plain or markdown")
    kind = str(_value(event, "type", ""))
    query = _text(_value(event, "query", ""), 160)
    completed, total = _value(event, "completed"), _value(event, "total")
    if kind == "search_started":
        return f'Searching {total} selected sources for "{query}"...' if type(total) is int else f'Searching selected sources for "{query}"...'
    if kind == "source_finished":
        source, status = _text(_value(event, "source_id", "source"), 120), _text(_value(event, "status", "finished"), 80)
        count = _value(event, "candidate_count")
        suffix = f"; {count} candidates" if type(count) is int else ""
        return f"[{completed}/{total}] {source}: {status}{suffix}"
    if kind == "ranking_started": return "Merging candidates and ranking unique skills..."
    if kind in {"validation_started", "page_started"}: return "Checking skill destinations for this page..."
    if kind in {"validation_progress", "validation_finished", "page_finished"}: return f"[{completed}/{total}] verified destinations" if type(completed) is int and type(total) is int else "Destination checks complete."
    if kind == "inventory_started": return "Checking installed skills..."
    if kind == "search_finished": return "Search complete."
    return ""


def render_preview(event: object, *, format: str = "plain") -> str:
    if str(_value(event, "type", "")) != "early_verified":
        return ""
    proof = _value(event, "link_proof", _value(event, "primary_destination"))
    url = _proof_url(proof, accepted=_ELIGIBLE) if proof is not None else None
    if not url:
        return ""
    name = _text(_value(event, "name", "verified skill"), 160)
    description = _text(_value(event, "description", ""), 180)
    if format == "markdown":
        return f"Early verified preview: {_link(name, url)}" + (f" · {description}" if description else "")
    return f"Early verified preview: {name} - {url}" + (f" · {description}" if description else "")


def render_explanation(snapshot: object, result: object, *, format: str = "markdown") -> str:
    if format not in {"markdown", "plain"}:
        raise ValueError("format must be markdown or plain")
    number = _number(result, 0)
    ranking = _value(result, "ranking", _value(result, "ranking_record"))
    validation = _value(result, "validation", _value(result, "target_proof"))
    lines = [f"## Explain #{number}", "", f"Skill: {_text(_value(result, 'name', 'unknown'), 160)}", f"Algorithm: {_text(_value(ranking, 'algorithm_version', _value(snapshot, 'ranking_algorithm_version', 'unavailable')), 120)}", f"Validation: {_text(_value(validation, 'status', 'unavailable'), 120)}"]
    components = _value(ranking, "components", {})
    if isinstance(components, dict) and components:
        lines.extend(["", "Evidence:"] + [f"- {cell(key, 80)}: {_text(value, 120)}" for key, value in sorted(components.items())])
    return _plain(lines) if format == "plain" else "\n".join(lines)
