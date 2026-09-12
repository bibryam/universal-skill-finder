# Changelog

## Unreleased

## 0.1.3 - 2026-09-12

- Stop repository and local catalogue searches from padding short result sets with zero-relevance skills; separator-only spellings such as `anti-slop` and `antislop` now match consistently.
- Make adapter retrieval policy explicit, invalidate pre-policy caches, and require explainable lexical overlap before any provider result enters the public pool.
- Keep provisional destination checks from granting final eligibility outside the bounded rank-ordered validation window, with a separate provisional request budget.
- Prevent sparse seeded pages from repeating already-numbered results during continuation, and label pooled totals as candidates rather than verified matches.

## 0.1.2 - 2026-09-12

- Render the canonical Universal Skill Finder repository URL directly in completed online report footers, without per-report verification or a misleading caveat.

## 0.1.1 - 2026-09-12

First tagged release, superseding the untagged `0.1.0` repository snapshot:

- Search 13 bundled registries, repositories, and indexes through one `find` skill.
- Install as a standalone skill or as the `skill@skill` native plugin for Claude Code and Codex.
- Return deterministic ranked cards with source coverage, checked destinations, stable pagination, and explicit inspection or installation actions.
- Keep raw `SKILL.md` URLs internal to target verification; link cards to separately checked GitHub directories, repository roots, and native source listings.
- Treat saved reports as untrusted portable data: demote every remote checked-status spelling on load and require fresh validation before links or installer proposals regain authority.
- Close provisional proof-cache publication at the search's freeze-anchored deadline, and isolate in-process health locks so unrelated sources cannot consume one another's bounded wait.
- Keep searching read-only; installation remains a separate, approval-gated action.
- Configure enabled sources, custom repositories, local directories, and declarative source packs without changing connector code.
- Run with Python 3.10+ and no third-party runtime dependencies.
- Ship bounded network, cache, credential, archive, and untrusted-metadata controls with offline regression coverage.
- Distribute under the MIT license with versioned schemas and reproducible release archives.
