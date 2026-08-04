"""
Search and ranking engine for mem.

Reads JSONL command history, scores each command using a deterministic
formula, and returns ranked results. No ML — just math.

Two passes, in a fixed order. The **literal** pass is substring search: every
word the user typed must appear in the command. When it finds nothing — which
is what happens to every question phrased in English, because "the command I
used to fix the certificate" shares no word with ``openssl x509 -in
cert.pem`` — the **expanded** pass runs the same query through
:mod:`mem.concepts`, a hand-written map from natural-language concepts to
shell vocabulary. See :func:`search` for the exact rule and why it is the one
that was chosen.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence

from mem import concepts, picks, ranking, storage
from mem.models import CapturedCommand, CommandPattern, WorkSession


def score_command(
    cmd: CapturedCommand,
    query: str,
    current_repo: str | None,
    frequency: int,
    pick_weight: float = 0.0,
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

    The arithmetic itself lives in :mod:`mem.ranking`, which imports nothing
    but the standard library. The interactive finder has to rank without
    paying Pydantic's ~58ms import, and two implementations of a ranking
    formula would drift until the same history sorted differently depending
    on how you asked for it.
    """
    return ranking.score(
        command=cmd.command,
        ts=cmd.ts,
        repo=cmd.repo,
        query=query,
        current_repo=current_repo,
        frequency=frequency,
        now=time.time(),
        pick_weight=pick_weight,
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


def _read_history(
    current_repo: str | None,
    needles: Sequence[str] | None = None,
    line_filter: Callable[[str], object] | None = None,
) -> Iterator[CapturedCommand]:
    """Yield every captured command, current repo first, each file read once.

    Order matters only for the caller's convenience; ranking is what decides
    what the user sees. Reading each file exactly once is what matters:
    ``_global`` used to be read twice, which doubled the frequency of every
    command that lived there.
    """
    if current_repo:
        yield from storage.read_commands(
            storage.resolve_repo_key(current_repo), needles, line_filter
        )

    yield from storage.read_commands("_global", needles, line_filter)

    repos_dir = storage.MEM_DIR / "repos"
    if not repos_dir.exists():
        return
    current_sanitized = storage.repo_key(current_repo) if current_repo else None
    for path in sorted(repos_dir.glob("*.jsonl")):
        repo_name = path.stem
        if repo_name == current_sanitized or repo_name == "_global":
            continue  # already read
        yield from storage.read_commands(repo_name, needles, line_filter)


def _rank(
    matched: list[CapturedCommand],
    query: str,
    current_repo: str | None,
    scale: Callable[[str, float], float] | None = None,
) -> list[tuple[CapturedCommand, float]]:
    """Score, deduplicate and sort a set of matching commands.

    A command string appears once, represented by its highest-scoring
    occurrence, with ``frequency`` counted across every occurrence — the same
    command run in three repos is one result run three times, not three
    results.

    ``scale`` optionally adjusts a command's score by how well it matched;
    the literal pass has nothing to adjust and passes nothing.
    """
    freq = Counter(cmd.command for cmd in matched)

    # What the user has actually chosen before, decayed. Read once for the
    # whole page rather than per candidate: it is one small file, and reading
    # it inside the loop would turn a search into thousands of stat() calls.
    pick_weights = picks.load()

    best: dict[str, tuple[CapturedCommand, float]] = {}
    for cmd in matched:
        s = score_command(
            cmd,
            query,
            current_repo,
            freq[cmd.command],
            pick_weights.get(cmd.command, 0.0),
        )
        if scale is not None:
            s = scale(cmd.command, s)
        if cmd.command not in best or s > best[cmd.command][1]:
            best[cmd.command] = (cmd, s)

    return sorted(best.values(), key=lambda x: x[1], reverse=True)


def _literal_search(
    terms: list[str], query: str, current_repo: str | None
) -> list[tuple[CapturedCommand, float]]:
    """Rank the commands containing every word the user typed."""
    # Cheap substring test on the raw JSONL line, so the expensive parse only
    # runs for lines that could match. The needles are a *necessary* condition,
    # never a sufficient one — `_matches` below is still the real filter.
    needles = storage.prefilter_needles(terms)
    matched = [
        cmd
        for cmd in _read_history(current_repo, needles)
        if _matches(cmd.command, terms)
    ]
    return _rank(matched, query, current_repo)


def _expanded_search(
    terms: list[str], query: str, current_repo: str | None
) -> list[tuple[CapturedCommand, float]]:
    """Rank the commands that match the *concepts* behind the words typed.

    Each query concept (see :func:`mem.concepts.expand`) is satisfied by the
    user's own words or by one of the concept's synonyms. A candidate must
    satisfy **at least one concept through a synonym** — that is the rule, and
    both halves of it are load-bearing:

    - *At least one*, not all of them. Requiring every concept was tried and
      answers none of the questions this pass exists for: nothing in
      ``openssl x509 -in cert.pem`` satisfies the "fix" in "the command I used
      to fix the certificate", so a single unsatisfiable concept would veto
      the correct answer. What separates a good candidate from a poor one is
      *coverage*, scored rather than filtered, weighted by idf so that
      satisfying "certificate" counts for far more than satisfying "fix".
    - *Through a synonym*. A command that matches only some of the literal
      words is not a discovery of the concept map — it is the plain AND search
      quietly downgraded to an OR, which is the exact defect fixed in #10:
      ``mem docker zzzz-no-such-term`` must return nothing, or the user cannot
      tell that a word was ignored. Requiring the map to have contributed
      something keeps that promise, and it also drops the noise it would
      otherwise let in: for "fix the certificate", a ``git commit -m "fix
      flaky test"`` matching nothing but the word "fix" is not an answer.

    A literal *anchor* — requiring at least one of the user's own words to
    match — was considered and rejected for the opposite reason: it scores 0
    on these questions, because the words a person uses to describe a command
    are usually not in the command.
    """
    data = concepts.load(storage.MEM_DIR / concepts.USER_CONCEPTS_FILENAME)
    groups = concepts.expand(terms, data)

    admitted = list(_read_history(current_repo, line_filter=_any_variant(groups)))

    # Judged once per distinct command rather than once per occurrence: the
    # prefilter admits whole files' worth of repeated commands, and the verdict
    # depends on nothing but the text.
    credits: dict[str, list[float]] = {}
    for command in {cmd.command for cmd in admitted}:
        lowered = command.lower()
        row = [_credit(lowered, group) for group in groups]
        if ranking.EXPANDED_CREDIT in row:
            credits[command] = row

    matched = [cmd for cmd in admitted if cmd.command in credits]
    if not matched:
        return []

    # Document frequency over the candidates, in distinct commands: three runs
    # of the same command are one piece of evidence about how specific a term
    # is, not three.
    n_documents = len(credits)
    weights = [
        ranking.idf(sum(1 for row in credits.values() if row[i] > 0), n_documents)
        for i in range(len(groups))
    ]

    coverages = {
        command: ranking.coverage(row, weights) for command, row in credits.items()
    }
    return _rank(
        matched,
        query,
        current_repo,
        scale=lambda command, s: ranking.expanded_score(s, coverages[command]),
    )


def _credit(lowered_command: str, group: concepts.QueryGroup) -> float:
    """How well a command satisfies one query concept: literally, or by synonym."""
    if group.literal_hit(lowered_command):
        return ranking.LITERAL_CREDIT
    if group.matched_expansions(lowered_command):
        return ranking.EXPANDED_CREDIT
    return 0.0


def _any_variant(
    groups: Sequence[concepts.QueryGroup],
) -> Callable[[str], object] | None:
    """A raw-line prefilter for "contains any of these", or None if unsafe.

    The literal pass can drop a needle it considers too short and still be
    correct, because its needles are ANDed: a missing one only means more
    lines get parsed. Here they are ORed, so dropping one would *exclude*
    every line that contains only that alternative — a wrong answer, not a
    slow one. If any variant reduces to nothing usable (``ss -``, an accented
    word, a bare flag someone added to their own map) the prefilter is
    abandoned entirely and every line is parsed.

    A loop of ``in`` tests, not a compiled alternation, and not
    ``re.IGNORECASE``. All three were measured over 20k lines on the query
    "change file permissions to executable" (15 needles): 17ms for this,
    36ms for ``re.compile("a|b|...").search(line.lower())``, and 117ms for
    the same pattern with ``IGNORECASE``. Each ``in`` gets the interpreter's
    optimised substring search and the loop stops at the first hit, while an
    alternation retries every branch at every position and a case-insensitive
    one gives up its literal optimisations entirely. Lowercasing the line once
    is cheaper than asking the matcher to ignore case.
    """
    variants = [v for group in groups for v in group.variants()]
    needles = storage.prefilter_needles(variants, min_length=_EXPANSION_NEEDLE_LEN)
    if len(needles) != len(variants):
        return None
    unique = tuple(dict.fromkeys(needles))

    def contains_any(line: str) -> bool:
        lowered = line.lower()
        return any(needle in lowered for needle in unique)

    return contains_any


# Two characters, not the three the literal pass uses. Half the shell's most
# useful vocabulary is two letters — df, du, ps, rm, ln, jq, go — and an OR
# prefilter that drops one of those needles drops results with it.
_EXPANSION_NEEDLE_LEN = 2


def search(
    query: str,
    current_repo: str | None = None,
    limit: int = 10,
    expand: bool = True,
) -> list[tuple[CapturedCommand, float]]:
    """Search command history for commands matching a query.

    Returns a list of (command, score) tuples, ranked by score descending.

    **The matching rule.** A command matches when every word of the query
    appears in it. If nothing does, and only then, the query is re-read
    through the concept map (:mod:`mem.concepts`) and commands are matched by
    what those words *mean*: "certificate" finds ``openssl x509``, "port"
    finds ``lsof -i``, "disk space" finds ``du -sh``.

    Expansion is a fallback rather than a blend, and that is the design, not
    an implementation shortcut. Three properties fall out of it, all of which
    were requirements:

    - **A literal match always outranks an expanded one.** Not by a weight
      that could be overcome by frequency and recency — expanded results are
      only ever consulted when there are no literal ones at all. Someone who
      typed ``openssl`` gets ``openssl``, never everything tagged
      "certificate".
    - **Expansion can only add.** Every result the substring search would have
      returned is still returned, in the same order, with the same score. The
      concept map cannot demote, dilute or reorder an answer that already
      worked, which is the property that makes a hand-edited file safe to ship
      — a bad entry can waste a fallback, never damage a working query.
    - **It costs nothing when it is not needed.** The map is not even loaded
      for a query that matches something.

    The price is that a query with one poor literal match gets no help. That
    is the right trade for a tool whose users type shell fragments far more
    often than sentences: it is better to answer ``docker compose`` exactly
    than to pad it with guesses.

    ``expand=False`` disables the fallback, leaving pure substring search. It
    exists so the recall benchmark in ``tests/test_concepts.py`` can measure
    both halves against one history.
    """
    terms = _terms(query)
    if not terms:
        return []

    literal = _literal_search(terms, query, current_repo)
    if literal or not expand:
        return literal[:limit]

    return _expanded_search(terms, query, current_repo)[:limit]


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
