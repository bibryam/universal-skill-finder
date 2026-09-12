# Result schema

Developer interface: use `search --json` for machine-readable output through the prerequisite launcher. End users invoke the plugin, not this internal command:

```bash
sh scripts/run.sh search "pdf forms" --json
```

By default, every enabled source is searched, a bounded accepted pool is frozen, and `results` contains the first page of up to 10 destination-verified findings. `--count N` sets the overall snapshot cap and `--page-size N` sets page size. The legacy `--max-results` controls the same overall cap. Explicit `--report-json PATH` saves ordering, evidence and numbering for `page` and `explain`; direct CLI searches do not persist reports automatically. `--json` and `--markdown` are mutually exclusive. See [SKILL.md](../SKILL.md).

For a saved report, `page --report SNAPSHOT --more` materializes the next verified page from the frozen pool without querying sources or extracting a cursor. It raises the cap by one page only when the current cap has been reached and candidates remain (maximum 100). `--extend-count N` sets an explicit larger overall cap. `page --report SNAPSHOT --cursor TOKEN` remains available for explicit continuation or replay; a consumed cursor is replay-safe and a forged, stale, or unrelated cursor is rejected. Do not combine `--cursor` and `--more`. Pool exhaustion requires a separate new or deeper search.

Page accepts `--progress auto|plain|off` and `--report-file FRESH_PATH`, like search. Progress goes to stderr; the complete rendered report goes to stdout and the optional new artifact. Existing artifacts are never overwritten. A page scans at most 30 frozen identities within its time/request budget; unscheduled candidates remain pending. Empty no-progress attempts do not become permanent exhausted pages. Older empty histories caused by missing immutable occurrence destinations can be resumed safely with `--more`.

Snapshots retain original source coverage, occurrence/duplicate counts, provenance, local inventory and search timings. Pages update current-page shown counts and proof partitions without presenting themselves as new searches. Legacy snapshots without saved coverage say that coverage is unavailable, not that zero sources were searched. Renderers accept both live records and their serialized mapping form without dropping metrics or inventory annotations.

Schema 2 deliberately changes old `--max-results` and programmatic `max_results` output: the value is an overall snapshot cap, while `results` is one page. Use `--page-size` (or `page_size`) for a larger first page or saved continuation for later pages. The legacy 1–500 argument range remains accepted, but new `--count` and Show more are capped at 100.

**Source** is the umbrella term; display types are **Registry**, **Repository**, **Local directory**, and **Search index**. The schema-2 report retains established names such as `source_id`, `source_ids`, `source_kind`, and `metrics_by_source`. Display types do not rename `source_kind`. `--repository` remains a compatibility alias for `--source`.

The top-level document contains:

| Field | Meaning |
|---|---|
| `schema_version` | Search output contract version, currently `2` |
| `report_format_version` | Human/paged report contract, currently `2` |
| `mode` | `online`, `offline_preview`, or `dry_run` |
| `provenance` | Release and contract versions plus code, catalogue, and effective-configuration revisions |
| `query` | Cleaned query sent to runnable sources |
| `generated_at` | UTC ISO 8601 generation time |
| `configuration_path` | User overlay used for the search |
| `results` | Ranked, merged results |
| `coverage` | One status record for every configured source |
| `installed_scan` | Local inventory scope/completeness, or why checking was skipped |
| `requested_count`, `page_size` | Overall requested cap and current page size |
| `accepted_occurrences`, `unique_count` | Frozen pre-merge and merged pool counts |
| `eligible_count`, `unavailable_count`, `inconclusive_count`, `not_checked_count` | Disjoint destination-validation partitions |
| `page_start`, `page_shown`, `materialized_total` | Current-page and cumulative numbering counts |
| `snapshot`, `continuation`, `show_more_available`, `show_more_cursor`, `can_explain` | Actual saved-state and follow-up availability; Show more is explicit and frozen-pool only |
| `timings` | Measured phase durations and selected deadline settings, not an SLA |
| `notes` | Bounded diagnostic summaries, including known cache-access and DNS failures; saved notes on continuation are labelled as original-search context |
| `continuation_page`, `coverage_context`, `pool_exhausted`, `has_pending` | Continuation rendering context; coverage context is `saved` or `unavailable` |
| `search_timings` | Original search phase measurements retained on continuation; page work is in `timings.page_ms` |

## Example

This example is illustrative, not a claim about a real registry or repository:

```json
{
  "schema_version": 2,
  "report_format_version": 2,
  "mode": "online",
  "query": "pdf forms",
  "generated_at": "2026-01-01T12:00:00+00:00",
  "configuration_path": "/home/user/.config/universal-skill-finder/sources.json",
  "results": [
    {
      "id": "skill:0123456789abcdef0123",
      "name": "PDF Forms",
      "description": "Work with fillable PDF forms.",
      "canonical_url": "https://github.com/example-org/example-skills/tree/main/skills/pdf-forms",
      "repository": "example-org/example-skills",
      "skill_path": "skills/pdf-forms",
      "ref": "main",
      "publisher": "example-org",
      "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "source_ids": ["example-catalog", "example-repository"],
      "trust": ["user-configured"],
      "text_match_percent": 100,
      "rank_fusion_score": 0.03252247,
      "result_number": 1,
      "validation_status": "eligible",
      "ranking": {
        "algorithm_version": "soft-native-v3",
        "score": 0.91,
        "components": {"lexical": 0.84, "skill_installs": 0.07}
      },
      "link_proofs": [
        {
          "role": "skill_destination",
          "url": "https://github.com/example-org/example-skills/tree/main/skills/pdf-forms",
          "status": "eligible",
          "identity_basis": "github-owner-repository-path-v1"
        },
        {
          "role": "repository",
          "url": "https://github.com/example-org/example-skills",
          "status": "eligible",
          "identity_basis": "github-owner-repository-v1"
        }
      ],
      "target_proof": {
        "kind": "github",
        "status": "eligible",
        "method": "anonymous_exact_skill_md_get",
        "identity_basis": "github-exact-skill-md-v1",
        "url": "https://raw.githubusercontent.com/example-org/example-skills/main/skills/pdf-forms/SKILL.md",
        "actual_name": "PDF Forms",
        "content_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "resolved": {
          "repository": "example-org/example-skills",
          "ref": "main",
          "skill_path": "skills/pdf-forms"
        }
      },
      "metrics_by_source": {
        "example-catalog": {"downloads": 42}
      },
      "install": {
        "kind": "github",
        "repository": "example-org/example-skills",
        "ref": "main",
        "skill_path": "skills/pdf-forms",
        "skill_installer": {
          "repository": "example-org/example-skills",
          "path": "skills/pdf-forms",
          "ref": "main"
        },
        "requires_approval": true
      },
      "warnings": [],
      "occurrences": []
    }
  ],
  "requested_count": 10,
  "page_size": 10,
  "eligible_count": 1,
  "unavailable_count": 0,
  "inconclusive_count": 0,
  "not_checked_count": 0,
  "page_start": 1,
  "page_shown": 1,
  "materialized_total": 1,
  "snapshot": {"status": "available_to_persist"},
  "continuation": {"available": false},
  "show_more_available": false,
  "show_more_cursor": null,
  "coverage": [
    {
      "source_id": "example-catalog",
      "enabled": true,
      "status": "ok",
      "result_count": 1,
      "elapsed_ms": 83,
      "detail": null,
      "cache_age_seconds": null,
      "host": "catalog.example.invalid",
      "target": "catalog.example.invalid",
      "requested_limit": 10,
      "effective_limit": 10,
      "source_total": 1,
      "total_relation": "exact",
      "shown": 1
    }
  ]
}
```

In real output, `occurrences` contains the complete normalized candidate records that formed the result.

The abbreviated example omits `provenance`. Real reports include release, code, catalogue, effective-configuration, schema, adapter-contract and cache-format revisions. Revisions identify installed content and effective configuration, not a Git commit or signature. Credential-variable names affect configuration identity, but environment values do not. Consumers must tolerate additive fields within schema version 2. Snapshot files have their own bounded schema and are not interchangeable with rendered report JSON.

## Result fields

| Field | Meaning |
|---|---|
| `id` | Deterministic hash of the best identity evidence available for this merge |
| `name`, `description` | Preferred occurrence's cleaned display text |
| `canonical_url` | Best identity URL reported by the source, if known; not automatically display-eligible |
| `repository` | Normalized GitHub `owner/repository`, if known |
| `skill_path` | Directory containing `SKILL.md`, if known |
| `ref` | Repository ref supplied by the source, if known |
| `publisher` | Registry publisher or repository owner, if known |
| `content_sha256` | Exact `SKILL.md` hash when read from a repository |
| `source_ids` | Sorted sources that reported this merged result |
| `trust` | Distinct source-declared provenance labels, strongest first |
| `text_match_percent` | Transparent query token/phrase match against preferred text and path |
| `rank_fusion_score` | Reciprocal rank fusion diagnostic retained for compatibility; not the selected ordering score |
| `metrics_by_source` | Original source metrics, kept separate by source ID; available count metrics appear in the human-facing table |
| `install` | Suggested handoff data; empty when no safe handoff is known |
| `warnings` | Distinct warnings raised by source adapters |
| `occurrences` | Every normalized source candidate retained for audit and comparison |
| `installed` | Fresh local installed-skill evidence; empty when not checked, never trusted from source/cache metadata |
| `ranking` | `soft-native-v3` version, score, components, tie-breaks and evidence actually used |
| `link_proofs` | Checked human-navigation evidence for GitHub directories, repository roots, and source listings; only an `eligible` or explicitly display-accepted proof for that exact destination authorizes its displayed link |
| `target_proof` | Reported versus resolved target identity and exact raw `SKILL.md` content/path evidence; this does not itself authorize a displayed link |
| `attributions` | Eligible, independently proof-gated native destinations; `source_ids` retains every contributor, including sources without a verified listing |
| `validation_status` | `eligible`, `unavailable`, `inconclusive`, or `not_checked` |
| `result_number` | Stable cumulative number assigned only when materialized |

`trust` is a provenance label, not a safety verdict. `text_match_percent` and `rank_fusion_score` remain diagnostics. Neither determines schema-2 ordering.

The `Metrics` column shows nonnegative `github_stars`, `stars`, `installs`, `downloads`, `bookmarks`, and `votes` with source IDs. Zero is valid; missing/invalid counts display as unavailable. Counts are never summed across sources. Unspecified scores, AI scores, and security grades are not presented as popularity or safety measures.

For an occurrence from the `tessl` adapter, the same column may show separately labelled `tessl_quality` (a raw normalized value from 0 to 1, rounded for display), `tessl_security_level` (`NONE`, `LOW`, `MEDIUM`, `HIGH` or `CRITICAL`), and `tessl_scored_at`. These are Tessl's version-specific assessments, not popularity counts or this finder's security verdict. Zero quality is preserved; missing or unsupported values are omitted rather than converted. `NONE` is not a safety guarantee. Deprecated `scores.security` is never displayed. `tessl_score_version` and other supported source-score fields remain in JSON; they are not Git refs, fingerprints or federation ranking inputs.

Tessl skill-containing packages may appear as explicitly labelled bundle candidates with a package listing URL and inspection warning. Their package name/version is not an exact skill name/ref, and they have no generated install command. `tessl_metric_scope` distinguishes `skill` from `bundle`; package assessments stay in JSON and are not displayed as individual-skill quality scores. `tessl_bundle_version` identifies the inspected package version. Individual GitHub skill rows retain their repository/path for deduplication and a separate connector-reviewed Tessl `/registry/skills/github/OWNER/REPOSITORY/SKILL` listing candidate. That source listing remains unlinked until its exact page confirms the encoded repository and skill name; unresolved GitHub refs still require inspection.

Configured GitHub repository sources can use separately cached metadata containing `github_stars`, `github_stars_scope: "repository"`, `github_stars_repository`, and `github_stars_observed_at`. Discovery never initiates or waits for a stars request; the current implementation only reads a fresh existing metadata entry. Stars describe the repository, not each skill, and never affect `soft-native-v3` ranking.

## Installed-skill evidence

For a known assistant, the CLI annotates merged results after ranking unless `--no-installed-check` is set. This is not a discovery source, installation action or full host inventory. Source/cache `installed` claims are ignored. Annotations never suppress installation proposals or affect ranking.

| `installed.status` | Evidence and limitation |
|---|---|
| `exact_local` | A validated local-directory result is the same directory found in a checked installation root |
| `matching_instructions` | Identical `SKILL.md` SHA-256 bytes found locally; companion files, source identity and activation are not verified |
| `name_collision` | Same name with different instruction bytes; do not overwrite based on name |
| `not_found` | No match within the checked roots; not proof of absence across the host |
| `unknown` | Same name without comparable hashes, or the scan was incomplete |

`evidence` contains fixed machine-readable tokens and `scopes` contains safe scope labels, not absolute paths. A `matching_instructions` result may also report `same_name_different_instructions` and `collision_scopes` when another scope contains a conflicting version; this does not resolve host precedence. A name-only unknown uses `name_only` evidence.

`installed_scan.status` is `complete`, `partial`, or `not_checked`. Checked reports contain `assistant`, `scopes`, per-root scope/status/count records, `skills_read` and fixed `limitations`. Completeness describes only the bounded checked roots, not every skill visible to the assistant. Skipped reports give a `reason` such as `preview`, `opted_out` or `assistant_unknown`. Parent projects, admin directories and plugin caches are outside scope; symlinks and special files are not followed. Unknown assistants and previews perform no installed-skill I/O.

## Candidate occurrence fields

An occurrence may contain `native_id`, `name`, `description`, `source_id`, `source_kind`, `adapter`, `native_rank`, `canonical_url`, `repository`, `skill_path`, `ref`, `slug`, `publisher`, `identity_namespace`, `updated_at`, `content_sha256`, `trust`, `metrics`, `tags`, `install`, `warnings`, `listing_url`, `listing_role`, `listing_derivation`, `source_evidence`, `metric_observations`, `target_proof`, and `link_proofs`.

Source-native values are deliberately preserved. Consumers should use the merged top-level fields for display and inspect `occurrences` when provenance, conflicting metadata, or a registry-specific install route matters.

## Identity rules

The finder never merges on name alone. It constructs strong aliases in this order of evidence:

1. GitHub repository plus skill path.
2. GitHub repository plus slug when no path is known.
3. Hosted namespace plus publisher plus slug.
4. Normalized canonical URL.
5. Source ID plus native ID as fallback.

Content hashes are evidence, not identity keys: identical instructions can accompany different executable files. A pathless repository occurrence joins a path-bearing result only when repository plus slug identifies one path group. This avoids collapsing different skills that share a name or slug. Repository refs remain occurrence metadata; results can group versions of the same repository/path, so inspect the selected ref before installation.

URL identities retain query, fragment, and semicolon parameters. Query order is not rewritten; different application routes must not silently collapse into one skill.

`id` is stable for the identity evidence in that response. It is not a permanent registry identifier: it may change when a later source provides stronger repository/path evidence.

## Ranking rules

`soft-native-v3` is the selected algorithm. It combines bounded lexical evidence from name, description and path with a bounded contribution only from typed, skill-scoped skills.sh install observations. Separator-only compounds such as `anti-slop` and `antislop` are equivalent. At least one compatible query term must be present before a result can enter the public pool. A query term family contributes once. Provider selection alone, destination validity, unknown native order, repository stars, generic popularity, security grades and source overlap contribute zero. Deterministic title/description corroboration, name and stable identity settle ties. `ranking.components`, `ranking.tie_breaks`, and `ranking.evidence` record the actual decision inputs. RRF remains a compatibility diagnostic only.

## Coverage records

| Field | Meaning |
|---|---|
| `source_id` | Configured source instance |
| `enabled` | Effective configured enablement, independent of query selection or source availability |
| `status` | Completion, selection, cache, or failure state |
| `incomplete_results` | Boolean, independent of `status`: upstream search or file validation was incomplete; default false for older compatible reports |
| `result_count` | Candidates admitted from this source after its code-owned relevance policy |
| `elapsed_ms` | Source work time |
| `detail` | Bounded diagnostic text, if any |
| `cache_age_seconds` | Cache age for cached results, if known |
| `host` | Network host or `github.com` for a repository source |
| `target` | Human-readable destination: registry host, GitHub repository, or local path |
| `public_url` | Configuration-derived public registry origin or GitHub repository URL; strips registry API paths, queries, and fragments; absent for local directories |
| `metadata_hosts` | Secondary metadata destinations that this operation may contact; empty for repository discovery because it performs no foreground metadata request |
| `requested_limit`, `effective_limit` | Requested and provider-effective candidate depths |
| `source_total`, `total_relation` | Provider-reported total plus `exact`, `lower_bound`, or `unknown`; unknown is null |
| `admission_status`, `live_status`, `cache_status`, `health_status` | Scheduling, request, cache and health state kept separate |
| `shown` | Unique results on this page attributed to the source; not additive across sources |

Common statuses include:

- Success: `ok`, `cached`
- Selection: `disabled`, `not_selected`, `excluded`, `planned`
- Availability: `offline_miss`, `auth_missing`, `auth_failed`, `rate_limited`, `timeout`
- Input/protocol: `schema_mismatch`, `archive_limit`, `not_found`
- Fallback: `failed`

Use `enabled` to show all enabled sources, including failures and zero-match completions. A selected enabled source that fails makes coverage partial; intentionally disabled, excluded, or unselected sources do not imply a failure. `ok` and `cached` may carry `incomplete_results: true`: retain verified candidates but render an explicit partial status and diagnostic. The flag survives query caching even for zero candidates; strict mode returns failure. An empty partial response must not be presented as a fully completed no-match search.

## Source configuration view

`sources list --json` returns a separate allowlisted configuration view. Its `sources` rows retain `id`, `kind`, `public_url`, `enabled`, `direct_enabled`, `pack_id`, `pack_enabled`, `credentials`, and `suggested_request`. The additive `type` field uses the display labels `Registry`, `Repository`, `Local directory`, and `Search index`; `kind` retains its existing values. Credential records contain only the environment-variable name, whether it is required, and whether it is present. Presence does not verify authentication.

The Markdown view uses `Source | Type | Requirements | Ask to change | Enabled` for every source. `enabled` is the effective configuration state after source and pack settings, separate from missing credentials or live availability. These views send no search requests and never rewrite configuration.

## Installation handoff

Search is read-only. A non-empty `install` object is a proposal for a separate action and includes `requires_approval: true` after federation.

Common handoffs are:

- `kind: "github"`: repository, ref, optional skill path, and structured `skill_installer` arguments. A missing path requires investigation before installation.
- `kind: "local"`: a local skill directory path.
- Registry-specific kinds: a structured reference when the registry does not expose a GitHub location.

Consumers must:

1. Show the user the selected source, repository/path or registry reference, ref, warnings, and proposed action.
2. Inspect third-party content when practical.
3. Obtain explicit approval immediately before installation.
4. Use an installer verified for the current assistant. A registry-specific reference alone does not establish Codex or Claude Code compatibility.
5. Treat handoff data as untrusted. Resolve mutable refs to immutable commits and review the actual skill and companion files before installing when possible. The finder does not verify that code is safe.

### Command availability and selected proposals

The default Markdown/plain presenter reports `Inspect and install: type **Inspect #N**  **Install #N**` only for compatible, fully specified targets with eligible target and destination proof. It offers those agent actions instead of repeating a long command in every result. Otherwise the action line gives the exact unavailability reason and a checked inspection destination when one exists. Missing safe links are reported, never guessed. After the user selects a result, the reviewed proposal has this form:

```text
npx skills@1.5.23 add https://github.com/OWNER/REPO/tree/REF/PATH --skill NAME --agent codex --copy
```

Commands require fresh anonymous exact `SKILL.md` GET proof (`kind: github`, `method: anonymous_exact_skill_md_get`, `identity_basis: github-exact-skill-md-v1`), its actual frontmatter name, content hash and resolved repository/path/ref. Remote rows, query caches, portable snapshots, and archive-only proof cannot declare themselves install-ready. A checked listing alone supports inspection, not a command. The raw URL remains internal target evidence. Report-derived human-facing links require a separate eligible proof for the GitHub tree directory, repository root, or native source listing; renderers never expose raw-content or GitHub blob-file URLs as the default destination. The fixed project footer URL is application-owned and outside this report-proof boundary.

Resolution checks a reported exact path first. An unpinned missing/stale ref can use the authoritative default branch, and a pathless result gets only a bounded root check. Explicit configured refs are not silently repaired. Reported identity stays separate from resolved command fields, and any change is disclosed. Ambiguity remains inspection-only; resolution never crawls repositories or installs anything.

Claude Code uses `--agent claude-code`. The exact directory and name filter prevent broad repository installation. Repository/path/ref fields must match the reconstructed handoff, and shell arguments use a narrow ASCII whitelist. Missing or ambiguous targets, display names needing verification, unknown hosts, unsafe locations, and unsupported refs (`HEAD`, raw commit SHAs, or refs containing `/`) produce an inspection reason instead. Remote `command` fields are never used.

The pinned CLI's [parser](https://github.com/vercel-labs/skills/blob/v1.5.23/src/source-parser.ts) splits tree URLs at the first ref segment, and [cloning](https://github.com/vercel-labs/skills/blob/v1.5.23/src/git.ts) uses `--branch`. Do not repair an unsupported ref into a different target. Its [skill selection](https://github.com/vercel-labs/skills/blob/v1.5.23/src/skills.ts) matches the exact frontmatter name; never guess it from a display title.

Execution is a separate user-approved action. Check Node.js 22.20.0+, npx, and Git before invoking this external installer; these are not search prerequisites. The [package requirements](https://github.com/vercel-labs/skills/blob/v1.5.23/package.json) are version-specific. The CLI can [automatically skip prompts inside agents](https://github.com/vercel-labs/skills/blob/v1.5.23/src/add.ts), so approval must happen before execution. `--copy` [copies to the selected assistant's directory](https://github.com/vercel-labs/skills/blob/v1.5.23/src/installer.ts); npm caches and a project lockfile are separate side effects. Do not add `--yes`, `--all`, `--full-depth`, or global scope to a displayed proposal.
