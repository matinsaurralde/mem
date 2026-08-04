# ADR-005: A Derived Local Index Is Allowed; JSONL Stays the Source of Truth

**Status**: Accepted — constitutional amendment. Amends PHILOSOPHY.md (Principles I and III) and supersedes the storage prohibition in ADR-001.
**Date**: 2026-08-04

## Context

The constitution said "no database imports — ever". ADR-001 backed that up with three arguments, one of which is factually wrong: SQLite does **not** add a compiled dependency. `sqlite3` is a Python standard library module, and the interpreter mem runs on ships it with FTS5 compiled in (verified: SQLite 3.53.3, `ENABLE_FTS5`). The two surviving arguments from ADR-001 — opaque binary files, and an append-only single-writer workload — are arguments against SQLite *as storage*, not against SQLite *as an index*.

Meanwhile the linear scan does not hold. Every query parses every line through Pydantic before deciding whether the line matters:

| | Measured |
|---|---|
| Parse 500k commands through Pydantic (one query) | **676 ms** |
| Equivalent FTS5 query over the same corpus | **0.16 ms** |
| Index size | **~26 MB** |
| CLI cold start (`mem --version`, before reading a byte) | 125 ms |

The constitutional latency target is <200 ms end-to-end. It is crossed at roughly 55,000 commands — about nine months of normal use. The cost grows linearly with history, forever, and the user cannot opt out of their own history growing.

The choice is therefore not "purity vs. speed". It is: amend the rule explicitly and argue for it, or ship an index that contradicts four documents and hand the first contributor who reads the constitution a valid objection.

## Decision

mem may maintain a **derived local index** — SQLite with FTS5, standard library only, at `~/.mem/index.db`.

The permission is conditional. The index exists only for as long as **all five** of these hold. They are the decision, not a caveat on it:

1. **The JSONL files are the only source of truth.** Every fact the index contains must be re-derivable from `~/.mem/**/*.jsonl` alone. If a value cannot be reconstructed from the JSONL, it does not go in the index — it goes in its own plain-text file (selection counters belong in `~/.mem/ranking.json`, not in `index.db`).
2. **The index is fully rebuildable.** `mem` must be able to reconstruct it from scratch from the JSONL at any time, with no other input, and must do so automatically when the schema version, the mem version, or the file watermarks do not match.
3. **Deleting the index must never lose data.** `rm ~/.mem/index.db` is a *supported operation* and the documented universal repair. After deleting it, every command must still work and every query must return the same results — only slower. This is testable, and it must be tested.
4. **The index is never a second write path.** Capture writes JSONL and nothing else. The index is only ever updated by the index builder, reading JSONL that is already durable on disk. No user data reaches `index.db` before it is in a JSONL file. A corrupt, stale, locked, or unreadable index degrades to scanning the JSONL — it never turns into an error the user has to solve.
5. **Still zero network and zero daemons.** `sqlite3` is stdlib, the file is local, and it is opened in-process by the CLI. Nothing about this decision loosens Principle I or ADR-003.

The rule in the constitution changes from *"no database"* to *"no second source of truth, and nothing whose loss loses user data"*. That is a stronger rule, not a weaker one: it forbids things "no database" never covered, and it is the property users actually care about.

## Alternatives Considered

- **Keep the linear scan.** Costs 676 ms of Pydantic parsing per query at 500k commands and degrades linearly and permanently. Rejected: the constitutional latency budget breaks at ~55k commands, and the tool is meant to be used for years.
- **Byte-prefilter before `json.loads` instead of an index.** Measured 2.8× (814 → 293 ms at 500k) with no format change, so it is worth doing on its own merits — but it only accelerates substring matching, which is the modality that scores 0/10 on natural-language queries. It is a bridge, not an architecture. Adopted as an optimization, rejected as a replacement for this decision.
- **An external search engine (Elasticsearch, Meilisearch, a Tantivy service).** Requires a server process, a port, and a client library. Violates Principle I (zero network) and ADR-003 (no daemon). Non-starter regardless of quality.
- **A local embedding / vector store.** Separate question, but measured and negative: `NLContextualEmbedding` scores 0/5 on top-1 accuracy for shell commands — they are out of distribution for a natural-language model — and `apple-fm-sdk` exposes no embedding API at all. Rejected on results, not on principle.
- **A hand-rolled postings-file index instead of SQLite.** Same conditions would apply, more code to own, worse query semantics, and no prefix/token machinery for free. Rejected: writing our own B-tree to avoid a stdlib module is not purity, it is duplication.

## Consequences

- We pay ~26 MB on disk and take on a second artifact that must stay coherent with the first: watermark bookkeeping, schema versioning, and an automatic rebuild path that has to be exercised in CI.
- `rm ~/.mem/index.db` becomes the answer to a whole class of bug reports. That is a feature, and condition 3 is what makes it true.
- Everything remains inspectable with `cat`, `grep`, `tail -f`, and `jq`. The human-readable guarantee of Principle III is untouched, because it was never a guarantee about *every* file — it is a guarantee about the file that holds the data.
- ADR-001's technical rationale is corrected on the record: SQLite is not a compiled dependency. Its conclusion still stands for *storage*; it no longer stands for *indexing*.
- PHILOSOPHY.md and CLAUDE.md are amended to match. An open source project that contradicts its own constitution loses more credibility than the performance is worth — which is why this ADR is written before the index, not after it.
