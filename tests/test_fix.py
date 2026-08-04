"""Contract tests for correction mining and ``mem fix``.

The thing under test is a *suggestion engine*, and the failure that matters
is not "found nothing" — it is "confidently proposed an unrelated command as
the fix". So this suite is weighted accordingly: for every test that pins a
pair mem must find, there is at least one pinning a pair it must refuse.

:class:`TestFalsePositives` in particular is the load-bearing class. Every
case in it is a real shell sequence where two neighbouring commands share a
program and a shape but not an intent — one test file after another, one pod
after another, plan then apply. If a future change to the matching rules
starts admitting those, this feature becomes a liar, and that is precisely
the regression a threshold tweak is most likely to introduce.

Every test builds its own history; nothing here reads the developer's real
``~/.mem`` (see ``conftest.tmp_mem_dir``, which is autouse).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from mem import fix, storage
from mem.cli import cli
from mem.models import CapturedCommand

NOW = int(time.time())
REPO = "/Users/dev/work/api"


# --- helpers ---------------------------------------------------------------


def cmd(
    command: str,
    ts: int,
    exit_code: int | None = 0,
    *,
    session: str | None = "session-1",
    directory: str = REPO,
    repo: str | None = REPO,
    duration_ms: int | None = 100,
) -> CapturedCommand:
    """Build one captured command with sane defaults for pairing.

    Defaults are chosen so a test that says nothing about sessions,
    directories or durations gets a pair mem *would* accept — leaving each
    test free to break exactly one condition and assert that this alone is
    what rejected the pair.
    """
    return CapturedCommand(
        command=command,
        ts=ts,
        dir=directory,
        repo=repo,
        exit_code=exit_code,
        duration_ms=duration_ms,
        session=session,
    )


def write_history(commands: list[CapturedCommand], key: str = "work-api") -> Path:
    """Write commands to a repo history file in the given order.

    Raw file writing rather than ``storage.append_command`` because capture
    order is the signal under test, and going through the capture layer would
    stamp its own timestamps over the ones each test carefully chose.
    """
    path = storage.MEM_DIR / "repos" / f"{key}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for command in commands:
            handle.write(command.to_jsonl() + "\n")
    return path


def mined(commands: list[CapturedCommand]) -> dict[str, str]:
    """Mine a command sequence down to a ``{failed: fix}`` mapping."""
    return {c.failed: c.fix for c in fix.mine(commands)}


def run_fix(*args: str) -> str:
    """Invoke ``mem fix`` and return its stdout."""
    result = CliRunner().invoke(cli, ["fix", *args])
    assert result.exception is None, result.exception
    assert result.exit_code == 0
    return result.output


# --- the core pairing rule -------------------------------------------------


class TestCleanPair:
    """The happy path: a failure and the corrected re-typing that followed."""

    def test_a_typo_and_its_correction_are_paired(self) -> None:
        pairs = mined(
            [
                cmd("npm run buld", NOW - 100, 1),
                cmd("npm run build", NOW - 95, 0),
            ]
        )
        assert pairs == {"npm run buld": "npm run build"}

    def test_the_pairing_records_when_and_how_often(self) -> None:
        """Occurrences and timestamps are the output's entire evidence."""
        (correction,) = fix.mine(
            [
                cmd("npm run buld", NOW - 500, 1),
                cmd("npm run build", NOW - 495, 0),
                cmd("npm run buld", NOW - 100, 1),
                cmd("npm run build", NOW - 95, 0),
            ]
        )
        assert correction.occurrences == 2
        assert correction.first_seen == NOW - 500
        assert correction.last_seen == NOW - 100
        assert correction.exit_code == 1

    def test_a_forgotten_sudo_is_the_pair_worth_finding_most(self) -> None:
        """``apt install x`` → ``sudo apt install x``: same command, more rights."""
        pairs = mined(
            [
                cmd("apt install htop", NOW - 40, 100),
                cmd("sudo apt install htop", NOW - 36, 0),
            ]
        )
        assert pairs == {"apt install htop": "sudo apt install htop"}

    def test_a_forgotten_flag_is_paired(self) -> None:
        pairs = mined(
            [
                cmd("npm i", NOW - 40, 1),
                cmd("npm i --legacy-peer-deps", NOW - 30, 0),
            ]
        )
        assert pairs == {"npm i": "npm i --legacy-peer-deps"}

    def test_a_bad_flag_removed_is_paired(self) -> None:
        """Dropping a flag is a fix; dropping an operand is not (see below)."""
        pairs = mined(
            [
                cmd("npm ci --frozen-lockfile", NOW - 40, 1),
                cmd("npm ci", NOW - 30, 0),
            ]
        )
        assert pairs == {"npm ci --frozen-lockfile": "npm ci"}

    def test_diagnostic_commands_between_do_not_break_the_pair(self) -> None:
        """The real sequence is: it broke, I looked, I fixed it."""
        pairs = mined(
            [
                cmd("pytest -q", NOW - 50, 1),
                cmd("ls tests/", NOW - 45, 0),
                cmd("cat pytest.ini", NOW - 43, 0),
                cmd("pytest -q tests/unit", NOW - 40, 0),
            ]
        )
        assert pairs == {"pytest -q": "pytest -q tests/unit"}

    def test_the_lookahead_does_not_reach_a_fourth_command(self) -> None:
        """MAX_LOOKAHEAD is 3; the fourth command is no longer "right after"."""
        pairs = mined(
            [
                cmd("pytest -q", NOW - 50, 1),
                cmd("ls", NOW - 48, 0),
                cmd("cat setup.cfg", NOW - 46, 0),
                cmd("git status", NOW - 44, 0),
                cmd("pytest -q tests/unit", NOW - 42, 0),
            ]
        )
        assert pairs == {}


class TestCommandNotFound:
    """Exit 127 is the only licence to pair two different programs."""

    def test_a_mistyped_program_name_is_paired_when_the_shell_said_127(self) -> None:
        pairs = mined(
            [
                cmd("gti status", NOW - 30, 127),
                cmd("git status", NOW - 28, 0),
            ]
        )
        assert pairs == {"gti status": "git status"}

    def test_the_same_pair_is_refused_under_any_other_exit_code(self) -> None:
        """Without 127 there is no evidence that argv[0] was the mistake.

        The pair is textually identical to the one above; only the exit code
        differs. That is the whole point: the licence comes from the exit
        code, not from the strings looking alike.
        """
        pairs = mined(
            [
                cmd("gti status", NOW - 30, 1),
                cmd("git status", NOW - 28, 0),
            ]
        )
        assert pairs == {}

    def test_127_still_requires_the_names_to_be_a_mistyping(self) -> None:
        """``cd`` is not a mistyping of ``cat``, whatever the exit code."""
        pairs = mined(
            [
                cmd("cat notes.txt", NOW - 30, 127),
                cmd("cd notes", NOW - 28, 0),
            ]
        )
        assert pairs == {}


class TestFalsePositives:
    """Pairs mem must refuse. The reason this feature can be trusted.

    Each case is two adjacent commands that pass every structural filter —
    same program, seconds apart, same session, failure then success — and are
    still not a correction. Only the textual rule can reject them.
    """

    @pytest.mark.parametrize(
        "failed,succeeded,reason",
        [
            ("git status", "git push", "a different subcommand is a different intent"),
            ("git diff", "git add .", "one token became two"),
            ("make clean", "make install", "different targets"),
            ("terraform plan", "terraform apply", "the next step, not a repair"),
            ("npm test", "npm run dev", "unrelated script"),
            (
                "pytest tests/test_a.py",
                "pytest tests/test_b.py",
                "sibling files score higher than real typos on fuzzy similarity",
            ),
            ("kubectl delete pod a", "kubectl delete pod b", "a different pod"),
            ("kubectl apply -f a.yaml", "kubectl apply -f b.yaml", "a different file"),
            ("ssh prod-1", "ssh prod-2", "a different host"),
            ("brew install jq", "brew install yq", "a different package"),
            ("go test ./pkg/a", "go test ./pkg/b", "a different package path"),
            ("mv a b", "mv c d", "two tokens changed"),
            ("docker build .", "docker push img", "a different verb"),
            ("ls /nope", "ls", "succeeded by doing less, not by being fixed"),
        ],
    )
    def test_a_neighbouring_success_is_not_automatically_the_fix(
        self, failed: str, succeeded: str, reason: str
    ) -> None:
        assert mined([cmd(failed, NOW - 30, 1), cmd(succeeded, NOW - 28, 0)]) == {}, (
            reason
        )

    def test_fuzzy_similarity_would_have_got_these_backwards(self) -> None:
        """The measurement that justifies rejecting all substitutions.

        Character similarity ranks two different test files *above* a genuine
        typo, so no threshold on it can separate them. Pinned as a test
        because it is the single fact the whole matching design rests on, and
        a future contributor reaching for ``SequenceMatcher.ratio`` deserves
        to trip over it.
        """
        import difflib

        def ratio(a: str, b: str) -> float:
            return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()

        assert ratio("test_a.py", "test_b.py") > ratio("psuh", "push")
        assert fix.looks_like_typo("psuh", "push")
        assert not fix.looks_like_typo("test_a.py", "test_b.py")

    def test_two_tokens_rewritten_at_once_is_not_a_correction(self) -> None:
        """Even when both rewrites are individually typo-shaped."""
        assert not fix.is_near_variant(
            ["git", "comit", "-m", "wipp"], ["git", "commit", "-m", "wip"]
        )


class TestTypoShape:
    """The unit rule that decides everything above."""

    @pytest.mark.parametrize(
        "wrong,right",
        [
            ("psuh", "push"),  # transposition
            ("docekr", "docker"),
            ("mian", "main"),
            ("buld", "build"),  # dropped character
            ("prd", "prod"),
            ("instal", "install"),
            ("hostt", "host"),  # doubled character
            ("sl", "ls"),
        ],
    )
    def test_finger_errors_are_typos(self, wrong: str, right: str) -> None:
        assert fix.looks_like_typo(wrong, right)
        assert fix.looks_like_typo(right, wrong), "the relation is symmetric"

    @pytest.mark.parametrize(
        "left,right",
        [
            ("push", "pull"),  # substitution: a different subcommand
            ("plan", "apply"),
            ("jq", "yq"),
            ("a", "b"),
            ("prod-1", "prod-2"),
            ("test_a.py", "test_b.py"),
            ("k", "kubectl"),  # an unset alias, not a mistyping
            ("", "ls"),  # nothing is not a mistyping of something
        ],
    )
    def test_substitutions_and_wholesale_differences_are_not(
        self, left: str, right: str
    ) -> None:
        assert not fix.looks_like_typo(left, right)

    def test_a_token_is_not_a_mistyping_of_itself(self) -> None:
        """Guards the caller's substitution budget from being spent on nothing."""
        assert not fix.looks_like_typo("git", "git")

    def test_a_command_is_not_a_correction_of_itself(self) -> None:
        assert not fix.is_plausible_correction("ls -la", "ls -la", 1)


class TestReplacementBlocks:
    """``_replacement_is_admissible`` distinguishes "free" from "refused".

    Returning ``0`` (a block that changed nothing chargeable) and returning
    ``None`` (a block that disqualifies the pair) are different answers, and
    collapsing them into a falsy value would let a rejected block pass.
    """

    def test_a_typo_plus_an_added_flag_costs_one_substitution(self) -> None:
        assert fix._replacement_is_admissible(["buld"], ["build", "--if-present"]) == 1

    def test_a_typo_plus_a_dropped_flag_is_allowed(self) -> None:
        assert fix._replacement_is_admissible(["psh", "-f"], ["push"]) == 1

    def test_a_dropped_operand_disqualifies_the_block(self) -> None:
        assert fix._replacement_is_admissible(["psh", "origin"], ["push"]) is None

    def test_an_unchanged_aligned_token_costs_nothing(self) -> None:
        assert fix._replacement_is_admissible(["build", "-v"], ["build"]) == 0

    def test_an_unrelated_token_disqualifies_the_block(self) -> None:
        assert fix._replacement_is_admissible(["status"], ["push"]) is None


class TestTimeWindow:
    """A correction is typed while the error is still on screen."""

    def test_a_pair_too_far_apart_in_time_is_not_mined(self) -> None:
        pairs = mined(
            [
                cmd("npm run buld", NOW - 5000, 1),
                cmd("npm run build", NOW - 5000 + fix.WINDOW_SECONDS + 5, 0),
            ]
        )
        assert pairs == {}

    def test_a_pair_at_the_edge_of_the_window_is_still_mined(self) -> None:
        pairs = mined(
            [
                cmd("npm run buld", NOW - 5000, 1),
                cmd(
                    "npm run build",
                    NOW - 5000 + fix.WINDOW_SECONDS,
                    0,
                    duration_ms=0,
                ),
            ]
        )
        assert pairs == {"npm run buld": "npm run build"}

    def test_a_slow_fix_is_not_penalised_for_its_own_runtime(self) -> None:
        """mem timestamps completion, so the window must discount duration.

        Without that, a four-minute ``docker build`` typed two seconds after
        the failure would be discarded for being slow rather than unrelated —
        and slow builds are exactly where corrections happen.
        """
        pairs = mined(
            [
                cmd("docker build -t app .", NOW - 400, 1),
                cmd(
                    "docker build -t app . --no-cache",
                    NOW - 400 + 242,
                    0,
                    duration_ms=240_000,
                ),
            ]
        )
        assert pairs == {"docker build -t app .": "docker build -t app . --no-cache"}

    def test_a_candidate_recorded_before_the_failure_is_ignored(self) -> None:
        """Out-of-order lines exist; a fix cannot precede what it fixed."""
        pairs = mined(
            [
                cmd("npm run buld", NOW - 100, 1),
                cmd("npm run build", NOW - 400, 0),
            ]
        )
        assert pairs == {}


class TestSameTerminal:
    """Two tabs in one repo interleave into one history file."""

    def test_a_command_from_another_session_is_not_a_correction(self) -> None:
        pairs = mined(
            [
                cmd("npm run buld", NOW - 30, 1, session="tab-1"),
                cmd("npm run build", NOW - 28, 0, session="tab-2"),
            ]
        )
        assert pairs == {}

    def test_without_sessions_the_directory_is_the_fallback(self) -> None:
        """Pre-session and imported captures still get a sanity check."""
        assert (
            mined(
                [
                    cmd("npm run buld", NOW - 30, 1, session=None, directory="/a"),
                    cmd("npm run build", NOW - 28, 0, session=None, directory="/b"),
                ]
            )
            == {}
        )
        assert mined(
            [
                cmd("npm run buld", NOW - 30, 1, session=None, directory="/a"),
                cmd("npm run build", NOW - 28, 0, session=None, directory="/a"),
            ]
        ) == {"npm run buld": "npm run build"}


class TestExitCodes:
    """Only explicit exit codes count."""

    def test_a_failure_never_fixed_yields_nothing(self) -> None:
        pairs = mined(
            [
                cmd("npm run buld", NOW - 30, 1),
                cmd("npm run buld", NOW - 20, 1),
                cmd("npm run buld", NOW - 10, 1),
            ]
        )
        assert pairs == {}

    def test_a_candidate_that_also_failed_is_not_a_fix(self) -> None:
        pairs = mined(
            [
                cmd("npm run buld", NOW - 30, 1),
                cmd("npm run build", NOW - 28, 1),
            ]
        )
        assert pairs == {}

    def test_imported_commands_carry_no_exit_code_and_are_never_paired(self) -> None:
        """``mem import`` cannot recover exit codes, so it contributes nothing.

        Asserted in both directions: a ``None`` must not be read as a failure
        *or* as a success, because either reading would fabricate evidence out
        of a file that recorded none.
        """
        assert (
            mined(
                [cmd("npm run buld", NOW - 30, None), cmd("npm run build", NOW - 28, 0)]
            )
            == {}
        )
        assert (
            mined(
                [cmd("npm run buld", NOW - 30, 1), cmd("npm run build", NOW - 28, None)]
            )
            == {}
        )

    def test_a_command_that_succeeds_unchanged_is_a_flake_not_a_fix(self) -> None:
        """ "Run it again" is not advice, and it ends the search."""
        pairs = mined(
            [
                cmd("terraform apply", NOW - 40, 1),
                cmd("terraform apply", NOW - 35, 0),
                cmd("terraform apply -refresh=false", NOW - 30, 0),
            ]
        )
        assert pairs == {}


class TestRanking:
    """Several fixes for one failure: pick the best, and say why."""

    def test_the_most_repeated_fix_wins(self) -> None:
        """Repetition is the only evidence of correctness available."""
        commands = []
        for offset in (900, 800, 700):
            commands += [
                cmd("npm run buld", NOW - offset, 1),
                cmd("npm run build", NOW - offset + 4, 0),
            ]
        commands += [
            cmd("npm run buld", NOW - 100, 1),
            cmd("npm run build --if-present", NOW - 96, 0),
        ]

        ranked = fix.fixes_for(fix.mine(commands), "npm run buld")

        assert [c.fix for c in ranked] == [
            "npm run build",
            "npm run build --if-present",
        ]
        assert ranked[0].occurrences == 3
        # The winner is the older observation: repetition beats recency, and
        # this is the case that proves the sort keys are in that order.
        assert ranked[0].last_seen < ranked[1].last_seen

    def test_a_tie_on_repetition_is_broken_by_recency(self) -> None:
        commands = [
            cmd("npm run buld", NOW - 900, 1),
            cmd("npm run build", NOW - 896, 0),
            cmd("npm run buld", NOW - 100, 1),
            cmd("npm run build --if-present", NOW - 96, 0),
        ]

        ranked = fix.fixes_for(fix.mine(commands), "npm run buld")

        assert [c.fix for c in ranked] == [
            "npm run build --if-present",
            "npm run build",
        ]

    def test_a_fix_that_itself_later_failed_is_flagged_and_demoted(self) -> None:
        """Still the best evidence available — but not silently so.

        The fix is reported, because a command that worked twice and has since
        broken is genuinely what fixed this last time. Suppressing it would
        answer "no idea" when mem does in fact know something. Reporting it
        without the caveat would be the dishonest option.
        """
        commands = [
            cmd("npm run buld", NOW - 900, 1),
            cmd("npm run build", NOW - 896, 0),
            cmd("npm run buld", NOW - 800, 1),
            cmd("npm run build --if-present", NOW - 796, 0),
            # The winner has since started failing on its own.
            cmd("npm run build", NOW - 200, 1),
            cmd("npm run build", NOW - 100, 1),
        ]

        ranked = fix.fixes_for(fix.mine(commands), "npm run buld")
        by_command = {c.fix: c for c in ranked}

        assert by_command["npm run build"].fix_failures == 2
        assert by_command["npm run build --if-present"].fix_failures == 0
        # Equal occurrences, so the tie-break on "has it broken since?" runs
        # before recency and puts the still-working fix first.
        assert ranked[0].fix == "npm run build --if-present"

    def test_confidence_wording_tracks_the_count(self) -> None:
        assert fix.confidence(1) == "weak"
        assert fix.confidence(2) == "moderate"
        assert fix.confidence(5) == "strong"


class TestCrossRepoMerge:
    """A fix learned in one checkout is still the fix in another."""

    def test_the_same_pair_from_two_repos_counts_twice(self) -> None:
        left = fix.mine(
            [
                cmd("docker ps", NOW - 900, 1, repo="/a", directory="/a"),
                cmd("sudo docker ps", NOW - 896, 0, repo="/a", directory="/a"),
            ]
        )
        right = fix.mine(
            [
                cmd("docker ps", NOW - 100, 1, repo="/b", directory="/b"),
                cmd("sudo docker ps", NOW - 96, 0, repo="/b", directory="/b"),
            ]
        )

        (merged,) = fix.merge(left + right)

        assert merged.occurrences == 2
        assert merged.first_seen == NOW - 900
        assert merged.last_seen == NOW - 100
        assert merged.repo == "/b"  # attributed to the most recent sighting


class TestEmptyHistory:
    """A machine with nothing captured must answer, not crash."""

    def test_mining_an_empty_store_is_empty(self) -> None:
        assert fix.mine_all() == ([], [])
        assert list(fix.iter_histories()) == []

    def test_build_report_on_an_empty_store(self) -> None:
        report = fix.build_report()
        assert report.failure is None
        assert report.fixes == []

    def test_the_command_says_so_plainly(self) -> None:
        assert "No failed commands captured yet" in run_fix()

    def test_a_query_matching_nothing_says_so_plainly(self) -> None:
        write_history([cmd("git status", NOW - 10, 0)])
        assert "No failed command matching" in run_fix("kubectl")

    def test_an_empty_command_line_is_not_paired(self) -> None:
        """Blank and whitespace-only lines reach the store; they match nothing."""
        assert not fix.is_plausible_correction("", "git status", 1)
        assert not fix.is_plausible_correction("git status", "   ", 1)

    def test_unbalanced_quotes_do_not_crash_the_tokenizer(self) -> None:
        """Half-typed lines are ordinary in a shell history."""
        assert fix.normalized_tokens("echo 'unterminated") == ("echo", "'unterminated")


# --- selection -------------------------------------------------------------


class TestFailureSelection:
    """Which failure ``mem fix`` is about."""

    def test_the_most_recent_failure_is_chosen(self) -> None:
        failures = [
            cmd("old-thing", NOW - 5000, 2),
            cmd("new-thing", NOW - 10, 1),
        ]
        assert fix.select_failure(failures).command == "new-thing"

    def test_the_current_repo_is_preferred_over_a_newer_failure_elsewhere(self) -> None:
        """Someone typing ``mem fix`` has just broken something *here*."""
        failures = [
            cmd("local-thing", NOW - 5000, 1, repo="/here"),
            cmd("other-thing", NOW - 10, 1, repo="/elsewhere"),
        ]
        chosen = fix.select_failure(failures, current_repo="/here")
        assert chosen.command == "local-thing"

    def test_it_falls_back_to_the_whole_store_outside_a_known_repo(self) -> None:
        failures = [cmd("other-thing", NOW - 10, 1, repo="/elsewhere")]
        assert fix.select_failure(failures, current_repo="/here").command == (
            "other-thing"
        )

    def test_every_query_word_must_match(self) -> None:
        failures = [
            cmd("npm run build", NOW - 20, 1),
            cmd("npm run test", NOW - 10, 1),
        ]
        assert fix.select_failure(failures, query="npm build").command == (
            "npm run build"
        )
        assert fix.select_failure(failures, query="npm missing") is None


# --- the command -----------------------------------------------------------


class TestFixCommand:
    """End-to-end through Click, against a real history file."""

    def test_it_reports_the_failure_the_fix_and_the_evidence(self) -> None:
        commands = []
        for offset in (900, 800, 700):
            commands += [
                cmd("npm run buld", NOW - offset, 1),
                cmd("npm run build", NOW - offset + 4, 0),
            ]
        write_history(commands)

        output = run_fix()

        assert "npm run buld" in output
        assert "npm run build" in output
        assert "seen 3 times" in output
        assert "exit 1" in output

    def test_one_observation_is_reported_as_one_observation(self) -> None:
        """A pair seen once is a different claim from a pair seen five times."""
        write_history(
            [
                cmd("npm run buld", NOW - 100, 1),
                cmd("npm run build", NOW - 96, 0),
            ]
        )

        output = run_fix()

        assert "seen once" in output
        assert "one observation only" in output

    def test_a_failure_with_no_known_fix_says_so_without_inventing_one(self) -> None:
        write_history(
            [
                cmd("terraform apply", NOW - 100, 1),
                cmd("git status", NOW - 96, 0),
            ]
        )

        output = run_fix()

        assert "terraform apply" in output
        assert "no record of anything fixing this" in output
        assert "git status" not in output

    def test_a_fix_that_has_since_broken_carries_a_warning(self) -> None:
        write_history(
            [
                cmd("npm run buld", NOW - 900, 1),
                cmd("npm run build", NOW - 896, 0),
                cmd("npm run build", NOW - 100, 1),
            ]
        )

        output = run_fix("buld")

        assert "npm run build" in output
        assert "failed 1 time since" in output

    def test_runner_up_fixes_are_listed_below_the_winner(self) -> None:
        commands = []
        for offset in (900, 800):
            commands += [
                cmd("npm run buld", NOW - offset, 1),
                cmd("npm run build", NOW - offset + 4, 0),
            ]
        commands += [
            cmd("npm run buld", NOW - 100, 1),
            cmd("npm run build --if-present", NOW - 96, 0),
        ]
        write_history(commands)

        output = run_fix("buld")

        assert output.index("fixed by") < output.index("also")
        assert "npm run build --if-present" in output

    def test_it_never_offers_to_run_anything(self) -> None:
        """The output is text to read. That is the whole safety model."""
        write_history(
            [
                cmd("npm run buld", NOW - 100, 1),
                cmd("npm run build", NOW - 96, 0),
            ]
        )

        output = run_fix()

        assert "does not run it for you" in output

    def test_the_module_cannot_execute_anything(self) -> None:
        """Pinned structurally: the capability is simply not imported.

        The dangerous future change is someone adding ``subprocess.run(fix)``
        for convenience. This fails the moment the import appears, which is
        earlier and louder than any behavioural assertion.
        """
        import ast

        tree = ast.parse(Path(fix.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        assert not imported & {"subprocess", "os", "pty", "ctypes"}


class TestJsonOutput:
    """``--json`` is the contract other tools read."""

    def test_the_payload_carries_the_evidence(self) -> None:
        write_history(
            [
                cmd("npm run buld", NOW - 900, 1),
                cmd("npm run build", NOW - 896, 0),
                cmd("npm run buld", NOW - 100, 1),
                cmd("npm run build", NOW - 96, 0),
            ]
        )

        payload = json.loads(run_fix("--json"))

        assert payload["failure"]["command"] == "npm run buld"
        assert payload["failure"]["exit_code"] == 1
        assert payload["count"] == 1
        assert payload["fixes"][0] == {
            "command": "npm run build",
            "occurrences": 2,
            "confidence": "moderate",
            "first_seen": NOW - 900,
            "last_seen": NOW - 100,
            "last_seen_iso": payload["fixes"][0]["last_seen_iso"],
            "failed_since": 0,
            "repo": REPO,
        }

    def test_an_empty_store_is_a_well_formed_answer_not_an_error(self) -> None:
        payload = json.loads(run_fix("--json"))
        assert payload == {"query": None, "failure": None, "count": 0, "fixes": []}

    def test_the_limit_caps_the_number_of_fixes(self) -> None:
        commands = [
            cmd("npm run buld", NOW - 900, 1),
            cmd("npm run build", NOW - 896, 0),
            cmd("npm run buld", NOW - 500, 1),
            cmd("npm run build --if-present", NOW - 496, 0),
            cmd("npm run buld", NOW - 100, 1),
            cmd("npm run build --silent", NOW - 96, 0),
        ]
        write_history(commands)

        assert len(json.loads(run_fix("--json", "-n", "1"))["fixes"]) == 1
        assert len(json.loads(run_fix("--json"))["fixes"]) == 3


class TestRedaction:
    """History is where credentials end up. mem fix quotes history back."""

    SECRET = "sk-ant-abcdefghijklmnopqrstuvwx"

    def _plant_secret(self) -> None:
        failed = f"curl -H 'Authorization: Bearer {self.SECRET}' https://api.example"
        write_history(
            [
                cmd(failed, NOW - 100, 7),
                cmd(f"{failed} --retry 3", NOW - 96, 0),
            ]
        )

    def test_a_credential_in_the_failure_is_redacted(self) -> None:
        self._plant_secret()
        output = run_fix()
        assert self.SECRET not in output
        assert "[REDACTED]" in output

    def test_a_credential_in_the_fix_is_redacted(self) -> None:
        self._plant_secret()
        output = run_fix()
        assert "--retry 3" in output  # the useful part survives
        assert output.count(self.SECRET) == 0

    def test_json_output_is_redacted_too(self) -> None:
        """Both renderings read the same payload, so neither can be forgotten."""
        self._plant_secret()
        assert self.SECRET not in run_fix("--json")

    def test_redaction_happens_in_the_payload_not_the_renderer(self) -> None:
        """One choke point, so a future output format inherits it.

        Asserted against ``report_payload`` directly: if redaction moved into
        the CLI's printing code, a second consumer of this function would
        silently start leaking.
        """
        self._plant_secret()
        payload = fix.report_payload(fix.build_report())
        assert self.SECRET not in json.dumps(payload)


class TestRichMarkupSafety:
    """A command containing Rich markup must render as itself.

    mem has already crashed once on a captured ``[/]``. ``mem fix`` prints
    commands more prominently than anything else in the CLI, so it gets its
    own regression test rather than trusting that render.py is used correctly.
    """

    def test_bracket_syntax_in_a_command_survives_verbatim(self) -> None:
        write_history(
            [
                cmd("sed 's/[a-z]//' fil", NOW - 100, 1),
                cmd("sed 's/[a-z]//' file", NOW - 96, 0),
            ]
        )

        output = run_fix()

        assert "sed 's/[a-z]//' file" in output

    def test_a_bare_closing_tag_does_not_raise(self) -> None:
        write_history(
            [
                cmd("awk '{print [/]}' fil", NOW - 100, 1),
                cmd("awk '{print [/]}' file", NOW - 96, 0),
            ]
        )

        output = run_fix()

        assert "[/]" in output


# --- the extraction shared with mem.mcp ------------------------------------


class TestSharedScan:
    """``mem.mcp.recent_failures`` mines the same facts through these helpers.

    Tested here rather than only through MCP because the point of the
    extraction is that there is *one* definition of "a failure and what came
    after it". A second copy would drift, which this codebase has paid for
    twice already.
    """

    def test_iter_histories_yields_files_in_capture_order(self) -> None:
        write_history([cmd("first", NOW - 30, 0), cmd("second", NOW - 20, 0)])

        ((key, commands),) = list(fix.iter_histories())

        assert key == "work-api"
        assert [c.command for c in commands] == ["first", "second"]

    def test_iter_histories_honours_the_include_predicate(self) -> None:
        write_history([cmd("a", NOW, 0)], key="repo-a")
        write_history([cmd("b", NOW, 0)], key="repo-b")

        keys = [key for key, _ in fix.iter_histories(lambda stem: stem == "repo-b")]

        assert keys == ["repo-b"]

    def test_iter_failures_attaches_the_following_commands(self) -> None:
        commands = [
            cmd("boom", NOW - 30, 2),
            cmd("look", NOW - 28, 0),
            cmd("fix", NOW - 26, 0),
            cmd("later", NOW - 24, 0),
        ]

        (failure,) = list(fix.iter_failures(commands, lookahead=2))

        assert failure.index == 0
        assert failure.command.command == "boom"
        assert [c.command for c in failure.following] == ["look", "fix"]

    def test_iter_failures_ignores_commands_with_no_exit_code(self) -> None:
        """The shared definition of "failure" excludes imported lines.

        ``exit_code is None`` is not a failure and not a success. Reporting it
        as either — which a naive ``!= 0`` test does — invents a fact.
        """
        commands = [cmd("imported", NOW - 30, None), cmd("real", NOW - 20, 1)]

        assert [f.command.command for f in fix.iter_failures(commands)] == ["real"]

    def test_mcp_recent_failures_still_works_through_the_shared_scan(self) -> None:
        """The extraction must not have changed the MCP tool's answer."""
        from mem import mcp

        write_history(
            [
                cmd("pytest -q", NOW - 300, 1),
                cmd("pip install -e '.[dev]'", NOW - 290, 0),
                cmd("pytest -q", NOW - 280, 0),
            ]
        )

        payload = mcp._tool_recent_failures({})

        assert payload["count"] == 1
        failure = payload["failures"][0]
        assert failure["command"] == "pytest -q"
        assert [f["command"] for f in failure["followed_by"]] == [
            "pip install -e '.[dev]'",
            "pytest -q",
        ]
        assert failure["retried_successfully"] is True
