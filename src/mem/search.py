"""
Search and ranking engine for mem.

Reads JSONL command history, scores each command using a deterministic
formula, and returns ranked results. No ML — just math.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from typing import TYPE_CHECKING

from mem import storage
from mem.models import CapturedCommand, CommandPattern, WorkSession

if TYPE_CHECKING:
    pass


def score_command(
    cmd: CapturedCommand,
    query: str,
    current_repo: str | None,
    frequency: int,
) -> float:
    """Score a command for search relevance.

    Uses a deterministic formula:
        score = (frequency * 0.4) + (recency * 0.4) + (context * 0.2)

    Why these weights:
    - Frequency (40%): Commands you run often are commands you need often.
      This is the strongest baseline signal.
    - Recency (40%): Equal weight to frequency because recent commands
      reflect current work context. A command from today is almost certainly
      more relevant than one from last month.
    - Context (20%): A tiebreaker that boosts commands from the current
      repo. Lower weight because frequency and recency already capture
      most relevance — context just refines the ranking.

    Why exit code is NOT included (v1): Per specification clarification,
    exit code is captured and stored but does not affect ranking. Failed
    commands may be intentional (e.g., checking if a service is down).
    Exit-code-based deprioritization is deferred to a future version.

    Recency uses exponential decay with a 7-day half-life:
        recency = exp(-days_since * ln(2) / 7)
    A command run today scores 1.0; 7 days ago scores 0.5; 14 days ago 0.25.
    This mirrors how human memory fades — recent events are vivid,
    older ones require stronger signals (high frequency) to surface.
    """
    now = time.time()
    days_since = max((now - cmd.ts) / 86400, 0)

    # Recency: exponential decay, half-life 7 days
    recency = math.exp(-days_since * math.log(2) / 7)

    # Context: 1.0 same repo, 0.5 same dir prefix, 0.0 otherwise
    if current_repo and cmd.repo and cmd.repo == current_repo:
        context = 1.0
    elif current_repo and cmd.repo and cmd.repo.startswith(current_repo.split("-")[0]):
        context = 0.5
    else:
        context = 0.0

    return (frequency * 0.4) + (recency * 0.4) + (context * 0.2)


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
    if not query:
        return []

    # Collect all matching commands
    all_commands: list[CapturedCommand] = []

    # Read current repo first for context boost
    if current_repo:
        repo_name = storage.sanitize_repo_name(current_repo)
        for cmd in storage.read_commands(repo_name):
            if query.lower() in cmd.command.lower():
                all_commands.append(cmd)

    # Read global fallback
    for cmd in storage.read_commands("_global"):
        if query.lower() in cmd.command.lower():
            all_commands.append(cmd)

    # Also read other repo files if current_repo didn't cover everything
    repos_dir = storage.MEM_DIR / "repos"
    if repos_dir.exists():
        current_sanitized = storage.sanitize_repo_name(current_repo) if current_repo else None
        for path in sorted(repos_dir.glob("*.jsonl")):
            repo_name = path.stem
            if repo_name == current_sanitized or repo_name == "_global":
                continue  # already read
            for cmd in storage.read_commands(repo_name):
                if query.lower() in cmd.command.lower():
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
