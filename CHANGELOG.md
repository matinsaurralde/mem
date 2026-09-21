# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] — 2026-09-21

The first release of the 0.5 line to reach a user. The 0.5.0 entry below was
merged on 2026-08-04 and never tagged, so PyPI, the Homebrew tap and the
GitHub releases page all still served 0.4.1 until this one. Everything under
0.5.0 ships here for the first time, plus the repairs from a full audit of
the repository against real data.

### Fixed

- **Terminal mouse reports were stored as failed commands.** A program that
  dies with mouse tracking on leaves the terminal reporting every mouse move
  to the shell; the SGR payload (`65;50;35M`, or hundreds of them in one
  line) runs, fails with 127, and was captured. `mem fix` then offered it as
  the last failure. Capture now drops a line that is nothing but mouse
  reports; `echo 65;50;35M` is still kept.
- **Options after a query were searched for as text.** `mem deploy --json`
  and `mem deploy -n 20`, the forms the README documents, searched for the
  literal text and, finding nothing, printed nothing with exit 0. The router
  now re-parses the line with mem's own options allowed anywhere; unknown
  dash-words stay query text and `--` still forces literal text. `mem grep
  -n` with no value is now a usage error rather than a silent search.
- **A query with no matches printed nothing.** Now one dim line on stderr,
  `no matches for "…"`; stdout stays empty so pipes see nothing; exit 0.
- **A JSONL line holding `42` crashed `rotate` and `forget`.** Valid JSON
  that is not an object made every rewriter call `.get` on an int. `rotate`
  runs inside the silent background sync, so retention stopped for that file
  and every file after it, with exit 0 and no message. One JSONL reader with
  one corrupt-line policy now serves every rewriter.
- **A hand-edited `picks.json` entry crashed the finder after Enter**, so the
  chosen command never reached the shell. Malformed entries are skipped.
- **An installed Apple SDK with the model unavailable never reached the
  heuristic fallback.** The availability probe was an import check, so every
  generalisation raised, was swallowed, and every command mapped to itself
  with exit 0. The probe now asks the model whether it is available.
- **A command running longer than the idle threshold ended its own session.**
  The session tracker compared completion timestamps without subtracting
  `duration_ms`, the fourth instance of the bug the code base warns about; a
  six-minute build was read as six minutes of idle time.
- **`.session_state.json` was the one file mem wrote world-readable and
  without the lock.** It now goes through the same atomic 0600 write as
  everything else.
- **`recent_failures` (MCP) reported `retried_successfully: false`** for a
  command that had succeeded before failing and again after; it recorded
  only the first success.
- **The empty-query view of Ctrl+R showed the least recently modified repo**,
  not the newest commands; with a stale repo of 300 commands and a fresh one
  of 5, none of the new ones appeared. The initial query (the shell buffer)
  could also retitle the window through an escape sequence, and a long one
  wrapped the header off the screen.
- **Every CLI invocation emitted a Click deprecation warning** (268 per test
  run), and the `perf` benchmarks ran under the coverage tracer and one
  always failed. Zero warnings; the suite passes under `-W error`.

### Changed

- Every `except Exception` is narrowed to the real type or names the concrete
  failure it absorbs; every numeric threshold says where it came from, or
  that it was chosen by eye and not measured.
- Helpers that existed three times (`iso`, `redact`, `is_flag`,
  `think_seconds`, the "every word must appear" rule, the session idle
  threshold) exist once. Dead branches, two test-only functions and the
  Linux clipboard paths are gone. Seven tests that could not fail now can.
- Auto-sync tests reach the threshold by writing the counter instead of
  spawning twenty processes: the suite runs in about 60 s instead of 69 s
  with 70 more tests.

### Known limitations

Carried over from 0.5.0 unless stated:

- `mem` records its own invocations. `mem forget X` therefore re-records
  `X` at the next prompt, and `mem …` rows rank high in a fresh store.
- The credential filter used by `mem import` (`looks_like_credential`) is a
  different, weaker table than the redactor used at the MCP boundary:
  `export AWS_SECRET_ACCESS_KEY=…` and `PGPASSWORD=… psql` are imported.
- `mem save -t <new group>` prompts for a description and hangs under a
  pipe; `save` has no `-y`.
- `mem session` shows nothing until a session has closed (300 s idle and
  one more command), and captured records carry `"session": null`.
- `$MEM_DIR` is honoured by the finder and by `picks.json` but not by the
  rest of the storage layer.
- Homebrew 5.1 refuses the tap as untrusted until `brew trust
  matinsaurralde/tap` is run.
- `mem _capture` costs 150–170 ms of CPU per prompt in the background; the
  "<5 ms" in the documentation is the foreground cost of forking a disowned
  job.

## [0.5.0] — 2026-08-04

A full audit of the codebase, and the repairs it turned up. 151 findings were
catalogued; the ones that mattered were written first as failing tests marked
`xfail(strict=True)` — 62 of them — so the roadmap lived in the repository
rather than in a document, and CI turned red the day any of them was fixed by
accident. This release closes the last one.

Read the "Fixed" section before the "Added" one. Most of these were silent:
they produced no error, no warning, and a zero exit code.

### Added

- **An interactive finder on `Ctrl+R`** (`mem tui`). Type to filter, `↑`/`↓`
  to move, `⏎` to put the command on your command line — never to run it.
  Built on the standard library: **20–30 ms** to first frame against 130–170 ms
  for the ordinary CLI path, because `mem` now dispatches to the finder before
  Click, Rich and Pydantic are imported. Failed commands are marked; results
  are ranked by the same formula `mem <query>` uses, so the two never
  disagree. `MEM_NO_KEYBINDING=1` keeps your shell's own `Ctrl+R`.
- **`mem import --from-shell-history`** — the years of history you already
  have in `~/.zsh_history`, `~/.bash_history` or fish. Idempotent, so running
  it twice does not double anyone's frequency counts. Commands that look like
  credentials are withheld and counted, never silently dropped. Timestamps are
  reconstructed from what the file records rather than stamped "now", which
  would have made years of history look like it all ran today.
- **`mem mcp`** — an MCP server for AI agents, over **stdio only** (no
  sockets, no listener, not even localhost). Four read-only tools:
  `search_history`, `list_runbooks`, `get_runbook`, `recent_failures`. Nothing
  executes anything. Access is off until `mem agent enable`, everything
  returned passes through credential redaction, and `mem agent log` shows what
  was asked for.
- **Ranking that learns from what you select.** Choosing a result in the
  finder is the one moment you say, unambiguously, which command you meant,
  and it was being thrown away. Simulated over 1,200 retrieval episodes,
  recording it moves MRR@10 from **0.039 to 0.575** and top-1 from 0.025 to
  0.477 — one selection is enough to move a command past one you happen to
  have run twenty times. Picks fade with a three-week half-life. With none
  recorded, ranking is byte-for-byte what it was.
- **`mem fix`** — what fixed this last time. Mined deterministically from
  exit codes and sequence, with no AI. The obvious approach (string
  similarity) turns out to be *inverted*: `pytest test_a.py` →
  `pytest test_b.py` scores 0.94 and is not a fix, while `git psuh` →
  `git push` scores 0.88 and is. Corrections are matched by edit shape
  instead: 19/20 recall at 0/20 false positives on hand-built pairs.
- **Variable values live in the macOS Keychain**, encrypted at rest, handed
  to `/usr/bin/security` over a pipe and never on a command line where `ps`
  would show them. Existing plaintext values migrate on first use, and mem
  refuses to store rather than fall back to plaintext.
- **Search answers questions, not just substrings.** Asking *"what did I use
  to fix the certificate"* used to return nothing, because the words people
  describe a command with are not the words in the command. A curated map of
  224 concepts takes recall@5 over natural-language questions from **0/10 to
  10/10** — four times better than an on-device language model at the same
  task, and 500 times faster. It is a JSON file you can read, grep, correct
  and extend at `~/.mem/concepts.json`; expansion only runs when the literal
  search found nothing, so a literal match is never displaced.
- **`mem promote`** — the runbooks you already have but never wrote down.
  Groups are the most original thing in mem and were barely used, because
  creating one meant remembering to run `mem save`. This reads the sequences
  you actually repeat and offers to save them, with the varying argument
  already extracted as a `$VAR`. Calibrated on 8 synthetic 45-day histories:
  **0 false positives out of 40** in the default five-candidate listing, with
  88% of planted workflows surfaced. Roughly 1 in 8 suggestions is the
  `git add; commit; push` class — real repetition, poor runbook — which mem
  cannot distinguish and says so.
- Architecture decision records in `docs/decisions/`, including a
  constitutional amendment permitting a *derived*, discardable index while the
  JSONL files remain the only source of truth.

### Fixed

- **The shell hooks were wrong in five ways**, and they produce every record
  mem stores.
  - `mem init` looked for the hooks at a path that only exists in a source
    checkout, so **every pip and Homebrew install silently ran a stale second
    copy** inlined in `cli.py`. The hooks now ship inside the wheel.
  - Durations came from `$SECONDS`, an integer, so roughly two thirds of a
    real history recorded `duration_ms == 0`. Now millisecond resolution in
    zsh and in bash 5+.
  - A leading space — the universal "do not record this" gesture — was
    ignored. It is now honoured in all three shells.
  - bash stored `a` for `a | b | c`, and `false` for `false || echo
    recovered`: not a visible truncation, but a different command that means
    something else. bash now records the command line, and the contract is
    simply *mem remembers exactly what your shell remembers*.
  - Installing the hook recorded its own installation, and on bash 5.1+ would
    have deleted a `PROMPT_COMMAND` set by a prompt framework.
- **Ranking was sorting on one signal and calling it three.** `frequency`
  entered the formula as a raw count while recency and context were bounded by
  1, so a command run ten times scored 4.0 against a ceiling of 0.6 for
  everything else — recency and context could not change any ranking. All four
  features are now normalised to [0, 1], and a new *prefix* signal means
  typing `mem git push` surfaces `git push origin main` rather than the
  `echo "remember to git push"` you ran more often.
- **Two different repos shared one history file.** `/w/a-b/c` and `/w/a/b/c`
  both sanitized to `w-a-b-c`, merging their histories and letting `forget`
  and `rotate` on one reach into the other. Filenames now carry a hash suffix,
  and existing history is migrated automatically — collided files are split
  back apart using the repo path each line records.
- **A command saved but never run was unforgettable.** `mem forget` previewed
  only command history, so text living solely in a saved runbook, a stored
  variable, an extracted pattern or the audit log was reported as absent and
  left in place — the worst answer this codebase can give, on the most likely
  case.
- **Pattern extraction and data retention were dead code** for four months.
  The background sync was spawned as `python -m mem.cli`, which imported the
  module, defined every command, ran none of them, and exited 0 inside a
  `try/except: pass`.
- **A variable value could supply shell syntax**, not just a value. `mem run`
  now passes values through the environment, where parameter expansion does
  not re-scan its result for operators.
- **Concurrent writes could lose data**, history files were world-readable,
  `forget` could resurrect what it deleted, and `rotate` deleted entries it
  could not date.
- **The query prefilter matched JSON field names, not commands.** Every
  record contains the names `command`, `dir`, `exit_code`, `duration_ms`,
  `session` and `imported`, so searching for `exit` matched all 20,000 lines
  of a test store and the filter saved nothing — for exactly the queries most
  likely to be slow. Never a wrong answer, which is why it went unseen.
- Multi-word queries silently dropped every word but the first. `-g` meant
  `--global` in nine commands and `--group` in two. Rich markup in a command
  could corrupt or crash the display. `--yes` could still hang on a prompt.
  `install.sh` installed a stranger's package.

### Changed

- The `mem` console script now dispatches through `mem._entry`, so a fast path
  can exist at all. Behaviour for every existing command is unchanged.
- Dependencies carry upper bounds. An unpinned linter and an unpinned Click
  had each already broken a green build without a line changing.
- CI runs on Python 3.10–3.13, checks formatting, builds the distributions,
  installs the wheel, and verifies the installed console script serves the
  real hooks. Stacked pull requests previously received no CI at all.

### Security

- `~/.mem` is `0700` and every file in it `0600`, applied retroactively to
  history written before the change.
- Secrets are no longer echoed to the terminal or placed in `argv`.
- Agent access is opt-in, redacted and auditable.

### Known limitations

Stated because they are real, not because they are comfortable:

- macOS ships bash 3.2, which has no sub-second clock without spawning a
  process, so bash durations there keep second resolution.
- `HISTCONTROL=ignoredups` collapses a repeated command into one history
  entry, so mem sees one occurrence. Frequency counts degrade slightly for
  those users; no command is lost.
- A legacy history line carrying no `repo` field cannot be attributed during
  the collision migration and follows the repo that triggered it.
- Pattern generalization runs on a language model and is not deterministic:
  over four identical runs, three produced the ideal merged pattern and one
  left a command un-generalized.
- Credential redaction does not catch bare positional secrets with no
  surrounding context, or attached short flags like `mysql -phunter2` — `-p`
  collides with `mkdir -p`. There is deliberately no entropy heuristic; it
  would eat git SHAs and base64.
- Selection-aware ranking is worth nothing until you have used the finder,
  and `~/.mem/picks.json` is now the one file here that cannot be rebuilt
  from your history. A mistaken selection is learned; the three-week decay
  bounds how long it costs, but there is no undo for a single pick.
- A machine with no usable Keychain can no longer store new variables at all.
  That is deliberate — the alternative is writing plaintext while promising
  encryption — but it is a capability that used to work.
- `security` truncates a command longer than ~4 KB **and executes the
  remainder**, so mem refuses to store a value that large rather than
  silently keeping a truncated secret.
- `mem fix` needs explicit exit codes on both sides of a pair, so imported
  shell history — which records none — is invisible to it. It also misses
  corrections that rewrite the command rather than adjust it: `git push` →
  `git pull --rebase` is a known miss.
- The 0.039 → 0.575 ranking figure is a simulation over 1,200 synthetic
  retrieval episodes, not a field study. The distribution is realistic and
  the mechanism is sound, but nobody has measured it against a real week.

## [0.4.1] and earlier

See the git history.
