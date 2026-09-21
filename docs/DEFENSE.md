# Defending mem in thirty minutes

A guide for the owner. A reviewer opens the repository, picks a file, and
asks "why is it like this?" three levels down. Every answer below can be
given without a transcript: level 1 is what it does, level 2 is why this
and not the obvious alternative, level 3 is what would break, what was tried
and failed, and the number that backs it. Every number has a source in the
repo. Nothing here is a claim the repo cannot support; where the repo cannot
support an answer, it says so.

Facts to have in hand (measured 2026-09-21 on the owner's machine, Python
3.11.7, warm cache):

| Fact | Value | Source |
|---|---|---|
| Source / test lines | 11,594 / 18,631 | `wc -l src/mem/*.py tests/*.py` |
| Tests | 1,520 passed, 4 skipped, 18 deselected by mark, 0 warnings, ~60 s, also green under `-W error` | `pytest -q` on `audit/2026-09` |
| Branch coverage | 92 % (91 % before the audit) | `pytest --cov` |
| `mem --version` / `mem tui --help` | 140–180 ms / 30–40 ms | measured |
| `mem _capture` per prompt | 150–170 ms, in the background | measured |
| `import mem.cli` | 112 ms, of which `mem.models` (Pydantic) 58 ms, click 6.5, rich.console 8.6 | `python -X importtime` |
| `except Exception` sites | 20 in `src/` before the audit; after it every one is narrowed to the real type or names the concrete failure next to it (AUDIT-2026-09.md item 7) | grep |
| Latest tag / PyPI / Homebrew tap | **v0.4.1 everywhere at the start of the audit; 0.5.0 was merged to master on 2026-08-04 and never tagged.** 0.5.1 was tagged and published the day the audit closed | `git tag`, `pip index versions cli-mem`, `Formula/mem.rb` |

That last row is the first thing to say out loud, before a reviewer finds it.

---

## The eight decisions

### 1. JSONL files, not SQLite

**Level 1.** Every captured command is one JSON line appended to
`~/.mem/repos/<repo-slug>-<hash>.jsonl`. Sessions, patterns, groups and the
variable index are plain JSON or JSONL too.

**Level 2.** ADR-001 (2026-03-08): `cat`, `grep`, `tail -f` and `jq` work on
every file; a single-line append is atomic at the OS level so concurrent
shells need no coordination for the common case; zero dependencies. JSON
arrays were rejected because every append is a rewrite; flat text because
timestamps, exit codes and repo context would be parsed out of prose.
ADR-005 (2026-08-04) corrected ADR-001 on the record: SQLite is **not** a
compiled dependency (it is stdlib), and the remaining arguments are against
SQLite as storage, not as an index. PHILOSOPHY.md Principle III was amended
the same day from "no database of any kind" to "no second source of truth",
which is a stronger rule.

**Level 3.** What was measured (ADR-005): at 100k commands a query costs
140 ms, of which `json.loads` per line is 110 ms, 82 % of the total, and
Pydantic validation adds 2 ms on top. The linear scan grows ~1.45 ms per
1,000 commands. An FTS5 index with one row per unique command would cost
9.6 MB and 0.67 s to rebuild at 100k, 90.8 MB and 8.4 s at 1M, with queries
of 1–15 ms; one row per capture was measured at 190 MB, larger than the JSONL
it indexes, and rejected. What was shipped instead: the byte prefilter that
skips `json.loads` on lines that cannot match (commit `2450c64`, 242 → 49 ms
on 100k; commit `97ced8e` fixed it matching JSON field names). **The index
does not exist in code**: `grep -rn sqlite3 src/` hits nothing; ADR-005 is a
conditional permission. What would break under SQLite: 138 storage tests that
assume line-append semantics, crash mid-write, corrupt-line skipping; `mem
forget` rewriting by line; the ADR-004 collision migration that re-files
lines by their `repo` field; the stdlib-only finder that reads raw text
(~20 ms per 100k) without importing anything; and the user's ability to
`cat` their own history. Retention keeps 90 days of captured commands, so
1M is reachable only by import.

### 2. A shell hook, not a daemon

**Level 1.** `eval "$(mem init zsh)"` installs a preexec/precmd pair; precmd
runs `mem _capture … &!`, disowned; every 20 captures the capture process
spawns a detached `mem _sync` that extracts patterns and rotates, then exits.

**Level 2.** ADR-003: launchd needs a plist, an approval dialog and idle
resources; cron has one-minute granularity and no shell awareness; fswatch
is a running process for an append-only file; the original manual `mem sync`
was never run by anyone, so patterns rotted (commit `fb86000`). ADR-006
records that anything bundle-shaped is "a second distribution channel and a
second language in the build, i.e. a different project".

**Level 3.** The hooks produce every record mem stores, and they were wrong
in five ways until commit `08879cf` (CHANGELOG 0.5.0): `mem init` looked for
them at a checkout-only path so every pip and brew install ran a stale inline
copy; `$SECONDS` gave integer durations so two thirds of a real history had
`duration_ms == 0`; a leading space was not honoured; bash stored the first
simple command of a pipeline; installation captured itself. Now the hooks are
package data, CI and the release workflow diff `mem init <shell>` from an
installed wheel against source, and 30 tests spawn real interactive
zsh/bash/fish. The background sync was dead for four months because the
spawn used `python -m mem.cli` with no `__main__` block, inside
`except Exception: pass` (commit `e7f311e`). Honest number: `mem _capture`
costs **150–170 ms of CPU per prompt** (interpreter start plus the
Click/Rich/Pydantic import plus one `git rev-parse`), in the background. The
"<5 ms" in README, ARCHITECTURE and ADR-003 is the foreground cost of forking
a disowned job, and no test measures it; the only perf test asserts ≤ 1 s
added over 12 commands and measured +0.5 s on this machine.

### 3. A fixed formula, not a model

**Level 1.** `score = 0.40·picks + 0.21·frequency + 0.21·recency +
0.09·prefix + 0.09·context`, every feature in [0, 1], in one stdlib module
(`ranking.py`) shared by `mem <query>` and the finder.

**Level 2.** ADR-009: the finder produces the one signal that is not an
inference about the user, an actual selection, and it was being thrown away.
The 0.60 that is not picks keeps the old 35/35/15/15 proportions so that
with no picks the ordering is byte-identical, which is why the whole existing
suite passed unchanged. ADR-011: a model for query understanding was
measured and lost (decision 6). Determinism and a 120-line formula a
contributor can read are the point.

**Level 3.** Simulation over 1,200 retrieval episodes with a Zipf intent
distribution: MRR@10 0.039 → 0.575, top-1 0.025 → 0.477, 14× better and 1.6×
faster. The learned weights converge to `picks +3.02, freq −0.06, recency
−0.05`, i.e. frequency and recency are worth nothing once picks exist; they
were **not** adopted wholesale because they are optimal only after picks
accumulate and would make day one worse. Keying picks by (query, command)
measured 3.3 % worse; a logarithmic pick curve let one pick lose to twenty
runs, hence `1 − 2^−w`; online learning measured no gain. Both ADR-009 and
CHANGELOG say plainly this is a simulation, not a field study. The failure
this formula replaced: frequency entered as a raw count while the other
terms were bounded by 1, so ten runs scored 4.0 against a 0.6 ceiling and
recency could not change any ranking, and every ordering test passed for
months (commit `2450c64`). The docs then drifted from the code two releases
later (commit `413dbf6`), which is why `test_e2e.py` now reads every weight
from `mem.ranking` and requires it in the published formula. What would
break: 37 exact-score tests, the finder/search agreement test, and the
22 ms first-frame budget (a model on the hot path, or 2,086 ms out of
process).

### 4. The Keychain, not a file

**Level 1.** `mem vars set` stores the value as a macOS Keychain generic
password (service `mem-cli-vars`, account = the name); `~/.mem/vars.json` is
an index with `value: null`.

**Level 2.** ADR-010: 0600 stops other users and nothing else. Time Machine,
a synced folder, a dotfiles repo that globbed too widely, an `npm install`
postinstall, and a disk image all read a plaintext file. For a project whose
first principle is privacy, a plaintext file of API tokens was the weakest
thing in it. `keyring`/`pyobjc` were rejected as dependencies; a
passphrase-encrypted file was rejected because the stdlib has no reviewed
AEAD and it would prompt on every `mem run`.

**Level 3.** The `security(1)` sharp edges were established by running it:
`-w <secret>` puts the secret in `argv` where `ps` sees it; `-w` as the last
option needs a tty and, omitted, silently stores an empty password;
`find-generic-password -w` prints printable and binary data identically so a
32-character hex key decodes to garbage, hence reads use `-g`; `security -i`
truncates a line at 4096 characters **and executes the truncated head**, so
mem refuses a value over ~2 KB (`KeychainValueTooLong`) rather than store a
truncated secret under a name that claims to be intact. Migration is
confirm-then-delete per variable under the existing lock, idempotent, and
every failure leaves the plaintext where it was. There is deliberately no
plaintext fallback: a machine without a usable Keychain cannot store new
values at all, and CHANGELOG says so. The suite never touches the login
keychain: `conftest.FakeKeychain` replaces the one function that spawns
`security`; the 12 `keychain_live` tests run against a throwaway keychain
and were verified today to leave `security list-keychains` byte-identical.

### 5. MCP over stdio, never HTTP

**Level 1.** `mem mcp` reads newline-delimited JSON-RPC 2.0 on stdin and
writes it on stdout, spawned by the client as a child; four read-only tools;
off until `mem agent enable`; every result redacted; every call audited.

**Level 2.** ADR-007: a listener is a network whether or not it binds to
127.0.0.1. The official Python SDK transitively installs `uvicorn`,
`starlette` and `httpx`, an HTTP server and client present in the process
even on the stdio transport, which is exactly what Principle I forbids. A
Unix socket is a listener with a lifecycle and reintroduces the daemon
ADR-003 rejected. So the protocol is hand-rolled: read a line, `json.loads`,
dispatch, write a line.

**Level 3.** `tests/test_e2e.py` installs a `sitecustomize` whose
`socket.socket` raises, proves the stub blocks (`test_stub_actually_blocks_sockets`),
then runs capture, search, import and the MCP server under it;
`test_mcp.py` also asserts structurally that no networking library is in the
`mcp` module's import graph. Protocol version `2025-06-18`, negotiating two
older ones. `serve()` rebinds `sys.stdout` to stderr for its lifetime so a
stray print cannot corrupt a frame. Honest limits: conformance is tested
only by the project's own 100 wire-level tests, there is no test against a
real client, and `import mem.mcp` does load `socket`/`ssl`/`asyncio`
through Pydantic, so ADR-007's sentence about the import graph is a
behavioural guarantee, not a structural one.

### 6. Apple Foundation Models, not a cloud API

**Level 1.** The on-device model is used for two things only: generalising
commands into patterns, and suggesting credentials at `mem save` time. Search,
ranking, `mem fix` and `mem promote` are deterministic.

**Level 2.** ADR-002: a cloud API violates Principle I; regex fails on novel
tools; Ollama needs a server process. PHILOSOPHY Principle II reserves
inference for tasks no deterministic approach matches. ADR-006 records the
strategic caveat in writing: "on-device AI stops being a moat this year";
the differentiator has to be what mem infers, not where the model runs.

**Level 3.** The model was tried for query understanding and lost (ADR-011):
substring search 0/10 at 9.6 ms, the on-device model expanding the query
2/10 at 2,086 ms (it suggested `certutil /rebuild`, a Windows tool), a
30-line synonym map 8/10 at 3.9 ms, the shipped 224-concept map 10/10 on the
fixture. `NLContextualEmbedding` scored 0/5 because shell commands are out
of distribution for a natural-language BERT. Non-determinism is stated:
three of four identical runs produced the ideal pattern. Verified today on
this machine: Apple Intelligence available, both `ai` tests pass, a seeded
sync produced `kubectl get <resource> -n <namespace>` in 5.9 s for 7
commands. Known gap (AUDIT 0.4): the availability probe is an import check,
so a machine with the SDK but no usable model silently maps every command to
itself instead of running the heuristic fallback; Phase 2 item 16.

### 7. Pydantic, not dataclasses (no ADR)

**Level 1.** `models.py` holds 15 Pydantic v2 models for every on-disk shape;
every read goes through `model_validate_json`, every write through
`model_dump_json`.

**Level 2, honestly.** This was inherited from the v1 scaffold (commit
`f0164c5`), never argued in an ADR, and never benchmarked against an
alternative. The stated purposes were validation on read, serialisation, and
guided-generation schemas for the SDK; the third is no longer true, because
`_generable.py` uses `@fm.generable` classes (the SDK inspects concrete
annotations), and ADR-002 still says "Pydantic-based guided generation".

**Level 3.** Two measurements settle what Pydantic costs. Per record it is
nothing: 2 ms on top of `json.loads` at 100k lines (ADR-005, "Pydantic is
not the culprit"). Per process it is the largest import in the tree: 58 ms
of `mem.cli`'s 112 ms today, 63 of 125 when ADR-008 measured it. That import
is why a parallel stdlib-only world exists (`tui.py`, `ranking.py`,
`picks.py`, `concepts.py`, `_fsutil.py`; CLAUDE.md "Some modules must not
import Pydantic"), enforced by tests that assert click/rich/pydantic are
absent from `sys.modules` on the finder path. What Pydantic is doing today:
schema evolution by defaults (`imported: bool = False`, nullable
`exit_code`/`duration_ms`) so every legacy line still validates, and one
validation policy at seven corrupt-line sites. The defensible sentence:
"inherited, not chosen; 2 ms per 100k records and 58 ms per process; we paid
the 58 ms by moving the hot path onto the standard library rather than
replacing the layer that validates seven file formats." Replacing it would
shorten every `mem` invocation by ~58 ms and is a bounded refactor, not a
correctness risk.

### 8. Click, not argparse (no ADR)

**Level 1.** Click drives 30 subcommands; the console script enters through
`_entry.py`, which dispatches `mem tui` before importing Click at all.

**Level 2, honestly.** Also inherited and never re-argued. Its import cost
is the smallest of the three dependencies (6–11 ms). Its real cost has been
API churn around the one non-standard thing mem does with it, treating an
unknown first argument as a search query: three routing commits in the first
two days (`6362e17`, `0a0d67b`, `724757b`), a `mix_stderr` removal for Click
9, an upper bound after `protected_args` was slated for removal (commit
`60e8bc3`, "the 87 DeprecationWarnings in the test suite are the advance
notice"), and, this audit, a shim for the 8.2 rename that was producing 268
warnings per run and a re-parse so that `mem deploy --json` works as the
README says.

**Level 3.** `_entry.py` exists precisely so a fast path can exist: "A
second console script would have been simpler but splits one tool into two
names." It covers `tui` only; `_capture` still pays the full import on every
prompt. What would break under argparse: nine test modules drive the CLI
through `CliRunner`; `MemGroup.parse_args` and the hidden `_capture`/`_sync`
commands would be rewritten; `--help` for 30 subcommands regenerated by
hand.

---

## Fifteen uncomfortable questions

**1. Twelve ADRs and 1,500 tests for a CLI nobody uses. Over-engineered?**
The tests exist because the July audit measured a 39 % mutation score on 258
green tests, with `_capture` as a no-op and `rotate()` deleting the whole
history both undetected. The ADRs exist because four of them are
constitutional amendments the code would otherwise contradict.
Evidence: commit `7d235db`; ADR-005 consequences; CHANGELOG 0.5.0 preamble.
Usage numbers: not answerable, by design (no telemetry).

**2. `picks.json` cannot be rebuilt. Is that your backup story?**
Yes, said in three places: it is the one file worth backing up and there is
no per-pick undo. Putting it in a derived index would have made
`rm index.db` lose data silently. No backup tooling exists; it is a 0600 JSON
file. Evidence: ADR-009 "Where it is stored"; CHANGELOG known limitations;
`picks.py` docstring.

**3. The 0.575 MRR is a simulation you designed yourself.**
Correct, and both ADR-009 and CHANGELOG say "not a field study". The design
choices it drove are each pinned by a test, and with no picks the ordering is
identical to before, so the downside on day one is zero. A field measurement
has not been done. Evidence: ADR-009 context and cost sections;
`tests/test_picks.py`.

**4. macOS-only. That is a large share of developers.**
By decision, with the seam named: `keychain.py` is the whole platform
backend and `unavailable_reason()` is where another platform's answer goes;
`apple-fm-sdk` is optional. Capture, search, groups, import and MCP have no
macOS dependency in code, and nothing tests them off macOS: CI runs on
`macos-latest` only. Evidence: ADR-010 consequences; `ci.yml:17,44`.

**5. The ranking weights sum to 1.0 but were chosen by hand.**
Yes. The commit that fixed the normalisation says "the weights are a
judgement call and are documented as one; the normalisation is not, that was
the defect". The learned weights were measured and deliberately not adopted
because they are optimal only after picks accumulate. Evidence: `2450c64`;
ADR-009 "Adopt the learned weights wholesale"; `ranking.py`.

**6. The concept map is English-only.**
Yes, stated in the ADR: "English until someone translates it". Concepts and
stopwords are data layered from `~/.mem/concepts.json`, so a translation is
a JSON edit, but none exists and the recall fixture is English. Evidence:
ADR-011 consequences; `src/mem/concepts.json` (224 concepts, 127 stopwords).

**7. The MCP server is hand-rolled. What spec version, and who tests conformance?**
`2025-06-18`, with `2024-11-05` and `2025-03-26` accepted on negotiation.
Conformance is tested by the project's own 100 wire-level tests only; there
is no external suite and no test against a real client. Evidence:
`mcp.py:96-97`; `tests/test_mcp.py`; ADR-007 consequences ("we own protocol
conformance").

**8. 62 `xfail(strict)` bugs in one release. What was the process failure?**
Eight days in March, 60 commits, 14 version bumps, features shipped with no
CLI tests, no `~/.mem` isolation in tests (a test that forgot the fixture
read the developer's real history), and a background spawn that never
executed anything. The July audit turned 151 findings into 62 strict xfails
so the roadmap lived in CI, then closed them. Zero xfail markers remain.
Evidence: `7d235db`, `e7f311e`; CHANGELOG 0.5.0.

**9. Hooks ship as package data. What about users on the old install?**
Their rc line re-evaluates `mem init zsh` on every shell start, so upgrading
the binary is the whole migration. But no user has the new hooks, because
0.5.0 was never published (question 11). Evidence: `08879cf`; README install
section; the tag/PyPI/tap state above.

**10. You catch `Exception` in twenty places.**
Twenty sites, dispositioned one by one in AUDIT-2026-09.md item 7: seven in
`storage.py` wrap Pydantic validation and narrow to `ValueError`; four in
`capture.py` protect the capture path and now name what they absorb; the
rest carry the concrete failure in a comment. The `TODO(0.16)` in
`pyproject.toml` described this work and sat for eight weeks. Evidence:
`pyproject.toml`; `60e8bc3` (the `except: pass` that hid the dead sync).

**11. The Homebrew formula is stuck at 0.4.1 while PyPI is at 0.5.0.**
The premise is wrong in the other direction: PyPI is also 0.4.1, the latest
GitHub release is v0.4.1 (2026-03-13), and no `v0.5.0` tag exists locally or
on `origin`. The release commit is on master; the tag that triggers
`release.yml` was never pushed. Nothing in the 0.5.0 changelog has reached
any user through any channel. Why the tag was not pushed is not answerable
from the repo. Evidence: `git tag`, `git ls-remote --tags origin`,
`pip index versions cli-mem`, `gh release list`, the tap's `Formula/mem.rb`.

**12. Coverage is 91 % but `fail_under` is disabled with a TODO.**
`pyproject.toml` says "enable once the follow-up branches land the missing
tests; target 80". The branches landed (PRs #9–#29), the TODO was not
revisited, and today the floor would pass by 11 points. Evidence:
`pyproject.toml` coverage section; `7d235db`.

**13. No Windows or Linux, and no fish keybinding?**
Fish has the keybinding (`mem.fish` binds `\cr` in both key tables) and
fish's `$CMD_DURATION` was the reference the other shells were brought up
to. Windows is out of scope by design; Linux is untested rather than
excluded, with the Keychain the one subsystem that hard-fails. Evidence:
`src/mem/hooks/mem.fish`; README platform table; ADR-010.

**14. Capture spawns a Python interpreter on every prompt. What is the real cost?**
150–170 ms wall per prompt in the background: ~112 ms importing `mem.cli`
(58 ms Pydantic) plus one `git rev-parse`. The "<5 ms" in three documents is
the foreground cost of forking a disowned job; no test measures 5 ms, and
the one perf test asserts ≤ 1 s over 12 commands and has failed on CI at
1.39 s. `_entry.py`'s fast path covers `tui` only. Evidence: measured today;
`tests/test_hooks_e2e.py::TestHookPerformance`; `_entry.py:27`.

**15. What happens at one million commands?**
Retention keeps 90 days of captures and exempts imports, so 1M is reachable
only by a large import. ADR-005 measured the scan at ~1.45 ms per 1,000
(~1.45 s at 1M before the prefilter; 814 → 293 ms at 500k with it) and the
unbuilt index at 6–15 ms per query, 8.4 s to rebuild, 90.8 MB. The finder
reads raw text at ~20 ms per 100k, so ~200 ms per keystroke at 1M. Not
measured in the current code beyond the 20k perf fixture. Evidence: ADR-005;
`storage.py` `rotate`; `803bf7a`.

---

## Three things the owner should say they would change

**1. Put `_capture` on the fast path, or make it stdlib-only.** The same
ADR that set the finder's 22 ms budget states the import floor is 120 ms,
and `_capture` runs through that floor on every prompt: 150–170 ms of CPU
per command plus a `git` subprocess, background or not. The perf test
already documents "~150 ms of Python interpreter startup" and has failed on
CI. The commit that built the fast path said a second dispatch target is
four lines. This is also the only thing that would make the "<5 ms" claim in
three documents honest. Evidence: `_entry.py`; `tests/test_hooks_e2e.py`;
ADR-005, ADR-008; the measurement above.

**2. Push the v0.5.0 tag, then fix the release process that let a merged
release commit sit unpublished for seven weeks.** Every feature the defence
rests on is unreleased. `CLAUDE.md` step 4 of the release process is manual;
`release.yml` runs only on a tag; the PyPI token is a long-lived secret with a
standing TODO to move to Trusted Publishing. The release path has the least
automation of anything in the repository. Evidence: the tag/PyPI/tap state;
`CLAUDE.md` Release Process; `release.yml`.

**3. Turn the two TODOs from July into mechanical guards: re-enable
`fail_under` and adopt ruff's BLE001/S110 with per-site `noqa`.** Both were
written in the same week (`7d235db`, `60e8bc3`), both name exact sites, both
were deferred to "the follow-up branches", all of which merged. The pattern
that hid the dead sync for four months (`except Exception: pass`, exit 0)
is the pattern CHANGELOG says dominated this codebase ("most of these were
silent"), and a lint is cheaper than a second audit. Evidence:
`pyproject.toml` TODOs; CHANGELOG 0.5.0 "Fixed" preamble; AUDIT-2026-09.md
item 7.

Considered and not chosen, for the record: replacing Pydantic (the 58 ms is
real, but the query-time cost is 2 ms and the hot path already avoids it),
and building the ADR-005 index (the ADR itself says the index without the
fast path "fixes the part of the latency the user notices least", and
retention caps captured history at 90 days).
