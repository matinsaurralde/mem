"""Correction mining — "last time this failed, you fixed it with Y".

Every other tool in this space has half the picture. Rule-based correctors
(``thefuck``) know that ``git psuh`` should be ``git push``, but have never
seen your history, so they cannot know that *your* fix for a rejected push in
this repo was ``git pull --rebase``. History tools (atuin, mcfly) have your
history but throw away the one field that makes it a debugging record: the
exit code.

mem has captured ``exit_code``, ``ts``, ``dir``, ``repo`` and ``session``
since its first commit, and until now search ignored all of them. This module
spends them. It looks for pairs where a command exited non-zero and was
followed, within seconds, by a near-variant of itself that exited zero — the
shape of a human reading an error message and typing the corrected line.

Deterministic by construction
-----------------------------

There is no inference here. A pair is admitted only on evidence that is
already in the store: two exit codes, two timestamps, and the textual
relationship between two command lines. That matters because the failure mode
of this feature is not "no suggestion" — it is a *confident wrong*
suggestion. Proposing an unrelated command as "the fix" costs the user more
than silence does, so every threshold below is set to reject a doubtful pair
rather than to catch one more real one.

The rules, and why each number is what it is
--------------------------------------------

A candidate ``Y`` is accepted as the correction of a failure ``X`` when all of
the following hold:

``X`` failed and ``Y`` succeeded, both explicitly
    ``X.exit_code`` is a non-zero integer and ``Y.exit_code`` is exactly 0.
    ``None`` is not "probably fine": commands from ``mem import`` carry no
    exit code at all, and treating a missing code as success would mine a
    correction out of a file that never recorded whether anything worked.
    The practical consequence — imported history is invisible to ``mem fix``
    — is stated in the README rather than papered over.

``Y`` came at most :data:`MAX_LOOKAHEAD` commands later
    Three, not one. A human very often looks before they leap: the real
    sequence is ``pytest`` (fails), ``cat pytest.ini``, ``ls tests/``,
    ``pytest -c setup.cfg`` (works). Allowing two diagnostic commands in
    between keeps that case; allowing twenty would let an unrelated command
    five minutes of shell activity later be crowned "the fix".

``Y`` was typed within :data:`WINDOW_SECONDS` of ``X`` finishing
    Sixty seconds. A correction written while the error message is still on
    screen is typed in seconds; past a minute the user has read docs, opened
    a browser or switched task, and any causal link is a guess. The gap is
    measured as *think time* — see :func:`_think_seconds` — because mem
    timestamps a command when it *completes*, so a fix that itself takes two
    minutes to run would otherwise fall out of a window it never left.

``X`` and ``Y`` came from the same terminal
    Same ``session`` when both have one; the same ``dir`` when they do not
    (imported and pre-session captures). Two shell tabs working in one repo
    interleave their lines in a single history file, and a command from the
    other tab is not a correction — it is a coincidence.

``Y`` invokes the same program as ``X``
    Compared after stripping a leading ``sudo``/``doas``, which is what makes
    the single most valuable pair in this whole file minable: ``apt install
    foo`` (fails, exit 100) → ``sudo apt install foo`` (works). The one
    licensed exception is exit code 127, "command not found" — the only exit
    code that is hard evidence that ``argv[0]`` itself was the mistake. Only
    then may the program names differ, and only if one is a mistyping of the
    other (:func:`looks_like_typo`) — which is how ``gti status`` → ``git
    status`` is mined without opening the door to ``cat x`` → ``cd x``.

``Y`` is a near variant of ``X``
    See :func:`is_near_variant` and :func:`looks_like_typo`. Same-program
    alone is far too weak: ``git status`` failing and ``git push`` succeeding
    one second later is two events, not a correction. Notably this is *not*
    done with fuzzy string similarity, which measures the wrong thing —
    ``test_a.py``/``test_b.py`` scores higher than ``psuh``/``push`` — but by
    classifying the *shape* of the edit.

``Y`` is not textually identical to ``X``
    A command that fails and then succeeds unchanged is a flake — a network
    blip, a lock that cleared — not a fix. Printing "the fix is: run it
    again" would be noise, and an identical success also *ends* the lookahead:
    whatever comes after it is a new activity, not a repair.

What this deliberately does not find
------------------------------------

Stated rather than hidden, because a mining rule's misses are as much a part
of its contract as its hits:

- **Imported history.** ``mem import`` cannot recover exit codes from a shell
  history file, so nothing imported is ever paired.
- **Substituted words.** ``git push`` → ``git pull`` is a real fix and is not
  mined, because the same rule that admitted it would admit ``pytest
  test_a.py`` → ``pytest test_b.py``.
- **Fixes typed more than a minute later**, in another terminal, or after
  three intervening commands.
- **Multi-step repairs.** ``npm ci`` then ``npm run build`` is recorded as a
  fix for whichever single command failed, not as a two-command procedure —
  that is what ``mem save``/``mem group`` are for.

Confidence is repetition
------------------------

A pair seen five times is a different claim from a pair seen once, and the
output says which (:func:`confidence`). This is the one place where the
feature can be honest cheaply, and it is why ``occurrences`` is the primary
sort key rather than recency.

Nothing here executes anything, and there is no code path that could: the
module imports no subprocess machinery. ``mem fix`` prints a command for a
human to read and decide on, for the same reason ``mem``'s finder does — a
tool that runs commands on your behalf is how people delete the wrong branch.
"""

from __future__ import annotations

import difflib
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Iterable, Iterator, Sequence

from mem import storage
from mem.models import CapturedCommand
from mem.variables import redact_secrets

# --- thresholds ------------------------------------------------------------
#
# Every number here is a false-positive control. Raising one loses real pairs;
# lowering one invents fake ones, which is the strictly worse failure. The
# reasoning for each is in the module docstring.

#: Maximum think time between a failure completing and its correction being
#: typed, in seconds.
WINDOW_SECONDS = 60

#: How many commands after a failure may be considered its correction.
MAX_LOOKAHEAD = 3

#: Longest length difference two tokens may have and still be called a
#: mistyping of one another. Two, so ``prd``/``prod`` and ``instal``/``install``
#: qualify while ``k``/``kubectl`` (an unset alias, not a typo) does not.
MAX_TYPO_LENGTH_DELTA = 2

#: How many tokens may be rewritten between a failure and its fix. One. A
#: person correcting an error message changes the thing the message named;
#: someone who changed two unrelated tokens has moved on to a different
#: command, and pairing those is how a suggestion engine starts lying.
MAX_SUBSTITUTIONS = 1

#: "command not found". The only exit code that identifies ``argv[0]`` itself
#: as the mistake, and therefore the only one under which ``mem fix`` will
#: pair two different programs.
COMMAND_NOT_FOUND = 127

#: Occurrence counts at which the wording of the claim changes.
STRONG_EVIDENCE = 3
MODERATE_EVIDENCE = 2

#: Program names that are a modifier rather than the command being run.
_PRIVILEGE_PREFIXES = frozenset({"sudo", "doas"})


# --- shared history scanning ----------------------------------------------
#
# `mem.mcp`'s recent_failures tool mines the same two facts this module does
# (a non-zero exit code, and what capture order put after it). It calls the
# helpers below rather than keeping a second copy: this codebase has already
# paid twice for duplicated logic — a stale shell hook shipped to every pip
# user, and a ranking formula that could drift between call sites — and a
# second failure scanner would drift the same way.


def iter_histories(
    include: Callable[[str], bool] | None = None,
) -> Iterator[tuple[str, list[CapturedCommand]]]:
    """Yield ``(storage key, commands in capture order)`` per history file.

    Order is file order, not timestamp order, and deliberately so: append
    order *is* the order the user typed things, which is the signal both
    callers are reading. Sorting by ``ts`` would look tidier and would
    reorder the one thing that must not be reordered.

    ``include`` filters on the file stem (the storage key). It is a predicate
    rather than a repo path because the two callers resolve a repo to a key
    differently, and unifying that resolution is a separate change from
    unifying this scan.
    """
    repos_dir = storage.MEM_DIR / "repos"
    if not repos_dir.exists():
        return
    for path in sorted(repos_dir.glob("*.jsonl")):
        if include is not None and not include(path.stem):
            continue
        yield path.stem, list(storage.read_commands(path.stem))


@dataclass(frozen=True)
class Failure:
    """One non-zero exit, with the commands capture order put after it."""

    index: int
    command: CapturedCommand
    following: tuple[CapturedCommand, ...]


def iter_failures(
    commands: Sequence[CapturedCommand], lookahead: int = MAX_LOOKAHEAD
) -> Iterator[Failure]:
    """Yield each explicitly-failed command with the next *lookahead* commands.

    "Explicitly" excludes ``exit_code is None``: an imported history line
    records no exit code, and reporting it as a failure would invent one.
    """
    for index, cmd in enumerate(commands):
        if cmd.exit_code is None or cmd.exit_code == 0:
            continue
        yield Failure(
            index=index,
            command=cmd,
            following=tuple(commands[index + 1 : index + 1 + lookahead]),
        )


# --- textual comparison ----------------------------------------------------


@lru_cache(maxsize=4096)
def normalized_tokens(command: str) -> tuple[str, ...]:
    """Split a command line into comparison tokens.

    Two normalizations, both narrow on purpose:

    - ``shlex`` splitting, so ``git commit -m "wip"`` compares as four tokens
      rather than by raw characters. Unbalanced quotes raise, and a shell
      history is full of half-typed lines, so that falls back to a plain
      whitespace split instead of discarding the command.
    - A leading ``sudo``/``doas`` is dropped, which is what lets
      ``apt install x`` and ``sudo apt install x`` be recognised as the same
      command modulo privilege — the pair this feature most wants to find.

    Explicitly *not* normalized: ``argv[0]`` keeps its path. Reducing
    ``./scripts/a/run.sh`` to ``run.sh`` would make it indistinguishable from
    ``./scripts/b/run.sh``, and "the fix is the other project's deploy
    script" is the precise kind of confident-wrong answer this module refuses
    to produce.

    Cached because mining compares each command against several neighbours
    and ``shlex`` is the most expensive thing in the loop.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    start = 0
    while start < len(tokens) and tokens[start] in _PRIVILEGE_PREFIXES:
        start += 1
    return tuple(tokens[start:])


def _is_flag(token: str) -> bool:
    """True if a token is an option rather than a thing being operated on.

    A bare ``-`` is not a flag: it means stdin, and dropping it changes what
    the command reads.
    """
    return len(token) > 1 and token.startswith("-")


def _is_subsequence(short: str, long: str) -> bool:
    """True if every character of *short* appears in *long*, in order."""
    remaining = iter(long)
    return all(char in remaining for char in short)


def looks_like_typo(wrong: str, right: str) -> bool:
    """True if *right* is *wrong* mistyped, rather than a different thing.

    This function is the whole false-positive story of ``mem fix``, so the
    reasoning is worth stating in full.

    The obvious implementation is fuzzy string similarity, and it does not
    work. Measured with ``difflib`` on real command tokens:

    ==============================  =====  ==========================
    pair                            ratio  what it actually is
    ==============================  =====  ==========================
    ``test_a.py`` / ``test_b.py``   0.89   two different test files
    ``prod-1`` / ``prod-2``         0.83   two different hosts
    ``psuh`` / ``push``             0.75   a typo
    ``push`` / ``pull``             0.50   two different subcommands
    ==============================  =====  ==========================

    The two things that must be separated are *ordered the wrong way round*
    by similarity: the sibling filenames score higher than the genuine typo.
    No threshold on that column can work, and one tuned to catch ``psuh``
    would confidently report "the fix for ``pytest test_a.py`` is ``pytest
    test_b.py``" — a wrong answer delivered with a straight face, which is
    the exact failure this feature cannot afford.

    What separates them is not *how much* changed but *what shape* the change
    has. A mistyping is a finger error: a transposition, a doubled or dropped
    character, an omitted letter. Choosing a different target is a
    substitution: same shape, different content. So:

    - **Transposition** — the two tokens are anagrams (``psuh``/``push``,
      ``docekr``/``docker``, ``mian``/``main``, ``sl``/``ls``).
    - **Insertion or deletion** — the shorter is a subsequence of the longer,
      within :data:`MAX_TYPO_LENGTH_DELTA` characters (``buld``/``build``,
      ``prd``/``prod``, ``instal``/``install``, ``hostt``/``host``).

    Every substitution is rejected, which costs real pairs — ``git push`` →
    ``git pull`` is not mined — and that price is paid deliberately. Missing
    a fix is invisible; inventing one is not.
    """
    if wrong == right:
        return False
    if sorted(wrong) == sorted(right):
        return True

    longer, shorter = (wrong, right) if len(wrong) > len(right) else (right, wrong)
    if not shorter or len(longer) - len(shorter) > MAX_TYPO_LENGTH_DELTA:
        return False
    return _is_subsequence(shorter, longer)


def is_near_variant(failed: Sequence[str], fix: Sequence[str]) -> bool:
    """True if *fix* looks like *failed* re-typed rather than a new thought.

    The two token lists are aligned with ``difflib`` and every edit between
    them is classified. An edit is admissible only if it is one of:

    **An insertion.** The user kept everything they had typed and added to
    it: ``docker ps`` → ``docker ps -a``, ``npm i`` → ``npm i
    --legacy-peer-deps``, ``kubectl logs api`` → ``kubectl logs api -n
    prod``. Intent is preserved by construction, which is what makes this the
    safest edit to accept unconditionally.

    **A deletion of flags only.** Dropping ``--frozen-lockfile`` is a fix.
    Dropping an operand is not: ``ls /nope`` → ``ls`` succeeded because it
    stopped doing the thing that was asked for, and reporting that as "the
    fix" would be actively misleading.

    **One token mistyped**, in the strict structural sense of
    :func:`looks_like_typo`, and at most :data:`MAX_SUBSTITUTIONS` of them.

    ``difflib`` merges adjacent edits into a single ``replace`` opcode, so
    ``npm run buld`` → ``npm run build --if-present`` arrives as "one token
    became two" rather than as a typo plus an insertion. Such a block is
    therefore re-aligned left-to-right by :func:`_replacement_is_admissible`
    before being judged, which is what keeps a genuinely common repair —
    correct the spelling *and* add the flag you also forgot — from being
    thrown away on a technicality of the diff algorithm.
    """
    matcher = difflib.SequenceMatcher(None, list(failed), list(fix), autojunk=False)
    substitutions = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or tag == "insert":
            continue
        if tag == "delete":
            if not all(_is_flag(token) for token in failed[i1:i2]):
                return False
            continue
        # tag == "replace"
        rewrites = _replacement_is_admissible(failed[i1:i2], fix[j1:j2])
        if rewrites is None:
            return False
        substitutions += rewrites
        if substitutions > MAX_SUBSTITUTIONS:
            return False

    return True


def _replacement_is_admissible(
    removed: Sequence[str], added: Sequence[str]
) -> int | None:
    """Judge one ``replace`` block. Returns its substitution count, or None.

    The block is re-aligned left to right, which is the order a person edits
    a command line in: the first removed token is judged against the first
    added one, and so on. Whatever is left over on the removed side is a
    deletion (flags only, per :func:`is_near_variant`) and whatever is left
    over on the added side is an insertion (always allowed).

    ``None`` means "not a correction" and is deliberately distinct from
    ``0``, which means "a block that changed nothing that counts as a
    rewrite" — collapsing the two would let a rejected block pass as free.
    """
    substitutions = 0
    for old, new in zip(removed, added):
        if old == new:
            continue
        if not looks_like_typo(old, new):
            return None
        substitutions += 1

    for leftover in removed[len(added) :]:
        if not _is_flag(leftover):
            return None

    return substitutions


def is_plausible_correction(failed: str, fix: str, exit_code: int | None) -> bool:
    """True if *fix* is admissible as the correction of *failed*.

    The textual half of the rule set; the caller still has to satisfy the
    temporal, session and exit-code conditions. Kept separate so it can be
    tested exhaustively against command pairs without constructing history.
    """
    if failed == fix:
        return False

    failed_tokens = normalized_tokens(failed)
    fix_tokens = normalized_tokens(fix)
    if not failed_tokens or not fix_tokens:
        return False

    if failed_tokens[0] != fix_tokens[0]:
        # Different programs. Only exit 127 makes that a correction rather
        # than a change of subject, and even then the two names must stand in
        # the same mistyping relationship any other token would have to.
        if exit_code != COMMAND_NOT_FOUND:
            return False
        if not looks_like_typo(failed_tokens[0], fix_tokens[0]):
            return False
        # Forgive the program name and judge the arguments on their own; the
        # exit code has already accounted for the difference.
        failed_tokens = (fix_tokens[0], *failed_tokens[1:])

    return is_near_variant(failed_tokens, fix_tokens)


def _think_seconds(failed: CapturedCommand, candidate: CapturedCommand) -> int:
    """Seconds between *failed* finishing and *candidate* being typed.

    mem timestamps a command when the shell hook fires, i.e. when it
    *completes*. Naively subtracting timestamps therefore charges the
    candidate's own runtime to the user's thinking time, and a correction that
    happens to be a four-minute ``docker build`` would be discarded for being
    slow rather than for being unrelated. Subtracting the known duration
    measures what the window is actually about: how long the human paused.

    Integer seconds against millisecond durations, so the result can land one
    second low; that slack is harmless against a 60-second window.
    """
    gap = candidate.ts - failed.ts
    if candidate.duration_ms:
        gap -= candidate.duration_ms // 1000
    return gap


def _same_terminal(failed: CapturedCommand, candidate: CapturedCommand) -> bool:
    """True if both commands plausibly came from the same shell.

    Session id when both have one — it is the only field that actually
    identifies a terminal. Falling back to the working directory when either
    is missing keeps pre-session captures usable while still rejecting the
    common interleaving case, two tabs in one repo.
    """
    if failed.session and candidate.session:
        return failed.session == candidate.session
    return failed.dir == candidate.dir


# --- mining ----------------------------------------------------------------


@dataclass(frozen=True)
class Correction:
    """One observed ``failure → fix`` pairing, aggregated over history.

    ``occurrences`` is the whole confidence story: the same pair mined five
    times is five independent instances of a human making a mistake and
    correcting it the same way.

    ``fix_failures`` counts times the *fix* command itself exited non-zero
    after this pairing was last seen. A fix that has since stopped working is
    still worth showing — it is the best evidence available — but presenting
    it without that caveat would be dishonest.
    """

    failed: str
    fix: str
    occurrences: int
    first_seen: int
    last_seen: int
    exit_code: int | None
    repo: str | None
    fix_failures: int = 0

    @property
    def confidence(self) -> str:
        """``strong`` / ``moderate`` / ``weak``, from :data:`occurrences`."""
        return confidence(self.occurrences)


def confidence(occurrences: int) -> str:
    """Label an occurrence count.

    Three is "strong" because two is where coincidence stops being a
    reasonable explanation and three is where it stops being an interesting
    one. The label exists so the output can make a proportionate claim
    instead of asserting the same certainty for one observation and fifty.
    """
    if occurrences >= STRONG_EVIDENCE:
        return "strong"
    if occurrences >= MODERATE_EVIDENCE:
        return "moderate"
    return "weak"


def _find_fix(failure: Failure) -> CapturedCommand | None:
    """The first admissible correction among a failure's following commands.

    First, not best: the commands in between are the user diagnosing, and the
    first thing they run that both resembles the failure and works is the
    thing that worked. Scanning further for a "better" match would mean
    preferring a later command over the one that actually resolved it.
    """
    for candidate in failure.following:
        if candidate.ts < failure.command.ts:
            continue  # out-of-order line; a fix cannot precede its failure
        if _think_seconds(failure.command, candidate) > WINDOW_SECONDS:
            break  # the window only widens from here
        if not _same_terminal(failure.command, candidate):
            continue
        if candidate.exit_code != 0:
            continue
        if candidate.command == failure.command.command:
            # Succeeded unchanged: the failure was transient, not fixed.
            # Anything after this is new work, so stop looking.
            break
        if is_plausible_correction(
            failure.command.command, candidate.command, failure.command.exit_code
        ):
            return candidate
    return None


def mine(commands: Sequence[CapturedCommand]) -> list[Correction]:
    """Extract every ``failure → fix`` pairing from one history file.

    Aggregation is by exact command text on both sides. Normalizing before
    counting (folding ``sudo apt update`` into ``apt update``) would inflate
    occurrence counts by merging pairs the user experienced as distinct, and
    ``occurrences`` is the number the output makes its claim from.
    """
    pairs: dict[tuple[str, str], list[CapturedCommand]] = {}
    for failure in iter_failures(commands):
        fix = _find_fix(failure)
        if fix is None:
            continue
        pairs.setdefault((failure.command.command, fix.command), []).append(
            failure.command
        )

    if not pairs:
        return []

    # When did each command text fail? Used to answer "has this fix itself
    # broken since?" without a second pass per pair.
    failure_times: dict[str, list[int]] = {}
    for cmd in commands:
        if cmd.exit_code:
            failure_times.setdefault(cmd.command, []).append(cmd.ts)

    corrections = []
    for (failed_text, fix_text), instances in pairs.items():
        timestamps = [c.ts for c in instances]
        last_seen = max(timestamps)
        newest = max(instances, key=lambda c: c.ts)
        corrections.append(
            Correction(
                failed=failed_text,
                fix=fix_text,
                occurrences=len(instances),
                first_seen=min(timestamps),
                last_seen=last_seen,
                exit_code=newest.exit_code,
                repo=newest.repo,
                fix_failures=sum(
                    1 for ts in failure_times.get(fix_text, ()) if ts > last_seen
                ),
            )
        )
    return corrections


def merge(corrections: Iterable[Correction]) -> list[Correction]:
    """Combine per-repo corrections describing the same pairing.

    A fix learned in one repository is still the fix in another — ``docker``
    and ``kubectl`` do not care which checkout the terminal was in — so the
    same pair mined from two history files is one piece of evidence seen
    twice, and its occurrence count has to say so.
    """
    merged: dict[tuple[str, str], Correction] = {}
    for correction in corrections:
        key = (correction.failed, correction.fix)
        existing = merged.get(key)
        if existing is None:
            merged[key] = correction
            continue
        newer = correction if correction.last_seen >= existing.last_seen else existing
        merged[key] = Correction(
            failed=correction.failed,
            fix=correction.fix,
            occurrences=existing.occurrences + correction.occurrences,
            first_seen=min(existing.first_seen, correction.first_seen),
            last_seen=max(existing.last_seen, correction.last_seen),
            exit_code=newer.exit_code,
            repo=newer.repo,
            fix_failures=existing.fix_failures + correction.fix_failures,
        )
    return list(merged.values())


def rank(corrections: Iterable[Correction]) -> list[Correction]:
    """Order candidate fixes best-first.

    The rule, in order:

    1. **Most occurrences.** Repetition is the only evidence of correctness
       this module has. A pair seen four times beat coincidence four times.
    2. **Fewest failures since.** Between equally-attested fixes, prefer the
       one that has not broken since it last worked.
    3. **Most recent.** Environments drift; the newer observation describes
       the machine as it is now.
    4. **Command text**, so that a tie produces the same answer twice.
    """
    return sorted(
        corrections,
        key=lambda c: (-c.occurrences, c.fix_failures, -c.last_seen, c.fix),
    )


def mine_all(
    include: Callable[[str], bool] | None = None,
) -> tuple[list[Correction], list[CapturedCommand]]:
    """Mine every history file. Returns ``(corrections, failures)``.

    Both halves come from one pass because ``mem fix`` needs both and the scan
    is the expensive part: the failures answer "what broke?" and the
    corrections answer "what fixed it?", and reading the store twice to
    produce them would double the cost of the only slow thing this command
    does.
    """
    corrections: list[Correction] = []
    failures: list[CapturedCommand] = []
    for _key, commands in iter_histories(include):
        corrections.extend(mine(commands))
        failures.extend(
            cmd for cmd in commands if cmd.exit_code is not None and cmd.exit_code != 0
        )
    return merge(corrections), failures


# --- selection -------------------------------------------------------------


def matches(command: str, terms: Sequence[str]) -> bool:
    """True if *command* contains every term, case-insensitively.

    Same all-terms-must-appear contract as ``mem`` search, so that
    ``mem fix npm build`` narrows to what the user expects rather than
    matching anything mentioning either word.
    """
    lowered = command.lower()
    return all(term in lowered for term in terms)


def select_failure(
    failures: Sequence[CapturedCommand],
    query: str | None = None,
    current_repo: str | None = None,
) -> CapturedCommand | None:
    """Pick which failure ``mem fix`` is about.

    Most recent wins, but the current repository is consulted first: someone
    typing ``mem fix`` has just broken something *here*, and answering with a
    failure from another checkout would be technically-most-recent and
    practically useless. Falling back to the whole store rather than giving up
    keeps the command useful outside a repo and right after a ``cd``.
    """
    candidates = list(failures)
    if query:
        terms = [t for t in query.lower().split() if t]
        candidates = [c for c in candidates if matches(c.command, terms)]
    if not candidates:
        return None

    local = [c for c in candidates if current_repo and c.repo == current_repo]
    return max(local or candidates, key=lambda c: c.ts)


def fixes_for(
    corrections: Sequence[Correction], failed_command: str
) -> list[Correction]:
    """Every recorded fix for one exact failing command line, best first."""
    return rank(c for c in corrections if c.failed == failed_command)


# --- report ----------------------------------------------------------------


@dataclass(frozen=True)
class FixReport:
    """What ``mem fix`` found: the failure it is about, and the fixes seen."""

    failure: CapturedCommand | None
    fixes: list[Correction]
    query: str | None = None


def build_report(
    query: str | None = None, current_repo: str | None = None, limit: int = 3
) -> FixReport:
    """Mine the store and answer one ``mem fix`` invocation."""
    corrections, failures = mine_all()
    failure = select_failure(failures, query=query, current_repo=current_repo)
    if failure is None:
        return FixReport(failure=None, fixes=[], query=query)
    return FixReport(
        failure=failure,
        fixes=fixes_for(corrections, failure.command)[:limit],
        query=query,
    )


def _iso(ts: int) -> str:
    """Format an epoch timestamp as UTC ISO-8601, for machine consumers."""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _redact(value: Any) -> Any:
    """Recursively redact every string in a JSON-shaped structure.

    Applied to the whole payload at one choke point rather than field by
    field, for the same reason ``mem.mcp`` does it: a per-field call is a rule
    a future field can forget, and the cost of forgetting is a printed
    credential. ``mem fix`` in particular quotes commands the user has
    forgotten they ran.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def report_payload(report: FixReport) -> dict[str, Any]:
    """Render a report as redacted, JSON-ready data.

    The single exit from this module. Both ``--json`` and the terminal
    rendering read this structure, so there is exactly one place where a
    command string can escape unredacted, and it is this function.
    """
    failure = report.failure
    payload: dict[str, Any] = {
        "query": report.query,
        "failure": None
        if failure is None
        else {
            "command": failure.command,
            "exit_code": failure.exit_code,
            "ts": failure.ts,
            "when": _iso(failure.ts),
            "repo": failure.repo,
            "dir": failure.dir,
        },
        "count": len(report.fixes),
        "fixes": [
            {
                "command": c.fix,
                "occurrences": c.occurrences,
                "confidence": c.confidence,
                "first_seen": c.first_seen,
                "last_seen": c.last_seen,
                "last_seen_iso": _iso(c.last_seen),
                "failed_since": c.fix_failures,
                "repo": c.repo,
            }
            for c in report.fixes
        ],
    }
    return _redact(payload)
