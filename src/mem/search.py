"""
Search and ranking engine for mem.

Reads JSONL command history, scores each command using a deterministic
formula, and returns ranked results. No ML — just math.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from mem import storage
from mem.models import CapturedCommand, CommandPattern, WorkSession


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


def score_command(
    cmd: CapturedCommand,
    query: str,
    current_repo: str | None,
    frequency: int,
) -> float:
    """Score a command for search relevance.

    A linear combination of four features, each normalised to [0, 1]:

        score = 0.35*frequency + 0.35*recency + 0.15*prefix + 0.15*context

    Why the normalisation matters more than the weights: the previous formula
    was ``0.4*frequency + 0.4*recency + 0.2*context`` with ``frequency`` as a
    *raw count*. Recency and context are bounded by 1, so a command run ten
    times scored 4.0 against a ceiling of 0.6 for everything else — the two
    signals the docstring described as equally weighted could not move the
    ranking at all. mem was sorting by frequency and calling it a formula. The
    weights are a design choice; that was a bug.

    - **Frequency** (35%): ``log1p(n) / log1p(50)``, capped at 1. Logarithmic
      because the jump from 1 run to 5 says much more than 100 to 105, and
      capped so one pathologically repeated command cannot own every result.
    - **Recency** (35%): exponential decay with a 7-day half-life,
      ``exp(-days * ln(2) / 7)``. Today scores 1.0, a week ago 0.5, two weeks
      0.25. Human memory fades the same way: older commands need a stronger
      signal to surface.
    - **Prefix** (15%): 1.0 when the command starts with the query. Someone
      typing ``mem git push`` wants ``git push origin main``, not the
      ``echo "remember to git push"`` they ran more often. Nothing else in the
      formula could express "this is what you meant", because every result
      already contains every term.
    - **Context** (15%): 1.0 for the current repo, 0.5 for a sibling sharing a
      parent directory. A refinement, not a driver.

    Why exit code is NOT included: a failed command is often deliberate —
    checking whether a service is down, or probing until something works. The
    useful version of this signal is the *pair* (what failed, what fixed it),
    which is a different feature, not a penalty term here.
    """
    now = time.time()
    days_since = max((now - cmd.ts) / 86400, 0)

    # Frequency: logarithmic and bounded, so it can be weighed against the rest
    normalized_frequency = min(
        1.0, math.log1p(max(frequency, 0)) / math.log1p(FREQUENCY_CEILING)
    )

    # Recency: exponential decay, half-life 7 days
    recency = math.exp(-days_since * math.log(2) / 7)

    # Prefix: the query is how the command begins, not just something it contains
    prefix = 1.0 if cmd.command.lower().startswith(query.strip().lower()) else 0.0

    # Context: 1.0 same repo, 0.5 sibling repos (same parent dir), 0.0 otherwise
    if current_repo and cmd.repo and cmd.repo == current_repo:
        context = 1.0
    elif (
        current_repo
        and cmd.repo
        and "/" in current_repo
        and "/" in cmd.repo
        and cmd.repo.rsplit("/", 1)[0] == current_repo.rsplit("/", 1)[0]
    ):
        context = 0.5
    else:
        context = 0.0

    return (
        normalized_frequency * W_FREQUENCY
        + recency * W_RECENCY
        + prefix * W_PREFIX
        + context * W_CONTEXT
    )


def _terms(query: str) -> list[str]:
    """Split a query into the terms a command must all contain.

    Multi-word queries used to keep only the first word, so `mem docker
    compose` silently answered for `docker` alone — and ranked an unrelated
    `docker ps` above the one line that actually matched both words. Matching
    every term independently also makes word order irrelevant, which is how
    people remember commands.
    """
    return [t for t in query.lower().split() if t]


def _matches(command: str, terms: list[str]) -> bool:
    """True if every term appears somewhere in the command."""
    lowered = command.lower()
    return all(term in lowered for term in terms)


def search(
    query: str,
    current_repo: str | None = None,
    limit: int = 10,
) -> list[tuple[CapturedCommand, float]]:
    """Search command history for commands matching a query.

    Returns a list of (command, score) tuples, ranked by score descending.

    Strategy:
    1. Read from current repo JSONL first (if applicable)
    2. Then read from _global.jsonl
    3. Filter by substring match on query
    4. Compute frequency counts per unique command string
    5. Score each unique command (keep highest score per command)
    6. Return top N by score
    """
    terms = _terms(query)
    if not terms:
        return []

    # Collect all matching commands
    all_commands: list[CapturedCommand] = []

    # Read current repo first for context boost
    if current_repo:
        repo_name = storage.sanitize_repo_name(current_repo)
        for cmd in storage.read_commands(repo_name):
            if _matches(cmd.command, terms):
                all_commands.append(cmd)

    # Read global fallback
    for cmd in storage.read_commands("_global"):
        if _matches(cmd.command, terms):
            all_commands.append(cmd)

    # Also read other repo files if current_repo didn't cover everything
    repos_dir = storage.MEM_DIR / "repos"
    if repos_dir.exists():
        current_sanitized = (
            storage.sanitize_repo_name(current_repo) if current_repo else None
        )
        for path in sorted(repos_dir.glob("*.jsonl")):
            repo_name = path.stem
            if repo_name == current_sanitized or repo_name == "_global":
                continue  # already read
            for cmd in storage.read_commands(repo_name):
                if _matches(cmd.command, terms):
                    all_commands.append(cmd)

    if not all_commands:
        return []

    # Compute frequency per unique command string
    freq = Counter(cmd.command for cmd in all_commands)

    # Score and deduplicate — keep highest score per unique command
    best: dict[str, tuple[CapturedCommand, float]] = {}
    for cmd in all_commands:
        s = score_command(cmd, query, current_repo, freq[cmd.command])
        if cmd.command not in best or s > best[cmd.command][1]:
            best[cmd.command] = (cmd, s)

    # Sort by score descending, return top N
    ranked = sorted(best.values(), key=lambda x: x[1], reverse=True)
    return ranked[:limit]


def search_patterns(tool: str) -> list[CommandPattern]:
    """Search for extracted patterns for a specific tool.

    Returns patterns sorted by frequency (most common first).
    Returns empty list if no patterns exist for this tool.
    """
    pf = storage.read_patterns(tool)
    if pf is None:
        return []
    return sorted(pf.patterns, key=lambda p: p.frequency, reverse=True)


def search_sessions(query: str) -> list[WorkSession]:
    """Search sessions by keyword.

    Matches against session summaries and individual commands.
    Returns sessions sorted by started_at descending (most recent first).
    """
    if not query:
        return []

    results: list[WorkSession] = []
    query_lower = query.lower()

    for session in storage.read_all_sessions():
        # Match in summary
        if query_lower in session.summary.lower():
            results.append(session)
            continue
        # Match in any command
        if any(query_lower in cmd.lower() for cmd in session.commands):
            results.append(session)

    return sorted(results, key=lambda s: s.started_at, reverse=True)
