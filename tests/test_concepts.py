"""Tests for the concept map and the query expansion built on it.

The first class is the one that matters. Everything else in this file checks a
mechanism; :class:`TestRecall` checks the *claim* — that a hand-written
dictionary turns questions a person would actually ask into the commands they
actually ran. A feature justified by a measurement needs the measurement in
the suite, or the number is folklore by the second refactor.
"""

from __future__ import annotations

import json
import time

import pytest

from conftest import make_command
from mem import concepts, ranking, search, storage

REPO = "/Users/test/projects/myapp"


@pytest.fixture(autouse=True)
def _fresh_concept_cache():
    """Drop the memoised map around every test.

    The loader caches on (path, mtime, size), and a test that writes
    ``~/.mem/concepts.json`` twice inside one filesystem timestamp tick would
    otherwise read the first version back.
    """
    concepts.clear_cache()
    yield
    concepts.clear_cache()


# --- The measurement --------------------------------------------------------


# A small but realistic history: the commands a working developer runs, plus
# the noise they run far more often. The noise is the point — recall is easy
# against ten commands and hard against ten commands buried in fifty.
HISTORY = [
    "openssl x509 -in cert.pem -noout -dates",
    "lsof -i :8080",
    "du -sh * | sort -h",
    "docker logs -f api --tail 100",
    'pkill -f "node server.js"',
    "git reset --hard HEAD~1",
    "tar -czf backup.tar.gz ./data",
    "pg_dump -U postgres mydb > dump.sql",
    "ssh -L 5432:localhost:5432 user@host -N",
    "chmod 755 deploy.sh",
    # Noise, including words the questions use ("fix", "check", "show").
    "git status",
    "git add -A",
    'git commit -m "fix flaky test"',
    'git commit -m "check the release notes"',
    "git push origin main",
    "git pull --rebase",
    "git log --oneline -20",
    "npm run dev",
    "npm install",
    "npm test",
    "ls -la",
    "cd ../api",
    "make build",
    "make test",
    "vim README.md",
    "cat package.json",
    "code .",
    "brew update",
    "kubectl get pods",
    "docker ps",
    "curl -s https://api.example.com/health",
    "echo $SHELL",
    "python3 -m pytest -q",
    "ruff check .",
    "gh pr create --fill",
]

# Questions phrased the way they are actually asked, with the one command in
# HISTORY that answers each. None of them share a word with their answer,
# which is exactly why substring search scores zero.
QUESTIONS = [
    (
        "the command I used to fix the certificate",
        "openssl x509 -in cert.pem -noout -dates",
    ),
    ("how do I see what's listening on a port", "lsof -i :8080"),
    ("how to check disk space", "du -sh * | sort -h"),
    ("show me the docker container logs", "docker logs -f api --tail 100"),
    ("how do I kill a process by name", 'pkill -f "node server.js"'),
    ("undo my last commit", "git reset --hard HEAD~1"),
    ("how do I create an archive of a folder", "tar -czf backup.tar.gz ./data"),
    ("dump the postgres database", "pg_dump -U postgres mydb > dump.sql"),
    ("how do I set up an ssh tunnel", "ssh -L 5432:localhost:5432 user@host -N"),
    ("change file permissions to executable", "chmod 755 deploy.sh"),
]

RECALL_AT = 5


def _recall(expand: bool) -> tuple[int, list[str]]:
    """Answer every question and count how often the right command is top-5."""
    hits = 0
    missed: list[str] = []
    for question, answer in QUESTIONS:
        found = [
            cmd.command
            for cmd, _ in search.search(
                question, current_repo=REPO, limit=RECALL_AT, expand=expand
            )
        ]
        if answer in found:
            hits += 1
        else:
            missed.append(question)
    return hits, missed


@pytest.fixture
def history(tmp_mem_dir):
    """Write HISTORY to the current repo, newest command last."""
    now = int(time.time())
    for offset, command in enumerate(HISTORY):
        storage.append_command(
            make_command(
                command=command, ts=now - (len(HISTORY) - offset) * 3600, repo=REPO
            )
        )


class TestRecall:
    """recall@5 over ten natural-language questions against a real history.

    The numbers this pins are the ones the feature was justified by. Measured
    on a developer's own history, the four approaches scored:

    | approach                         | recall@5 | latency |
    |----------------------------------|----------|---------|
    | substring search                 | 0/10     | 9.6 ms  |
    | Apple Foundation Models expansion| 2/10     | 2086 ms |
    | hybrid ranker, no synonyms       | 2/10     | 2.4 ms  |
    | hybrid ranker + concept map      | 8/10     | 3.9 ms  |

    A dictionary beat the on-device LLM by 4x on accuracy and 500x on latency.
    """

    def test_substring_search_answers_none_of_them(self, history) -> None:
        """The baseline, and the reason this feature exists: 0/10."""
        hits, _ = _recall(expand=False)

        assert hits == 0

    def test_the_concept_map_answers_most_of_them(self, history) -> None:
        hits, missed = _recall(expand=True)

        assert hits >= 8, f"recall@5 fell to {hits}/10; missed: {missed}"

    def test_expansion_only_ever_adds(self, history) -> None:
        """Every result substring search returns is still returned, in order.

        The property that makes a hand-edited map safe to ship: a bad entry
        can waste a fallback, but it cannot damage a query that already works.
        """
        for query in ("git commit", "docker", "openssl", "make", "npm run"):
            literal = search.search(query, REPO, limit=50, expand=False)
            expanded = search.search(query, REPO, limit=50)

            assert [c.command for c, _ in expanded][: len(literal)] == [
                c.command for c, _ in literal
            ]
            assert [s for _, s in expanded][: len(literal)] == pytest.approx(
                [s for _, s in literal], abs=1e-6
            )

    def test_a_literal_match_outranks_everything_the_map_finds(self, history) -> None:
        """Someone who typed `openssl` wants openssl, not everything TLS."""
        results = search.search("openssl", current_repo=REPO)

        assert [cmd.command for cmd, _ in results] == [
            "openssl x509 -in cert.pem -noout -dates"
        ]


# --- The rule ---------------------------------------------------------------


class TestMatchingRule:
    def test_a_term_matching_nothing_still_returns_nothing(self, history) -> None:
        """The #10 invariant survives expansion.

        Loosening the query's AND into an OR would answer this with every
        docker command, and the user could not tell their second word was
        ignored. Expansion is allowed to *add* what the concept map found —
        never to quietly drop a word.
        """
        assert search.search("docker zzzz-no-such-term", current_repo=REPO) == []

    def test_a_command_needs_more_than_a_shared_english_word(self, history) -> None:
        """`git commit -m "fix flaky test"` is not the answer to a question about
        fixing a certificate, however literally it matches the word "fix"."""
        found = [
            cmd.command
            for cmd, _ in search.search(
                "the command I used to fix the certificate", REPO, limit=10
            )
        ]

        assert 'git commit -m "fix flaky test"' not in found
        assert "openssl x509 -in cert.pem -noout -dates" in found

    def test_expansion_is_not_consulted_when_the_query_matches(self, history) -> None:
        """A query with literal hits never pays for the map, or shows its results."""
        found = [cmd.command for cmd, _ in search.search("logs", REPO, limit=50)]

        assert found == ["docker logs -f api --tail 100"]

    def test_an_unknown_single_word_finds_nothing(self, tmp_mem_dir) -> None:
        storage.append_command(make_command(command="git status", repo=REPO))

        assert search.search("zzzznotaword", current_repo=REPO) == []

    def test_multi_word_concepts_are_matched_as_one(self, tmp_mem_dir) -> None:
        """ "disk space" is a concept; "disk" AND "space" are two vague ones."""
        data = concepts.load(None)

        groups = concepts.expand(["how", "much", "disk", "space"], data)

        assert [g.text for g in groups] == ["much", "disk space"]

    def test_question_scaffolding_is_dropped(self, tmp_mem_dir) -> None:
        data = concepts.load(None)

        groups = concepts.expand(
            ["how", "do", "i", "check", "the", "certificate"], data
        )

        assert [g.text for g in groups] == ["check", "certificate"]

    def test_a_query_of_pure_scaffolding_is_still_searched(self, tmp_mem_dir) -> None:
        """`mem how to` is a strange search, but it is not an empty one."""
        data = concepts.load(None)

        groups = concepts.expand(["how", "to"], data)

        assert [g.text for g in groups] == ["how", "to"]


# --- idf weighting ----------------------------------------------------------


class TestIdfWeighting:
    def test_a_synonym_matching_everything_carries_no_weight(self) -> None:
        """The property that makes a hand-written map safe.

        A concept whose synonym appears in every candidate cannot separate
        them, so it must not be able to move the ranking. This is what stops a
        careless `"version control": ["git"]` from dragging every git command
        into every question that mentions version control.
        """
        assert ranking.idf(document_frequency=100, n_documents=100) == pytest.approx(
            0.0, abs=0.01
        )
        assert ranking.idf(document_frequency=50, n_documents=100) < 0.2
        assert ranking.idf(document_frequency=1, n_documents=100) == pytest.approx(1.0)

    def test_idf_is_monotonic(self) -> None:
        weights = [ranking.idf(df, 1000) for df in (1, 10, 100, 500, 1000)]

        assert weights == sorted(weights, reverse=True)

    def test_a_single_document_is_not_a_division_by_zero(self) -> None:
        assert ranking.idf(1, 1) == 1.0

    def test_coverage_weights_concepts_by_how_much_they_narrow(self) -> None:
        """A hit on a rare concept beats a hit on a common one."""
        rare_hit = ranking.coverage(credits=[1.0, 0.0], weights=[0.9, 0.1])
        common_hit = ranking.coverage(credits=[0.0, 1.0], weights=[0.9, 0.1])

        assert rare_hit > common_hit

    def test_coverage_falls_back_when_nothing_discriminates(self) -> None:
        """All weights zero — every concept matched every candidate."""
        assert ranking.coverage(credits=[1.0, 0.0], weights=[0.0, 0.0]) == 0.5
        assert ranking.coverage(credits=[], weights=[]) == 0.0

    def test_an_expanded_score_can_never_exceed_a_perfect_one(self) -> None:
        assert ranking.expanded_score(1.0, 1.0) == 1.0
        assert ranking.expanded_score(1.0, 0.5) == 0.5

    def test_a_useless_synonym_cannot_outrank_a_useful_one(self, tmp_mem_dir) -> None:
        """End to end: a concept satisfied by a term present everywhere loses.

        Every command here contains "git", so a query concept satisfied only
        through "git" has learned nothing about which one to return.
        """
        now = int(time.time())
        for command in (
            "git commit -m wip",
            "git status",
            "git push",
            "git log",
            "git config --global user.name",
        ):
            for _ in range(5):
                storage.append_command(make_command(command=command, ts=now, repo=REPO))
        storage.append_command(
            make_command(
                command="git config --global user.email a@b.c", ts=now, repo=REPO
            )
        )

        user_map = tmp_mem_dir / "concepts.json"
        user_map.write_text(
            json.dumps({"vcs": ["git"], "email": ["user.email", "--global user.email"]})
        )

        results = search.search("vcs email", current_repo=REPO, limit=3)

        assert results[0][0].command == "git config --global user.email a@b.c"


# --- The user's own map -----------------------------------------------------


class TestUserMap:
    def test_a_user_concept_is_searchable(self, tmp_mem_dir) -> None:
        storage.append_command(make_command(command="pnpm run typecheck", repo=REPO))
        (tmp_mem_dir / "concepts.json").write_text(
            json.dumps({"types": ["typecheck", "tsc"]})
        )

        results = search.search("check the types", current_repo=REPO)

        assert [c.command for c, _ in results] == ["pnpm run typecheck"]

    def test_a_user_concept_replaces_the_shipped_one(self, tmp_mem_dir) -> None:
        (tmp_mem_dir / "concepts.json").write_text(json.dumps({"port": ["myportcmd"]}))

        data = concepts.load(tmp_mem_dir / "concepts.json")

        assert data.concepts["port"] == ("myportcmd",)
        assert "certificate" in data.concepts  # everything else survives

    def test_user_stopwords_are_added_not_substituted(self, tmp_mem_dir) -> None:
        (tmp_mem_dir / "concepts.json").write_text(json.dumps({"_stopwords": ["cómo"]}))

        data = concepts.load(tmp_mem_dir / "concepts.json")

        assert "cómo" in data.stopwords
        assert "how" in data.stopwords

    def test_an_edit_takes_effect_without_a_restart(self, tmp_mem_dir) -> None:
        path = tmp_mem_dir / "concepts.json"
        path.write_text(json.dumps({"widget": ["first"]}))
        assert concepts.load(path).concepts["widget"] == ("first",)

        path.write_text(json.dumps({"widget": ["second"]}))

        assert concepts.load(path).concepts["widget"] == ("second",)

    def test_broken_json_warns_and_falls_back(self, tmp_mem_dir, capsys) -> None:
        path = tmp_mem_dir / "concepts.json"
        path.write_text('{"port": ["lsof",}')

        data = concepts.load(path)

        assert "certificate" in data.concepts
        err = capsys.readouterr().err
        assert "not valid JSON" in err
        assert "built-in concept map" in err

    @pytest.mark.parametrize(
        "content",
        [
            '["certificate", "openssl"]',  # a list, not a map
            '{"certificate": "openssl"}',  # a string, not a list
            '{"certificate": [1, 2]}',  # not strings
            '{"_stopwords": {"the": true}}',  # reserved key, wrong type
        ],
    )
    def test_a_map_of_the_wrong_shape_warns_and_falls_back(
        self, tmp_mem_dir, capsys, content: str
    ) -> None:
        path = tmp_mem_dir / "concepts.json"
        path.write_text(content)

        data = concepts.load(path)

        assert data.concepts["port"][0] == "lsof"
        assert "is not a concept map" in capsys.readouterr().err

    def test_an_unreadable_map_warns_and_falls_back(self, tmp_mem_dir, capsys) -> None:
        path = tmp_mem_dir / "concepts.json"
        path.write_text("{}")
        path.chmod(0o000)
        try:
            data = concepts.load(path)
        finally:
            path.chmod(0o600)

        assert "certificate" in data.concepts
        assert "could not read" in capsys.readouterr().err

    def test_a_broken_map_never_breaks_search(self, tmp_mem_dir, capsys) -> None:
        """The whole point of the fallback: `mem <query>` still answers."""
        storage.append_command(
            make_command(command="openssl x509 -in cert.pem", repo=REPO)
        )
        (tmp_mem_dir / "concepts.json").write_text("not json at all")

        results = search.search("check the certificate", current_repo=REPO)

        assert [c.command for c, _ in results] == ["openssl x509 -in cert.pem"]

    def test_no_user_map_is_the_normal_case(self, tmp_mem_dir) -> None:
        data = concepts.load(tmp_mem_dir / "concepts.json")

        assert "certificate" in data.concepts

    def test_comments_are_allowed_and_ignored(self, tmp_mem_dir) -> None:
        (tmp_mem_dir / "concepts.json").write_text(
            json.dumps({"_comment": "mine", "_unknown": ["x"], "thing": ["mycmd"]})
        )

        data = concepts.load(tmp_mem_dir / "concepts.json")

        assert data.concepts["thing"] == ("mycmd",)
        assert "_comment" not in data.concepts
        assert "_unknown" not in data.concepts

    def test_a_concept_cannot_vouch_for_itself(self, tmp_mem_dir) -> None:
        """A synonym equal to its own key is already covered by the literal test."""
        (tmp_mem_dir / "concepts.json").write_text(
            json.dumps({"grep": ["grep", "rg ", "grep"]})
        )

        data = concepts.load(tmp_mem_dir / "concepts.json")

        assert data.concepts["grep"] == ("rg ",)

    def test_an_overlong_key_is_ignored(self, tmp_mem_dir) -> None:
        """Keys are concepts, not sentences; the phrase scan has to be bounded."""
        (tmp_mem_dir / "concepts.json").write_text(
            json.dumps({"a b c d e": ["nope"], "ok": ["yes"]})
        )

        data = concepts.load(tmp_mem_dir / "concepts.json")

        assert "a b c d e" not in data.concepts
        assert data.concepts["ok"] == ("yes",)

    def test_an_empty_synonym_list_is_dropped(self, tmp_mem_dir) -> None:
        (tmp_mem_dir / "concepts.json").write_text(json.dumps({"empty": ["", " "]}))

        assert "empty" not in concepts.load(tmp_mem_dir / "concepts.json").concepts


# --- The shipped map --------------------------------------------------------


class TestShippedMap:
    """Curation rules, enforced rather than described.

    A concept map is data, and data rots quietly: a synonym added in a hurry
    is invisible until someone notices their search got worse.
    """

    @pytest.fixture
    def shipped(self):
        return concepts.load(None)

    def test_it_is_big_enough_to_be_useful(self, shipped) -> None:
        assert 150 <= len(shipped.concepts) <= 250

    def test_every_key_is_lowercase_and_normalised(self, shipped) -> None:
        for key in shipped.concepts:
            assert key == " ".join(key.lower().split())

    def test_every_synonym_survives_the_prefilter(self, shipped) -> None:
        """The OR prefilter is only sound if every alternative has a needle.

        One synonym reducing to nothing usable disables the prefilter for
        every query that touches its concept — correct, but it silently parses
        the entire history. Failing here instead makes that a review comment.
        """
        for concept, expansions in shipped.concepts.items():
            for expansion in expansions:
                needles = storage.prefilter_needles([expansion], min_length=2)
                assert needles, f"{concept!r}: {expansion!r} reduces to no needle"

    def test_no_synonym_is_a_bare_stopword(self, shipped) -> None:
        """A synonym that is also scaffolding would match half the history."""
        for concept, expansions in shipped.concepts.items():
            for expansion in expansions:
                assert expansion not in shipped.stopwords, f"{concept}: {expansion}"

    def test_no_concept_is_also_a_stopword(self, shipped) -> None:
        """A word cannot be both meaningless and a concept; the map would never
        reach the concept, because stopwords are dropped first."""
        assert not set(shipped.concepts) & shipped.stopwords

    def test_synonyms_are_unique_within_a_concept(self, shipped) -> None:
        for concept, expansions in shipped.concepts.items():
            assert len(set(expansions)) == len(expansions), concept

    def test_it_ships_inside_the_package(self) -> None:
        """Read through importlib.resources, like the shell hooks.

        A path walked up from ``__file__`` works in a checkout and fails in a
        wheel, which is how a stale hook reached every pip user for months.
        """
        from importlib import resources

        raw = (
            resources.files("mem")
            .joinpath(concepts.CONCEPTS_FILENAME)
            .read_text(encoding="utf-8")
        )

        assert isinstance(json.loads(raw), dict)

    def test_a_missing_shipped_map_degrades_to_literal_search(
        self, tmp_mem_dir, monkeypatch, capsys
    ) -> None:
        """A packaging fault must not take `mem <query>` down with it."""
        monkeypatch.setattr(concepts, "CONCEPTS_FILENAME", "no-such-file.json")
        storage.append_command(make_command(command="git status", repo=REPO))

        assert search.search("check the certificate", current_repo=REPO) == []
        assert "expansion is off" in capsys.readouterr().err

    def test_a_malformed_shipped_map_degrades_to_literal_search(
        self, tmp_mem_dir, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(concepts, "_parse", lambda raw: None)

        data = concepts.load(None)

        assert data.concepts == {}
        assert "expansion is off" in capsys.readouterr().err


# --- The prefilter ----------------------------------------------------------


class TestExpansionPrefilter:
    def test_it_never_changes_the_answer(self, history, monkeypatch) -> None:
        """Differential check: the optimised path and the unfiltered one agree.

        The prefilter skips `json.loads` for lines that cannot match. Unlike
        the literal pass it ORs its needles, so dropping one silently loses
        results — the failure mode nobody notices.
        """
        with_filter = {q: _answers(q) for q, _ in QUESTIONS}

        monkeypatch.setattr(search, "_any_variant", lambda groups: None)
        without_filter = {q: _answers(q) for q, _ in QUESTIONS}

        assert with_filter == without_filter

    def test_it_is_abandoned_when_a_synonym_has_no_needle(self, tmp_mem_dir) -> None:
        """A user's one-character synonym costs speed, never correctness."""
        storage.append_command(make_command(command="ps aux | grep node", repo=REPO))
        (tmp_mem_dir / "concepts.json").write_text(
            json.dumps({"whatsup": ["x", "ps "]})
        )

        results = search.search("whatsup", current_repo=REPO)

        assert [c.command for c, _ in results] == ["ps aux | grep node"]


class TestConceptsCommand:
    """``mem concepts > ~/.mem/concepts.json`` is the whole customisation story."""

    def test_it_prints_a_map_that_can_be_loaded_back(self, tmp_mem_dir) -> None:
        from click.testing import CliRunner

        from mem.cli import cli

        result = CliRunner().invoke(cli, ["concepts"])

        assert result.exit_code == 0
        (tmp_mem_dir / "concepts.json").write_text(result.stdout)
        data = concepts.load(tmp_mem_dir / "concepts.json")
        assert data.concepts == concepts.load(None).concepts
        assert data.stopwords == concepts.load(None).stopwords


def _answers(query: str) -> list[str]:
    return [cmd.command for cmd, _ in search.search(query, REPO, limit=RECALL_AT)]
