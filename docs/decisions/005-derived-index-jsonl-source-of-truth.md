# ADR-005: A Derived Local Index Is Allowed; JSONL Stays the Source of Truth

**Status**: Accepted — constitutional amendment. Amends PHILOSOPHY.md (Principles I and III) and supersedes the storage prohibition in ADR-001.
**Date**: 2026-08-04

## Context

The constitution said "no database imports — ever". ADR-001 backed that up with three arguments, one of which is factually wrong: SQLite does **not** add a compiled dependency. `sqlite3` is a Python standard library module, and the interpreter mem runs on ships it with FTS5 compiled in (verified: SQLite 3.53.3, `ENABLE_FTS5`). The two surviving arguments from ADR-001 — opaque binary files, and an append-only single-writer workload — are arguments against SQLite *as storage*, not against SQLite *as an index*.

Meanwhile the linear scan does not hold. Every query deserializes every line before deciding whether the line matters. Measured in-process at 100k commands, layer by layer:

| Layer added | Cumulative |
|---|---|
| Read the raw bytes | 8.6 ms |
| + substring match over raw bytes | 25.5 ms |
| + `json.loads` per line | **135.4 ms** |
| + Pydantic validation | 138.0 ms |
| + full `search()` | 140 ms |

Two things follow, and the second matters more. First, the `json.loads` step alone adds ~110 ms of the 140 — **82% of query time goes to deserializing lines that are then thrown away.** Second, **Pydantic is not the culprit** — it costs about 2 ms on top of `json.loads`. Removing the model layer would buy nothing; the cost is deserializing 100% of the history to answer a question about a fraction of it. Any fix that does not change *how much gets deserialized* is not a fix.

The scan grows at ~1.45 ms per 1,000 commands. The constitutional target is <200 ms end-to-end: in-process that is crossed at ~138k commands, and counting the import floor below, at ~55k — roughly nine months of normal use. The cost grows linearly with history, forever, and the user cannot opt out of their own history growing.

An FTS5 index built with one row per **unique** command plus aggregates, benchmarked at 42% unique commands (calibrated against the real `~/.mem`, which is 47.9% unique):

| Corpus | Unique | Index size | Full rebuild | Query |
|---|---|---|---|---|
| 100k commands | 42,777 | 9.6 MB | 0.67 s | **0.67–1.3 ms** |
| 1M commands | 414,844 | 90.8 MB | 8.4 s | **5.7–14.8 ms** |

**And a floor this ADR has to state honestly: the index is necessary and not sufficient.** Importing `mem.cli` costs 120 ms before a single byte of data is touched (pydantic 63 ms, rich 23 ms, click 11 ms), and no index can get below that. A 1 ms query still shows the user 130 ms. A fast path that imports only `sqlite3` and `argparse` runs the same query in 25 ms wall (280 ms → 25 ms, 11×). The index without the fast path fixes the part of the latency the user notices least.

The choice is therefore not "purity vs. speed". It is: amend the rule explicitly and argue for it, or ship an index that contradicts four documents and hand the first contributor who reads the constitution a valid objection.

## Decision

mem may maintain a **derived local index** — SQLite with FTS5, standard library only, at `~/.mem/index.db`, with **one row per unique command** plus aggregates (not one row per capture).

The permission is conditional. The index exists only for as long as **all five** of these hold. They are the decision, not a caveat on it:

1. **The JSONL files are the only source of truth.** Every fact the index contains must be re-derivable from `~/.mem/**/*.jsonl` alone. If a value cannot be reconstructed from the JSONL, it does not go in the index — it goes in its own plain-text file (selection counters belong in `~/.mem/ranking.json`, not in `index.db`).
2. **The index is fully rebuildable.** `mem` must be able to reconstruct it from scratch from the JSONL at any time, with no other input, and must do so automatically when the schema version, the mem version, or the file watermarks do not match.
3. **Deleting the index must never lose data.** `rm ~/.mem/index.db` is a *supported operation* and the documented universal repair. After deleting it, every command must still work and every query must return the same results — only slower. This is testable, and it must be tested.
4. **The index is never a second write path.** Capture writes JSONL and nothing else. The index is only ever updated by the index builder, reading JSONL that is already durable on disk. No user data reaches `index.db` before it is in a JSONL file. A corrupt, stale, locked, or unreadable index degrades to scanning the JSONL — it never turns into an error the user has to solve.
5. **Still zero network and zero daemons.** `sqlite3` is stdlib, the file is local, and it is opened in-process by the CLI. Nothing about this decision loosens Principle I or ADR-003.

The rule in the constitution changes from *"no database"* to *"no second source of truth, and nothing whose loss loses user data"*. That is a stronger rule, not a weaker one: it forbids things "no database" never covered, and it is the property users actually care about.

## Alternatives Considered

- **Keep the linear scan.** 140 ms in-process at 100k commands, of which 82% is `json.loads` on lines that are immediately discarded, growing at ~1.45 ms per 1,000 commands forever. Rejected: the latency budget breaks at ~55k commands end-to-end, and the tool is meant to be used for years.
- **Byte-prefilter before `json.loads` instead of an index.** Measured 2.8× (814 → 293 ms at 500k) with no format change, so it is worth doing on its own merits — but it only accelerates substring matching, which is the modality that scores 0/10 on natural-language queries. It is a bridge, not an architecture. Adopted as an optimization, rejected as a replacement for this decision.
- **Index every occurrence instead of deduplicating.** The obvious port — one FTS row per capture — measured at 1M commands: **190 MB of index, larger than the 169 MB of JSONL it indexes**, with 10–25 ms queries, against 90.8 MB and 5.7–14.8 ms for the dedup schema. Smaller *and* faster, and it is also the semantically correct shape: mem returns ranked unique command strings, not ranked executions. Rejected — this is the expensive mistake that looks right on paper.
- **Trigram tokenizer as the primary index.** At 1M commands: 138 MB vs 90.8 MB, build 13.1 s vs 6.0 s, and queries equal or worse (23.1 ms vs 8.2 ms for `git push`). Its one advantage is infix matching, which a `LIKE '%q%'` fallback over the dedup table covers. Rejected.
- **An external search engine (Elasticsearch, Meilisearch, a Tantivy service).** Requires a server process, a port, and a client library. Violates Principle I (zero network) and ADR-003 (no daemon). Non-starter regardless of quality.
- **A local embedding / vector store.** Separate question, but measured and negative: `NLContextualEmbedding` scores 0/5 on top-1 accuracy for shell commands — they are out of distribution for a natural-language model — and `apple-fm-sdk` exposes no embedding API at all. Rejected on results, not on principle.
- **A hand-rolled postings-file index instead of SQLite.** Same conditions would apply, more code to own, worse query semantics, and no prefix/token machinery for free. Rejected: writing our own B-tree to avoid a stdlib module is not purity, it is duplication.

## Consequences

- We pay 9.6 MB on disk at 100k commands and 90.8 MB at 1M, and take on a second artifact that must stay coherent with the first: watermark bookkeeping, schema versioning, and an automatic rebuild path that has to be exercised in CI. A full rebuild is 0.67 s at 100k and 8.4 s at 1M — cheap enough that condition 2 is a real fallback and not a theoretical one.
- **The index alone does not deliver a fast `mem`.** It has to land together with the fast path that keeps pydantic, rich, and click out of the query path; otherwise a 1 ms query still costs the user 130 ms, and the ADR will look like it did not work.
- **Benchmark on realistic data or size this wrong.** Synthetic repetitive datasets gave 2.2% unique commands at 1M and made the index look magical — 4.1 MB and 0.07 ms queries. At the realistic 42% unique it is 90.8 MB and up to 14.8 ms. Every number in this ADR is from the 42%-unique corpus; anyone re-benchmarking must do the same or they will under-size the thing and be surprised in production.
- `rm ~/.mem/index.db` becomes the answer to a whole class of bug reports. That is a feature, and condition 3 is what makes it true.
- Everything remains inspectable with `cat`, `grep`, `tail -f`, and `jq`. The human-readable guarantee of Principle III is untouched, because it was never a guarantee about *every* file — it is a guarantee about the file that holds the data.
- ADR-001's technical rationale is corrected on the record: SQLite is not a compiled dependency. Its conclusion still stands for *storage*; it no longer stands for *indexing*.
- PHILOSOPHY.md and CLAUDE.md are amended to match. An open source project that contradicts its own constitution loses more credibility than the performance is worth — which is why this ADR is written before the index, not after it.
