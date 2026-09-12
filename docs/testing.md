# Testing

## Offline suite

Run the complete deterministic suite without network access:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check_release.py
```

The tests cover configuration, cache behavior, connector contracts, ranking,
destination proof, pagination, presentation, installation handoffs, packaging,
and security boundaries. Tests use temporary state and must not read or change
the user's installed skills, source preferences, caches, or credentials.

## Source contracts

Each bundled source has a synthetic route and identity fixture plus reusable
cases for result limits, empty responses, malformed rows, duplicates, optional
fields, credentials, caching, and selection.

```sh
python3 scripts/test_sources.py --list
python3 scripts/test_sources.py --source skillsmp --offline
python3 scripts/test_sources.py --source tessl --case contract --offline
python3 scripts/test_sources.py --all --offline
python3 scripts/test_sources.py --full --offline
python3 scripts/test_sources.py --changed-since BASE_REF --plan
```

`PASS` means the selected assertion ran successfully. `FAIL` means a contract
regression. `SKIP` means a required platform or credential is unavailable.
`INCONCLUSIVE` is reserved for transient live failures such as timeouts, rate
limits, or upstream outages. Exit codes are 0 for complete passing coverage, 1
for failure, 2 for invalid invocation, and 3 for incomplete selected coverage.

Shared HTTP, cache, configuration, model, and federation changes select all
source contracts. A connector-specific change selects that source first.
Unknown relevant paths fall back to all sources.

## Optional live checks

Live checks require the explicit `--live` flag. They run each source through
the real CLI in an isolated child process with temporary configuration and
cache directories, fixed nonsensitive queries, bounded concurrency, and hard
deadlines. The runner removes unrelated authentication, OIDC, and proxy
variables from child processes. A credentialed source receives only its named
credential when authenticated mode is explicitly selected.

```sh
python3 scripts/test_sources.py --source skillsmp --live
python3 scripts/test_sources.py --all --live --public-only
```

Live checks can fail for reasons unrelated to a code change. Keep them separate
from the offline CI gate, preserve their PASS/FAIL/SKIP/INCONCLUSIVE result, and
never treat an unreachable source as proof that it contains no matching skill.

## Security and packaging

Install the contributor dependencies in an isolated environment, then run:

```sh
python3 -m bandit -r skills/find/scripts scripts
python3 -m pip_audit -r requirements-dev.txt
python3 scripts/scan_secrets.py
python3 -m build
```

Build a review archive with `scripts/build_release.py`. Final archives require
a clean tagged commit and exact byte-for-byte agreement with tracked files.
