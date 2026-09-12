# Security policy

## Report a vulnerability

Use GitHub's **Security → Report a vulnerability** on this repository once private vulnerability reporting is enabled. Do not put credentials, working exploits, private queries, or sensitive logs in a public issue. If the private reporting button is unavailable, open an issue asking for a private reporting channel without disclosing the vulnerability.

Include the affected version, operating system/Python version, reproduction steps, expected impact, and a minimal sanitized fixture. There is no promised response-time SLA. Security fixes target the latest release; older releases may not receive backports.

## What this tool protects

Universal Skill Finder is a local discovery client. Its trusted components are the installed engine, reviewed adapters, your source configuration, and your Python/client environment. Registry responses, repository contents, metadata, and cached results are untrusted inputs.

- No discovered scripts, hooks, package managers, or skill instructions are executed. Frontmatter parsing does not load executable YAML objects.
- Registry requests use HTTPS. Plain HTTP requires explicit opt-in and a local development host. Cross-origin redirects and HTTPS downgrades are rejected.
- Credentials are referenced by environment-variable name, not stored in packs. Public skills.sh search ignores ambient Vercel OIDC credentials. Custom endpoints receive only explicitly configured credentials.
- Responses, JSON nesting, archive expansion (including tar extension headers), archive members, local traversal, and per-skill reads are bounded. Archives are inspected in memory, never extracted to disk.
- Filesystem reads reject symlinks and special files where applicable. POSIX cache/local operations use directory descriptors; the portable fallback has weaker protection against a hostile concurrent filesystem writer.
- Cache writes are atomic and optional. New private files use restrictive permissions where supported. Malformed cached candidates cannot assign their own source identity or provenance tier.
- Deduplication uses location evidence, not names or identical instruction content alone. Installation hints are structured metadata, not executable command arrays.

## What this tool does not protect

The plugin checks Python 3.10+ and SSL before running, not necessarily while its files are installed by the plugin manager. Prerequisite failures stop before discovery; the plugin never installs dependencies automatically. A PowerShell execution-policy restriction may stop the launcher before its own check and must not be bypassed by weakening system policy.

Human-facing reports contain display-only installation commands built locally from validated target fields for the current assistant. They never use executable hints from registry responses or caches. Rendering does not invoke an installer. External installation is outside the search engine's safety boundary: review the skill, destination and existing files, check installer prerequisites, and obtain explicit approval before execution. Installer prompts are not an approval boundary because the external CLI can skip them inside coding agents. A pinned installer version is not an independent safety certification.

It does **not** scan or certify discovered skills. A malicious skill can have convincing metadata, many installs, or a registry security label. Review the actual instructions and companion files before installation. Prefer an immutable commit over a mutable branch when reproducing reviewed content.

Imported packs are an authority boundary. Review their destinations and credential-variable references: a pack can send the referenced credential to its declared host. Explicitly configured private endpoints are supported. This CLI is not a hardened multi-tenant service, and must not be exposed as a public search proxy accepting untrusted source definitions.

Public registries receive search text and connection metadata. GitHub archive downloads disclose the configured repository/ref but not the local search query. The finder has no telemetry or analytics. Upstream services have separate privacy and availability policies.

GitHub catalogue search does not request repository star counts. It may read a fresh, separately scoped metadata cache entry after validating its repository identity, value, and observation time. Stars and registry metrics are source-reported popularity snapshots, never a skill safety verdict.

Optional `github-code-search` is different: when explicitly enabled, it sends capability text and the explicitly configured `UNIVERSAL_SKILL_FINDER_GITHUB_TOKEN` to the fixed `https://api.github.com` origin. It never reads `gh` credentials or automatically adopts ambient GitHub tokens. Non-public or unverified repository records are rejected before content fetches. There is no documented public-only REST code-search qualifier: use a dedicated token without private-repository access. The finder cannot attest token permissions, and a broader token may return private search metadata that is discarded and never cached. Blob GETs omit authorization entirely, so stale search metadata cannot authorize a private-content read if visibility changes. The lower anonymous quota can cause partial file verification. Credentialed endpoint/header overrides and cross-origin redirects are rejected; no token values are stored in configuration or cache. Search rows and file blobs have shared request/time/byte limits. Git blob hashes validate consistency, not authorship or safety; downloaded instructions remain untrusted data.

Local installed-skill detection reads bounded standard current-project and user skill directories for the selected assistant without network access, execution or changes. It excludes parent projects, admin directories and plugin caches; inaccessible, symlinked and over-limit entries make inventory incomplete. Annotation contains scope labels, not absolute paths, and cannot be supplied by a registry or cache. A matching `SKILL.md` hash proves only equal instruction bytes, not the same bundle, source, active installation or safety. Name collisions are explicitly unverified and never justify overwriting local files.

Fingerprint evidence from cached candidates is unsigned metadata. Someone able to rewrite the user's cache can influence advisory instruction-match labels by forging a digest; that never grants exact-local identity or bypasses source inspection and installation approval. A preferred source with no fingerprint can also leave a match unverified despite another occurrence having a hash. The scanner makes conservative annotations, not provenance attestations.

The source-management table is a read-only configuration view, not a live endpoint check. It links public origins instead of credential-bearing API routes and reports credential presence without values. Source toggles require an explicit request; enabling a source does not silently enable its pack.

The simple `sources.json` file with its `sources` list accepts only known IDs and boolean `enabled` flags. It cannot change bundled endpoints, credential references, or adapter code. Listing, path inspection, and searching never create or rewrite this file. Explicit initialization and toggles preserve current choices and advanced configuration. Legacy `repositories` lists and `source_overrides` maps remain readable, but a file may contain only one choice format; invalid or ambiguous configuration fails before search.

The default Tessl registry sends capability text to the fixed `https://api.tessl.io/experimental/search` endpoint anonymously, with public/valid/skill-bearing filters. It neither reads Tessl credentials nor invokes Tessl's CLI or MCP server. Endpoint/credential overrides are rejected, responses are bounded, and remote next-page links are never followed. Unknown row types, unsupported identities and malformed entries produce partial coverage. Skill bundles link to a package for inspection, never an invented individual installation target. Tessl quality/security fields are untrusted, version-specific assessments; they do not certify a skill or change federation ranking. This connector does not fetch repositories or execute discovered content.

`sources explain` uses the same allowlisted metadata and does not dump raw endpoints, header values, or local-directory paths. `doctor` checks both named-adapter and generic-header credential requirements. Presence is not proof that a credential works. Existing JSON `source_*` names remain unchanged and match the source terminology.

Cooperating configuration writers use an exclusive adjacent lock and compare the loaded file revision before replacing the overlay. Stale changes fail without overwriting another session. A terminated writer may leave a lock that requires inspection; the finder never removes an unknown lock automatically. External editors ignoring the lock are not synchronized.

The cache retains queries and skill metadata in plaintext on your machine. TTL controls freshness, not deletion; there is no automatic eviction or total-disk quota. Use `doctor` to locate it and remove it through your file manager when needed. Do not share unredacted cache files or JSON reports: reports include a local configuration path.

Cache envelopes and keys have explicit format and adapter-contract versions. Legacy or incompatible entries are ignored, not deleted. Search reports carry code/catalogue/configuration hashes; release archives include per-file hashes and a checksum. These identify bytes, not a signature, Git history, security attestation, or trustworthy upstream skill. Environment credential values are not read into configuration hashes; never embed secrets in source definitions.

Resource limits are per source, not a global memory budget. Default archive limits are 50 MiB compressed and 256 MiB expanded with at most six concurrent workers. Response-body reading has a deadline, but DNS and connection behavior remain platform dependent. Rate limits and provider API changes can cause partial results.

No review or scanner can prove the absence of all vulnerabilities. This project has no independent security certification.

## Maintainer release checks

Run the offline regression suite, static scan, dependency audit, secret scan, copied-skill test, and clean package/plugin installation checks in [CONTRIBUTING.md](CONTRIBUTING.md). Before publishing, enable [GitHub private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository), branch protection, and dependency alerts. Do not claim these repository settings are active until verified.
