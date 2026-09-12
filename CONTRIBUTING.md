# Contributing

For a failed search, include the source ID, status, finder/Python version, and a sanitized fixture. Avoid posting private queries, API keys, cache files, or local paths. Use [SECURITY.md](SECURITY.md) for vulnerabilities. Contributions are covered by the [MIT license](LICENSE).

## Local checks

Python 3.10+ with SSL support is required. Third-party runtime dependencies: none. The commands below are for contributors; public onboarding uses the Skills CLI, native plugins, or the portable skill folder in [README.md](README.md).

```bash
python3 -m unittest discover -s tests -v
python3 scripts/check_release.py
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m bandit -r skills/find/scripts scripts
.venv/bin/python -m pip_audit -r requirements-dev.txt
.venv/bin/python scripts/scan_secrets.py
.venv/bin/python -m build
```

On Windows, use `py -3` to create the environment and `.venv\Scripts\python.exe` afterward. Development scanners are not shipped runtime dependencies. CI is configured for the offline suite and clean package installation; passing native Windows jobs, including PowerShell prerequisite tests, must be verified before release. Live smoke checks remain separate because external services can fail or rate-limit unrelated changes.

Test the plugin launchers with missing, unsupported, and broken Python runtimes, plus a supported runtime. Failure must stop with a clear warning and nonzero exit before engine import, configuration/cache changes, or source requests. Do not add automatic software installs or claim plugin-manager installation performs this check. Native PowerShell behavior needs Windows verification; a skipped test on macOS is not a pass.

## Project layout

```text
skills/find/                # The complete, independently portable skill
  SKILL.md                  # Agent instructions
  scripts/run.sh / run.ps1   # Plugin launchers and prerequisite checks
  scripts/universal_skill_finder/       # Standard-library engine and one default catalogue
  references/               # Architecture, configuration, output contract
.claude-plugin/             # Claude plugin and marketplace manifests
.codex-plugin/              # Codex plugin manifest
.agents/plugins/            # Codex marketplace manifest
scripts/                    # Checkout entry point and release checks
tests/                      # Offline fixtures and adversarial regressions
```

Do not add a second copy of the engine or default catalogue. Build the CLI from the canonical skill directory. Test a copied skill directory outside the checkout; plugin validation alone does not prove portability.

Preserve the plugin response contract: every configured source is accounted for; the default page contains up to 10 verified cards; cards keep the skill name, description, Location, Found on, Signals, and inspection/install action in that order. Hyperlinks require eligible destination proof, and commands additionally require exact target proof. Cached results must not imply a live request. Generate commands locally from validated metadata, never copy executable hints from registry responses, and never execute during search. Keep snapshot numbering deterministic and withhold unresolved targets instead of guessing.

Use **source** as the umbrella term. The source types are **Registry**, **Repository**, **Local directory**, and **Search index**; a **catalogue** is a combined list or index, and a **connector** is its search implementation. Search reports use schema 2; additive source/occurrence field names and source-pack definitions remain compatible. After changing bundled source defaults, run `python3 scripts/update_readme_sources.py`; the release smoke check rejects a stale README table. New setup uses one `sources.json` file containing a `sources` list of known IDs and boolean `enabled` flags. Reads must not create or migrate it; explicit `sources init` and toggles preserve existing choices, advanced settings, and concurrent-write protection. Existing `repositories.json` paths retain precedence, and legacy choice formats and CLI aliases remain readable. Explicit saves normalize choices to the `sources` list in the selected file. Test default-path precedence, legacy aliases, strict entry validation, and disabled-pack blockers.

## Source and connector changes

For a compatible repository or endpoint, prefer a source definition or declarative pack. The disabled [JSON registry example](skills/find/config/source-packs/example-json-registry.json) is a starting point, not a live service. For a new protocol, add a reviewed adapter with success, empty, malformed, authentication, timeout/limit, and unsafe-metadata fixtures. Register its `AdapterSpec` once in the immutable connector catalogue; validation and federation share it. Follow [Architecture](ARCHITECTURE.md#add-a-source). No dynamic adapter imports, HTML-scraping fallbacks, or automatic installation.

Keep failures isolated and visible in coverage. Do not merge by name alone or normalize unrelated popularity metrics into a quality score. Add a regression test for every bug fix. Preserve query privacy, source selection, offline guarantees, and exact installation identity.

Before enabling a bundled source, verify its ownership, official API documentation, anonymous/authenticated behavior, rate limits, and current response format. Keep source provenance in its definition. Never commit a credential.

## Release

`skills/find/scripts/universal_skill_finder/versioning.py` owns the release, public schema, adapter-contract, and cache-format versions. At release time, update the engine version, `metadata.version` in the skill, both plugin manifests, and the Claude marketplace version together. Collect pending changes under `Unreleased`, then move them to a dated version heading when releasing. Keep the root and portable skill copies of `LICENSE` identical. Bump contract versions only for incompatible changes and document migration requirements. Additive JSON fields remain compatible; consumers must tolerate them. Update [CHANGELOG.md](CHANGELOG.md).

After local checks, create review artifacts without publishing:

```bash
python3 scripts/build_release.py --output-dir /path/to/new-candidate-directory
```

The builder creates a plugin ZIP, per-file provenance manifest, and SHA-256 sidecar. It does not overwrite existing artifacts. Caches, IDE state, environments, credentials, and Git internals are excluded. Runtime provenance is computed from captured archive bytes, not a later reread of the checkout. Repeated builds with identical inputs and the same compression toolchain are byte-identical. Hashes prove content identity, not authorship or safety.

For the first release, rename the `Unreleased` changelog heading to `0.1.0 - YYYY-MM-DD` using the actual release date. Later releases keep a new `Unreleased` section above dated entries. For final promotion, review the complete tracked-file inventory and obtain approval to commit/tag the release. Build from the clean commit tagged with its matching `vVERSION`:

```bash
python3 scripts/build_release.py --output-dir /path/to/new-final-directory --final
```

Final mode refuses missing evidence or version mismatches. It also requires every captured file to match a regular tracked blob at the verified commit; ignored or assume-unchanged files cannot evade this check. Git state must remain stable during capture/verification. Build final artifacts from an LF checkout without content-changing Git filters, as the Ubuntu artifact job does.

The builder does not run the full test suite, create a tag, publish artifacts, or modify remote services. CI builds artifacts only after its test matrix and security/package job pass; version-tag runs invoke final mode. Artifacts are retained for review, not automatically published as a GitHub release.

Before publishing, inspect the archive and checksum, verify CI on every supported platform, confirm repository security settings, and test marketplace installation from clean profiles. Validate the real repository state rather than inferring it from a local artifact.
