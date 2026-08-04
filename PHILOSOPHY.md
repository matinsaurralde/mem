# Philosophy

> Your shell history, understood. Not just searched.

These are the principles that guide every design decision in mem.

---

## I. Privacy First

All processing executes entirely on-device. Zero network requests.
Zero telemetry. Zero cloud dependencies.

- Shell history is treated as sensitive data at all times.
- No command, pattern, or session data ever leaves the machine.
- mem does not import or depend on any networking library —
  and does not depend on a library that pulls one in transitively.
- mem binds no port, opens no socket, and listens for nothing.
  Not even on `127.0.0.1`. A listener is a network, regardless
  of the interface it binds to.
- Reading stdin and writing stdout is not networking. A pipe
  handed to mem by the process that launched it has no address
  and no remote peer. This is why the MCP server for AI agents
  exists over stdio and will never exist over HTTP
  ([ADR-007](docs/decisions/007-mcp-over-stdio-never-http.md)).
- Any feature that would require network access is rejected at
  the design stage — however it is framed: "just localhost",
  "just for sync", "just opt-in", "just an update check".

**Why**: The shell history is one of the most sensitive artifacts
a developer has. Privacy is non-negotiable.

## II. Simple Where Simple Works

Deterministic algorithms are used wherever they produce
acceptable results. AI inference (Apple Foundation Models) is
reserved exclusively for tasks where no deterministic approach
can match the quality.

- Frequency ranking and recency scoring are deterministic
  (exponential decay, weighted sums).
- Pattern extraction is the sole AI-powered operation — justified
  because no regex or heuristic can infer abstract command structures
  from arbitrary history.
- Session summaries use AI for semantic matching — justified
  because keyword search alone misses intent.

**Why**: AI adds latency, complexity, and opacity. Use it only
where it earns its keep.

## III. Unix Citizen

mem is composable, pipeable, and respectful of existing
shell conventions.

- The data is human-readable plain text (JSONL or JSON), and it
  is the only source of truth.
- `cat`, `grep`, and `tail -f` work on every file that holds data.
- A derived index may exist alongside it purely for speed. It is
  rebuildable from the plain-text files, discardable at any time,
  and never a second write path — deleting it loses nothing
  ([ADR-005](docs/decisions/005-derived-index-jsonl-source-of-truth.md)).
  Any storage whose loss would lose user data is forbidden.
- One exception, and only one: a **credential** is held by the OS
  keychain rather than by a file mem writes
  ([ADR-010](docs/decisions/010-keychain-for-variable-values.md)).
  It is still a single source of truth, it is still readable without
  mem (`security find-generic-password`, Keychain Access), and mem
  keeps no copy of it. A secret is the one kind of data where
  "you can `cat` it" is a defect and not a feature.
- CLI output supports both human-readable (default) and JSON
  (`--json`) formats.
- mem reads from stdin and writes results to stdout; errors
  and diagnostics go to stderr.
- mem does not replace, wrap, or intercept the user's shell.
  It hooks into existing precmd/preexec mechanisms only.

**Why**: A tool that fights the Unix philosophy will be
abandoned. mem extends the shell; it does not compete with it.

## IV. Context Is Everything

The same command means different things in different repositories.
mem is context-aware by default.

- Commands are stored per git repository in separate JSONL files.
- Search results are ranked with a context multiplier:
  higher for the current repo, lower for unrelated repos.
- Session boundaries are defined by idle time or switching
  to a different repository.

**Why**: Context transforms a flat list of 10,000 commands into
a focused, relevant recall system.

## V. Learn Silently, Surface Explicitly

mem does not interrupt the user's workflow. It captures data
passively and speaks only when asked.

- Shell hooks append one line per command execution — no
  prompts, no confirmations, no output.
- Pattern extraction runs automatically in a detached background
  process every 20 captures. Completely invisible — no stdout,
  no stderr, no wait. The user never notices it.
- No daemons, no cron jobs, no persistent processes. The
  background subprocess runs, finishes, and exits on its own.
- When the user searches or lists, results reflect the latest
  patterns without any manual step.

**Why**: A tool that nags or slows the shell will be uninstalled
within a day. Silence is a feature.

## VI. Open Source

mem is built to be open source from day one. The codebase is
inspectable, forkable, and contributable.

- Every design decision is documented and defensible.
- No magic, no black boxes. If a contributor cannot understand
  why a piece of code exists by reading it and its context,
  documentation is missing.
- Internal APIs and data formats are stable enough that
  forks and community tools can rely on them.

**Why**: Open source is not a distribution model — it is
a quality bar. Code that must survive public scrutiny is better code.

---

## What mem is NOT

- Not a shell replacement (no Warp, no Fig)
- Not a semantic search engine (no embeddings, no vector DB)
- Not a cloud product
- Not an AI assistant that chats back
- Not a replacement for `man` pages or docs
- Not a database-backed tool: the source of truth is always the
  plain text you can `cat`. A derived, discardable index is
  allowed under the conditions in
  [ADR-005](docs/decisions/005-derived-index-jsonl-source-of-truth.md);
  a second source of truth never is.

---

## Amendments

Principles are amended in public, with the argument attached —
never silently.

- **2026-08-04** — Principle III: "no database of any kind" became
  "no second source of truth". A local, derived, rebuildable index
  is permitted; storage whose loss would lose user data is not.
  See [ADR-005](docs/decisions/005-derived-index-jsonl-source-of-truth.md).
- **2026-08-04** — Principle III: one carve-out for credentials. Values
  set with `mem vars` live in the macOS Keychain, encrypted at rest,
  instead of in plaintext JSON — because "human-readable" is the wrong
  guarantee for an API token, and 0600 stops other users but not
  backups, sync folders, or anything else running as you.
  See [ADR-010](docs/decisions/010-keychain-for-variable-values.md).
- **2026-08-04** — Principle I: strengthened. Explicitly no sockets,
  no listeners, no localhost, and no transitive networking
  dependencies; stdio pipes are explicitly not networking.
  See [ADR-007](docs/decisions/007-mcp-over-stdio-never-http.md).
