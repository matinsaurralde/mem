"""Tests for the search and ranking engine."""

from __future__ import annotations

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


class TestScoreFormula:
    """The documented formula: 0.4*frequency + 0.4*recency + 0.2*context.

    These tests assert the numeric score, not just the resulting order.
    Order-only assertions are satisfied by many wrong formulas — for example
    dropping the recency term entirely still orders correctly whenever
    frequency happens to agree with recency, which is the common case in a
    hand-written fixture.
    """

    def test_weights_for_a_current_repo_command(self):
        now = int(time.time())
        cmd = make_command(command="git status", ts=now, repo=REPO)

        score = search.score_command(cmd, "git", current_repo=REPO, frequency=3)

        # 3*0.4 (frequency) + 1.0*0.4 (recency, run just now) + 1.0*0.2 (context)
        assert score == pytest.approx(1.8, abs=1e-3)

    def test_sibling_repo_gets_half_the_context_boost(self):
        """Repos sharing a parent directory are related, but not the same repo."""
        now = int(time.time())
        cmd = make_command(command="git status", ts=now, repo=SIBLING)

        score = search.score_command(cmd, "git", current_repo=REPO, frequency=1)

        # 0.4 + 0.4 + 0.5*0.2
        assert score == pytest.approx(0.9, abs=1e-3)

    def test_unrelated_repo_gets_no_context_boost(self):
        now = int(time.time())
        cmd = make_command(command="git status", ts=now, repo=UNRELATED)

        score = search.score_command(cmd, "git", current_repo=REPO, frequency=1)

        assert score == pytest.approx(0.8, abs=1e-3)

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
        ) == pytest.approx(0.8, abs=1e-3)

    @pytest.mark.parametrize(
        ("age_days", "recency"), [(0, 1.0), (7, 0.5), (14, 0.25), (21, 0.125)]
    )
    def test_recency_half_life_is_seven_days(self, age_days: int, recency: float):
        """Exponential decay, halving every 7 days — the memory-fade model."""
        now = int(time.time())
        cmd = make_command(command="c", ts=now - age_days * 86400, repo=None)

        score = search.score_command(cmd, "c", current_repo=None, frequency=1)

        assert score == pytest.approx(0.4 + 0.4 * recency, abs=1e-3)

    def test_future_timestamps_are_clamped_to_now(self):
        """Clock skew must not produce a recency bonus above 1.0."""
        now = int(time.time())
        cmd = make_command(command="c", ts=now + 10 * 86400, repo=None)

        assert search.score_command(
            cmd, "c", current_repo=None, frequency=1
        ) == pytest.approx(0.8, abs=1e-3)

    def test_exit_code_does_not_affect_score(self, frozen_clock: int):
        """Documented v1 decision: failures are captured but not deprioritised."""
        ok = make_command(command="c", ts=frozen_clock, repo=REPO, exit_code=0)
        failed = make_command(command="c", ts=frozen_clock, repo=REPO, exit_code=127)

        assert search.score_command(ok, "c", REPO, 1) == search.score_command(
            failed, "c", REPO, 1
        )


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
        assert top_score == pytest.approx(2 * 0.4 + 0.4 + 0.2, abs=1e-3)

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
        assert score == pytest.approx(2 * 0.4 + 0.4 + 0.2, abs=1e-3)

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
        assert score == pytest.approx(0.4 + 0.4, abs=1e-3)


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
