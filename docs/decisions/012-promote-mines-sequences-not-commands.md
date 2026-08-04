# ADR-012: Promote Mines Sequences, and Ranking Is What Makes It Trustworthy

**Status**: Accepted
**Date**: 2026-08-04

## Context

mem has two halves that never met. One captures every command you type. The other — named groups — replays runbooks with variables filled in, and is the most original thing in the product. It is also barely used, because creating a group requires remembering to run `mem save` at exactly the moment you are busy doing the thing it would save. The sequence you repeat six times a month never becomes a runbook, because you only recognise it as a sequence in hindsight.

`mem promote` is the hindsight: find command sequences that recur across separate work sessions, work out which argument changed between runs, and offer the result as a group with that argument already a `$VAR`.

The failure mode that matters is not missing a sequence. It is proposing junk. A suggestion list that offers nonsense is dismissed once and never opened again, and no amount of correct behaviour afterwards recovers it. So this was built the way `mem fix` was: pick the thresholds from a measurement, and report the false-positive rate rather than asserting one.

## The measurement

Eight hand-designed 45-day histories, ~1,100 commands each. Every session is one or two *activities*, each labelled by hand:

- **workflow** — a deliberate ordered procedure with varying arguments (deploy, release, migrations, plan-and-apply, namespace check). The ground truth.
- **habit** — a fixed-shape sequence genuinely repeated but a poor runbook, because its content is typed fresh each time. `git add -A; git commit -m …; git push` is the archetype.
- **ad-hoc** — debugging, container poking, log trawling. Modelled as a random-length, random-order draw from a pool of related commands, **because that is what makes it ad-hoc**: real exploration has a stable vocabulary and no stable shape. Anything mined out of one of these is a false positive.

35% of sessions run two activities back to back with no idle gap, so the miner gets every opportunity to splice the tail of one onto the head of the next.

| configuration | FP rate, top 5 | workflows surfaced |
|---|---|---|
| **as shipped** | **0.0%** (0/40) | **88%** |
| whole mined list (~12/history) | 42.3% | — |
| top 8 instead of top 5 | 12.5% | — |
| **without the inspection-command filter** | **80.0%** | collapses |
| `MIN_OCCURRENCES = 2` | 0.0% shown, but the mined pool goes 97 → 223 and its FP rate 42% → 75% | — |
| ranking on occurrences alone | 0.0% | 80% |

Mining costs ~15 ms per history.

## Decision

### Sessions are re-derived from the command files, not read from the session files

`SessionTracker` already writes `WorkSession` records and they are the obvious input. They are not used, for four reasons that all point the same way: they carry no exit codes (so a sequence of *failed* commands would be proposed as a runbook), their `started_at` is documented in `capture.py` as "approximate", they are rotated after 30 days while commands are kept for 90, and `mem import` writes none at all — so the user who has just imported ten years of `.zsh_history`, the one with most to gain, would get nothing.

The boundary rule is the tracker's — 300 idle seconds or a change of repository — with one correction that is not cosmetic. **mem timestamps a command's completion, so a bare timestamp difference charges a command's own runtime to the user's idle time.** A six-minute `docker build` read as six minutes of distraction and ended the session, which cut every deploy sequence in half at precisely its slowest step — that is, cut exactly the sequences this feature exists to find. The threshold is unchanged; what is measured against it is think time. This is the third place this bug class has appeared, after the shell hooks and `mem fix`'s correction window, and it is now called out in the code.

### Inspection commands are deleted, and the survivors must be strictly contiguous

This is how "interleaved noise does not break a sequence" is implemented, and the alternative was measured. Allowing *gaps* means any two commands can be called adjacent if enough unrelated ones separate them. Deleting the noise instead keeps the contiguity requirement intact, and the list is deliberately short, boring, and read-only — with a guard that matters more than the list: a command containing a pipe, a redirection or a separator is never called noise, because `cat x` looks but `cat x > y` is a step.

**Removing this filter takes the top-5 false-positive rate from 0% to 80%.** The feature stops working. It is the one result a future contributor is most likely to undo by simplifying, so it is pinned as a test.

### "The same sequence" is decided by shape, then by the values observed

A command is reduced to a shape where argument positions become holes. Two rules decide which positions may be holes:

- **Never `argv[0]`, never the token after it, never a flag name.** That single rule separates `git checkout main` / `git checkout staging` (index 2 varies — a branch, so a variable) from `git push` / `git pull` (index 1 varies — a different intent, so not the same sequence at all). The subcommand is the verb; the verb is not a parameter. `sudo`/`doas` shift the window right.
- **A command containing shell grammar is never generalised.** mem does not parse shell, so it cannot know which side of a pipe an operand belongs to. Such commands still match themselves byte for byte.

Which holes *actually* varied is then read off the occurrences: a hole with one observed value is a literal. Surviving holes are grouped into variables **by the value vector they take, not by position**. `kubectl config use-context prod`, `apply -n prod` and `rollout status -n prod` have three varying positions that all change together to the same value — that is one parameter used three times, and counting positions would reject the single most useful shape this feature has. At most two variables: three independent holes is a template with more holes than content.

### Three distinct sessions, not three repetitions

The same number `mem fix` calls strong evidence. Repetitions *inside* one session are not counted — a build loop run eight times in one afternoon is one episode of work — so "you ran this 6 times" means six separate occasions. Dropping to two does not hurt the shown list, but more than doubles the mined pool (97 → 223) and takes its FP rate to 75%, which is a listing that only survives because ranking hides it.

### Ranking is `occurrences × length`, and the obvious choice is wrong

Every prefix of a six-step deploy recurs *at least as often as the deploy does*. So an occurrence-first order puts `docker build` / `docker push` (7 times) above the whole deploy it belongs to (6 times), and the listing fills with fragments of one workflow. Multiplying by length asks the question the user is actually asking — how much typing would this have saved — and the whole sequence wins it. Measured: 88% of planted workflows surfaced, against 80% for occurrence-first. Pinned as a test, because this reasoning is exactly the kind that is lost the moment it is not written down.

Two further passes: *closed* drops a sequence contained in an equally-attested longer one (it carries no evidence of its own), and *dominant* keeps one representative per family of overlapping sequences (it carries no attention of its own). A sequence that merely shares a command with another, without containing it, is a different procedure and both are shown.

### The default limit of 5 is part of the design

**0% false positives at 5; 12.5% at 8; 42% across the whole mined list.** Ranking, not filtering, is what makes this output trustworthy, so the limit is load-bearing and raising it trades directly against the one property the feature cannot afford to lose. Said in a comment on the option itself, where someone changing it will read it.

### Nothing is written without confirmation, nothing is ever executed

`mem promote` lists. `mem promote <n>` prints what it would save and asks. The module imports nothing that could run a command, and that is asserted structurally, as in `mem fix`.

A candidate whose text trips either of `variables.py`'s detectors — `looks_like_credential` or a difference under `redact_secrets` — is shown with a warning and **cannot be promoted**. There is no `--force`: a secret in a saved runbook is a second copy of it on disk, and the right answer is `mem save --var`. Displayed text goes through one redaction choke point, so there is exactly one place a command string can escape.

## Cost of this decision (stated plainly)

- **It proposes the commit dance.** 12.5% of shown candidates were the `git add -A; git commit -m $MSG; git push` class: genuine repetition, poor runbook, because the whole point is the message you type fresh each time. mem cannot tell the difference from the outside, and it is documented in the README rather than hidden, because a user who sees it and is not warned will conclude the feature is broken.
- **The corpus is hand-designed, not a field study.** The activity mix and the noise ratio are informed guesses. The *mechanism* by which ad-hoc work differs from a procedure — no stable shape — is real, but nobody has yet run this against a stranger's actual year of history.
- **Variable names are derived, not understood.** A long flag names itself (`--namespace` → `$NAMESPACE`), version- and filename-shaped values are recognised, and everything else becomes `$CHECKOUT_ARG`-style: honest about knowing nothing. `-n` is deliberately not read as "namespace", because it is a line count to `head`. Users will rename.
- **Two-token commands can never be parameterised.** `cd $DIR`, `ssh $HOST` are unreachable, because protecting index 1 is what keeps `git push`/`git pull` apart. A knowingly paid price.
- **Evidence is not merged across repositories.** A runbook is made of *this* repository's branch names and script paths, so adding two repositories' counts would claim one workflow recurred six times when two workflows recurred three times each.

## Alternatives rejected

**Read `WorkSession` records.** Four independent reasons above; the decisive one is that imported history has no sessions.

**Allow gaps between steps instead of deleting noise.** Lets any two commands be called adjacent given enough unrelated ones between them — the coincidence this feature must refuse.

**Fuzzy similarity between command lines.** Already measured and rejected in `mem fix` (ADR-less, but pinned in `test_fix.py`): `test_a.py`/`test_b.py` scores *above* the genuine typo `psuh`/`push`, so no threshold on it separates a parameter from a different intent. The shape-based rule here is the same lesson applied to a different problem.

**Rank by occurrences.** Measured 8 points worse on workflow coverage, for the structural reason that fragments always out-count the whole.

**A `--force` to promote a candidate containing a credential.** Defeats the check entirely; `mem save --var` already does the right thing.
