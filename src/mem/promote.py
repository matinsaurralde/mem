"""Sequence mining — the runbook you already have but never wrote down.

``mem`` has two halves that never met. One captures every command you type.
The other, :mod:`mem.groups`, replays *named runbooks* with variables filled
in — the most original thing in the product, and the least used, because
building one requires remembering to run ``mem save`` at the exact moment you
are busy doing the thing. The sequence you repeat six times a month never
becomes a group, because you only notice it is a sequence in hindsight.

This module is the hindsight. It reads the history that is already on disk,
finds command sequences that recur across separate work sessions, works out
which argument changed between runs, and offers the result as a group with
that argument already turned into a ``$VAR``.

Why "the argument changed" is the point
---------------------------------------

A sequence that recurs *identically* is worth saving. A sequence that recurs
with one thing different every time — a namespace, a branch, a release tag —
is worth more, because that difference is precisely the parameter the runbook
should take. Generalising is therefore not a convenience here, it is the
feature.

It is also the danger. Generalise one token too eagerly and two unrelated
workflows collapse into one runbook that describes neither. ``git push`` and
``git pull`` differ by one token; so do ``kubectl delete pod a`` and
``kubectl delete pod b``. The first pair must never merge and the second
must.

The rules, and why each number is what it is
--------------------------------------------

**Sessions come from the history, not from the session files.**
:class:`mem.capture.SessionTracker` already writes ``WorkSession`` records,
and they are the obvious input. They are not used, for four reasons that all
point the same way: they carry no exit codes (so a sequence of commands that
*failed* would be proposed as a runbook), their ``started_at`` is documented
in ``capture.py`` as "approximate", they are rotated after 30 days while
commands are kept for 90, and ``mem import`` writes none at all — so the user
who has just imported ten years of ``.zsh_history`` and has the most to gain
would get nothing. Sessions are re-derived here from the command files using
the same rule the tracker uses (:data:`SESSION_IDLE_SECONDS`), against data
that is richer and lives longer.

**A session boundary is 300 idle seconds or a change of repository.**
Not a new threshold — :class:`mem.capture.SessionTracker` chose it and this
mirrors it, because a "session" that meant one thing in ``mem session`` and
another in ``mem promote`` would be a lie in one of the two places.

**Inspection commands are removed before mining.** ``ls``, ``cd``, ``cat``,
``git status`` and their kind are how you *look* at a repository, not how you
change it. They are also the overwhelming majority of a real history, and
mining with them in place produces runbooks whose steps are mostly ``ls``.
Removing them is what implements "interleaved noise does not break a
sequence": rather than allowing gaps — which lets any two commands be called
adjacent if enough unrelated ones separate them — the noise is deleted and
the remainder must be strictly contiguous. See :func:`is_inspection`, which
refuses to call anything noise if it contains a pipe, a redirection or a
command separator, because ``cat x > y`` is not an inspection.

**Failed commands are removed too.** A command that exited non-zero is not a
step in a working procedure. Commands with no exit code at all (imported
history) are kept: absence of evidence is not evidence of failure, and
excluding them would make this feature useless on imported history, which is
the opposite of the intent.

**Steps more than :data:`MAX_STEP_GAP_SECONDS` apart are not one procedure.**
Fifteen minutes. The session rule bounds the gap between *adjacent* commands
at five minutes, but once inspection commands are removed, two surviving
steps can be an hour of poking around apart. Measured as think time — the
gap minus the command's own runtime — for the reason :mod:`mem.fix` measures
it that way: a ten-minute ``docker build`` is a slow command, not a pause.

**A sequence is between :data:`MIN_SEQUENCE_LENGTH` and
:data:`MAX_SEQUENCE_LENGTH` steps.** Two, because ``terraform plan`` then
``terraform apply`` is a genuine runbook and a single command is what
``mem save`` is already for. Eight, because past that it is not a runbook,
it is a description of your afternoon — and nobody confirms a suggestion to
save eighteen commands they do not recognise.

**A sequence must recur in :data:`MIN_OCCURRENCES` distinct sessions.**
Three, the same number :mod:`mem.fix` calls strong evidence, and the single
most important false-positive control in this file. Two is entirely
compatible with coincidence: two commands that often follow one another will
follow one another twice. Repetitions *within* one session are deliberately
not counted — running a build loop eight times in one afternoon is one
episode of work, not eight — so "you ran this 6 times" means six separate
occasions.

**At most :data:`MAX_VARIABLES` variables.** Two. One variable is the
overwhelmingly common shape (an environment, a branch, a version). Two covers
``deploy $ENV $VERSION``. A "sequence" needing three independent holes to
match itself is a template with more holes than content, and the evidence
that those runs were the same workflow has evaporated.

**Only argument positions may become variables.** Never ``argv[0]``, never
the token after it, and never a flag name. That single rule is what separates
``git checkout main`` / ``git checkout staging`` (index 2 varies — a branch,
so a variable) from ``git push`` / ``git pull`` (index 1 varies — a different
intent, so not the same sequence at all). The subcommand is the verb; the
verb is not a parameter. ``sudo``/``doas`` shift the protected window right,
so ``sudo systemctl restart nginx`` protects ``systemctl restart``.

**A command containing shell grammar is never generalised.** A pipe, a
redirection, ``&&``, a subshell: mem does not parse shell, so it cannot know
which side of a pipe an operand belongs to. Such commands still match
themselves byte for byte and can still be steps; they simply never grow a
hole. Guessing here would be guessing about the one construct where being
wrong rewrites what the command does.

**Positions are grouped into variables by the values they take, not by where
they are.** ``kubectl config use-context prod``, ``kubectl apply -n prod``
and ``kubectl rollout status -n prod`` have three varying positions, but all
three change together and to the same value, so they are *one* variable used
three times — which is exactly what the runbook needs. Counting positions
instead would reject the single most useful case this module has.

Overlapping candidates
----------------------

Every sub-sequence of a recurring sequence also recurs, at least as often. A
five-step deploy generates four four-step candidates, and offering all of
them is how a suggestion list becomes unreadable. Only *closed* candidates
survive :func:`closed`: a shorter sequence contained in a longer one with the
same occurrence count carries no evidence the longer one does not, so it is
dropped. If it occurs strictly more often it is kept, because then it really
is a more common workflow than its parent.

What this deliberately does not do
----------------------------------

- **It never creates anything.** :func:`build_report` reads. Writing a group
  happens only in the CLI, only for a numbered candidate, and only after an
  explicit confirmation.
- **It never executes anything.** As with ``mem fix``, there is no import in
  this module that could.
- **It refuses to store a credential.** A candidate whose text trips
  :func:`mem.variables.looks_like_credential` or the redactor is shown with a
  warning and cannot be promoted. There is no override flag: a secret in a
  saved runbook is a secret written to disk in a second place, and the right
  fix is ``mem save --var``, not a ``--force``.
- **Names are a starting point.** The suggested group and variable names are
  derived deterministically from the commands themselves (see
  :func:`suggest_name` and :func:`_variable_name`). They are not an attempt to
  understand what you were doing; ``mem group rename`` exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from mem import storage
from mem.fix import iter_histories
from mem.models import CapturedCommand, GroupFile
from mem.variables import EXCLUDED_SHELL_VARS, looks_like_credential, redact_secrets

# --- thresholds ------------------------------------------------------------
#
# Every number here trades a missed runbook against an invented one. A missed
# runbook costs the user nothing they had before; an invented one costs them
# their trust in the whole command, once, permanently. The reasoning for each
# is in the module docstring.

#: Idle seconds that end a work session. Mirrors
#: :class:`mem.capture.SessionTracker`, which owns this definition.
SESSION_IDLE_SECONDS = 300

#: Longest think-time gap between two consecutive steps of one procedure.
MAX_STEP_GAP_SECONDS = 900

#: Shortest and longest sequence that may be proposed as a group.
MIN_SEQUENCE_LENGTH = 2
MAX_SEQUENCE_LENGTH = 8

#: Distinct sessions a sequence must recur in before it is proposed.
MIN_OCCURRENCES = 3

#: Most variables a single candidate may carry.
MAX_VARIABLES = 2

#: Occurrence counts at which the wording of the claim changes. Same ladder as
#: ``mem fix``, because it is the same kind of claim about the same evidence.
STRONG_EVIDENCE = 6
MODERATE_EVIDENCE = 3

#: Programs that modify the command they precede rather than being it. The
#: protected prefix (argv[0] plus the subcommand) slides past them.
_PRIVILEGE_PREFIXES = frozenset({"sudo", "doas"})

#: Shell grammar. A command containing any of these is matched literally and
#: never generalised — see the module docstring.
_SHELL_GRAMMAR = ("|", ">", "<", "&&", "||", ";", "`", "$(", "&")

#: Marker for a token position that may vary between occurrences.
_HOLE = "\x00"

#: Prefix marking a shape that stands for one exact command line.
_OPAQUE = "\x01"


# --- tokenizing ------------------------------------------------------------
#
# Not ``shlex.split``. mem has to be able to rebuild the command line it
# proposes, byte for byte apart from the holes it punches, and shlex discards
# the quotes: ``git commit -m "wip thing"`` comes back as four tokens that
# rejoin into a five-word command that means something else. Spans are kept
# instead, so a template is the original text with substrings replaced.

_TOKEN = re.compile(r"""(?:[^\s"']|"[^"]*"|'[^']*'|["'])+""")


@dataclass(frozen=True)
class Token:
    """One command-line token and where it sits in the original string."""

    text: str
    start: int
    end: int


def tokenize(command: str) -> list[Token]:
    """Split a command line into tokens, keeping quoted runs intact.

    A balanced quoted run is one token including its quotes, so
    ``--message="a b"`` stays a single token and can be reassembled exactly.
    An unbalanced quote — ordinary in a half-typed history line — is matched
    literally rather than raising, because discarding the command would lose
    a step from the middle of an otherwise good sequence.
    """
    return [Token(m.group(0), m.start(), m.end()) for m in _TOKEN.finditer(command)]


def _is_flag(token: str) -> bool:
    """True if a token is an option rather than a thing being operated on.

    The same rule :mod:`mem.fix` applies, deliberately restated rather than
    imported: that module classifies ``shlex`` tokens with the quotes already
    stripped, this one classifies raw spans, and a shared helper would have to
    pretend those are the same kind of value. A bare ``-`` means stdin, not a
    flag.
    """
    return len(token) > 1 and token.startswith("-")


def _protected_prefix_length(tokens: Sequence[Token]) -> int:
    """How many leading tokens may never become a variable.

    Two — the program and the token after it — pushed right by any
    ``sudo``/``doas`` in front, so ``sudo systemctl restart nginx`` protects
    ``systemctl restart`` rather than ``sudo systemctl``.

    Protecting the second token is the rule that keeps ``git push`` and
    ``git pull`` apart. It costs the case where the second token really is a
    parameter (``cd $DIR``, ``ssh $HOST``), and that price is paid knowingly:
    for a two-token command the argument *is* the entire meaning, and a
    runbook step of ``cd $ARG`` says nothing at all.
    """
    offset = 0
    while offset < len(tokens) and tokens[offset].text in _PRIVILEGE_PREFIXES:
        offset += 1
    return offset + 2


def command_shape(command: str) -> tuple[str, ...]:
    """Reduce a command line to the pattern it would match.

    Literal tokens keep their text; a position that is allowed to vary becomes
    :data:`_HOLE`. A ``--flag=value`` token becomes ``--flag=`` plus a hole,
    so the flag name stays structure while its value may move.

    Two commands with equal shapes are candidates to be *the same step*; the
    decision of which holes actually vary is made later, from the values
    observed across occurrences (:func:`_analyse_slots`), because a hole that
    never changes is a literal and should stay one.

    A command containing shell grammar gets a one-element opaque shape and can
    therefore only ever match itself.
    """
    if any(marker in command for marker in _SHELL_GRAMMAR):
        return (_OPAQUE + command,)

    tokens = tokenize(command)
    protected = _protected_prefix_length(tokens)
    shape: list[str] = []
    for index, token in enumerate(tokens):
        if index < protected or not token.text:
            shape.append(token.text)
        elif _is_flag(token.text):
            name, sep, _value = token.text.partition("=")
            shape.append(name + sep + _HOLE if sep else token.text)
        else:
            shape.append(_HOLE)
    return tuple(shape)


def _slot_value(command: str, shape: tuple[str, ...], index: int) -> str:
    """The text occupying one hole of *shape* in *command*.

    For a whole-token hole that is the token; for a ``--flag=`` hole it is
    only the half after the ``=``, because the flag name is not part of what
    varies.
    """
    token = tokenize(command)[index]
    entry = shape[index]
    if entry == _HOLE:
        return token.text
    return token.text.partition("=")[2]


def _slot_span(command: str, shape: tuple[str, ...], index: int) -> tuple[int, int]:
    """Character range of one hole, for rewriting it into ``$VAR``."""
    token = tokenize(command)[index]
    if shape[index] == _HOLE:
        return token.start, token.end
    return token.start + len(shape[index]) - 1, token.end


# --- sessions --------------------------------------------------------------


def _think_seconds(previous: CapturedCommand, command: CapturedCommand) -> int:
    """Seconds the user paused between two commands.

    mem stamps a command when it *finishes*, so a raw timestamp difference
    charges a command's own runtime to the user's thinking time. Subtracting
    the known duration is what keeps a four-minute ``terraform apply`` from
    looking like four minutes of distraction. Same correction, and same
    reason, as :mod:`mem.fix`.

    Imported commands carry no duration, so the correction is a no-op for
    them and the gap is the raw one — which is the best available answer, not
    a guess dressed up as one.

    **Read this before writing anything else that reasons about elapsed
    time.** mem records a command's *completion*, so a bare ``b.ts - a.ts`` is
    not the time the user spent thinking — it includes however long ``b`` took
    to run. This is now the third place that has bitten: the shell hooks, the
    correction window in :mod:`mem.fix`, and session splitting here, where it
    silently cut every deploy sequence in half at its slowest step. Any new
    comparison against a duration threshold has to subtract ``duration_ms``.
    """
    gap = command.ts - previous.ts
    if command.duration_ms:
        gap -= command.duration_ms // 1000
    return max(gap, 0)


def split_sessions(
    commands: Sequence[CapturedCommand],
) -> list[list[CapturedCommand]]:
    """Group a history file's commands into work sessions, in capture order.

    The boundary rule is :class:`mem.capture.SessionTracker`'s: more than
    :data:`SESSION_IDLE_SECONDS` idle, or a change of repository.

    With one correction, and it is not a cosmetic one. The tracker subtracts
    raw completion timestamps, which charges a command's own runtime to the
    user's idle time — so a six-minute ``docker build`` reads as six minutes
    of distraction and ends the session. That is survivable for
    ``mem session``, whose job is a rough summary. It is fatal here, because
    the sequences this module exists to find are build and deploy procedures,
    which are made *of* the slowest commands a developer runs: every deploy
    would be cut in half at its slowest step. The threshold is unchanged; what
    is measured against it is think time (:func:`_think_seconds`), the same
    correction ``mem fix`` applies for the same reason.

    Two further additions the tracker does not need:

    - An explicit ``session`` id, when both commands carry one, wins over the
      timing heuristic. Nothing writes that field today, but it is the only
      field that actually identifies a terminal, and when it exists it is
      right where a gap is a guess.
    - A timestamp going backwards starts a new session. Interleaved lines from
      two shells appear that way, and stitching them into one sequence would
      invent a procedure nobody ran.
    """
    sessions: list[list[CapturedCommand]] = []
    current: list[CapturedCommand] = []
    for command in commands:
        if current:
            previous = current[-1]
            if previous.session and command.session:
                boundary = previous.session != command.session
            else:
                boundary = _think_seconds(previous, command) > SESSION_IDLE_SECONDS
            boundary = boundary or command.repo != previous.repo
            boundary = boundary or command.ts < previous.ts
            if boundary:
                sessions.append(current)
                current = []
        current.append(command)
    if current:
        sessions.append(current)
    return sessions


# --- noise -----------------------------------------------------------------
#
# A deliberately short, deliberately boring list. Its job is not to be
# complete — it is to remove the handful of commands that make up most of a
# real history and none of a runbook. Every entry is read-only and
# unconditionally safe to skip; anything with an argument that could make it
# write (a redirection, a pipe) is excluded by is_inspection() before the list
# is consulted at all.

_INSPECTION_PROGRAMS = frozenset(
    {
        "ls",
        "ll",
        "la",
        "cd",
        "pwd",
        "clear",
        "cat",
        "bat",
        "less",
        "more",
        "head",
        "tail",
        "tree",
        "which",
        "whoami",
        "whereis",
        "type",
        "file",
        "stat",
        "man",
        "tldr",
        "history",
        "echo",
        "printf",
        "exit",
        "logout",
        "df",
        "du",
        "top",
        "htop",
        "ps",
        "env",
        "printenv",
        "date",
        "uptime",
        "open",
        "code",
        "vim",
        "nvim",
        "vi",
        "nano",
        "emacs",
        "fzf",
        "mem",
    }
)

#: ``program subcommand`` pairs that only look. ``git branch`` is absent on
#: purpose — bare it lists, but ``git branch -d x`` deletes, and one entry
#: cannot mean both.
_INSPECTION_SUBCOMMANDS = frozenset(
    {
        ("git", "status"),
        ("git", "log"),
        ("git", "diff"),
        ("git", "show"),
        ("git", "blame"),
        ("git", "remote"),
        ("docker", "ps"),
        ("docker", "images"),
        ("brew", "list"),
        ("npm", "ls"),
        ("pip", "list"),
    }
)


def is_inspection(command: str) -> bool:
    """True if a command looks at the system rather than changing it.

    Inspection commands are dropped before mining, which is how interleaved
    noise stops breaking a sequence without any notion of a "gap": the
    surviving steps must still be strictly contiguous, so two genuinely
    unrelated commands can never be called adjacent merely because enough
    other work happened between them.

    The guard clause matters more than the lists. ``cat`` is an inspection;
    ``cat template.yaml > out.yaml`` is a step in a procedure, and any shell
    grammar at all is enough to disqualify a command from being called noise.
    """
    if any(marker in command for marker in _SHELL_GRAMMAR):
        return False
    tokens = [token.text for token in tokenize(command)]
    while tokens and tokens[0] in _PRIVILEGE_PREFIXES:
        tokens = tokens[1:]
    if not tokens:
        return True
    if tokens[0] in _INSPECTION_PROGRAMS:
        return True
    return len(tokens) > 1 and (tokens[0], tokens[1]) in _INSPECTION_SUBCOMMANDS


def steps_of(session: Sequence[CapturedCommand]) -> list[list[CapturedCommand]]:
    """Reduce one session to the runs of commands a runbook could be made of.

    Inspection commands and explicit failures are removed; what remains is
    split wherever two surviving commands are more than
    :data:`MAX_STEP_GAP_SECONDS` of think time apart. The split is what stops
    the removal step from inventing adjacency: dropping half an hour of
    ``ls`` between two commands does not make them consecutive.
    """
    runs: list[list[CapturedCommand]] = []
    current: list[CapturedCommand] = []
    previous: CapturedCommand | None = None
    for command in session:
        if command.exit_code is not None and command.exit_code != 0:
            continue
        if not command.command.strip() or is_inspection(command.command):
            continue
        if (
            previous is not None
            and _think_seconds(previous, command) > MAX_STEP_GAP_SECONDS
        ):
            if len(current) >= MIN_SEQUENCE_LENGTH:
                runs.append(current)
            current = []
        current.append(command)
        previous = command
    if len(current) >= MIN_SEQUENCE_LENGTH:
        runs.append(current)
    return runs


# --- mining ----------------------------------------------------------------


@dataclass(frozen=True)
class Occurrence:
    """One time a sequence shape was observed, in one session."""

    session: tuple[str, int]
    commands: tuple[str, ...]
    ts: int
    repo: str | None


@dataclass(frozen=True)
class Variable:
    """One value that changed between runs of an otherwise fixed sequence."""

    name: str
    #: ``(step index, token index)`` positions this variable fills.
    positions: tuple[tuple[int, int], ...]
    #: Distinct values observed, most recent first.
    values: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    """A recurring sequence, ready to be offered as a group.

    ``steps`` is already parameterised: every position that varied between
    runs holds a ``$VAR`` token, so the tuple is exactly what would be written
    into a group if the user says yes.
    """

    steps: tuple[str, ...]
    variables: tuple[Variable, ...]
    occurrences: int
    first_seen: int
    last_seen: int
    repo: str | None
    has_credential: bool
    shape: tuple[tuple[str, ...], ...]

    @property
    def confidence(self) -> str:
        """``strong`` / ``moderate`` / ``weak``, from :attr:`occurrences`."""
        return confidence(self.occurrences)


def confidence(occurrences: int) -> str:
    """Label an occurrence count.

    Three separate sessions is where coincidence stops being a reasonable
    explanation; six is where the sequence is plainly a habit and the runbook
    would already have paid for itself. The label exists so the output can
    make a proportionate claim rather than asserting the same certainty for
    the minimum and for fifty.
    """
    if occurrences >= STRONG_EVIDENCE:
        return "strong"
    if occurrences >= MODERATE_EVIDENCE:
        return "moderate"
    return "weak"


def _collect(
    key: str, commands: Sequence[CapturedCommand]
) -> dict[tuple[tuple[str, ...], ...], list[Occurrence]]:
    """Index every mineable sub-sequence of one history file by its shape."""
    found: dict[tuple[tuple[str, ...], ...], list[Occurrence]] = {}
    for index, session in enumerate(split_sessions(commands)):
        for run in steps_of(session):
            shapes = [command_shape(c.command) for c in run]
            longest = min(MAX_SEQUENCE_LENGTH, len(run))
            for length in range(MIN_SEQUENCE_LENGTH, longest + 1):
                for start in range(len(run) - length + 1):
                    window = run[start : start + length]
                    shape = tuple(shapes[start : start + length])
                    found.setdefault(shape, []).append(
                        Occurrence(
                            session=(key, index),
                            commands=tuple(c.command for c in window),
                            ts=window[-1].ts,
                            repo=window[-1].repo,
                        )
                    )
    return found


def _one_per_session(occurrences: Sequence[Occurrence]) -> list[Occurrence]:
    """Keep the most recent observation from each distinct session.

    Repetition *inside* one session is not independent evidence — a build loop
    run eight times in one afternoon is one episode of work — so the
    occurrence count this feature quotes has to be a count of occasions.
    Keeping the most recent one per session also means the values a variable
    is shown to take are the ones the user last used.
    """
    latest: dict[tuple[str, int], Occurrence] = {}
    for occurrence in occurrences:
        current = latest.get(occurrence.session)
        if current is None or occurrence.ts >= current.ts:
            latest[occurrence.session] = occurrence
    return sorted(latest.values(), key=lambda o: o.ts)


def _analyse_slots(
    shape: tuple[tuple[str, ...], ...], occurrences: Sequence[Occurrence]
) -> dict[tuple[str, ...], list[tuple[int, int]]] | None:
    """Work out which holes actually varied, and which of them move together.

    Returns ``{value vector: positions}`` — a mapping from the tuple of values
    a position took across occurrences, in occurrence order, to every position
    that took exactly that tuple. Positions sharing a vector are one variable:
    ``prod`` appearing in three commands and becoming ``dev`` in all three at
    once is one parameter used three times, not three parameters, and counting
    positions instead would reject the most useful case this module has.

    Holes with a single observed value are literals and are dropped here, so a
    shape that never actually varied yields an empty mapping rather than a
    fistful of one-valued variables.

    ``None`` means the candidate is refused: more than :data:`MAX_VARIABLES`
    independent things changed, which is not one workflow seen several times.
    """
    vectors: dict[tuple[str, ...], list[tuple[int, int]]] = {}
    for step, step_shape in enumerate(shape):
        for index, entry in enumerate(step_shape):
            if entry != _HOLE and not entry.endswith(_HOLE):
                continue
            values = tuple(
                _slot_value(o.commands[step], step_shape, index) for o in occurrences
            )
            if len(set(values)) == 1:
                continue
            vectors.setdefault(values, []).append((step, index))
    if len(vectors) > MAX_VARIABLES:
        return None
    return vectors


_VERSION = re.compile(r"^v?\d+(?:\.\d+){0,3}$")
#: A filename, strictly: the extension must start with a letter and the value
#: must carry no ``:``. Both guards were added after calibration, where
#: ``registry.internal/api:1.4.0`` was named ``$FILE`` — its trailing ``.0``
#: satisfied a laxer rule and produced a name that actively misled.
_FILENAME = re.compile(r"^[^\s:]+\.[A-Za-z][A-Za-z0-9]{0,5}$")


def _variable_name(
    shape: tuple[tuple[str, ...], ...],
    positions: Sequence[tuple[int, int]],
    values: Sequence[str],
) -> str:
    """Derive a variable name from the evidence, in that order of preference.

    1. **The flag in front of it.** ``--namespace prod`` and
       ``--namespace=prod`` both name themselves; a long flag is the one piece
       of self-documentation a command line reliably carries. Short flags are
       not used: ``-n`` is a namespace to ``kubectl`` and a line count to
       ``head``, and inventing a meaning from an initial is exactly the kind
       of confident guess this codebase avoids.
    2. **The shape of the values.** All version-like, or all filename-like.
    3. **The verb it belongs to** — the last protected token of its command,
       so ``git checkout main`` yields ``CHECKOUT_ARG``.

    Names that collide with a shell variable mem deliberately ignores (``USER``
    from ``--user``, ``HOME``) fall through to rule 3, because
    :func:`mem.variables.parse_variables` would refuse to see them in the
    stored command and the group would silently lose its parameter.
    """
    # Every position is consulted for a long flag, not just the first: one
    # value often appears bare in one command and behind ``--namespace`` in
    # the next, and the labelled occurrence is the one that knows its name.
    for step, index in positions:
        step_shape = shape[step]
        flag = None
        if step_shape[index].endswith(_HOLE) and step_shape[index] != _HOLE:
            flag = step_shape[index].rstrip(_HOLE).rstrip("=")
        elif index and step_shape[index - 1].startswith("--"):
            flag = step_shape[index - 1]
        if flag and flag.startswith("--"):
            name = _sanitize(flag[2:])
            if name and name not in EXCLUDED_SHELL_VARS:
                return name

    step, index = positions[0]
    step_shape = shape[step]

    stripped = [value.strip("\"'") for value in values]
    if all(_VERSION.match(value) for value in stripped):
        return "VERSION"
    if all(_FILENAME.match(value) for value in stripped):
        return "FILE"

    # The verb is the token right after any privilege prefix and the program.
    # Read off the shape directly: those positions are protected, so they are
    # always literal text and never a hole.
    offset = 0
    while offset < len(step_shape) and step_shape[offset] in _PRIVILEGE_PREFIXES:
        offset += 1
    verb = _sanitize(step_shape[offset + 1] if offset + 1 < len(step_shape) else "")
    if verb and verb not in EXCLUDED_SHELL_VARS:
        return f"{verb}_ARG"
    return "ARG"


def _sanitize(text: str) -> str:
    """Fold arbitrary text into a legal ``$VAR`` name, or the empty string."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
    name = re.sub(r"_+", "_", name)
    return name if re.fullmatch(r"[A-Z][A-Z0-9_]+", name) else ""


def _build(
    shape: tuple[tuple[str, ...], ...], occurrences: Sequence[Occurrence]
) -> Candidate | None:
    """Turn one shape and its observations into a parameterised candidate."""
    vectors = _analyse_slots(shape, occurrences)
    if vectors is None:
        return None

    newest = occurrences[-1]
    variables: list[Variable] = []
    used: set[str] = set()
    for values, positions in vectors.items():
        base = _variable_name(shape, positions, values)
        name = base
        suffix = 2
        while name in used:
            name = f"{base}_{suffix}"
            suffix += 1
        used.add(name)
        # Most recent first: the value the user last used is the one that makes
        # the variable recognisable, and it is what a default would be.
        ordered = list(dict.fromkeys(reversed(values)))
        variables.append(
            Variable(name=name, positions=tuple(positions), values=tuple(ordered))
        )

    # Rewrite right to left so earlier spans keep their offsets.
    steps: list[str] = list(newest.commands)
    rewrites: dict[int, list[tuple[int, int, str]]] = {}
    for variable in variables:
        for step, index in variable.positions:
            start, end = _slot_span(steps[step], shape[step], index)
            rewrites.setdefault(step, []).append((start, end, f"${variable.name}"))
    for step, edits in rewrites.items():
        text = steps[step]
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        steps[step] = text

    timestamps = [o.ts for o in occurrences]
    return Candidate(
        steps=tuple(steps),
        variables=tuple(variables),
        occurrences=len(occurrences),
        first_seen=min(timestamps),
        last_seen=max(timestamps),
        repo=newest.repo,
        has_credential=any(_is_sensitive(step) for step in steps),
        shape=shape,
    )


def _is_sensitive(text: str) -> bool:
    """True if a command must never be written into a stored runbook.

    Both of :mod:`mem.variables`' detectors are consulted, and neither is
    reimplemented here: ``looks_like_credential`` is the deterministic
    shape matcher used for bulk work, and a difference between ``text`` and
    its redaction catches everything the MCP boundary would have masked. A
    credential that either one recognises is enough to refuse.
    """
    return looks_like_credential(text) or redact_secrets(text) != text


def mine(key: str, commands: Sequence[CapturedCommand]) -> list[Candidate]:
    """Extract every promotable sequence from one history file."""
    candidates: list[Candidate] = []
    for shape, raw in _collect(key, commands).items():
        occurrences = _one_per_session(raw)
        if len(occurrences) < MIN_OCCURRENCES:
            continue
        candidate = _build(shape, occurrences)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _contains(haystack: Sequence[Any], needle: Sequence[Any]) -> bool:
    """True if *needle* appears as a contiguous run inside *haystack*."""
    if len(needle) > len(haystack):
        return False
    return any(
        tuple(haystack[i : i + len(needle)]) == tuple(needle)
        for i in range(len(haystack) - len(needle) + 1)
    )


def closed(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Drop candidates wholly contained in an equally-attested longer one.

    A five-step deploy makes its own four-step prefix recur just as often, and
    listing both says the same thing twice while burying everything else. The
    shorter one survives only if it occurs *strictly* more often, which means
    it genuinely is a more common workflow than the sequence containing it —
    the standard closed-sequential-pattern rule, and the reason this list is
    readable at all.
    """
    return [
        candidate
        for candidate in candidates
        if not any(
            other is not candidate
            and len(other.shape) > len(candidate.shape)
            and other.occurrences >= candidate.occurrences
            and _contains(other.shape, candidate.shape)
            for other in candidates
        )
    ]


def steps_saved(candidate: Candidate) -> int:
    """How many command lines this runbook would already have saved.

    ``occurrences × length``. Ranking on occurrences alone was measured on the
    calibration corpus and is wrong in a specific, visible way: every prefix of
    a six-step deploy recurs at least as often as the deploy does, so an
    occurrence-first order puts ``docker build`` / ``docker push`` (7 times)
    above the whole deploy it belongs to (6 times), and the list fills up with
    fragments of one workflow. Multiplying by length asks the question the user
    is actually asking — how much typing would this have saved — and the whole
    sequence wins it.
    """
    return candidate.occurrences * len(candidate.steps)


def rank(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Order candidates best-first.

    1. **Most steps saved** (:func:`steps_saved`) — the value of the runbook.
    2. **Most occurrences.** Between candidates worth the same, prefer the one
       with more evidence behind it, for the reason ``mem fix`` sorts on
       occurrences: repetition is the only proof this is a workflow at all.
    3. **Most recent**, then the command text, so a tie is broken the same way
       twice and ``mem promote 2`` means the same candidate on both runs.
    """
    return sorted(
        candidates,
        key=lambda c: (-steps_saved(c), -c.occurrences, -c.last_seen, c.steps),
    )


def dominant(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Keep one representative of each family of overlapping sequences.

    :func:`closed` removes candidates that carry no evidence of their own.
    This removes candidates that carry no *attention* of their own: a six-step
    deploy and its five-step tail are one thing a user would promote once, and
    showing both spends two of the five slots on the same workflow while a
    different one falls off the list. Measured on the calibration corpus, half
    of a ten-candidate listing could be fragments of a single sequence.

    "Family" is deliberately narrow — one sequence containing the other, not
    merely sharing a command. Two sequences that overlap in a step without one
    containing the other are two different procedures that happen to share a
    move, and suppressing either would hide a real answer.

    Input must be ranked; the first member of each family is the one kept.
    """
    kept: list[Candidate] = []
    for candidate in candidates:
        related = any(
            other.repo == candidate.repo
            and (
                _contains(other.shape, candidate.shape)
                or _contains(candidate.shape, other.shape)
            )
            for other in kept
        )
        if not related:
            kept.append(candidate)
    return kept


def existing_step_lists(paths: Iterable[Path]) -> set[tuple[str, ...]]:
    """Every group already saved in the given scopes, as its list of commands.

    Used to stop ``mem promote`` proposing what the user has already promoted:
    the stored commands are byte-identical to the template that produced them,
    so exact equality is enough and is the honest test. A group that merely
    overlaps a candidate is left alone — mem does not know that the user
    considers those the same runbook.
    """
    known: set[tuple[str, ...]] = set()
    for path in paths:
        try:
            data: GroupFile = storage.read_group_file(path)
        except ValueError:
            continue
        for group in data.groups.values():
            known.add(tuple(command.cmd for command in group.commands))
    return known


def mine_all() -> list[Candidate]:
    """Mine every history file, ranked best-first and closed.

    Evidence is counted per repository and never merged across them, which is
    the opposite of what ``mem fix`` does with corrections — and deliberately
    so. A fix for ``docker`` is a fix in any checkout; a *runbook* is made of
    this repository's branch names, service names and script paths, and adding
    up two repositories' counts would claim a workflow recurred six times when
    what actually happened is two different workflows recurring three times
    each. Closure is applied per file for the same reason: a longer sequence
    from another repository is not evidence about this one.
    """
    candidates: list[Candidate] = []
    for key, commands in iter_histories():
        candidates.extend(closed(mine(key, commands)))
    return dominant(rank(candidates))


# --- naming ----------------------------------------------------------------

_TRAILING_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def suggest_name(candidate: Candidate, taken: Iterable[str] = ()) -> str:
    """Propose a group name for a candidate.

    Derived from the *last* step, because that is what the earlier ones were
    for: a sequence ending in ``./scripts/deploy.sh`` is a deploy, whatever it
    checked out first. The program's basename supplies the head of the name
    and its subcommand, when there is one, the tail — ``terraform apply``
    becomes ``terraform-apply`` and ``./scripts/deploy.sh`` becomes ``deploy``.

    This is a label, not an understanding. ``--name`` overrides it and
    ``mem group rename`` fixes it later.
    """
    tokens = [token.text for token in tokenize(candidate.steps[-1])]
    while tokens and tokens[0] in _PRIVILEGE_PREFIXES:
        tokens = tokens[1:]

    parts: list[str] = []
    if tokens:
        head = _TRAILING_EXTENSION.sub("", tokens[0].rsplit("/", 1)[-1])
        parts.append(head)
    if len(tokens) > 1 and not _is_flag(tokens[1]) and "$" not in tokens[1]:
        parts.append(tokens[1].rsplit("/", 1)[-1])

    slug = re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"routine-{slug}".strip("-")

    reserved = set(taken)
    if slug not in reserved:
        return slug
    suffix = 2
    while f"{slug}-{suffix}" in reserved:
        suffix += 1
    return f"{slug}-{suffix}"


# --- report ----------------------------------------------------------------


@dataclass(frozen=True)
class PromoteReport:
    """What ``mem promote`` found: ranked candidates, with their names."""

    candidates: list[Candidate]
    names: list[str]


def build_report(
    scopes: Sequence[Path] = (), limit: int = 5, existing_names: Iterable[str] = ()
) -> PromoteReport:
    """Mine the store and answer one ``mem promote`` invocation.

    ``scopes`` are the group files to check against, so a sequence the user has
    already promoted stops being suggested. Nothing here writes.
    """
    already = existing_step_lists(scopes)
    candidates = [c for c in mine_all() if c.steps not in already][: max(1, limit)]

    taken = set(existing_names)
    names: list[str] = []
    for candidate in candidates:
        name = suggest_name(candidate, taken)
        taken.add(name)
        names.append(name)
    return PromoteReport(candidates=candidates, names=names)


def _iso(ts: int) -> str:
    """Format an epoch timestamp as UTC ISO-8601, for machine consumers."""
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _redact(value: Any) -> Any:
    """Recursively redact every string in a JSON-shaped structure.

    At one choke point rather than field by field, exactly as ``mem fix`` and
    ``mem mcp`` do it: a per-field call is a rule the next field can forget,
    and the cost of forgetting is a printed credential. ``mem promote`` quotes
    whole sequences of commands the user has stopped thinking about.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def report_payload(report: PromoteReport) -> dict[str, Any]:
    """Render a report as redacted, JSON-ready data.

    The single exit from this module. Both ``--json`` and the terminal
    rendering read this structure, so there is exactly one place a command
    string can escape unredacted, and it is here.
    """
    payload: dict[str, Any] = {
        "count": len(report.candidates),
        "candidates": [
            {
                "index": index,
                "name": name,
                "steps": list(candidate.steps),
                "occurrences": candidate.occurrences,
                "confidence": candidate.confidence,
                "first_seen": candidate.first_seen,
                "last_seen": candidate.last_seen,
                "last_seen_iso": _iso(candidate.last_seen),
                "repo": candidate.repo,
                "has_credential": candidate.has_credential,
                "variables": [
                    {"name": variable.name, "values": list(variable.values)}
                    for variable in candidate.variables
                ],
            }
            for index, (candidate, name) in enumerate(
                zip(report.candidates, report.names), 1
            )
        ],
    }
    return _redact(payload)
