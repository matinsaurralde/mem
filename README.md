<p align="center">
  <h1 align="center">mem</h1>
  <p align="center">
    <strong>Your shell, remembered.</strong>
  </p>
  <p align="center">
    A privacy-first CLI that captures, searches, and organizes your terminal history<br>
    with on-device AI. Nothing ever leaves your machine.
  </p>
  <p align="center">
    <a href="#install"><img alt="macOS 26+" src="https://img.shields.io/badge/macOS-26%2B-blue?logo=apple&logoColor=white"></a>
    <a href="#install"><img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white"></a>
    <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
    <a href="PHILOSOPHY.md"><img alt="Privacy: on-device" src="https://img.shields.io/badge/privacy-100%25%20on--device-brightgreen"></a>
  </p>
</p>

---

## What mem does

mem silently captures every command you type, then lets you search, save, and replay them — scoped to the git repo you're in.

```bash
mem deploy             # search your history
mem save "cmd" -g ops  # save a command to a group
mem run ops            # run the group interactively
mem vars set API_KEY   # store a secret for saved commands
```

Unlike `Ctrl+R`, mem ranks results by frequency, recency, and the repo you're currently in. Unlike cloud-based tools, everything stays on your machine as plain text files in `~/.mem/`.

---

## Install

```bash
# Homebrew (recommended)
brew install matinsaurralde/tap/mem

# pip
pip install cli-mem

# With AI features (pattern extraction + credential detection)
pip install "cli-mem[ai]"
```

Then activate the shell hook:

```bash
echo 'eval "$(mem init zsh)"' >> ~/.zshrc
source ~/.zshrc
```

That's it. Every command you type is now silently captured with full context (directory, git repo, exit code, duration).

---

## Search

Just type `mem` followed by any keyword. Results are ranked by your current repo.

```bash
mem kubectl              # search by keyword
mem "docker compose"     # search by phrase
mem deploy -n 20         # more results
mem deploy --json        # machine-readable output
```

```
  1  kubectl apply -f deployment.yaml    infra       2h ago
  2  docker compose up -d                backend     1d ago
  3  fly deploy                          api         3d ago
```

### Patterns

mem automatically learns structural patterns from your history using on-device AI. No manual step needed — extraction runs in the background every 20 commands.

```bash
mem kubectl -p
```

```
Patterns for "kubectl":

  kubectl get <resource>
  kubectl describe <resource> <name>
  kubectl logs <pod> [--tail=<n>]
  kubectl apply -f <file>
```

---

## Groups

Groups are named collections of commands — like runbooks you can execute.

### Save commands to a group

```bash
mem save "kubectl get pods -n production" --group k8s --comment "list pods"
mem save "docker compose up -d" -g deploy -c "start services"
```

Save the last command you ran:

```bash
mem save "!" -g troubleshooting
```

### List groups

```bash
mem list                 # show all groups and saved commands
mem list k8s             # show commands in a specific group
mem list -g              # global scope only
mem list -r              # current repo only
mem list --json          # JSON output
```

### Run a group

```bash
mem run k8s              # run interactively (pick one or all)
mem run deploy -y        # run all without prompts
```

### Manage groups

```bash
mem group rename old new       # rename a group
mem group remove k8s           # delete a group
mem group copy k8s --global    # copy from repo to global scope
mem group edit k8s             # open in $EDITOR
```

### Export and import

```bash
mem export k8s                       # copy JSON to clipboard
mem export k8s --format markdown     # copy as markdown
mem export k8s --stdout              # print instead of clipboard

mem import runbook.json -g ops       # import from file (auto-detects format)
mem import runbook.md -g ops         # markdown works too
```

---

## Variables

Saved commands can contain `$VAR_NAME` placeholders that get resolved at runtime. Values never get stored in group files.

### Save commands with variables

```bash
# Variables are detected automatically from $VAR_NAME tokens
mem save "ssh -i ~/.ssh/\$KEY_NAME ubuntu@\$BASTION_HOST" -g ssh

# Set a default value with --var
mem save "kubectl get pods -n \$NAMESPACE" -g k8s --var NAMESPACE=production

# AI detects hardcoded credentials and suggests variables
mem save "curl -H 'Authorization: Bearer eyJhbGci...' https://api.example.com/users" -g api
#  Detected possible credential: Bearer token
#  Suggested: curl -H 'Authorization: Bearer $API_TOKEN' ...
#  Variable name [API_TOKEN]: █
```

### Resolution priority

When `mem run` encounters variables, it resolves them in this order:

1. **Inline arguments** — `mem run api API_TOKEN=abc123`
2. **Shell environment** — `export API_TOKEN=abc123`
3. **Persistent store** — `mem vars set API_TOKEN`
4. **Default value** — from `--var NAME=default` at save time
5. **Interactive prompt** — asks you, only as a last resort

All prompts are collected upfront before any command runs. With `--yes`, unresolved variables cause an immediate error listing what's missing.

### Variable store

For values that persist across sessions but shouldn't be in `.zshrc`:

```bash
mem vars set API_TOKEN           # hidden input (like sudo)
mem vars set DB_HOST staging.db  # inline for non-sensitive values
mem vars list                    # shows names only, never values
mem vars remove API_TOKEN
mem vars clear
```

### Variable status in listings

`mem list` shows whether each variable is ready:

```
● backend / api
  ──────────────────────────────────────────────────────
  1. curl -H 'Authorization: Bearer $API_TOKEN' .../users/$USER_ID
     ✓ $API_TOKEN  resolved from environment
     ⚠ $USER_ID    unset — pass inline: mem run api USER_ID=42
```

---

## Scoping

Every group and saved command lives in either **repo scope** (tied to the current git repo) or **global scope** (available everywhere).

- Inside a git repo: defaults to repo scope
- Outside a git repo: defaults to global scope
- Use `--global` / `-g` to force global scope
- A repo group **shadows** a global group with the same name

---

## Sessions

mem groups your commands into work sessions (based on 5-minute idle gaps and repo changes) so you can recall exactly what you did.

```bash
mem session "api outage"       # search sessions by keyword
mem session debug --json       # machine-readable output
```

```
┌ [1] Session: 2026-03-07 14:30  myapp ──────────────────┐
│   1  kubectl logs api-7f9b --tail=100                   │
│   2  kubectl get pods -n production                     │
│   3  kubectl rollout restart deploy api                 │
│   4  curl -s localhost:8080/health                      │
└─────────────────────────────────────────────────────────┘

Replay a session? [number/n]: _
```

Replaying a session executes each command with per-command confirmation.

---

## Other commands

```bash
mem stats                        # top commands, repos, totals
mem stats --json                 # machine-readable stats
mem forget "API_KEY=sk-..."      # permanently delete matching commands
mem forget "password" --yes      # skip confirmation
mem init zsh                     # print shell hook code
```

---

## How it works

```
You type a command
       │
       ▼
  Shell hook (preexec/precmd)
       │
       ▼
  mem _capture  ← runs in background, <5ms
       │
       ├─→ Append to ~/.mem/repos/<repo>.jsonl
       └─→ Every 20 captures: background pattern extraction
```

**Search scoring:**

```
score = (frequency × 0.4) + (recency × 0.4) + (context × 0.2)
```

- **Frequency** — how often you've run this command
- **Recency** — exponential decay, 7-day half-life
- **Context** — 1.0 same repo, 0.5 same directory prefix, 0.0 otherwise

**AI features** use [Apple Foundation Models](https://developer.apple.com/machine-learning/api/) running entirely on your Mac's neural engine. No API keys, no cloud, no data leaves the machine. If Apple Intelligence isn't available, everything still works — you just don't get pattern extraction or credential detection.

---

## Storage

All data lives in `~/.mem/` as human-readable plain text:

```
~/.mem/
  repos/
    myapp.jsonl              # commands captured in this git repo
    _global.jsonl            # commands outside any repo
  sessions/
    2026-03-07.jsonl         # work sessions by date
  patterns/
    kubectl.json             # AI-extracted command patterns
    docker.json
  groups/
    repos/
      myapp.json             # repo-scoped groups and saved commands
    _global.json             # global groups and saved commands
  vars.json                  # persistent variable store (0600 permissions)
```

Inspect anything:

```bash
cat ~/.mem/repos/myapp.jsonl
tail -f ~/.mem/repos/myapp.jsonl    # watch commands arrive in real-time
grep "docker" ~/.mem/repos/*.jsonl  # search across repos
```

Data rotation happens automatically in the background:

| Data | Retention |
|------|-----------|
| Commands | 90 days |
| Sessions | 30 days |
| Patterns | Forever |

---

## Privacy

- Zero network requests — not even update checks
- Zero telemetry — no analytics, no crash reports
- Zero cloud dependencies — fully offline, always
- On-device AI only — runs on your Mac's neural engine
- Plain text storage — no proprietary formats, you own your data

Read more in [PHILOSOPHY.md](PHILOSOPHY.md).

---

## Requirements

| Requirement | Version |
|-------------|---------|
| macOS | 26.0+ |
| Python | 3.10+ |
| Apple Intelligence | Optional (for patterns + credential detection) |

---

## Uninstall

```bash
brew uninstall mem          # or: pip uninstall cli-mem
rm -rf ~/.mem               # remove all captured data
```

Remove the `eval "$(mem init zsh)"` line from `~/.zshrc`.

---

## Contributing

```bash
git clone https://github.com/matinsaurralde/mem.git
cd mem
pip install -e ".[dev]"
pytest
```

Read [PHILOSOPHY.md](PHILOSOPHY.md) first.

## License

[MIT](LICENSE)
