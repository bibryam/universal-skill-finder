# Configuration

This is an internal reference for the coding assistant and maintainers. End users can configure the installed plugin through plain requests or one editable JSON file. Use **source** for a configured place to search. A registry indexes or publishes skills, a Git repository stores skill files, a search index finds files across repositories, and a local directory stores files on the user’s machine. A catalogue is a collected list; a connector is the implementation that reads a source. All source types share the same enabled flag and management table. Resolve `<skill-root>` relative to `SKILL.md` and use the prerequisite launcher below; on Windows use the PowerShell launcher documented in [SKILL.md](../SKILL.md).

## One file for source choices

Ask **Create source config** to create a complete list of known sources with their current choices. The default file is `~/.config/universal-skill-finder/sources.json`; **Show source config** reports the selected path without creating or changing it. Edit only each `enabled` flag, or ask **Enable tessl** / **Disable skillsmp** to save the same change for you.

```json
{
  "sources": [
    {"id": "skills-sh", "enabled": true},
    {"id": "skillsmp", "enabled": false},
    {"id": "tessl", "enabled": true}
  ]
}
```

This short example overrides three choices; omitted IDs keep their catalogue defaults. Each entry accepts only a known `id` and a boolean `enabled`. Duplicate or unknown IDs, extra entry fields, and non-boolean values fail validation. Endpoints and credentials do not belong in this list. Listing and searching never create or rewrite the file.

Internally, `sources init` materializes the complete list without resetting existing choices. Explicit saves update this list and retain advanced pack and custom-source settings. A disabled pack remains a blocker until the user explicitly enables that pack.

## Files and precedence

`scripts/universal_skill_finder/data/sources.default.json`, relative to the skill directory, is the single immutable bundled catalogue. User changes are stored in a JSON overlay. The overlay path is selected in this order:

1. `--config /path/to/sources.json`
2. `UNIVERSAL_SKILL_FINDER_CONFIG`
3. `universal-skill-finder/sources.json` beneath `$XDG_CONFIG_HOME`, or `~/.config` when unset

Cache selection follows the same pattern: `--cache-dir`, then `UNIVERSAL_SKILL_FINDER_CACHE`, then the `universal-skill-finder` directory beneath `$XDG_CACHE_HOME` or `~/.cache`.

A rejected, unreadable, or symlinked path fails visibly. Ask **Show source config**, or use `sources config-path`, to inspect the selected location.

Global options must precede the command:

```bash
sh "<skill-root>/scripts/run.sh" --config ./test-sources.json sources list
sh "<skill-root>/scripts/run.sh" --cache-dir ./cache search "task"
```

The finder creates overlay directories with restrictive permissions and writes the overlay atomically.

An exclusive adjacent lock serializes finder writers, and an exact loaded-file fingerprint prevents stale sessions from replacing newer choices. On a concurrent-edit error, reload the list and retry the requested change. An interrupted process may leave `.sources.json.lock` (or the equivalent for a custom filename); inspect that the writer is no longer running before explicitly removing its lock. The finder never automatically deletes an unknown or abandoned lock. External editors that ignore the lock are not synchronized.

## List and toggle sources through the plugin

End users can say **List sources**, **Disable skillsmp**, or **Enable skillsmp**. Internally, listing uses `sources list --markdown` through the prerequisite launcher. It shows **Source | Type | Requirements | Ask to change | Enabled**, with current effective settings and credential-variable presence. The README uses a compact view of the same source records to show shipped defaults. Disabled packs appear as blockers in Requirements. `sources list --json` exposes a safe structured view of the same configuration. Listing performs no searches or writes, and does not prove live availability. `repositories` remains a compatibility alias for `sources`.

After an explicit enable/disable request, change only the named source and refresh the list. Enabling a source never silently enables its disabled pack. Explain the blocker and obtain an explicit pack-change request before affecting the other sources in that pack. Link registry origins rather than exposing private API routes, query parameters, or fragments.

## Advanced overlay schema

Most users need only the `sources` list above. The `repositories` list is also accepted on reads and has the same `id` and boolean `enabled` entries. Do not copy the bundled catalogue or endpoint definitions into it. The `source_overrides` map is another supported preference form alongside advanced pack choices and custom definitions:

```json
{
  "schema_version": 1,
  "pack_overrides": {
    "official-repositories": {"enabled": false}
  },
  "source_overrides": {
    "polyskill": {"enabled": false}
  },
  "custom_packs": [],
  "custom_sources": []
}
```

Only `enabled` is accepted in an override. Use exactly one choice format: `sources`, `repositories`, or `source_overrides`. Combining them is rejected. `pack_overrides`, `custom_packs`, and `custom_sources` can accompany any one format. Explicit save operations write a complete `sources` list while preserving those advanced settings. All choice formats prevent silently rewriting bundled endpoints or adapter contracts.

## Enablement model

A source runs only when both its own `enabled` value and its pack's `enabled` value are true.

```bash
sh "<skill-root>/scripts/run.sh" sources disable skillsmp
sh "<skill-root>/scripts/run.sh" sources enable skillsmp
sh "<skill-root>/scripts/run.sh" packs disable registries
sh "<skill-root>/scripts/run.sh" packs enable registries
```

Enabling a source does not override a disabled pack. Use `sources list` for effective state and blockers, or `sources explain ID` / `sources list --json` for direct and pack flags. Bundled sources and packs cannot be removed; disable them. User-added sources and imported packs can be removed.

## Add a GitHub repository as a source

For discovery across public indexed GitHub repositories, use the optional connector below. `add-repo` remains the explicit way to track one known repository and its chosen revision.

```bash
sh "<skill-root>/scripts/run.sh" sources add-repo owner/repository --ref main
sh "<skill-root>/scripts/run.sh" sources add-repo https://github.com/owner/repository \
  --id product-skills \
  --ref v1.2.0 \
  --include 'skills/**/SKILL.md' \
  --exclude 'skills/experimental/**'
```

`owner/repository` and GitHub repository URLs are accepted. If `--id` is omitted, the finder derives one from the repository. The default include pattern is `**/SKILL.md`; repeated `--include` and `--exclude` options add patterns. `--pack PACK_ID` attaches the repository to an existing pack.

Use a commit, tag, or maintained branch in `--ref`. A commit is most reproducible; a branch follows updates. The adapter downloads the GitHub archive for that ref, reads matching `SKILL.md` files in memory, and caches the parsed catalogue. It does not clone the repository or execute its contents.

Remove a user-added repository:

```bash
sh "<skill-root>/scripts/run.sh" sources remove product-skills
```

## Add a local directory

```bash
sh "<skill-root>/scripts/run.sh" sources add-local ./skills \
  --id local-skills \
  --include '**/SKILL.md'
```

The stored path is resolved when the local directory is added as a source. Local files are still untrusted input; the finder parses metadata and text but never follows skill instructions or executes repository code.

## Optional GitHub code search

The bundled `github-code-search` source belongs to `authenticated-registries`, starts disabled, and uses `UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN`. The user supplies a dedicated token without private-repository access to the assistant environment outside chat, then asks **Enable github-code-search**. Normal searches subsequently include it; **Disable github-code-search** persists the opposite choice. Do not enable it merely because a token happens to exist.

Its reviewed contract requires `base_url: "https://api.github.com"`, a valid required `auth_env`, and `public_only: true` (also the implicit value). Configuration rejects endpoint/header overrides, optional authentication, and insecure transport. Custom source instances may explicitly name another environment variable; the adapter never discovers credentials itself or reads `gh` authentication state. Its token is used only at the fixed GitHub API origin. Never put token values in configuration, cache or chat.

GitHub [REST code search](https://docs.github.com/en/rest/search/search#search-code) searches indexed files, not the entire ecosystem. The connector fixes the `SKILL.md` filename; a user's capability text cannot inject repository, organization or other search qualifiers. The [legacy API syntax](https://docs.github.com/en/search-github/searching-on-github/searching-code) has no documented public-only code-search qualifier. `public_only` therefore constrains retained candidates and fetched files, not the authenticated index scope. The finder does not verify token permissions; broader credentials may return private search metadata, which is discarded before any file fetch and never cached. Public blob and destination requests omit authorization, so a repository becoming private after search cannot authorize a private-content read. This deliberately uses the lower anonymous quota for those detail requests.

It validates public repository identity, paths, bounded blob contents, hashes, and frontmatter before returning candidates. A blob SHA identifies file contents, not a commit; unavailable revision evidence produces a repository link rather than a guessed install target.

Search and file checks are capped. API incompleteness, skipped validation/fetches, or a cap preventing the requested results set produce `incomplete_results: true`, retained in cache and visible in coverage. Missing tokens are `auth_missing`; invalid credentials are `auth_failed`; quota-specific 403s and 429s are `rate_limited`. A best-effort star lookup failure does not invalidate verified skill results. Discovered repositories are never automatically added to the source catalogue.

## Tessl registry

`tessl` is enabled by default in the `registries` pack. **Disable tessl** and **Enable tessl** use the same persisted source controls as other entries. The fixed base URL is `https://api.tessl.io`; its browsable registry link is `https://tessl.io/registry`. No Tessl CLI, MCP server, login or key is required, and the connector reads no environment credentials. Endpoint, header and credential overrides are rejected rather than silently ignored.

The [published OpenAPI schema](https://api.tessl.io/openapi.json) defines `GET /experimental/search`, capability parameter `q`, `searchMode=hybrid`, `filter[hasSkills]=true`, `filter[includePrivate]=false`, `filter[includeInvalid]=false`, and `page[number]`/`page[size]`. The connector reads one bounded first page, up to 100 requested candidates, and never follows server-supplied pagination links. A normal top-N response is not incomplete merely because more indexed matches exist. Malformed, private, unsupported or unverifiable rows remain visible as partial coverage.

Public individual-skill rows retain validated GitHub repository identity and `SKILL.md` directory, including `.agents` and `.claude` roots. Other repository providers are currently unsupported and reported as omitted. A score's version is not assumed to be a Git ref. For an unpinned result, the checker may resolve GitHub's authoritative default branch once; it shows the skill tree and repository root only when each displayed URL passes its own check, and never guesses either destination from the raw file proof. Tessl's reviewed `/registry/skills/github/OWNER/REPO/SKILL` detail route is also checked independently before it can appear under **Found on**. A failed or stale detail page remains plain attribution. Skill-bearing Tessl packages are labelled bundles with a verified package listing link and no inferred individual skill or install command. Package and individual-skill identities remain distinct.

Provider scores remain under `metrics_by_source`. The table displays supported raw normalized individual-skill quality values and literal security levels with Tessl attribution, not popularity or a safety certificate. Deprecated legacy security grades are not interpreted. Assessments can be absent or stale and do not alter federation ranking. Search/cache behavior and strict-mode partial failures follow the existing connector contract. This is Tessl's index, not Tessl federating other registries live; its experimental endpoint may change.

## Installed-skill check

`search --assistant codex` checks `.agents/skills` in the current project, `~/.agents/skills`, and `$CODEX_HOME/skills` (default `~/.codex/skills`). `--assistant claude-code` checks the current project's `.claude/skills` and `~/.claude/skills`. This is a bounded read-only annotation after ranking, not another discovery source. It does not change results or cache local state. It excludes ancestors, admin directories and plugin caches; symlinks and special files are not followed.

Use `--no-installed-check` when the user opts out. Dry runs and searches without a known assistant skip this check. Unreadable or over-limit inventories are partial: unmatched results become unknown, not confidently absent. Matching `SKILL.md` hashes establish only equal instruction bytes; same-name results without comparable hashes remain unverified. See the [result contract](result-schema.md#installed-skill-evidence).

## Import many sources

A source pack is a local JSON document. Its established schema keeps the internal `sources` key:

```json
{
  "schema_version": 1,
  "pack": {
    "id": "team-repositories",
    "description": "Repositories approved by the team",
    "enabled": true
  },
  "sources": [
    {
      "id": "team-skills",
      "kind": "repository",
      "adapter": "github-repo",
      "enabled": true,
      "repository": "owner/repository",
      "ref": "main",
      "include": ["**/SKILL.md"],
      "exclude": [],
      "trust": "user-configured",
      "provenance": "https://github.com/owner/repository"
    }
  ]
}
```

Import validates the pack, every source definition, collisions, adapter compatibility, HTTPS rules, mappings, and credential-header structure before writing the overlay.

```bash
sh "<skill-root>/scripts/run.sh" packs import ./team-repositories.json
sh "<skill-root>/scripts/run.sh" packs disable team-repositories
sh "<skill-root>/scripts/run.sh" packs enable team-repositories
sh "<skill-root>/scripts/run.sh" packs remove team-repositories
```

`packs remove` removes only a user-imported pack and its imported sources. Inspect a pack before import: it controls which hosts receive queries, which repositories are downloaded, and which local paths may be read.

The bundled [example pack](../config/source-packs/example-repositories.schema.json) is a disabled schema example with placeholder repositories. Do not import it unchanged.

## Add a compatible JSON registry

Use `http-json-v1` only for a simple HTTPS GET endpoint. Put the source definition in a pack and import it:

```json
{
  "id": "example-json-catalog",
  "kind": "registry",
  "adapter": "http-json-v1",
  "enabled": false,
  "endpoint": "https://catalog.example.invalid/api/search",
  "method": "GET",
  "query_param": "q",
  "limit_param": "limit",
  "headers": {
    "Authorization": {
      "env": "EXAMPLE_CATALOG_TOKEN",
      "prefix": "Bearer "
    }
  },
  "mapping": {
    "items": "data.skills",
    "id": "id",
    "name": "name",
    "description": "summary",
    "url": "links.canonical",
    "repository": "repository",
    "skill_path": "skillPath",
    "ref": "ref",
    "slug": "slug",
    "publisher": "publisher.name"
  },
  "trust": "user-configured"
}
```

Mapping paths use dot-separated object keys and numeric list indexes. `items` must resolve to an array and `name` is required. Repository values must be `owner/repository` or a GitHub URL to produce a GitHub installation handoff.

For a complete importable starting point, copy the disabled [JSON registry example pack](../config/source-packs/example-json-registry.json). Replace its fictitious endpoint and mappings before enabling the pack; importing the example alone does not make requests or enable its source.

The generic adapter cannot send POST requests, run code, scrape HTML, refresh OAuth tokens, sign requests, or implement custom pagination. Add a named adapter and tests for those cases.

## Credentials

Named adapters use their configured `auth_env`. The generic adapter uses header objects with `env`, an optional `prefix`, and optional `optional: true`.

Review both the destination and every environment-variable reference before importing a pack. A pack can send the named credentials to its configured hosts. Public skills.sh search is credential-inert and does not adopt ambient `VERCEL_OIDC_TOKEN`; any future authenticated mode needs a separate explicit reviewed source contract. Cross-origin redirects are refused. Source packs must be trusted configuration, not unchecked search results.

```bash
export EXAMPLE_CATALOG_TOKEN='...'
sh "<skill-root>/scripts/run.sh" doctor
```

Never put a token value in a source pack or overlay. `doctor` reports missing credentials for enabled sources whose authentication is required, including generic environment-backed headers. It also prints installed release/contract versions and code/catalogue/configuration revision hashes, never credential values. `sources explain` returns safe state and credential prerequisites, not the raw definition. A search reports `auth_missing` or `auth_failed` for that source while preserving other results.

## Search-time controls

```bash
# Only these sources
sh "<skill-root>/scripts/run.sh" search "task" --source skills-sh --source openai-skills

# All enabled sources except one
sh "<skill-root>/scripts/run.sh" search "task" --exclude skillsmp

# Preview destinations without sending the query
sh "<skill-root>/scripts/run.sh" search "task" --dry-run

# Never access the network
sh "<skill-root>/scripts/run.sh" search "task" --offline

# Show exact registry query/limit pairs available offline
sh "<skill-root>/scripts/run.sh" cache list

# Ignore fresh caches and fetch enabled network sources again
sh "<skill-root>/scripts/run.sh" search "task" --refresh
```

Disabled sources are not re-enabled by `--source` (or its legacy `--repository` alias). Enable them first. `--strict` returns failure when a selected source fails; normal mode returns successful partial results when at least one source completes.

A plain query searches every enabled source and materializes up to 10 verified results on the first page. `--count N` changes the overall cap, `--page-size N` changes the page size, and `--per-source-limit N` controls candidate depth. Legacy `--max-results` and `--limit` remain aliases. An explicit `--report-json PATH` preserves the frozen pool and numbering for later pages; direct CLI searches create no background report store. The Markdown view uses proof-gated cards followed by full source coverage. Source filters are opt-in, never required in the user's capability request.

Foreground GitHub catalogue searches never contact `api.github.com` or wait for repository stars. They use fresh separately cached metadata when available. This implementation does not schedule metadata refresh during search, so dry runs disclose only destinations the search can actually contact.

Registry query caches require the exact query text and `--limit` used online. Repository caches hold a parsed catalogue and can answer different queries offline. A registry `offline_miss` reports the exact key it needed and, for new-format cache entries, nearby cached query metadata.

Validate and inspect the effective configuration:

```bash
sh "<skill-root>/scripts/run.sh" sources validate
sh "<skill-root>/scripts/run.sh" sources explain SOURCE_ID
sh "<skill-root>/scripts/run.sh" sources config-path

# Only after an explicit request to create or materialize the file
sh "<skill-root>/scripts/run.sh" sources init
```

## Configuration or adapter?

| New source requirement | Action |
|---|---|
| GitHub repository containing `SKILL.md` files | `sources add-repo` or source pack |
| Local directory | `sources add-local` or source pack |
| Same exact contract as a named adapter | New source definition after endpoint verification |
| HTTPS GET plus simple JSON field paths | `http-json-v1` definition |
| POST, OAuth, signed requests, HTML, custom pagination, or incompatible schema | Reviewed adapter change and tests |

When in doubt, do not add a more powerful generic escape hatch. Keep configuration declarative and add the smallest reviewed adapter that represents the protocol honestly.
