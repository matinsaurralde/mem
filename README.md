<p align="center">
  <h1 align="center">mem</h1>
  <p align="center">
    <strong>Your shell history, understood. Not just searched.</strong>
  </p>
  <p align="center">
    A privacy-first CLI that turns your terminal history into an intelligent,<br>
    searchable memory system — powered by on-device AI, with zero cloud dependencies.
  </p>
  <p align="center">
    <a href="#installation"><img alt="macOS 26+" src="https://img.shields.io/badge/macOS-26%2B-blue?logo=apple&logoColor=white"></a>
    <a href="#installation"><img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white"></a>
    <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
    <a href="PHILOSOPHY.md"><img alt="Privacy: 100% on-device" src="https://img.shields.io/badge/privacy-100%25%20on--device-brightgreen"></a>
  </p>
</p>

<p align="center">
  <img src="assets/demo.gif" alt="mem demo" width="700">
</p>

<!-- TODO: Record a demo GIF showing: mem deploy → results from different repos -->
<!-- Use https://github.com/faressoft/terminalizer or asciinema + agg -->

---

Unlike `Ctrl+R`, mem knows *where* you are. The same query returns different results depending on your current git repository — because `kubectl apply` means something different in your infra repo than in your backend repo.

Unlike cloud-based history tools, **nothing ever leaves your machine**. Every command, pattern, and session stays in plain text files you can `cat`, `grep`, and `tail`.

## Features

- **Context-aware search** — results ranked by your current git repo, not just recency
- **AI pattern extraction** — learns that `kubectl get pods`, `kubectl get services`, `kubectl get deployments` are all `kubectl get <resource>`
- **100% on-device** — uses Apple Foundation Models locally. Zero network. Zero telemetry
- **Plain text storage** — everything in `~/.mem/` as JSONL files. Inspect with `cat`. Search with `grep`
- **Silent capture** — shell hook adds <5ms to prompt. You won't notice it
- **Session replay** — recall the exact sequence of commands from last Tuesday's debugging session

## Quick start

### 1. Install

```bash
# Homebrew (recommended)
brew install matinsaurralde/tap/mem

# Quick install script
curl -fsSL https://raw.githubusercontent.com/matinsaurralde/mem/main/install.sh | bash

# pip / pipx
pipx install mem-cli
```

### 2. Activate

```bash
echo 'eval "$(mem init zsh)"' >> ~/.zshrc
source ~/.zshrc
```

### 3. Use your terminal

Every command is silently captured with full context — directory, git repo, exit code, duration.

### 4. Search

```bash
mem deploy
```

```
 1  kubectl apply -f deployment.yaml    infra       2h ago
 2  docker compose up -d                backend     1d ago
 3  fly deploy                          api         3d ago
```

That's it. mem gets smarter the more you use it.

## Usage

### Search history

```bash
mem kubectl                    # search by keyword
mem "docker compose"           # search by phrase
mem deploy -n 20               # show more results
mem deploy --json              # machine-readable output
```

### See patterns

After running `mem sync`, mem uses on-device AI to extract structural patterns from your history:

```bash
mem kubectl --pattern
```

```
Patterns for "kubectl":

  kubectl get <resource>
  kubectl describe <resource> <name>
  kubectl logs <pod> [--tail=<n>]
  kubectl apply -f <file>
```

### Recall sessions

```bash
mem session "api outage"
```

```
+-----------------------------------------+
| Session: 2026-03-07 14:30  myapp        |
|-----------------------------------------|
|  1  kubectl logs api-7f9b --tail=100    |
|  2  kubectl get pods -n production      |
|  3  kubectl rollout restart deploy api  |
|  4  curl -s localhost:8080/health       |
+-----------------------------------------+

Replay a session? [number/n]: _
```

### More commands

```bash
mem stats                      # top commands, repos, totals
mem sync                       # extract patterns + clean old data
mem forget "API_KEY=sk-..."    # permanently delete sensitive commands
mem init zsh                   # print shell hook code
```

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
        ▼
   Append one JSON line to ~/.mem/repos/<repo>.jsonl
```

When you search, mem reads the JSONL file for your current repo and scores each command:

```
score = (frequency × 0.4) + (recency × 0.4) + (context × 0.2)
```

- **Frequency** — how often you've run this exact command
- **Recency** — exponential decay with a 7-day half-life
- **Context** — 1.0 if same repo, 0.5 if same directory prefix, 0.0 otherwise

Pattern extraction uses [Apple Foundation Models](https://developer.apple.com/machine-learning/api/) running entirely on-device. No API keys, no cloud calls, no data exfiltration — just your Mac's neural engine.

## Storage

All data lives in `~/.mem/` as human-readable plain text:

```
~/.mem/
  repos/
    infra-k8s.jsonl          # commands from this git repo
    backend.jsonl
    _global.jsonl            # commands outside any repo
  sessions/
    2026-03-05.jsonl         # grouped work sessions
  patterns/
    kubectl.json             # AI-extracted patterns
    docker.json
```

Every file is inspectable:

```bash
cat ~/.mem/repos/myapp.jsonl
tail -f ~/.mem/repos/myapp.jsonl    # watch commands arrive in real-time
grep "docker" ~/.mem/repos/*.jsonl  # search across all repos with grep
```

## Privacy

mem is built on a simple promise: **your shell history never leaves your machine**.

- Zero network requests — not even update checks
- Zero telemetry — no usage tracking, no analytics, no crash reports
- Zero cloud dependencies — works fully offline, always
- On-device AI only — Apple Foundation Models run on your Mac's neural engine
- Plain text storage — no proprietary formats, no encrypted blobs. You own your data

Read more in [PHILOSOPHY.md](PHILOSOPHY.md).

## Requirements

| Requirement | Version |
|-------------|---------|
| macOS       | 26.0+  |
| Python      | 3.10+  |
| Apple Intelligence | Enabled (for pattern extraction) |

> **Note:** mem works without Apple Intelligence — you just won't get AI-extracted patterns. Search, capture, and everything else works fine.

## Installation

### Homebrew

```bash
brew tap matinsaurralde/tap
brew install mem
```

### Quick install

```bash
curl -fsSL https://raw.githubusercontent.com/matinsaurralde/mem/main/install.sh | bash
```

### pipx (recommended for Python users)

```bash
pipx install mem-cli
```

### From source

```bash
git clone https://github.com/matinsaurralde/mem.git
cd mem
pip install -e ".[ai]"     # with AI pattern extraction
pip install -e "."         # without AI (search-only)
```

### Shell setup

After installation, add the hook to your shell:

```bash
# zsh (v1)
echo 'eval "$(mem init zsh)"' >> ~/.zshrc
source ~/.zshrc
```

Bash and fish support coming in v1.5.

## Data retention

mem never grows unbounded. Running `mem sync` automatically cleans old data:

| Data | Retention | Rationale |
|------|-----------|-----------|
| Commands | 90 days | High-volume, older ones rarely recalled |
| Sessions | 30 days | Useful for recent postmortems |
| Patterns | Forever | Small files, accumulated learning |

Override defaults: `mem sync --keep-commands 180 --keep-sessions 60`

## Uninstall

```bash
brew uninstall mem          # or: pipx uninstall mem-cli
rm -rf ~/.mem               # remove all captured data
```

Remove the `eval "$(mem init zsh)"` line from your `~/.zshrc`.

## Contributing

Contributions are welcome. Please read the [PHILOSOPHY.md](PHILOSOPHY.md) first to understand the principles that guide this project.

```bash
git clone https://github.com/matinsaurralde/mem.git
cd mem
pip install -e ".[dev]"
pytest
```

## License

[MIT](LICENSE)
