<div align="center">

# mem

**Your shell, remembered.**

A privacy-first CLI that captures, ranks, and organizes your terminal history.<br>
Nothing ever leaves your machine.

[![CI](https://github.com/matinsaurralde/mem/actions/workflows/ci.yml/badge.svg)](https://github.com/matinsaurralde/mem/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/cli-mem?label=pypi&color=3776AB)](https://pypi.org/project/cli-mem/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![macOS 26+](https://img.shields.io/badge/macOS-26%2B-000000?logo=apple&logoColor=white)](#requirements)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

## What mem does

mem silently captures every command you type — with its directory, git repo, exit code and duration — and gives it back ranked, scoped to the repo you're in.

```bash
mem deploy               # search your history
mem kubectl -p           # the patterns you actually use
mem save "cmd" -t ops    # save a command to a group
mem run ops              # run the group, step by step
```

Press `Ctrl+R` to search interactively, right at your prompt.

Your shell's own `Ctrl+R` does a literal reverse scan. mem ranks by how often you run a command, how recently, whether it *starts* with what you typed, and which repo you're standing in. Everything stays on your machine as plain text in `~/.mem/`.

---

## Install

```bash
# Homebrew (recommended)
brew install matinsaurralde/tap/mem

# pip
pip install cli-mem

# pip, with on-device pattern extraction
pip install "cli-mem[ai]"
```

Then add the hook for your shell:

```bash
# zsh
echo 'eval "$(mem init zsh)"' >> ~/.zshrc
source ~/.zshrc

# bash (macOS terminals read ~/.bash_profile; Ctrl+R needs bash 4+)
echo 'eval "$(mem init bash)"' >> ~/.bash_profile
source ~/.bash_profile

# fish
echo 'mem init fish | source' >> ~/.config/fish/config.fish
source ~/.config/fish/config.fish
```

That's it. From the next command on, everything you type is captured, and `Ctrl+R` searches it.

To start from the history your shell has already been keeping:

```bash
mem import --from-shell-history --dry-run   # see what would be imported
mem import --from-shell-history             # import it
```

---

## Search

Type `mem` followed by any words. A command matches when it contains every word, and the best matches come first.

```bash
mem kubectl              # one word
mem docker compose       # every word must appear
mem deploy -n 20         # more results (default: 10)
mem deploy --json        # machine-readable output
```

```
  1  kubectl apply -f deployment.yaml    infra       2h ago
  2  kubectl get pods -n production      infra       1d ago
  3  kubectl rollout status deploy/api   api         3d ago
```

When nothing matches literally, mem reads the words through a concept map, so a question finds the command that answers it:

```bash
mem "check disk space"   # -> du -sh .
```

When nothing matches at all, stdout stays empty, one line on stderr says so, and the exit code is still `0`.

### Ctrl+R

The shell hook binds `Ctrl+R` to mem's finder. Type to filter, `↑`/`↓` to move, `⏎` to put the command on your prompt — where you can read and edit it before running it. `esc` cancels and leaves your line untouched.

```
mem kube▏  3/12417
────────────────────────────────────────────────────────────
 ▸   kubectl logs -f deploy/api -n prod            infra  2h
     kubectl get pods -w                           infra  1d
   ✗ kubectl rollout undo deploy/api               infra  3d
↑↓ select · ⏎ accept · ^U clear · esc cancel
```

Commands that failed are marked with `✗`, and the finder never runs anything for you.

It also learns from what you pick. Choosing a result is the one moment you say exactly which command you meant, so a pick outweighs anything mem can only infer. If you never open the finder, your ranking is unaffected.

Prefer your shell's own `Ctrl+R`? Set `MEM_NO_KEYBINDING=1` before loading the hook. Capture keeps working.

### Patterns

mem learns the structure of the commands you run and shows it with `-p`:

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

No manual step: extraction runs in the background every 20 commands, with Apple Foundation Models on your Mac.

---

## Scoping

mem knows which git repo every command was run in.

- **History** is stored per repo. Searching from inside a repo favors that repo's commands, and those of sibling checkouts, without hiding the rest.
- **Groups and saved commands** live in either **repo scope** or **global scope**:
  - inside a git repo they default to repo scope; outside one, to global scope
  - `--global` / `-g` forces global scope
  - a repo group **shadows** a global group with the same name

---

## Groups

Groups are named collections of commands — runbooks you can execute.

```bash
mem save "kubectl get pods -n production" --group k8s --comment "list pods"
mem save '!' -t troubleshooting      # the last command you ran

mem list                             # all groups and saved commands
mem list k8s                         # the commands in one group
mem list --json                      # JSON output

mem run k8s                          # pick one step, or run them all
mem run k8s -y                       # run every step without prompting

mem group rename k8s kube            # rename, remove, copy --global, or edit in $EDITOR
mem export k8s --stdout              # print as JSON (without --stdout: copy to the clipboard)
mem import runbook.json -t ops       # import from a file, or from the clipboard with no file
```

---

## Variables

Saved commands can contain `$VAR_NAME` placeholders, resolved when the command runs. Values are never stored in group files.

```bash
mem save "ssh -i ~/.ssh/\$KEY_NAME ubuntu@\$BASTION_HOST" -t ssh
mem save "kubectl get pods -n \$NAMESPACE" -t k8s --var NAMESPACE=production
```

When `mem run` meets a variable, it takes the first value it finds, in this order:

1. **Inline** — `mem run api API_TOKEN=abc123`
2. **Environment** — `export API_TOKEN=abc123`
3. **Variable store** — `mem vars set API_TOKEN`
4. **Default** — from `--var NAME=default` at save time
5. **Prompt** — asked once, before any command runs

```bash
mem vars set API_TOKEN           # hidden input, like sudo
mem vars list                    # names, never values
mem vars remove API_TOKEN
```

Stored values live in the **macOS Keychain**, under the service `mem-cli-vars`, never in a file. mem hands them to `/usr/bin/security` over a pipe, so they never appear on a command line where `ps` could see them.

`mem list <group>` shows whether each variable is ready:

```
● global / api
  ──────────────────────────────────────────────────
  1. curl -H "Authorization: Bearer $API_TOKEN" .../users/$USER_ID
     ✓ $API_TOKEN  from environment
     ⚠ $USER_ID  unset — pass inline: mem run api USER_ID=<value>
```

---

## Other commands

```bash
mem fix                          # what fixed the last command that failed
mem stats                        # top commands, repos, totals
mem forget "API_KEY=sk-..."      # permanently delete matching commands
mem tui -- kubectl               # open the Ctrl+R finder on a query
mem init zsh                     # print the shell hook (also: bash, fish)
```

---

## How it works

```
You type a command
       │
       ▼
  Shell hook (preexec / precmd)
       │
       ▼
  mem _capture              ← in the background; never blocks your prompt
       │
       ├─→ append to ~/.mem/repos/<repo>-<hash>.jsonl
       └─→ every 20 captures: pattern extraction and retention, in the background
```

### Ranking

```
score = 0.40 × picks + 0.21 × frequency + 0.21 × recency + 0.09 × prefix + 0.09 × context
```

| Signal | Meaning |
|---|---|
| **Picks** | How often you chose the command in `Ctrl+R`, halving every 21 days |
| **Frequency** | How often you ran it: `log1p(n) / log1p(50)`, capped at 1 |
| **Recency** | Exponential decay with a 7-day half-life |
| **Prefix** | 1 when the command *starts with* your query |
| **Context** | 1 in the current repo, 0.5 in a sibling checkout, 0 otherwise |

Every signal is normalized to [0, 1] and the weights sum to 1, so a score reads as a fraction. The weights, and the measurement behind them, are in [ADR-009](docs/decisions/009-ranking-learns-from-selections.md).

---

## Storage

Everything lives in `~/.mem/` as plain text:

```
~/.mem/
  repos/
    Users-you-code-myapp-3f9a1c07.jsonl   # commands captured in one git repo
    _global.jsonl                         # commands run outside any repo
  patterns/
    kubectl.json                          # extracted command patterns
  groups/
    repos/
      Users-you-code-myapp.json           # repo-scoped groups and saved commands
    _global.json                          # global groups and saved commands
  picks.json                              # what you chose in Ctrl+R
  vars.json                               # which variables exist (values are in the Keychain)
```

The suffix on a history file is the first 8 hex characters of the SHA-256 of the repo's path, so two repos whose paths slugify the same never share a history.

```bash
tail -f ~/.mem/repos/_global.jsonl     # watch commands arrive in real time
grep docker ~/.mem/repos/*.jsonl        # search across every repo
```

Captured commands are kept for 90 days; patterns are kept forever.

---

## Privacy

- **Zero network requests** — not even update checks. The test suite runs mem with sockets disabled to keep it that way.
- **Zero telemetry** — no analytics, no crash reports.
- **On-device AI only** — Apple Foundation Models, on your Mac.
- **Plain-text storage** — your data is yours to read, grep and delete. The one exception is variable *values*, which belong in the Keychain rather than in a file.

Read more in [PHILOSOPHY.md](PHILOSOPHY.md).

---

## Requirements

| Requirement | Version |
|-------------|---------|
| macOS | 26.0+ |
| Python | 3.10+ |
| Apple Intelligence | Optional, for pattern extraction |

---

## Uninstall

```bash
mem vars clear              # remove stored variables from the Keychain
brew uninstall mem          # or: pip uninstall cli-mem
rm -rf ~/.mem               # remove all captured data
```

Then remove the hook line from your shell config.

---

## Development

```bash
git clone https://github.com/matinsaurralde/mem.git
cd mem
pip install -e ".[dev]"
pytest
```

CI runs the suite on macOS against Python 3.10–3.13; the end-to-end tests drive the real binary inside real interactive shells. Design decisions and the measurements behind them are recorded as [ADRs](docs/decisions/). [ARCHITECTURE.md](ARCHITECTURE.md) maps the code, and [PHILOSOPHY.md](PHILOSOPHY.md) explains the principles it follows.

## License

[MIT](LICENSE)
