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
Ctrl+R                 # interactive finder, ranked
mem deploy             # or search from the command line
mem fix                # what fixed the last thing that broke
mem save "cmd" -t ops  # save a command to a group
mem run ops            # run the group interactively
mem vars set API_KEY   # store a secret for saved commands
```

mem replaces `Ctrl+R` rather than competing with it. Your shell's version does a literal reverse scan; mem ranks by how often you run a command, how recently, whether it's how the line *starts*, and which repo you're standing in. Unlike cloud-based tools, everything stays on your machine as plain text files in `~/.mem/` — readable with `cat`, greppable, and yours to delete.

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

Then activate the shell hook for your shell:

```bash
# zsh
echo 'eval "$(mem init zsh)"' >> ~/.zshrc
source ~/.zshrc

# bash
echo 'eval "$(mem init bash)"' >> ~/.bashrc
source ~/.bashrc

# fish
echo 'mem init fish | source' >> ~/.config/fish/config.fish
source ~/.config/fish/config.fish
```

That's it. Every command you type is now silently captured with full context (directory, git repo, exit code, duration), and **Ctrl+R** now searches it.

---

## Ctrl+R

The shell hook rebinds Ctrl+R to mem's finder. Type to filter, `↑`/`↓` to move, `⏎` to put the command on your command line — where you can read it and edit it before running it. `esc` cancels and leaves your line untouched.

```
mem kube▏  3/12417
────────────────────────────────────────────────────────────
 ▸   kubectl logs -f deploy/api -n prod            infra  2h
     kubectl get pods -w                           infra  1d
   ✗ kubectl rollout undo deploy/api               infra  3d
↑↓ select · ⏎ accept · ^U clear · esc cancel
```

Results are ranked by the same formula `mem <query>` uses, so the two never disagree. Commands that failed are marked, because "the one that worked" is usually what you are looking for.

**It learns from what you pick.** Choosing a result is the one moment you say, unambiguously, which command you meant — so mem counts it, and weighs it above everything it can only infer. One selection is enough to move a command past one you happen to have run twenty times:

```bash
mem "git commit"                  # 1. git commit --amend   (run 20 times)
                                  # 3. git commit -v        (run once)
# ...pick `git commit -v` in the finder, once...
mem "git commit"                  # 1. git commit -v
```

Picks fade with a three-week half-life, so a command you chose constantly last quarter stops steering results once you stop choosing it. They live in `~/.mem/picks.json` — the one file here that cannot be rebuilt from your history, and the only one worth backing up. If you never open the finder, your ranking is exactly what it was.

It never runs anything for you. A history search that executes behind your back is how people delete the wrong branch.

Prefer your shell's own Ctrl+R? Set `MEM_NO_KEYBINDING=1` before loading the hook; capture still works.

### Import what you already typed

The hook only sees commands typed after it was installed. To start from the years of history your shell has already been keeping:

```bash
mem import --from-shell-history --dry-run   # see what would be imported
mem import --from-shell-history             # import it

mem import --from-shell-history --shell zsh          # one shell only
mem import --from-shell-history --file ~/backup.hist # a specific file
```

`~/.zsh_history`, `~/.bash_history` and `~/.local/share/fish/fish_history` are detected automatically. Imported commands keep their recorded timestamps where the file has them, and are back-dated (never stamped as "now") where it does not. Because a history file records no directory, exit code or duration, mem stores those as unknown rather than guessing, and imported commands go to the global scope.

Running the import twice is safe: it never double-counts a command. Commands that look like they contain credentials are skipped, and the count is reported.

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

## Fix

`mem fix` answers one question: **last time this failed, what did you do next that worked?**

mem has been recording the exit code of every command since day one. `mem fix` reads them back, looking for a command that failed and a near-variant of it that succeeded seconds later — the shape of you reading an error message and retyping the line.

```bash
mem fix                  # the last thing that failed here
mem fix npm build        # a specific one
mem fix --json           # machine-readable
mem fix -n 5             # show more candidate fixes
```

```
  failed   npm run buld
           exit 1 · 3m ago · /Users/dev/work/api

  fixed by npm run build
           seen 4 times · last worked 3m ago

  also     npm run build --if-present
           seen once · last worked 2d ago · one observation only

           mem does not run it for you. Read it, then decide.
```

**It never runs anything.** `mem fix` prints the command and stops. Same reason `mem run` asks before each step: a tool that executes on your behalf is how people delete the wrong branch.

**Confidence is repetition.** A pair seen four times and a pair seen once are different claims, and the output says which. Candidates are ranked by how often the pairing was observed, then by whether the fix has itself broken since, then by recency.

```
  fixed by npm ci
           seen 3 times · last worked 1h ago
           careful: this command has itself failed 2 times since.
```

**When there is no evidence, it says so.** No suggestion is invented:

```
  failed   terraform apply
           exit 1 · just now · /Users/dev/infra

           mem has no record of anything fixing this.
```

### What it will and won't pair

The matching is deterministic — no AI, no network, just exit codes and text. It is deliberately strict, because a wrong "fix" suggested confidently is worse than no suggestion at all. A pair is only recorded when all of these hold:

| Condition | Why |
|---|---|
| The first command exited non-zero, the second exited exactly `0` | A missing exit code is not a success |
| At most 3 commands apart | You often `ls` and `cat` before fixing |
| Typed within 60 seconds | Longer, and the connection is a guess |
| Same terminal session | Two tabs in one repo interleave in one history file |
| Same program (after ignoring `sudo`) | `git status` failing then `git push` working is two events |
| The second is the first *retyped* | See below |

"Retyped" means the edit has the shape of a correction: a flag or argument **added**, a flag **removed**, or **one token mistyped** — a transposition (`psuh` → `push`), a dropped letter (`buld` → `build`), a doubled one (`hostt` → `host`).

It specifically does **not** use fuzzy string similarity, because that measures the wrong thing. `test_a.py`/`test_b.py` scores *higher* than `psuh`/`push`, so any threshold that catches the real typo also reports "the fix for `pytest tests/test_a.py` is `pytest tests/test_b.py`". Substituting one token for a different one is never treated as a correction.

Things mem finds:

```
apt install htop        →  sudo apt install htop
npm i                   →  npm i --legacy-peer-deps
gti status              →  git status          (only when the shell said "command not found")
kubectl get pods -n prd →  kubectl get pods -n prod
npm ci --frozen-lockfile → npm ci
```

Things mem deliberately refuses, even though they sit next to each other in your history:

```
pytest tests/test_a.py  →  pytest tests/test_b.py     a different file
terraform plan          →  terraform apply            the next step, not a repair
brew install jq         →  brew install yq            a different package
ssh prod-1              →  ssh prod-2                 a different host
ls /nope                →  ls                         worked by doing less
```

Two consequences worth knowing:

- **Imported history is invisible to `mem fix`.** `mem import` reads your old `.zsh_history`, which records no exit codes, so there is nothing to mine. Only commands captured by the shell hook are paired.
- **Some real fixes are missed.** `git push` → `git pull --rebase` is not found, because the rule that would find it would also produce the false positives above. Missing a fix is invisible; inventing one is not.

Credentials are removed from the output using the same redaction that guards the MCP server, so a `curl` with an auth header still shows you the fix without reprinting the token.

---

## Groups

Groups are named collections of commands — like runbooks you can execute.

### Save commands to a group

```bash
mem save "kubectl get pods -n production" --group k8s --comment "list pods"
mem save "docker compose up -d" -t deploy -c "start services"
```

Save the last command you ran:

```bash
mem save "!" -t troubleshooting
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

mem import                           # import from clipboard (auto-detect format + group name)
mem import -t renamed                # import from clipboard with custom group name
mem import runbook.json -t ops       # import from file (auto-detects format)
mem import runbook.md -t ops         # markdown works too
```

---

## Variables

Saved commands can contain `$VAR_NAME` placeholders that get resolved at runtime. Values never get stored in group files.

### Save commands with variables

```bash
# Variables are detected automatically from $VAR_NAME tokens
mem save "ssh -i ~/.ssh/\$KEY_NAME ubuntu@\$BASTION_HOST" -t ssh

# Set a default value with --var
mem save "kubectl get pods -n \$NAMESPACE" -t k8s --var NAMESPACE=production

# AI detects hardcoded credentials and suggests variables
mem save "curl -H 'Authorization: Bearer eyJhbGci...' https://api.example.com/users" -t api
#  Detected possible credential: Bearer token
#  Suggested: curl -H 'Authorization: Bearer $API_TOKEN' ...
#  Variable name [API_TOKEN]: █
```

### Resolution priority

When `mem run` encounters variables, it resolves them in this order:

1. **Inline arguments** — `mem run api API_TOKEN=abc123`
2. **Shell environment** — `export API_TOKEN=abc123`
3. **Persistent store** — `mem vars set API_TOKEN` (macOS Keychain)
4. **Default value** — from `--var NAME=default` at save time
5. **Interactive prompt** — asks you, only as a last resort

All prompts are collected upfront before any command runs. With `--yes`, unresolved variables cause an immediate error listing what's missing.

### Variable store

For values that persist across sessions but shouldn't be in `.zshrc`:

```bash
mem vars set API_TOKEN           # hidden input (like sudo)
mem vars set DB_HOST staging.db  # inline for non-sensitive values
mem vars list                    # shows names and backend, never values
mem vars remove API_TOKEN
mem vars clear
```

**Values live in the macOS Keychain**, not in a file ([ADR-010](docs/decisions/010-keychain-for-variable-values.md)). They are encrypted at rest under your login password, and mem hands them to `/usr/bin/security` over a pipe — never on a command line, where `ps` would show them to every process on the machine.

```bash
# Everything mem stores is filed under one service, and it is yours:
security find-generic-password -s mem-cli-vars -a API_TOKEN -w
```

They are also visible in **Keychain Access** under the service `mem-cli-vars`, listed as `mem-cli-vars:API_TOKEN`.

If you used `mem vars` before this landed, your values are moved out of `~/.mem/vars.json` and into the Keychain the first time you run any `mem vars` command — one at a time, and each plaintext copy is deleted only after the Keychain has been read back and agrees. `vars.json` stays as the index of *which* variables exist:

```json
{"vars": {"API_TOKEN": {"value": null, "last_used": 0, "backend": "keychain"}}}
```

**When the Keychain is not available** — a locked keychain, a declined authorization prompt, a non-macOS machine — `mem vars set` fails and stores nothing. It does not fall back to writing your token in cleartext under a promise of encryption. Values that could not be migrated keep working and are listed as `plaintext`, with a warning, every time:

```
Stored variables (values hidden) — macOS Keychain, service 'mem-cli-vars'
  API_TOKEN            keychain   last used 2h ago
  LEGACY_TOKEN         plaintext  never used

  ! 1 value(s) are still in plaintext in ~/.mem/vars.json.
```

Two limits worth knowing: a value is capped at roughly 2 KB (`security` truncates longer command lines instead of failing, so mem refuses them), and `mem forget` has to ask the Keychain for each stored value in order to match it — the only place mem reads them all.

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

## AI agents (MCP)

mem can lend your shell memory to an AI agent — Claude Code, Claude Desktop, or
anything else that speaks MCP — so it stops guessing at commands you have
already run a hundred times.

**It is off until you turn it on.**

```bash
mem agent status                 # disabled by default
mem agent enable                 # opt in
mem agent log                    # what an agent asked for, and when
mem agent disable                # revoke — takes effect on the next request
```

### Register the server

Claude Code:

```bash
claude mcp add mem -- mem mcp
```

Claude Desktop (`claude_desktop_config.json`) or a project-level `.mcp.json`:

```json
{
  "mcpServers": {
    "mem": {
      "command": "mem",
      "args": ["mcp"]
    }
  }
}
```

That is the whole configuration. `mem mcp` speaks JSON-RPC 2.0 over stdin and
stdout — there is no port, no URL and no token, because there is no server
listening for anything. Run it by hand and it will simply wait on stdin.

### What an agent can see

| Tool | What it answers |
|---|---|
| `search_history` | How *you* actually run a tool in this repo — real flags, real hosts |
| `list_runbooks` | Which named groups you have curated |
| `get_runbook` | The ordered commands of one runbook, with your comments |
| `recent_failures` | What broke recently, and what you ran next to fix it |

Read-only, all four. **No tool executes anything.** An agent gets the *text* of
a command to propose to you; you still press Enter.

### What it cannot see

- Anything, until `mem agent enable`.
- Credentials: every string leaving mem passes through redaction first — AWS
  keys, bearer tokens and JWTs, `PGPASSWORD=`, `curl -u user:pass`,
  `--token=`, private key blobs, `.env`-style assignments and vendor tokens
  (`ghp_`, `xox…`, `sk-…`, `AKIA…`) come out as `[REDACTED]`.
- Your stored variable values (`~/.mem/vars.json`) — they are never exposed;
  a runbook shows `$API_TOKEN`, never the token.

Every request is appended to `~/.mem/agent-audit.jsonl`, redacted, and shown by
`mem agent log`. `mem forget` scrubs that file too.

---

## Other commands

```bash
mem fix                          # what fixed the last failure (see Fix)
mem fix kubectl --json           # ...for a specific one, machine-readable
mem stats                        # top commands, repos, totals
mem stats --json                 # machine-readable stats
mem forget "API_KEY=sk-..."      # permanently delete matching commands
mem forget "password" --yes      # skip confirmation
mem init zsh                     # print shell hook code (also: bash, fish)
mem tui                          # the Ctrl+R finder, on demand
mem tui -- kubectl               # ...opened on a query

mem import --from-shell-history --dry-run   # see what your old history holds
mem import --from-shell-history             # bring it in

mem agent status                 # is AI-agent access on?
mem agent enable / disable       # turn it on or revoke it
mem agent log                    # what an agent asked for, and when
mem mcp                          # run the MCP server (agents call this, not you)
```

`mem forget` reaches everywhere mem stores text — history, saved commands and runbooks, variables, extracted patterns, sessions and the agent log — not just the command history.

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
       ├─→ Append to ~/.mem/repos/<repo-slug>-<hash>.jsonl
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

All your data lives in `~/.mem/` as human-readable plain text, and these
files are the only source of truth. Any index mem keeps for speed sits
beside them, is rebuilt from them, and is safe to delete:

```
~/.mem/
  repos/
    myapp-3f9a1c07.jsonl     # commands captured in this git repo
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
  agent.json                 # AI agent access flag (off unless you enabled it)
  agent-audit.jsonl          # append-only record of every agent request
```

The suffix on a repo file is the first 8 hex characters of the sha256 of the
repo's absolute path. It exists because the readable slug alone is ambiguous —
`/work/a-b/c` and `/work/a/b/c` both slugify to `work-a-b-c` — and two unrelated
repos sharing one history file merged their commands and leaked them into each
other. History written by an older version is migrated to the new name
automatically the first time mem touches that repo.

Inspect anything:

```bash
cat ~/.mem/repos/myapp-*.jsonl
tail -f ~/.mem/repos/myapp-*.jsonl  # watch commands arrive in real-time
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
- Agent access off by default — opt-in, redacted and audited ([MCP](#ai-agents-mcp))

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

Remove the shell hook line from your shell config (`~/.zshrc`, `~/.bashrc`, or `~/.config/fish/config.fish`).

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
