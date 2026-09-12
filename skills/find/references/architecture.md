# Architecture

## Position

The system should federate discovery, not centralize ownership. It searches configured sources through registry APIs, GitHub archives, local directories, and GitHub code search, then returns a traceable merged view. It does not mirror registries, execute third-party adapters, or combine unrelated popularity numbers into a false universal score.

**Source** covers four types: **Registry** for a hosted skill index, **Repository** for Git files containing skills, **Local directory** for files on disk, and **Search index** for GitHub code search. A **catalogue** is a combined list or index; a **connector** is the code that searches a source. The `source_*` contract fields and source-pack definitions retain these names.

## Integration approach

The engine talks to each configured source through a reviewed connector instead
of delegating discovery to third-party finder tools. Source definitions are
declarative, connector code is allowlisted, and installation handoffs are
structured separately from search. The presentation layer can build a
display-only command for a verified target, but the search engine never runs
that command or parses another tool's console output.

```text
bundled defaults + sources.json
             |
             v
 packs -> source instances -> reviewed adapters
                              |       |
                         registries  repository archives/local trees
                              \       /
                         normalized candidates
                                  |
                   identity merge + relevance-first ranking
                                  |
                    results + coverage + handoff
                                  |
             verified cards + coverage + host-specific proposals
```

## Three extension units

| Unit | Owns | Typical change | Review level |
|---|---|---|---|
| Connector / adapter | Transport, authentication pattern, pagination, and response mapping | Support a new protocol or incompatible API | Code change and tests |
| Source | One endpoint, repository, or local tree plus adapter options | Add `owner/repository` or a compatible endpoint | Configuration only |
| Pack | A named group of sources | Import, enable, disable, or remove a curated collection | Review JSON destinations and mappings |

Adapters are code-owned. Source configuration cannot name a Python module, shell command, executable, or downloaded plugin. This is deliberate. An arbitrary executable adapter would turn a search query into remote code execution authority.

`adapters/__init__.py` contains one immutable `ADAPTER_SPECS` catalogue. Each typed descriptor declares its factory, source kind, required fields, cache policy, and contract version. Configuration validation, source-pack kind inference, runtime construction, and federation cache routing use it. Add one registration for a new protocol; do not maintain parallel allowlists.

The `Adapter.search(source, query, limit, context) -> list[Candidate]` contract returns at most the requested limit in native order or raises `SourceUnavailable` for a recognized failure. Instances are shared across concurrent source calls and must be stateless. The context supplies guarded transport, cache, settings, and offline/refresh flags. Query caching belongs to federation, catalogue caching to repository connectors, and local reads are uncached. Federation validates candidates and rebinds source authority.

## Adapter families

The implementation has four useful families:

1. Named registry adapters handle known API contracts and their specific authentication or response details.
2. `github-repo` and `local-directory` find `SKILL.md` files using include/exclude patterns, then search their parsed name, description, and path.
3. `http-json-v1` supports constrained HTTPS GET APIs with dotted-field mappings and environment-backed credential headers.
4. `github-code-search` searches public indexed `SKILL.md` files through a fixed authenticated GitHub API boundary, then validates bounded blob contents without downloading whole repositories.

Use an existing family when its contract fits exactly. Add a reviewed adapter when a source needs OAuth, request signing, POST semantics, custom pagination, HTML parsing, non-JSON data, or a response that cannot be expressed safely by `http-json-v1`.

`tessl` is a named registry adapter for the public JSON:API hybrid-search contract, not a wrapper around the Tessl CLI or MCP server. It requests public skill-bearing index entries in one bounded anonymous GET, maps validated individual GitHub skills and inspection-only skill bundles separately, and preserves incomplete coverage. For an individual GitHub skill, the adapter retains the GitHub repository/path as canonical identity and separately constructs Tessl's reviewed `/registry/skills/github/OWNER/REPOSITORY/SKILL` source-listing route from strict repository and skill-name fields. Construction does not activate the link: the destination validator must confirm the exact Tessl page's canonical URL, repository, and skill name first. It does not follow remote pagination links or infer Git revisions from score versions. Scores are source-labelled assessments outside ranking; missing source identity and unsupported providers cannot silently merge unrelated skills.

## Upstream finder integration

An upstream finder such as `skill-fetch` can be valuable as a maintained inventory of repositories and registries. The stable integration boundary is its source definitions, not its runtime:

1. Extract candidate source definitions.
2. Verify ownership, endpoint, protocol, and current response shape.
3. Map each candidate to an existing adapter or write and test a new adapter.
4. Publish the reviewed definitions as a source pack or bundled defaults.
5. Track provenance in the source definition.

The finder does not invoke upstream search code, scrape its console output, or inherit its install behavior. Executing another finder would make coverage opaque, duplicate caching and ranking policy, widen the supply-chain boundary, and make per-source enable/disable controls unreliable.

There is intentionally no automatic remote pack import. `packs import` reads a local JSON file so the user can inspect destinations and credential references before changing the overlay.

## Source choices

Ordinary enable/disable choices use one configuration file, defaulting to `~/.config/universal-skill-finder/sources.json` for a new installation and honoring `$XDG_CONFIG_HOME`. It contains a `sources` list of known `id` / boolean `enabled` pairs; omitted IDs retain catalogue defaults. It does not expose endpoint or credential editing. Duplicate/unknown IDs, extra entry fields, and invalid boolean values fail validation.

`sources config-path` is read-only. `sources init` explicitly materializes all known entries with existing choices. `sources enable|disable ID` writes the same list. Searches and lists never initialize or rewrite it. The older `repositories` command and `--repository` flag remain aliases for `sources` and `--source`; result schema-1 fields retain their existing names.

User choices live in `universal-skill-finder/sources.json` unless an explicit path or `UNIVERSAL_SKILL_FINDER_CONFIG` is supplied. A file must contain only one choice format. Explicit saves normalize choices to `sources` while retaining advanced `custom_sources`, `custom_packs`, and `pack_overrides`. A disabled pack still requires explicit pack enablement. Public search reports are schema 2; source candidate, cache, and adapter contracts remain version 1 with additive evidence fields. See [configuration](configuration.md#files-and-precedence) for exact precedence.

## Federation pipeline

Before this pipeline, the plugin's POSIX or PowerShell launcher checks Python 3.10+ and working SSL. Missing, old, or broken runtimes stop with exit 4 before engine imports, configuration/cache access, or network requests. The plugin manager itself may install files without this check. No global hooks or automatic dependency installation are used.

1. Merge immutable bundled defaults with the user overlay.
2. Resolve direct and pack-level enablement.
3. Select or exclude sources requested on the command line.
4. In `--dry-run`, report planned hosts without sending the query.
5. Otherwise, admit runnable sources fairly by source class and execute them under the shared process supervisor, deadline, request/byte budget, per-origin permit, and API quota contracts. Supported process-isolation paths can terminate and reap blocked children; deterministic injected test adapters remain in-process.
6. Isolate every source failure. Freeze the accepted pool at collection cutoff; late or preempted work remains explicit coverage rather than evidence that the source is down.
7. Normalize source results into `Candidate` records and merge strong identities, then narrowly reconcile pathless repository matches.
8. Rank the frozen merged pool with `soft-native-v2`, retaining component evidence and deterministic tie-breaks.
9. Validate exact public destinations under the remaining shared proof budget. Resolve exact GitHub `SKILL.md` content for target identity and install evidence, then validate the corresponding browsable GitHub directory and repository root as separate destinations under their reviewed GitHub contracts. Validate contributing native registry listings separately so successful target proof does not discard provenance links. Use authoritative branch/root checks only when needed; otherwise retain a reviewed inspection destination. Replenish failed identities from the same frozen rank order, checking at most 30 identities per page.
10. Return schema-2 results, validation partitions, progress/provenance, and ordered source coverage. A saved snapshot can continue or explicitly extend its cap without rerunning discovery.
11. For a known assistant, annotate the complete frozen result pool from one bounded local installed-skill inventory unless explicitly skipped. These local observations never enter ranking, source candidates or query caches.

Registry query responses use a short cache keyed by source, exact query, and requested limit. `cache list` exposes available query metadata for offline use. Repository catalogues use a longer cache because indexing a repository archive costs more than filtering an existing catalogue; a cached catalogue can answer new queries locally. `--offline` uses cached registry queries, cached repository catalogues, and local directories only. A cache miss is reported, not bypassed with a network request.

Cross-process cache leases return boolean-compatible typed outcomes: acquired, busy, permission denied or storage unavailable. Failed acquisition never grants ownership or bypasses source cooldowns. Local access/storage errors are distinct from lock contention, including in cached fallback coverage and proof diagnostics. Known DNS and cache-access failures are summarized in report notes without copying arbitrary exception text. A sandboxed host must request access through its normal permission mechanism for the configured cache and public network; the engine does not silently relocate state or relax verification.

## Identity and deduplication

Names are not identities. Two publishers can legitimately ship skills with the same name.

Strong aliases are derived from the best evidence available:

- normalized GitHub repository plus skill path;
- repository plus slug when no path is known;
- hosted registry namespace plus publisher plus slug;
- normalized canonical URL;
- source ID plus native ID as a final fallback.

A pathless repository occurrence may join a path-bearing result only when its repository-and-slug points to one unambiguous path group. It is not used to merge two different paths. Every original occurrence remains in the output for auditability.

Canonical URL identities retain query, fragment, and semicolon parameters because they can distinguish different skills or SPA routes. Query order is preserved; conservative separation is preferable to merging unrelated install targets.

## Ranking

`soft-native-v2` combines bounded lexical evidence from the title, description, and path with a bounded contribution only from typed, skill-scoped skills.sh install observations. A query term family contributes once. Unknown native order, repository stars, generic popularity, security grades, and source overlap contribute zero. Deterministic title/description corroboration, name, and stable identity settle ties. The report retains algorithm version, component scores, tie-breaks, and the evidence actually used.

Reciprocal rank fusion remains a schema compatibility diagnostic, not an ordering input. This avoids treating correlated registries as independent votes or normalizing incompatible metrics into a universal score. Lexical evidence is still not semantic confidence; destination proof establishes reachability and identity, not capability quality or safety.

## Coverage is part of the answer

Every configured source gets one coverage record. Common states include `ok`, `cached`, `disabled`, `not_selected`, `excluded`, `planned`, `offline_miss`, `auth_missing`, `auth_failed`, `rate_limited`, `timeout`, `schema_mismatch`, `archive_limit`, and `failed`.

`AdapterContext.incomplete_results` and `detail` let a connector disclose incomplete upstream searches, skipped validations or exhausted bounds without discarding verified candidates. Federation persists those fields through query caching, including zero-result responses; `Coverage.incomplete_results` is independent of `ok`/`cached`. The presenter labels partial coverage explicitly, and strict mode fails. Legacy compatible cache payloads without these optional fields default to complete.

GitHub code search has one fixed API origin and explicitly named search credential, public-only retained candidates, bounded file/byte/request/time budgets, verified Git blob hashes and required name/description frontmatter. The legacy API has no documented public-only query filter, so setup calls for a token without private-repository access; broader tokens may return private metadata that is discarded. Public blob requests omit credentials to enforce public accessibility, at the cost of lower anonymous quotas. Raw content proof, the browsable GitHub skill directory, and the repository root remain separate destinations with separate roles. Missing commit evidence never becomes a guessed branch. Optional repository metrics are cached-only and never delay this connector. It is opt-in, not a replacement for known-repository catalogue scanning.

Therefore “no results” means only “no matches in the sources that completed.” It never proves ecosystem-wide absence.

The plugin uses `search --markdown --assistant codex` or `--assistant claude-code` internally through the launcher. `presentation.py` renders deterministic scan-first numbered cards directly in the terminal/conversation: skill name, up to 400 characters of description, a combined repository/folder Location, Found on provenance, source-labelled Signals, and numbered actions. `main` is omitted while non-default refs and reported/resolved target differences remain visible. A checked GitHub tree directory is the primary browse destination, the repository label requires its own checked root proof, and each Found on label uses that source's independently checked native listing. Raw content and GitHub blob-file URLs are verification evidence, never default human navigation. Missing fields fall back to a checked listing or repository role when available, then to explicit unavailable text; the renderer never invents a location. Every coverage record follows, distinguishing configured enablement from live success, cached data, failure, and intentional skipping. JSON remains the separate structured-data contract.

Only an explicit HTML-report request should use `search --html` or `page --html`. This optional export has native disclosure controls and the same proof gates for report-derived links, without JavaScript or external assets. Its fixed project footer is the same application-owned exception as in Markdown. It is not the default skill presentation; browser availability is not a reason to select it. Compact-output requests use Markdown/plain output and do not launch a browser.

Reports lead with a short summary block separating query, result counts and searched/cached source counts; detailed candidate, deduplication, validation, pagination and coverage counts live in Notes and source coverage below the cards. Markdown keeps bold summary/field labels. `terminal.py` adds fixed ANSI color/bold only after plain report rendering, at the interactive CLI stdout boundary. The renderer stays deterministic and ANSI-free; saved artifacts, pipes, Markdown and JSON are never styled. `NO_COLOR` and unsupported/dumb terminals disable styling. Default Markdown/plain cards say `Inspect and install: type Inspect #N  Install #N` only when the checked target and assistant support an exact proposal; they do not repeat the command. Styling never activates additional links or implies a safety verdict.

Completed online reports end with the single-line canonical project link `[https://github.com/bibryam/universal-skill-finder](https://github.com/bibryam/universal-skill-finder)`, without ASCII art or a verification caveat. The application owns this fixed URL, so the footer needs no report proof or network work. It is absent from previews, offline output, help, source management and all-source failures.

`source_presentation.py` provides a read-only linked source table for `sources list --markdown`, with `Source | Type | Requirements | Ask to change | Enabled` columns and an allowlisted JSON view. Public links are derived from configuration, never guessed from an ambiguous target string or copied from credential-bearing API URLs. Explicit source toggles persist in the overlay; disabled packs remain a separate authority boundary.

`sources explain` uses the same allowlisted metadata, never raw API routes, headers, or local-directory paths. `doctor` checks required named-adapter and generic-header credential references without printing values. Setup diagnostics are not live authentication checks.

Installation-command fallbacks contain a repository or listing link and reason. The result table also shows available count metrics with provenance. Repository stars are used only from a fresh, separately scoped cache entry whose repository identity, value, and observation time validate. Search never fetches or waits for them, and they never affect ranking.

Display-only installation commands use the pinned Skills CLI interface with an exact GitHub directory, explicit branch/tag, exact skill name, `--agent`, and `--copy`. No executable hints from registries or caches are accepted. Source inspection and explicit approval precede any separate execution by the coding assistant. The external CLI has additional runtime requirements and can skip its own prompts inside agents; see the [installation handoff](result-schema.md#installation-handoff).

## Security boundaries

- Disabled, excluded, unselected, and offline cache-miss sources receive no query.
- Network definitions require HTTPS, except an explicitly allowed local development endpoint.
- The generic adapter is GET-only and declarative.
- Secret values come from named environment variables, never pack files.
- Repository archives have compressed size, member count, actual gzip expansion (including extended tar headers), declared uncompressed size, skill count, and per-file limits.
- Archive entries are read in memory. Hooks, scripts, packages, and discovered skill instructions are never executed.
- Registry descriptions, repository contents, metrics, URLs, and structured installation hints are untrusted data. Markdown text and links are escaped; display-only commands are constructed locally from a narrow argument whitelist, never copied from source metadata.
- Installation is a separate, explicit-approval action.
- Anonymous production validation bounds DNS and the complete HTTPS exchange, including trickling headers/body, with killable child processes. Executor joins are deadline-bounded too; arbitrary injected in-process transports must honor their timeout and remain responsible for their own I/O. They cannot publish result or proof-cache state after the deadline.
- Cache writes check their publication guard immediately before atomic replacement, as well as before serialization. A source or proof that completes late cannot publish a late cache entry.

## Deliberate limits

- No exhaustive GitHub crawl. Optional GitHub code search is bounded and limited by GitHub's public-file index; the repository adapter still scans only configured repositories.
- No complete installed-plugin inventory or automatic replacement. Local annotation checks only standard current-project/user roots and distinguishes instruction matches from name collisions.
- No live remote adapter or pack loading.
- No HTML scraping fallback.
- No universal trust or popularity score.
- No automatic installation or update.

These limits keep discovery predictable. Add breadth through reviewed source packs; add protocol capability through reviewed adapters.

## Version and revision contract

`versioning.py` is authoritative for the release version, public JSON schema, adapter contract, and cache format. Search reports include code, catalogue, and effective-configuration SHA-256 revisions. `doctor` prints the same versions and revisions. Code identity covers engine files and the packaged instructions, launchers, references, and example packs; catalogue identity uses canonical JSON. Effective configuration hashes source order/settings/state and credential-variable names, never environment credential values or the overlay location.

Copied skill payloads retain identical revisions. A wheel without adjacent skill instructions reports the narrower `engine` scope. These are content identifiers, not Git commits, signatures, or security attestations. Compatible caches can survive code changes; incompatible cache/adapter versions and legacy envelopes are treated as misses without deletion or online fallback.

Configuration writes use an exclusive adjacent lock and compare the originally loaded bytes before atomic replacement. Stale writers fail and must reload, preserving the other session's changes. An interrupted writer may leave a lock requiring inspection. External editors that ignore the lock can still race a write.
