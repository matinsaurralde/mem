"""Tests for the search and ranking engine."""

from __future__ import annotations

import math
import time

import pytest

from conftest import make_command
from mem import search, storage
from mem.models import CommandPattern, PatternFile, WorkSession

REPO = "/Users/test/projects/myapp"
SIBLING = "/Users/test/projects/other-repo"
UNRELATED = "/Users/someone/elsewhere/tool"

FROZEN_NOW = 1_700_000_000


class _FrozenClock:
    """Minimal stand-in for the `time` module with a fixed `time()`."""

    def __init__(self, value: float) -> None:
        self._value = value

    def time(self) -> float:
        return self._value


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> int:
    """Freeze the clock `score_command` reads, and return the frozen epoch.

    ``score_command`` calls ``time.time()`` on every invocation, so two calls
    a microsecond apart produce slightly different recency terms. Without a
    frozen clock, exact-value and equality assertions on scores are flaky.
    """
    monkeypatch.setattr(search, "time", _FrozenClock(float(FROZEN_NOW)))
    return FROZEN_NOW


def expected_score(
    frequency: int,
    recency: float,
    prefix: float,
    context: float,
) -> float:
    """Re-derive the documented score from literal weights.

    Deliberately spelled out with literals instead of importing the module's
    own constants: a test that reads its expectation out of the code under
    test cannot fail when that code changes.
    """
    normalized_frequency = min(1.0, math.log1p(frequency) / math.log1p(50))
    return 0.35 * normalized_frequency + 0.35 * recency + 0.15 * prefix + 0.15 * context


class TestScoreFormula:
    """0.35*frequency + 0.35*recency + 0.15*prefix + 0.15*context, all in [0,1].

    These tests assert the numeric score, not just the resulting order.
    Order-only assertions are satisfied by many wrong formulas — for example
    dropping the recency term entirely still orders correctly whenever
    frequency happens to agree with recency, which is the common case in a
    hand-written fixture. That is exactly how the unnormalised frequency term
    survived: every ordering test passed while the formula was, in effect,
    sorting on one signal.
    """

    def test_weights_for_a_current_repo_command(self):
        now = int(time.time())
        cmd = make_command(command="git status", ts=now, repo=REPO)

        score = search.score_command(cmd, "git", current_repo=REPO, frequency=3)

        # run 3 times, run just now, starts with the query, current repo
        assert score == pytest.approx(
            expected_score(3, recency=1.0, prefix=1.0, context=1.0), abs=1e-3
        )

    def test_sibling_repo_gets_half_the_context_boost(self):
        """Repos sharing a parent directory are related, but not the same repo."""
        now = int(time.time())
        cmd = make_command(command="git status", ts=now, repo=SIBLING)

        score = search.score_command(cmd, "git", current_repo=REPO, frequency=1)

        assert score == pytest.approx(
            expected_score(1, recency=1.0, prefix=1.0, context=0.5), abs=1e-3
        )

    def test_unrelated_repo_gets_no_context_boost(self):
        now = int(time.time())
        cmd = make_command(command="git status", ts=now, repo=UNRELATED)

        score = search.score_command(cmd, "git", current_repo=REPO, frequency=1)

        assert score == pytest.approx(
            expected_score(1, recency=1.0, prefix=1.0, context=0.0), abs=1e-3
        )

    def test_context_ladder_is_strictly_ordered(self, frozen_clock: int):
        """same repo > sibling > unrelated, with everything else held equal."""

        def s(repo: str | None) -> float:
            return search.score_command(
                make_command(command="c", ts=frozen_clock, repo=repo),
                "c",
                current_repo=REPO,
                frequency=1,
            )

        assert s(REPO) > s(SIBLING) > s(UNRELATED) == s(None)

    def test_no_current_repo_means_no_context_boost(self):
        now = int(time.time())
        cmd = make_command(command="git status", ts=now, repo=REPO)

        assert search.score_command(
            cmd, "git", current_repo=None, frequency=1
        ) == pytest.approx(
            expected_score(1, recency=1.0, prefix=1.0, context=0.0), abs=1e-3
        )

    @pytest.mark.parametrize(
        ("age_days", "recency"), [(0, 1.0), (7, 0.5), (14, 0.25), (21, 0.125)]
    )
    def test_recency_half_life_is_seven_days(self, age_days: int, recency: float):
        """Exponential decay, halving every 7 days — the memory-fade model."""
        now = int(time.time())
        cmd = make_command(command="c", ts=now - age_days * 86400, repo=None)

        score = search.score_command(cmd, "c", current_repo=None, frequency=1)

        assert score == pytest.approx(
            expected_score(1, recency=recency, prefix=1.0, context=0.0), abs=1e-3
        )

    def test_future_timestamps_are_clamped_to_now(self):
        """Clock skew must not produce a recency bonus above 1.0."""
        now = int(time.time())
        cmd = make_command(command="c", ts=now + 10 * 86400, repo=None)

        assert search.score_command(
            cmd, "c", current_repo=None, frequency=1
        ) == pytest.approx(
            expected_score(1, recency=1.0, prefix=1.0, context=0.0), abs=1e-3
        )

    def test_exit_code_does_not_affect_score(self, frozen_clock: int):
        """Documented v1 decision: failures are captured but not deprioritised."""
        ok = make_command(command="c", ts=frozen_clock, repo=REPO, exit_code=0)
        failed = make_command(command="c", ts=frozen_clock, repo=REPO, exit_code=127)

        assert search.score_command(ok, "c", REPO, 1) == search.score_command(
            failed, "c", REPO, 1
        )


# Commands whose JSONL encoding differs from the text the user typed, plus a
# query that must still find each one. These are the cases where a naive
# "grep the raw line" prefilter silently returns nothing.
ENCODED_COMMANDS = [
    (r"grep \d+ access.log", r"\d+"),
    (r"grep -P '\bword\b' file", r"\bword"),
    ('echo "hello world"', '"hello'),
    (r'sed "s/\\/x/g" f', "sed"),
    ("echo café", "café"),
    ("echo 日本語テスト", "日本語"),
    ("printf 'a\tb'", "printf"),
    ("git commit -m 'fix: don\\'t crash'", "crash"),
    ("curl -H 'X-Auth: {\"k\":1}'", '{"k"'),
    ("awk '{print $1}' f", "$1"),
]


class TestPrefilterNeverDropsAMatch:
    """The raw-line prefilter is an optimisation; it must be invisible.

    Skipping `json.loads` for lines that cannot match is where the speedup
    comes from, but the raw line is *encoded*: `grep \\d` is stored as
    `grep \\\\d`. Testing the raw text for the term as typed would silently
    return nothing — and a search that quietly finds fewer results is far
    worse than a slow one, because nobody notices.
    """

    @pytest.mark.parametrize(("command", "query"), ENCODED_COMMANDS)
    def test_a_command_is_found_by_a_substring_of_itself(
        self, tmp_mem_dir, command: str, query: str
    ) -> None:
        storage.append_command(make_command(command=command, repo=REPO))

        results = search.search(query, current_repo=REPO)

        assert [c.command for c, _ in results] == [command], (
            f"prefilter dropped {command!r} when searching {query!r}"
        )

    def test_the_prefilter_matches_the_unfiltered_answer(self, tmp_mem_dir) -> None:
        """Differential check: filtered and unfiltered reads agree exactly.

        The parametrised cases above are the ones we thought of. This one
        compares the optimised path against reading every line and filtering
        in Python, over every command and every query, so a case nobody
        thought of still fails the build.
        """
        for command, _ in ENCODED_COMMANDS:
            storage.append_command(make_command(command=command, repo=REPO))

        # Read through `read_all_commands` rather than naming the files: the
        # baseline must not depend on how a repo path maps to a filename, or
        # the test silently compares against an empty list the day that
        # mapping changes.
        every_command = [c.command for c in storage.read_all_commands()]

        for _, query in ENCODED_COMMANDS:
            terms = [t for t in query.lower().split() if t]
            unfiltered = sorted(
                c for c in every_command if all(t in c.lower() for t in terms)
            )
            found = sorted(c.command for c, _ in search.search(query, REPO, limit=100))

            assert found == unfiltered, f"prefilter changed the answer for {query!r}"


class TestRankingIsNotJustFrequency:
    """Every signal other than frequency must be able to change the order.

    `frequency` used to enter the formula as a raw count while every other
    feature was bounded by 1, so a command run ten times scored 4.0 against a
    ceiling of 0.6 for recency and context combined. Nothing except frequency
    could ever decide a ranking. These are the orderings that were impossible.
    """

    def test_a_recent_command_beats_a_stale_one_run_far_more_often(
        self, tmp_mem_dir
    ) -> None:
        """Twenty runs six months ago lose to one run today."""
        now = int(time.time())
        for _ in range(20):
            storage.append_command(
                make_command(command="deploy old-way", ts=now - 180 * 86400, repo=REPO)
            )
        storage.append_command(
            make_command(command="deploy new-way", ts=now, repo=REPO)
        )

        results = search.search("deploy", current_repo=REPO)

        assert [c.command for c, _ in results] == ["deploy new-way", "deploy old-way"]

    def test_the_current_repo_beats_an_unrelated_one_run_more_often(
        self, tmp_mem_dir
    ) -> None:
        """Context is a real signal, not decoration."""
        now = int(time.time())
        for _ in range(8):
            storage.append_command(
                make_command(command="make elsewhere", ts=now, repo=UNRELATED)
            )
        storage.append_command(make_command(command="make here", ts=now, repo=REPO))

        results = search.search("make", current_repo=REPO)

        assert [c.command for c, _ in results][0] == "make here"

    def test_the_command_you_meant_beats_one_that_merely_mentions_it(
        self, tmp_mem_dir
    ) -> None:
        """Typing `git push` should surface the command, not a note about it."""
        now = int(time.time())
        for _ in range(6):
            storage.append_command(
                make_command(command='echo "remember to git push"', ts=now, repo=REPO)
            )
        storage.append_command(
            make_command(command="git push origin main", ts=now, repo=REPO)
        )

        results = search.search("git push", current_repo=REPO)

        assert [c.command for c, _ in results][0] == "git push origin main"

    def test_frequency_saturates_instead_of_running_away(self, tmp_mem_dir) -> None:
        """A pathologically repeated command cannot own every result.

        The gap between 1 and 10 runs must be much larger than the gap between
        50 and 500 — that is what a logarithm with a ceiling is for. Under a
        raw count the second gap was fifty times the first.
        """
        cmd = make_command(command="c", ts=int(time.time()), repo=None)

        def score(n: int) -> float:
            return search.score_command(cmd, "c", current_repo=None, frequency=n)

        early_gain = score(10) - score(1)
        late_gain = score(500) - score(50)

        assert late_gain == pytest.approx(0.0, abs=1e-9)
        assert early_gain > 0.05
        assert score(10_000) <= 1.0

    def test_no_score_can_exceed_one(self, tmp_mem_dir) -> None:
        """Every feature is in [0,1] and the weights sum to 1.

        Worth pinning: a score that reads as a fraction is the only reason the
        `--json` output's `score` field means anything to a caller.
        """
        cmd = make_command(command="git status", ts=int(time.time()), repo=REPO)

        assert search.score_command(cmd, "git", REPO, 10_000) <= 1.0


class TestScoring:
    def test_ranks_frequent_commands_higher(self, tmp_mem_dir):
        """Commands run more often should rank higher."""
        now = int(time.time())
        # Run "git status" 5 times, "git log" once
        for _ in range(5):
            storage.append_command(
                make_command(command="git status", ts=now, repo=REPO)
            )
        storage.append_command(make_command(command="git log", ts=now, repo=REPO))

        results = search.search("git", current_repo=REPO)
        commands = [cmd.command for cmd, _ in results]
        assert commands == ["git status", "git log"]

    def test_ranks_recent_commands_higher(self, tmp_mem_dir):
        """Recent commands should rank higher than old ones (same frequency)."""
        now = int(time.time())
        old = now - (30 * 86400)  # 30 days ago
        storage.append_command(
            make_command(command="docker build .", ts=old, repo=REPO)
        )
        storage.append_command(
            make_command(command="docker compose up", ts=now, repo=REPO)
        )

        results = search.search("docker", current_repo=REPO)
        commands = [cmd.command for cmd, _ in results]
        assert commands == ["docker compose up", "docker build ."]

    def test_context_boost_for_same_repo(self, tmp_mem_dir):
        """Commands from the current repo should rank higher."""
        now = int(time.time())
        storage.append_command(make_command(command="make test", ts=now, repo=SIBLING))
        storage.append_command(make_command(command="make test", ts=now, repo=REPO))

        results = search.search("make", current_repo=REPO)

        assert len(results) == 1  # same string -> deduplicated
        top_cmd, top_score = results[0]
        assert top_cmd.repo == REPO
        # frequency 2 across both files, current-repo context
        assert top_score == pytest.approx(
            expected_score(2, recency=1.0, prefix=1.0, context=1.0), abs=1e-3
        )

    def test_sibling_repo_outranks_unrelated_repo(self, tmp_mem_dir):
        """The 0.5 sibling tier must actually change the ranking."""
        now = int(time.time())
        storage.append_command(
            make_command(command="make sibling", ts=now, repo=SIBLING)
        )
        storage.append_command(
            make_command(command="make unrelated", ts=now, repo=UNRELATED)
        )

        scores = {cmd.command: score for cmd, score in search.search("make", REPO)}

        assert scores["make sibling"] > scores["make unrelated"]

    def test_deduplication_keeps_highest_score(self, tmp_mem_dir):
        """Same command string appears once, represented by its BEST occurrence."""
        now = int(time.time())
        storage.append_command(make_command(command="npm run dev", ts=now, repo=REPO))
        storage.append_command(
            make_command(command="npm run dev", ts=now - 30 * 86400, repo=REPO)
        )

        results = search.search("npm", current_repo=REPO)

        assert len(results) == 1
        cmd, score = results[0]
        # The surviving occurrence must be the recent one, not the stale one
        assert cmd.ts == now
        assert score == pytest.approx(
            expected_score(2, recency=1.0, prefix=1.0, context=1.0), abs=1e-3
        )

    def test_results_are_sorted_by_score_descending(self, tmp_mem_dir):
        now = int(time.time())
        for i in range(4):
            for _ in range(i + 1):
                storage.append_command(
                    make_command(command=f"tool run-{i}", ts=now, repo=REPO)
                )

        scores = [score for _, score in search.search("tool", current_repo=REPO)]
        assert scores == sorted(scores, reverse=True)

    def test_limit_caps_the_number_of_results(self, tmp_mem_dir):
        now = int(time.time())
        for i in range(10):
            storage.append_command(
                make_command(command=f"tool run-{i}", ts=now, repo=REPO)
            )

        assert len(search.search("tool", current_repo=REPO, limit=3)) == 3
        assert len(search.search("tool", current_repo=REPO)) == 10

    def test_match_is_case_insensitive(self, tmp_mem_dir):
        storage.append_command(make_command(command="DOCKER Compose Up", repo=REPO))

        results = search.search("docker compose", current_repo=REPO)
        assert [c.command for c, _ in results] == ["DOCKER Compose Up"]

    def test_searches_repos_other_than_the_current_one(self, tmp_mem_dir):
        """History from every repo is reachable, just ranked lower."""
        now = int(time.time())
        storage.append_command(make_command(command="rare-cmd", ts=now, repo=UNRELATED))

        results = search.search("rare-cmd", current_repo=REPO)
        assert [c.command for c, _ in results] == ["rare-cmd"]

    def test_empty_query_returns_empty(self, tmp_mem_dir):
        """Empty query should return no results."""
        storage.append_command(make_command(command="ls", repo=REPO))
        results = search.search("", current_repo=REPO)
        assert results == []

    def test_no_matches_returns_empty(self, tmp_mem_dir):
        """Query with no matches returns empty list, not an error."""
        storage.append_command(make_command(command="git status", repo=REPO))
        results = search.search("nonexistent-tool", current_repo=REPO)
        assert results == []

    def test_global_fallback_when_no_repo(self, tmp_mem_dir):
        """Search works when not inside any git repo."""
        now = int(time.time())
        storage.append_command(make_command(command="ls -la", ts=now, repo=None))

        results = search.search("ls", current_repo=None)
        assert len(results) == 1
        assert results[0][0].command == "ls -la"

    def test_global_history_is_counted_once(self, tmp_mem_dir):
        """_global must not be read twice (it would double every frequency)."""
        now = int(time.time())
        storage.append_command(make_command(command="ls -la", ts=now, repo=None))

        _, score = search.search("ls", current_repo=REPO)[0]
        # frequency 1, not 2
        assert score == pytest.approx(
            expected_score(1, recency=1.0, prefix=1.0, context=0.0), abs=1e-3
        )


class TestSearchPatterns:
    def test_returns_patterns_most_frequent_first(self, tmp_mem_dir):
        """Patterns are ranked by how often the user actually runs them."""
        storage.write_patterns(
            PatternFile(
                tool="kubectl",
                patterns=[
                    CommandPattern(pattern="rare", example="kubectl a", frequency=1),
                    CommandPattern(pattern="common", example="kubectl b", frequency=50),
                    CommandPattern(pattern="mid", example="kubectl c", frequency=7),
                ],
                last_updated=0,
            )
        )

        result = search.search_patterns("kubectl")

        assert [p.pattern for p in result] == ["common", "mid", "rare"]

    def test_unknown_tool_returns_empty_list(self, tmp_mem_dir):
        """No patterns yet is an empty list, never None."""
        assert search.search_patterns("nonexistent") == []


class TestSearchSessions:
    def _session(self, sid: str, started: int, summary: str, commands: list[str]):
        return WorkSession(
            id=sid,
            summary=summary,
            started_at=started,
            ended_at=started + 60,
            dir="",
            repo=REPO,
            commands=commands,
        )

    def test_matches_the_summary(self, tmp_mem_dir):
        storage.append_session(
            self._session("s1", 1700000000, "Debugging auth flow", ["ls"])
        )

        assert [s.id for s in search.search_sessions("auth")] == ["s1"]

    def test_matches_a_command_inside_the_session(self, tmp_mem_dir):
        storage.append_session(
            self._session("s1", 1700000000, "Some work", ["pytest tests/test_auth.py"])
        )

        assert [s.id for s in search.search_sessions("pytest")] == ["s1"]

    def test_matching_is_case_insensitive(self, tmp_mem_dir):
        storage.append_session(
            self._session("s1", 1700000000, "Debugging AUTH flow", ["ls"])
        )

        assert [s.id for s in search.search_sessions("auth")] == ["s1"]

    def test_session_matching_both_summary_and_command_appears_once(self, tmp_mem_dir):
        """The `continue` after a summary hit prevents a duplicate result row."""
        storage.append_session(
            self._session("s1", 1700000000, "running pytest", ["pytest -q"])
        )

        assert [s.id for s in search.search_sessions("pytest")] == ["s1"]

    def test_results_are_most_recent_first(self, tmp_mem_dir):
        """Session search is a timeline: newest first."""
        day = 86400
        storage.append_session(self._session("old", 1700000000, "deploy", ["a"]))
        storage.append_session(
            self._session("newest", 1700000000 + 3 * day, "deploy", ["a"])
        )
        storage.append_session(self._session("mid", 1700000000 + day, "deploy", ["a"]))

        assert [s.id for s in search.search_sessions("deploy")] == [
            "newest",
            "mid",
            "old",
        ]

    def test_empty_query_returns_empty(self, tmp_mem_dir):
        storage.append_session(self._session("s1", 1700000000, "work", ["ls"]))
        assert search.search_sessions("") == []

    def test_no_match_returns_empty(self, tmp_mem_dir):
        storage.append_session(self._session("s1", 1700000000, "work", ["ls"]))
        assert search.search_sessions("kubernetes") == []
