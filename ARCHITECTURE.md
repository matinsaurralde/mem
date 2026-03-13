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
│  mem <query>       mem save         mem run              │
│  mem list          mem session      mem stats            │
│  mem forget        mem export       mem import           │
│  mem group *       mem saved *      mem vars *           │
│  mem init <shell>                                       │
│  mem _capture (hidden)  mem _sync (hidden)              │
└───┬────────────┬──────────┬──────────┬──────────────────┘
    │            │          │          │
    ▼            ▼          ▼          ▼
┌────────┐ ┌─────────┐ ┌──────────┐ ┌──────────────────┐
│capture │ │ search  │ │ patterns │ │    storage        │
│  .py   │ │   .py   │ │   .py    │ │      .py         │
│        │ │         │ │          │ │                   │
│ hook   │ │ score   │ │ Apple FM │ │ JSONL read/write  │
│ capture│ │ rank    │ │ SDK      │ │ JSON read/write   │
│ session│ │ filter  │ │ guided   │ │ rotate / forget   │
│ tracker│ │ dedup   │ │ gen      │ │ groups / vars     │
│ auto-  │ │         │ │ caching  │ │                   │
│ sync   │ │         │ │          │ │                   │
└───┬────┘ └────┬────┘ └────┬─────┘ └────────┬──────────┘
    │           │           │                 │
    ▼           ▼           ▼                 ▼
┌────────┐ ┌─────────┐ ┌──────────┐ ┌──────────────────┐
│groups  │ │variables│ │_generable│ │   models.py      │
│  .py   │ │   .py   │ │   .py    │ │                  │
│        │ │         │ │          │ │ Pydantic schemas  │
│ save   │ │ parse   │ │ @fm.     │ │ CapturedCommand  │
│ list   │ │ resolve │ │ generable│ │ WorkSession      │
│ export │ │ detect  │ │ types    │ │ GroupFile / Group │
│ import │ │ credent.│ │          │ │ VarsFile          │
│ scope  │ │ store   │ │          │ │ PatternFile       │
└────────┘ └─────────┘ └──────────┘ └──────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│                    ~/.mem/ (filesystem)                  │
│                                                         │
│  repos/              sessions/           patterns/      │
│    myapp.jsonl         2026-03-05.jsonl    kubectl.json  │
│    infra.jsonl         2026-03-06.jsonl    docker.json   │
│    _global.jsonl                           git.json      │
│                                                         │
│  groups/                                                │
│    repos/                                               │
│      myapp.json      vars.json            .sync_counter │
│    _global.json      .session_state.json                │
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
Auto-sync check
    │ increments capture counter
    │ every 20 captures: spawns `mem _sync` as detached background process
    │   └── pattern extraction + data rotation, fully silent
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

### Automatic Pattern Extraction

```
Every 20 captured commands:
    │
    ▼
capture.py spawns `mem _sync` as detached background process
    │ subprocess.Popen with start_new_session=True
    │ no stdout, no stderr — completely invisible
    ▼
patterns.sync_all_patterns(silent=True)
    │ reads ALL commands from all repo files
    │ groups by tool (first token)
    │ skips tools with <5 commands
    │
    ▼ (for each tool with enough data)
patterns.extract_patterns_for_tool()
    │
    ├── Load cached processed_commands from existing PatternFile
    ├── Skip already-processed commands (cache hit)
    │
    ├── If apple-fm-sdk available:
    │   │ generalize each NEW unique command individually
    │   │   └── fresh LanguageModelSession per command (context safety)
    │   │ merge with cached generalizations
    │   └── aggregate frequencies by pattern (code)
    │
    └── If not available:
        │ groups identical commands
        └── returns frequency-based "patterns" (fallback)
    │
    ▼
storage.write_patterns()
    │ writes to ~/.mem/patterns/<tool>.json
    │ includes processed_commands list for caching
    │ atomic: write tmp file, then rename
    ▼
storage.rotate()
    │ removes commands older than 90 days
    │ deletes session files older than 30 days
    │ NEVER touches patterns/ (accumulated learning)
    ▼
Done (user never sees any of this)
```

### Saving Commands with Variables

```
User runs: mem save "curl -H 'Bearer eyJhbG...' https://api.example.com" -g api
    │
    ▼
cli.save()
    │ interactive terminal detected
    ▼
variables.detect_credentials(cmd)
    │
    ├── _command_may_contain_credentials() — heuristic pre-filter
    │   checks for credential keywords + long tokens (≥16 chars)
    │   skips simple commands (echo, ls, cd) entirely
    │
    ├── If pre-filter passes and apple-fm-sdk available:
    │   │ _detect_credentials_async() via Apple FM
    │   │   └── guided generation with CredentialList generable
    │   │
    │   └── _deduplicate_detections() — post-filter
    │       removes: hallucinations, URLs, hostnames, short values, subsets
    │       extracts: actual secret from --flag=value syntax
    │       normalizes: CamelCase → UPPER_SNAKE_CASE
    │
    └── If SDK unavailable: returns empty list (save proceeds normally)
    │
    ▼
User confirms/renames each detected credential
    │ command text updated with $VAR_NAME tokens
    ▼
variables.parse_variables() — detect $VAR_NAME tokens
variables.merge_var_declarations() — merge with --var flags
    ▼
groups.save_command()
    │ stores command structure + var declarations (never values)
    ▼
Done
```

### Running Commands with Variables

```
User runs: mem run api API_TOKEN=abc123
    │
    ▼
Parse inline VAR=VALUE arguments
    ▼
Resolve all variables upfront (before any execution):
    1. Inline arguments  ← highest priority
    2. Shell environment (os.environ)
    3. Persistent store (~/.mem/vars.json)
    4. Default value (from --var at save time)
    5. Interactive prompt ← last resort
    ▼
Display resolution summary
    ✓ $API_TOKEN resolved from arguments
    ✓ $NAMESPACE resolved from default
    ▼
Execute commands with substituted values
```

## JSONL Schemas

### Command Entry (`repos/<repo>.jsonl`)

```json
{
  "command": "kubectl rollout restart deployment api",
  "ts": 1709600000,
  "dir": "/Users/mati/projects/services/api",
  "repo": "/Users/mati/projects/services/api",
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
  "repo": "/Users/mati/projects/services/api",
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
    }
  ],
  "last_updated": 1709600000,
  "processed_commands": [
    "kubectl get pods",
    "kubectl get services",
    "kubectl describe pod api-7f9b"
  ]
}
```

### Group File (`groups/repos/<repo>.json` or `groups/_global.json`)

```json
{
  "saved": [
    { "cmd": "echo hello", "comment": "test" }
  ],
  "groups": {
    "api": {
      "description": "API troubleshooting",
      "commands": [
        {
          "cmd": "curl -H 'Authorization: Bearer $API_TOKEN' https://api.example.com/users",
          "comment": "list users",
          "vars": [
            { "name": "API_TOKEN", "default": null }
          ]
        }
      ]
    }
  }
}
```

### Variable Store (`vars.json`)

```json
{
  "vars": {
    "API_TOKEN": { "value": "sk-abc123...", "last_used": 1709600000 },
    "DB_HOST": { "value": "staging.db.internal", "last_used": 1709500000 }
  }
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
- [003: No Daemon, Background Subprocess Instead](docs/decisions/003-no-daemon.md)
- [004: Per-Repository JSONL Files](docs/decisions/004-per-repo-jsonl.md)
