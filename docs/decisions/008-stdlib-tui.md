# ADR-008: The Interactive TUI Is Built on the Standard Library

**Status**: Accepted
**Date**: 2026-08-04

## Context

mem's interactive finder replaces `Ctrl+R`. It is not a screen the user opens deliberately and waits for — it is a reflex, hit mid-thought, dozens of times a day, in the middle of typing. For that surface, **first-frame latency is the product**: past roughly 50 ms the widget stops feeling like part of the shell and starts feeling like an application that has to load, and a slow `Ctrl+R` is worse than no `Ctrl+R`, because the user goes back to `Ctrl+R` and never returns.

The budget context is unforgiving. `mem --version` alone — the CLI doing nothing but starting Python and importing itself — measures **125 ms**. That is 2.5× the entire first-frame budget before a single pixel is drawn, which is why the finder cannot be a normal entry point into the existing CLI: it has to dispatch before Click, Rich, and Pydantic are imported at all.

Against that budget, the stacks were benchmarked with the same method in a clean temporary environment:

| Stack | Cost |
|---|---|
| Pure stdlib (`termios` + `tty` + `select`) | **22 ms to first frame** in a real PTY (working prototype, ~280 lines) |
| `prompt_toolkit` `Application` | **88 ms** — import and construction alone |
| Textual `App` + `Input` + `ListView` | **126 ms** — import and construction alone |

The 88 ms and 126 ms are measured *before touching a byte of history*. They are not optimization targets; they are the floor those libraries start from.

## Decision

Build the interactive finder on the **Python standard library**: raw terminal mode via `termios`/`tty`, input multiplexed with `select`, rendering written to `/dev/tty`, `curses` used only where it earns its place. No TUI framework on this path.

The prototype's structural trick is part of the decision: **draw the first frame before loading the index.** The empty prompt appears at ~22 ms; candidates arrive a few milliseconds later. Perceived latency is when the user sees the cursor, not when the data is complete.

## Cost of this decision (stated plainly)

We write and own, forever:

- the input loop and key decoding — escape sequences, meta/alt keys, bracketed paste, signals;
- rendering and redraw, including not repainting more than we must;
- resize handling (`SIGWINCH`) and narrow-terminal clamping (the prototype crashed at small widths until every computed width was clamped to a positive minimum);
- display-width math for CJK via `east_asian_width` — naive character padding misaligns columns;
- terminal-capability edge cases: tmux, `TERM=dumb`, non-TTY stdout, and the decision of what `mem find | head` does.

That is real, permanent maintenance, and it is what the 22 ms costs. There is no version of this decision where we get the latency and someone else handles the terminal.

## Alternatives Considered

- **Textual (126 ms).** Rejected: 2.5× the entire budget before reading history, and it assumes it owns the full screen — the wrong shape for an inline widget that must coexist with the shell prompt.
- **prompt_toolkit (88 ms).** Rejected for the finder: 1.8× the budget. Lazy-importing it inside the function does not help — the cost is the import itself, so deferring it only moves the 88 ms to *after* the first frame. It remains the right choice for a deliberately-opened second-level UI (`mem browse`, a group editor, a pattern inspector), where 90–110 ms is fine; that exception is recorded here so it is not read as a contradiction later.
- **`rich.console` (57.7 ms) / `readchar` (46 ms).** Rejected: still over budget on the hot path, for a fraction of what the stdlib prototype already does.
- **Shell out to `fzf`.** Rejected: an external binary we neither control nor can assume is installed, and we lose per-keystroke control — cycling search scope (repo → dir → global) and expanding session context on a keypress are the parts that differentiate this from a generic fuzzy picker.

## Consequences

- The measured target is real, not aspirational: a working stdlib prototype hits 22 ms to first frame in a real PTY.
- The finder must bypass the standard CLI import path entirely, which ties this decision to the fast-path work — the two share one latency budget.
- All the terminal edge cases listed above are ours. They need tests in a real PTY, not mocked stdin.
- Degradation is explicit: if stdout is not a TTY, or the terminal cannot be put into raw mode, the finder falls back to plain list output instead of failing.
- Any future proposal to adopt a TUI framework for the `Ctrl+R` path has to beat 22 ms. As the benchmark notes: this is arithmetic, not optimization.
