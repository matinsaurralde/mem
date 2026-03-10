# Architecture

Technical overview of how mem works under the hood.

## Component Diagram

```
┌─────────────────────────────────────────────────────────┐
│                      User's Shell                       │
│                                                         │
│  preexec ──► capture cmd + start time                   │
│  precmd  ──► compute exit code + duration               │
│              └── mem _capture (background, &!)          │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                    src/mem/cli.py                        │
│                                                         │
│  mem <query>          mem sync         mem session       │
│  mem init <shell>     mem stats        mem forget        │
│  mem _capture (hidden)                                   │
└───┬────────────┬──────────────┬──────────────┬──────────┘
    │            │              │              │
    ▼            ▼              ▼              ▼
┌────────┐ ┌─────────┐ ┌──────────┐ ┌──────────────────┐
│capture │ │ search  │ │ patterns │ │    storage        │
│  .py   │ │   .py   │ │   .py    │ │      .py         │
│        │ │         │ │          │ │                   │
│ hook   │ │ score   │ │ Apple FM │ │ JSONL read/write  │
│ capture│ │ rank    │ │ SDK      │ │ JSON read/write   │
│ session│ │ filter  │ │ guided   │ │ rotate / forget   │
│ tracker│ │ dedup   │ │ gen      │ │                   │
└───┬────┘ └────┬────┘ └────┬─────┘ └────────┬──────────┘
    │           │           │                 │
    └───────────┴───────────┴─────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                    ~/.mem/ (filesystem)                   │
│                                                         │
│  repos/              sessions/           patterns/       │
│    myapp.jsonl         2026-03-05.jsonl    kubectl.json  │
│    infra.jsonl         2026-03-06.jsonl    docker.json   │
│    _global.jsonl                           git.json      │
└─────────────────────────────────────────────────────────┘
```

## Data Flows

### Command Capture

```
User runs: kubectl get pods
    │
    ▼
preexec hook fires
    │ saves command text + start timestamp
    ▼
Command executes...
    │
    ▼
precmd hook fires
    │ computes exit code + duration
    │ runs: mem _capture "kubectl get pods" "/path" "0" "312" &!
    │        └── background, disowned, silent
    ▼
capture.capture_command()
    │ detects git repo via `git rev-parse --show-toplevel`
    │ builds CapturedCommand with all metadata
    ▼
storage.append_command()
    │ appends one JSON line to ~/.mem/repos/<repo>.jsonl
    ▼
capture.SessionTracker.update()
    │ checks idle time and repo change
    │ closes session if boundary detected
    ▼
Done (total overhead: <5ms added to prompt)
```

### Search Query

```
User runs: mem kubectl
    │
    ▼
cli.cli() dispatches to search
    │ detects current repo via git
    ▼
search.search("kubectl", current_repo="infra")
    │
    ├── read ~/.mem/repos/infra.jsonl (current repo first)
    ├── read ~/.mem/repos/_global.jsonl
    ├── read other repo files
    │
    ▼
Filter: substring match "kubectl" in command text
    │
    ▼
Score each match:
    score = (frequency × 0.4) + (recency × 0.4) + (context × 0.2)
    │
    ├── frequency: count of identical commands
    ├── recency: exp(-days × ln2/7), half-life 7 days
    └── context: 1.0 same repo, 0.5 same prefix, 0.0 other
    │
    ▼
Deduplicate by command string (keep highest score)
    │
    ▼
Return top N sorted by score descending
```

### Pattern Extraction

```
User runs: mem sync
    │
    ▼
patterns.sync_all_patterns()
    │ reads ALL commands from all repo files
    │ groups by tool (first token)
    │ skips tools with <5 commands
    │
    ▼ (for each tool with enough data)
patterns.extract_patterns_for_tool()
    │
    ├── If apple-fm-sdk available:
    │   │ builds prompt with command list
    │   │ calls Apple FM with Pydantic guided generation
    │   └── returns PatternExtractionResult
    │
    └── If not available:
        │ groups identical commands
        └── returns frequency-based "patterns" (fallback)
    │
    ▼
storage.write_patterns()
    │ writes to ~/.mem/patterns/<tool>.json
    │ atomic: write tmp file, then rename
    ▼
storage.rotate()
    │ removes commands older than 90 days
    │ deletes session files older than 30 days
    │ NEVER touches patterns/
    ▼
Done
```

## JSONL Schemas

### Command Entry (`repos/<repo>.jsonl`)

```json
{
  "command": "kubectl rollout restart deployment api",
  "ts": 1709600000,
  "dir": "/Users/mati/projects/services/api",
  "repo": "services",
  "exit_code": 0,
  "duration_ms": 312,
  "session": "abc123def456"
}
```

### Session Entry (`sessions/YYYY-MM-DD.jsonl`)

```json
{
  "id": "abc123def456",
  "summary": "debugging API outage in production",
  "started_at": 1709599500,
  "ended_at": 1709600400,
  "dir": "/Users/mati/projects/services/api",
  "repo": "services",
  "commands": [
    "kubectl logs api-7f9b --tail=100",
    "kubectl get pods -n production",
    "kubectl rollout restart deployment api"
  ]
}
```

### Pattern Entry (`patterns/<tool>.json`)

```json
{
  "tool": "kubectl",
  "patterns": [
    {
      "pattern": "kubectl get <resource>",
      "example": "kubectl get pods",
      "frequency": 42
    },
    {
      "pattern": "kubectl describe <resource> <name>",
      "example": "kubectl describe pod api-7f9b",
      "frequency": 17
    }
  ],
  "last_updated": 1709600000
}
```

## Session Boundary Detection

A new session begins when either condition is met:
- **Idle time > 300 seconds (5 minutes)**: Long enough that brief interruptions don't split sessions, short enough that genuine context switches are detected.
- **Repository change**: Switching to a different git repo almost always means a different task.

## Design Decisions

See `docs/decisions/` for detailed ADRs:

- [001: JSONL Over SQLite](docs/decisions/001-jsonl-over-sqlite.md)
- [002: Apple FM SDK for Patterns](docs/decisions/002-apple-fm-sdk-for-patterns.md)
- [003: No Background Daemon](docs/decisions/003-no-daemon.md)
- [004: Per-Repository JSONL Files](docs/decisions/004-per-repo-jsonl.md)
