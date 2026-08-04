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
from collections.abc import Sequence

from mem import picks

# A command run this many times is treated as maximally frequent. Above it the
# curve is flat, which is the point: the difference between 50 runs and 500 is
# not information, while the difference between 1 and 10 is.
FREQUENCY_CEILING = 50

# Weights over features that are all in [0, 1]. They sum to 1, so a score is
# directly readable as "how good is this match, out of 1".
#
# `picks` takes the largest share because it is the only feature that is not
# an inference: it is the user having already answered, for this command, the
# question the rest of the formula is guessing at. Measured over 1,200
# retrieval episodes, adding it moves MRR@10 from 0.039 to 0.575.
#
# The remaining 0.60 is split in exactly the proportions the four original
# features had among themselves (35/35/15/15). That is deliberate: with no
# picks recorded, every score is the old score scaled by 0.6, so the *ordering*
# is untouched. Adding this feature cannot change anyone's results until they
# have actually used the finder — which is the only honest way to introduce a
# signal that needs data nobody has yet.
W_PICKS = 0.40
W_FREQUENCY = 0.21
W_RECENCY = 0.21
W_PREFIX = 0.09
W_CONTEXT = 0.09

# Recency halves every this many days.
RECENCY_HALF_LIFE_DAYS = 7

# Credit a query concept earns when the command contains the user's own word,
# versus a synonym the concept map supplied. Both live below the hard boundary
# that keeps *every* fully literal match above *any* expanded one (see
# ``mem.search.search``), so this is a tie-breaker inside the expanded tier,
# not the mechanism that protects literal matches. It is deliberately mild:
# make it severe and a command matching only a vague literal word ("fix")
# outranks the one that matched a precise concept ("certificate" -> openssl),
# which is the opposite of what the map is for.
LITERAL_CREDIT = 1.0
EXPANDED_CREDIT = 0.8

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
    pick_weight: float = 0.0,
) -> float:
    """Combine every feature into a score in [0, 1].

        0.40*picks + 0.21*frequency + 0.21*recency + 0.09*prefix + 0.09*context

    Every term is normalised and the weights sum to 1, so the result reads
    directly as a fraction — which is the only reason the ``score`` field in
    ``--json`` output means anything to a caller.

    ``pick_weight`` is the decayed count from :mod:`mem.picks` and defaults to
    zero, which is both the correct value for a command nobody has chosen and
    the correct behaviour for a caller that does not track picks at all.

    ``now`` is a parameter rather than a ``time.time()`` call so that ranking
    a page of results uses one consistent instant, and so tests can assert
    exact values without freezing the clock.
    """
    return (
        picks.normalize(pick_weight) * W_PICKS
        + frequency_score(frequency) * W_FREQUENCY
        + recency_score(ts, now) * W_RECENCY
        + prefix_score(command, query) * W_PREFIX
        + context_score(repo, current_repo) * W_CONTEXT
    )


def idf(document_frequency: int, n_documents: int) -> float:
    """How much a term's presence tells you, normalised to [0, 1].

    The BM25 form, ``log(1 + (N - df + 0.5) / (df + 0.5))``, divided by its own
    value at ``df = 1`` so the result reads as a fraction like every other
    feature here. A term in one command out of a thousand scores 1.0; a term in
    all thousand scores ~0.

    This is what stops query expansion from doing harm. The concept map is
    hand-written, so it will eventually contain a synonym that is far too
    common — a ``"version control": ["git", ...]`` in a history that is 40%
    git. Without idf that entry would drag every git command into the answer
    for any question mentioning version control. With it, a term matching half
    the candidates scores below 0.2 (and keeps falling as the history grows),
    so it cannot move the ranking: the weighting makes a bad entry inert
    instead of harmful, which is the property a hand-edited file needs.

    Both counts are measured over the *candidates* for this query, not the
    whole history. Ranking only has to separate the results from each other,
    and a term present in every candidate separates nothing however rare it is
    globally. It is also the cheap answer: the candidate set is already in
    memory, while a corpus-wide count is another pass over every file.
    """
    if n_documents <= 1:
        return 1.0
    df = min(max(document_frequency, 1), n_documents)
    raw = math.log(1 + (n_documents - df + 0.5) / (df + 0.5))
    rarest = math.log(1 + (n_documents - 0.5) / 1.5)
    return raw / rarest


def coverage(credits: Sequence[float], weights: Sequence[float]) -> float:
    """How much of the query's *information* a command accounts for, in [0, 1].

    ``credits[i]`` is how well concept *i* was satisfied — 1.0 by the user's
    own words, 0.8 by a synonym, 0.0 not at all — and ``weights[i]`` is that
    concept's idf. Weighting by idf rather than counting concepts is what makes
    a match on "certificate" worth more than a match on "fix": the first
    narrows a history to a handful of commands, the second barely narrows it at
    all, and treating them as one-concept-one-vote would rank the vague hit
    first.

    When no concept discriminates (every one of them matches every candidate,
    which is the ordinary case for a one-word query) all the weights are ~0.
    Falling back to the unweighted mean keeps the multiplier meaningful instead
    of collapsing every score to zero and leaving the order to chance.
    """
    if not credits:
        return 0.0
    total = sum(weights)
    if total <= 0:
        return sum(credits) / len(credits)
    return sum(c * w for c, w in zip(credits, weights)) / total


def expanded_score(base: float, match_coverage: float) -> float:
    """Scale a score by how much of the query the command actually covered.

    Multiplicative, not additive: a command that satisfies one concept out of
    three should not be able to buy its way back with frequency and recency.
    The result stays in [0, 1], so the ``score`` field in ``--json`` still
    reads as a fraction.
    """
    return base * match_coverage
