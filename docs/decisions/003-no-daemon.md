# ADR-003: No Background Daemon

**Status**: Accepted
**Date**: 2026-03-08

## Context

mem needs to capture commands and periodically extract patterns. The capture mechanism must add less than 5ms to prompt render time. Pattern extraction is computationally expensive.

## Decision

Use shell hooks (preexec/precmd) for capture and explicit `mem sync` for pattern extraction. No background daemon, no cron job, no launchd agent.

## Alternatives Considered

- **launchd agent**: Adds complexity (plist authoring, load/unload lifecycle), requires user approval for background execution, consumes resources when idle.
- **cron job**: Coarse timing (minimum 1 minute), requires crontab installation, no awareness of shell activity.
- **fswatch**: Overkill for monitoring append-only files. Adds a dependency and a running process for a problem that doesn't need real-time reaction.

## Consequences

- No process lifecycle bugs, no permissions dialogs, no idle resource consumption.
- Shell hook runs `mem _capture` in the background (`&!`) so it never blocks the prompt.
- Patterns only update when the user explicitly runs `mem sync` — acceptable because patterns don't need real-time freshness.
- The user is always in control of when analysis happens.
