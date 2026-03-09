# ADR-004: Per-Repository JSONL Files

**Status**: Accepted
**Date**: 2026-03-08

## Context

mem's core differentiator is context-aware recall — the same query should return different results depending on the current git repository. The storage structure must support fast, context-filtered reads.

## Decision

Store commands in one JSONL file per git repository under `~/.mem/repos/<repo-name>.jsonl`, with `_global.jsonl` as a fallback for commands outside any repo.

## Alternatives Considered

- **Single global file with a repo field**: Requires filtering on every read. The file grows unbounded across all repos, making reads slower over time.
- **Partitioned by tool**: Wrong axis. Users think in terms of projects, not CLI tools. A kubectl-partitioned file mixes unrelated project contexts.

## Consequences

- Contextual queries are fast: only the relevant repo file is loaded.
- Storage structure is self-documenting: `ls ~/.mem/repos/` shows all tracked repos.
- File rotation and cleanup operate per-repo without affecting other repos.
- Cross-repo search requires reading multiple files (handled by `read_all_commands()` iterator).
