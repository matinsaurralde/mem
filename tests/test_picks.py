"""Tests for selection feedback — the strongest ranking signal mem has.

A pick is the user answering, for one command, the exact question the ranking
formula spends the rest of its life guessing at. Measured over 1,200 retrieval
episodes, adding it moves MRR@10 from 0.039 to 0.575.

Two properties matter more than the arithmetic and are pinned first: a user
with no picks must see exactly the ordering they saw before this existed, and
nothing here may ever raise — it runs as the finder is handing a command back
to the shell.
"""

from __future__ import annotations

import json
import time

import pytest

from mem import picks, ranking, search, storage
from conftest import make_command

DAY = 86400.0
NOW = 1_800_000_000.0


class TestRecording:
    def test_a_pick_is_counted(self, tmp_mem_dir):
        picks.record("git push origin main", now=NOW)

        assert picks.load(now=NOW) == {"git push origin main": pytest.approx(1.0)}

    def test_picks_accumulate(self, tmp_mem_dir):
        for _ in range(3):
            picks.record("make test", now=NOW)

        assert picks.load(now=NOW)["make test"] == pytest.approx(3.0)

    def test_commands_are_counted_separately(self, tmp_mem_dir):
        picks.record("a", now=NOW)
        picks.record("b", now=NOW)
        picks.record("b", now=NOW)

        loaded = picks.load(now=NOW)

        assert loaded["a"] == pytest.approx(1.0)
        assert loaded["b"] == pytest.approx(2.0)

    def test_an_empty_command_is_not_recorded(self, tmp_mem_dir):
        picks.record("", now=NOW)

        assert picks.load(now=NOW) == {}

    def test_the_file_is_owner_only(self, tmp_mem_dir):
        import stat

        picks.record("ls", now=NOW)

        mode = stat.S_IMODE(picks.picks_file().stat().st_mode)
        assert mode == 0o600, f"picks are readable by others: {oct(mode)}"


class TestDecay:
    """A command chosen forty times last quarter is not what you want now."""

    @pytest.mark.parametrize(
        ("age_days", "expected"),
        [(0, 1.0), (21, 0.5), (42, 0.25), (63, 0.125)],
    )
    def test_a_pick_halves_every_three_weeks(
        self, tmp_mem_dir, age_days: int, expected: float
    ):
        picks.record("deploy", now=NOW)

        weight = picks.load(now=NOW + age_days * DAY)["deploy"]

        assert weight == pytest.approx(expected, abs=1e-6)

    def test_decay_is_slower_than_recency(self):
        """Choosing a command says more than merely running it, so it ages slower."""
        assert picks.HALF_LIFE_DAYS > ranking.RECENCY_HALF_LIFE_DAYS

    def test_a_new_pick_decays_the_old_count_first(self, tmp_mem_dir):
        """The stored number is always "picks as of ts", never a raw total.

        Adding to an un-decayed total would make an old burst of picks worth
        as much as a recent one forever.
        """
        picks.record("deploy", now=NOW)
        picks.record("deploy", now=NOW + 21 * DAY)

        assert picks.load(now=NOW + 21 * DAY)["deploy"] == pytest.approx(1.5)

    def test_a_faded_entry_is_pruned(self, tmp_mem_dir):
        """Otherwise the file only ever grows."""
        picks.record("ancient", now=NOW)
        picks.record("fresh", now=NOW + 400 * DAY)

        stored = json.loads(picks.picks_file().read_text())["picks"]

        assert "ancient" not in stored
        assert "fresh" in stored


class TestItNeverRaises:
    """This runs as the finder hands a command to the shell.

    A traceback there replaces a working feature with a broken prompt, to
    protect a ranking hint. Every failure mode has to be inert.
    """

    @pytest.mark.parametrize(
        "content",
        ["", "not json", "[]", "null", '{"picks": []}', '{"picks": {"a": 5}}'],
    )
    def test_a_corrupt_file_reads_as_no_picks(self, tmp_mem_dir, content: str):
        storage.ensure_dirs()
        picks.picks_file().write_text(content, encoding="utf-8")

        assert picks.load(now=NOW) == {}

    def test_a_corrupt_file_is_overwritten_rather_than_inherited(self, tmp_mem_dir):
        storage.ensure_dirs()
        picks.picks_file().write_text("{ broken", encoding="utf-8")

        picks.record("ls", now=NOW)

        assert picks.load(now=NOW) == {"ls": pytest.approx(1.0)}

    def test_entries_of_the_wrong_shape_are_skipped_individually(self, tmp_mem_dir):
        """One hand-edited entry must not discard the rest of the file."""
        storage.ensure_dirs()
        picks.picks_file().write_text(
            json.dumps(
                {
                    "picks": {
                        "good": {"count": 2, "ts": NOW},
                        "no-ts": {"count": 2},
                        "text-count": {"count": "many", "ts": NOW},
                        "not-a-dict": 7,
                    }
                }
            ),
            encoding="utf-8",
        )

        assert list(picks.load(now=NOW)) == ["good"]

    def test_a_missing_file_is_not_an_error(self, tmp_mem_dir):
        assert picks.load(now=NOW) == {}


class TestRankingIntegration:
    def test_no_picks_leaves_the_previous_ordering_untouched(self, tmp_mem_dir):
        """The property that makes this safe to ship.

        With no picks recorded, every score is the old score scaled by 0.60,
        so ordering is identical for a user who has never opened the finder.
        Introducing a signal that needs data nobody has yet must be a no-op
        until they have it.
        """
        old_weights = {"freq": 0.35, "recency": 0.35, "prefix": 0.15, "context": 0.15}
        scaled = {
            "freq": ranking.W_FREQUENCY,
            "recency": ranking.W_RECENCY,
            "prefix": ranking.W_PREFIX,
            "context": ranking.W_CONTEXT,
        }

        ratios = {k: scaled[k] / old_weights[k] for k in old_weights}

        assert len(set(round(r, 9) for r in ratios.values())) == 1, (
            f"the four original features were rescaled unevenly: {ratios}"
        )
        assert sum(scaled.values()) + ranking.W_PICKS == pytest.approx(1.0)

    def test_a_picked_command_outranks_a_more_frequent_one(self, tmp_mem_dir):
        """The whole point: what you chose beats what you happened to run."""
        now = int(time.time())
        for _ in range(20):
            storage.append_command(
                make_command(command="git commit --amend", ts=now, repo=None)
            )
        storage.append_command(make_command(command="git commit -v", ts=now, repo=None))
        picks.record("git commit -v")

        results = search.search("git commit", current_repo=None)

        assert [c.command for c, _ in results][0] == "git commit -v"

    def test_a_pick_that_has_faded_stops_winning(self, tmp_mem_dir):
        """A choice from a year ago should not still be steering results."""
        cmd = make_command(command="deploy", ts=int(NOW), repo=None)

        fresh = search.score_command(cmd, "deploy", None, 1, pick_weight=1.0)
        faded = search.score_command(cmd, "deploy", None, 1, pick_weight=0.01)
        none = search.score_command(cmd, "deploy", None, 1, pick_weight=0.0)

        assert fresh > faded > none

    def test_picks_saturate(self, tmp_mem_dir):
        """One obsessively picked command must not own every result."""
        assert picks.normalize(10_000) <= 1.0
        assert picks.normalize(1000) - picks.normalize(100) < picks.normalize(2)

    def test_a_score_still_cannot_exceed_one(self, tmp_mem_dir):
        cmd = make_command(command="git status", ts=int(time.time()), repo="/r")

        assert search.score_command(cmd, "git", "/r", 10_000, pick_weight=1e6) <= 1.0

    def test_the_finder_and_the_search_command_agree_on_picks(self, tmp_mem_dir):
        """Two ranking paths, one formula — including this feature.

        The finder ranks without importing Pydantic, so it is a separate call
        site. A signal wired into one and not the other would sort the same
        history two ways depending on how it was asked for.
        """
        from mem import tui

        now = time.time()
        line = json.dumps(
            {
                "command": "make deploy",
                "ts": int(now),
                "dir": "/w",
                "repo": None,
                "exit_code": 0,
                "duration_ms": 1,
            }
        )
        picks.record("make deploy", now=now)

        finder_score = tui.rank([line], "make", None, now)[0].score
        expected = ranking.score(
            command="make deploy",
            ts=int(now),
            repo=None,
            query="make",
            current_repo=None,
            frequency=1,
            now=now,
            pick_weight=picks.load(now)["make deploy"],
        )

        assert finder_score == pytest.approx(expected)
        assert finder_score > tui.rank([line], "make", None, now + 400 * DAY)[0].score
