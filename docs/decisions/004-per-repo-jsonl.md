# ADR-004: Per-Repository JSONL Files

**Status**: Accepted
**Date**: 2026-03-08

## Context

mem's core differentiator is context-aware recall — the same query should return different results depending on the current git repository. The storage structure must support fast, context-filtered reads.

## Decision

Store commands in one JSONL file per git repository under `~/.mem/repos/<repo-slug>-<hash>.jsonl`, with `_global.jsonl` as a fallback for commands outside any repo.

The filename is a readable slug of the repo path plus the first 8 hex characters of the sha256 of the *exact* path. The slug alone is not injective — every separator collapses to a hyphen, so `/work/a-b/c` and `/work/a/b/c` both produce `work-a-b-c` — and two unrelated repos sharing one file merged their histories and let `forget`/`rotate` on one reach into the other. A pure hash would also have fixed that, at the cost of the browsable plain-text store this project exists to provide, so the readable part stays and the hash only disambiguates.

## Alternatives Considered

- **Single global file with a repo field**: Requires filtering on every read. The file grows unbounded across all repos, making reads slower over time.
- **Partitioned by tool**: Wrong axis. Users think in terms of projects, not CLI tools. A kubectl-partitioned file mixes unrelated project contexts.

## Consequences

- Contextual queries are fast: only the relevant repo file is loaded.
- Storage structure is self-documenting: `ls ~/.mem/repos/` shows all tracked repos.
- File rotation and cleanup operate per-repo without affecting other repos.
- Cross-repo search requires reading multiple files (handled by `read_all_commands()` iterator).
- History written under the old slug-only name is migrated the first time mem resolves that repo (`storage.resolve_repo_key`). Entries are re-filed by the `repo` path each line records, so a previously collided file is split back into the repos that produced it. Lines carrying no `repo` cannot be attributed and follow the repo that triggers the migration — they are kept rather than dropped or guessed at.
