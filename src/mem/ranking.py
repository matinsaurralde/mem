"""The ranking formula, expressed over primitives instead of models.

Why this module exists at all: the interactive finder must draw its first
frame in tens of milliseconds, and importing ``mem.models`` costs ~58ms
because it pulls in Pydantic. The finder therefore reads JSONL with
``json.loads`` and never constructs a ``CapturedCommand`` — but it must rank
results *identically* to ``mem <query>``, or the same history would be
ordered two different ways depending on how you asked.

So the formula lives here, over plain strings and ints, with no imports
beyond the standard library. ``mem.search`` calls it after unwrapping a
model; the finder calls it directly. One implementation, two callers — the
alternative being a second copy that drifts, which is exactly the defect
pattern that put a stale shell hook in front of every pip user.
"""

from __future__ import annotations

import math

# A command run this many times is treated as maximally frequent. Above it the
# curve is flat, which is the point: the difference between 50 runs and 500 is
# not information, while the difference between 1 and 10 is.
FREQUENCY_CEILING = 50

# Weights over features that are all in [0, 1]. They sum to 1, so a score is
# directly readable as "how good is this match, out of 1".
W_FREQUENCY = 0.35
W_RECENCY = 0.35
W_PREFIX = 0.15
W_CONTEXT = 0.15

# Recency halves every this many days.
RECENCY_HALF_LIFE_DAYS = 7

_LN2 = math.log(2)
_LOG_CEILING = math.log1p(FREQUENCY_CEILING)


def frequency_score(frequency: int) -> float:
    """Normalise a run count to [0, 1], logarithmically and with a ceiling.

    Logarithmic because the jump from 1 run to 5 says much more than 100 to
    105. Capped so one pathologically repeated command cannot own every
    result.
    """
    return min(1.0, math.log1p(max(frequency, 0)) / _LOG_CEILING)


def recency_score(ts: int | float, now: float) -> float:
    """Exponential decay with a 7-day half-life.

    Today scores 1.0, a week ago 0.5, two weeks 0.25. Human memory fades the
    same way: older commands need a stronger signal to surface. Future
    timestamps are clamped so clock skew cannot manufacture a bonus above 1.
    """
    days_since = max((now - ts) / 86400, 0)
    return math.exp(-days_since * _LN2 / RECENCY_HALF_LIFE_DAYS)


def prefix_score(command: str, query: str) -> float:
    """1.0 when the command *starts with* the query, 0.0 otherwise.

    Someone typing ``mem git push`` wants ``git push origin main``, not the
    ``echo "remember to git push"`` they happened to run more often. No other
    feature can express "this is the one you meant", because every result
    already contains every term by construction.
    """
    return 1.0 if command.lower().startswith(query.strip().lower()) else 0.0


def context_score(repo: str | None, current_repo: str | None) -> float:
    """1.0 for the current repo, 0.5 for a sibling, 0.0 otherwise.

    Siblings are directories sharing a parent — checkouts kept side by side
    are usually related work. A refinement, not a driver.
    """
    if not current_repo or not repo:
        return 0.0
    if repo == current_repo:
        return 1.0
    if (
        "/" in current_repo
        and "/" in repo
        and repo.rsplit("/", 1)[0] == current_repo.rsplit("/", 1)[0]
    ):
        return 0.5
    return 0.0


def score(
    command: str,
    ts: int | float,
    repo: str | None,
    query: str,
    current_repo: str | None,
    frequency: int,
    now: float,
) -> float:
    """Combine every feature into a score in [0, 1].

        0.35*frequency + 0.35*recency + 0.15*prefix + 0.15*context

    Every term is normalised and the weights sum to 1, so the result reads
    directly as a fraction — which is the only reason the ``score`` field in
    ``--json`` output means anything to a caller.

    ``now`` is a parameter rather than a ``time.time()`` call so that ranking
    a page of results uses one consistent instant, and so tests can assert
    exact values without freezing the clock.
    """
    return (
        frequency_score(frequency) * W_FREQUENCY
        + recency_score(ts, now) * W_RECENCY
        + prefix_score(command, query) * W_PREFIX
        + context_score(repo, current_repo) * W_CONTEXT
    )
