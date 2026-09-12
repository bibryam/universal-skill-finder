# Changelog

## Unreleased

Initial public release:

- Search 13 bundled registries, repositories, and indexes through one `find` skill.
- Install as a standalone skill or as the `skill@skill` native plugin for Claude Code and Codex.
- Return deterministic ranked cards with source coverage, checked destinations, stable pagination, and explicit inspection or installation actions.
- Keep searching read-only; installation remains a separate, approval-gated action.
- Configure enabled sources, custom repositories, local directories, and declarative source packs without changing connector code.
- Run with Python 3.10+ and no third-party runtime dependencies.
- Ship bounded network, cache, credential, archive, and untrusted-metadata controls with offline regression coverage.
- Distribute under the MIT license with versioned schemas and reproducible release archives.
