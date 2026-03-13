# ADR-003: No Daemon, Background Subprocess Instead

**Status**: Revised (originally "No Background Daemon", updated 2026-03-12)
**Date**: 2026-03-08 (revised 2026-03-12)

## Context

mem needs to capture commands and periodically extract patterns. The capture mechanism must add less than 5ms to prompt render time. Pattern extraction is computationally expensive but doesn't need real-time freshness.

Originally, pattern extraction required the user to explicitly run `mem sync`. In practice, nobody remembers to sync manually, so patterns were never extracted. The feature was invisible.

## Decision

Use shell hooks (preexec/precmd) for capture. Every 20 captures, spawn `mem _sync` as a **fully detached background subprocess** (`subprocess.Popen` with `start_new_session=True`, stdout/stderr devnull). No persistent daemon, no cron job, no launchd agent.

The background process runs pattern extraction and data rotation, then exits. It is completely invisible — no output, no errors, no user interaction. If it crashes, the next trigger at 20 more captures will try again.

## Why not a daemon or cron?

- **launchd agent**: Adds complexity (plist authoring, load/unload lifecycle), requires user approval for background execution, consumes resources when idle.
- **cron job**: Coarse timing (minimum 1 minute), requires crontab installation, no awareness of shell activity.
- **fswatch**: Overkill for monitoring append-only files. Adds a dependency and a running process for a problem that doesn't need real-time reaction.
- **Explicit `mem sync`**: Nobody remembers to run it. Patterns silently rot.

## Why 20 captures?

Frequent enough that patterns stay reasonably fresh. Infrequent enough that the background process isn't spawned constantly. Pattern caching (via `processed_commands` in PatternFile) ensures only new commands hit the LLM — the cost per sync drops as the cache grows.

## Consequences

- No process lifecycle bugs, no permissions dialogs, no idle resource consumption.
- Shell hook runs `mem _capture` in the background (`&!`) so it never blocks the prompt.
- Patterns update automatically without user intervention.
- If the background process fails silently, the worst case is stale patterns — search and capture are unaffected.
