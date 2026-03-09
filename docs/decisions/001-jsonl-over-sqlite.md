# ADR-001: JSONL Over SQLite for Storage

**Status**: Accepted
**Date**: 2026-03-08

## Context

mem needs to persist shell command history with metadata (timestamp, directory, exit code, duration). The storage system must support concurrent writes from multiple shell sessions, be inspectable with standard Unix tools, and have zero external dependencies.

## Decision

Use JSONL (JSON Lines) files — one file per git repository — stored under `~/.mem/repos/`.

## Alternatives Considered

- **SQLite**: Produces opaque binary files that cannot be inspected with `cat` or piped through Unix tools. Adds a compiled dependency. Overkill for an append-only, single-writer workload.
- **JSON arrays**: Not append-friendly. Every write requires reading the entire file, deserializing, appending, and rewriting. Breaks on concurrent writes from multiple shell sessions.
- **Flat text**: No structured metadata. Parsing timestamps, exit codes, and repo context from free-form text is fragile and error-prone.

## Consequences

- Append-only writes are atomic at the OS level for single lines — no locking needed.
- Every file is human-readable: `cat`, `grep`, `tail -f`, and `jq` all work.
- Cross-repo queries require iterating multiple files (accepted tradeoff — contextual single-repo queries are the primary use case).
- Zero dependencies beyond Python stdlib (`json` + `pathlib`).
