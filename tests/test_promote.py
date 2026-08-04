"""Contract tests for sequence mining and ``mem promote``.

The thing under test is a *suggestion engine that writes to disk*, so it has
two ways to fail and only one of them is loud. The quiet one is proposing
junk: a suggestion list nobody trusts is dismissed once and never opened
again, and no amount of correct behaviour afterwards recovers it. The loud one
is creating a group nobody asked for.

The suite is weighted accordingly. :class:`TestNotTheSameSequence` is the
load-bearing class — every case in it is two commands that share a program, a
shape and a token count and are still not two runs of one thing — and
:class:`TestNothingIsWrittenWithoutConsent` pins the second failure shut.

:class:`TestCalibration` pins the measurement the whole design rests on. See
ADR-012 for the corpus and the numbers.

Every test builds its own history; nothing here reads the developer's real
``~/.mem`` (see ``conftest.tmp_mem_dir``, which is autouse).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from mem import groups, promote, storage
from mem.cli import cli
from mem.models import CapturedCommand

NOW = int(time.time())
REPO = "/Users/dev/work/api"
KEY = "work-api"

#: Comfortably more than SESSION_IDLE_SECONDS, so each run is its own session.
BETWEEN_SESSIONS = 86_400


# --- helpers ---------------------------------------------------------------


def cmd(
    command: str,
    ts: int,
    exit_code: int | None = 0,
    *,
    repo: str | None = REPO,
    session: str | None = None,
    duration_ms: int | None = 100,
) -> CapturedCommand:
    """One captured command, with defaults a candidate would be built from."""
    return CapturedCommand(
        command=command,
        ts=ts,
        dir=repo or "/tmp",
        repo=repo,
        exit_code=exit_code,
        duration_ms=duration_ms,
        session=session,
    )


def history(*runs: list[str], gap: int = 30) -> list[CapturedCommand]:
    """Lay out each run as its own work session, one per day.

    Sessions are a day apart so the idle rule separates them, and commands
    within a session are *gap* seconds apart so nothing trips the step-gap
    rule. A test that wants either boundary crossed sets it explicitly.
    """
    commands: list[CapturedCommand] = []
    for index, run in enumerate(runs):
        ts = NOW - (len(runs) - index) * BETWEEN_SESSIONS
        for text in run:
            commands.append(cmd(text, ts))
            ts += gap
    return commands


def write_history(commands: list[CapturedCommand], key: str = KEY) -> Path:
    """Write commands to a repo history file in capture order.

    Raw file writing rather than ``storage.append_command`` because capture
    order and timestamps are the signal under test, and the capture layer
    would stamp its own over the ones each test chose.
    """
    path = storage.MEM_DIR / "repos" / f"{key}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for command in commands:
            handle.write(command.to_jsonl() + "\n")
    return path


def top_of(commands: list[CapturedCommand]) -> list[promote.Candidate]:
    """Mine raw commands exactly as ``mine_all`` would, for one history file."""
    candidates = promote.closed(promote.mine(KEY, commands))
    return promote.dominant(promote.rank(candidates))


def mined(*runs: list[str], gap: int = 30) -> list[promote.Candidate]:
    """Mine a set of sessions, ranked and de-overlapped as the CLI sees them."""
    return top_of(history(*runs, gap=gap))


def steps_of(candidates: list[promote.Candidate]) -> list[tuple[str, ...]]:
    """The templated command lists, for comparing against an expectation."""
    return [candidate.steps for candidate in candidates]


DEPLOY = ["make build", "make test", "./scripts/deploy.sh"]


@pytest.fixture
def global_scope(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force group writes into the global scope.

    ``groups.resolve_scope`` shells out to ``git`` for the current directory,
    which under pytest is whatever checkout the suite is running in. Pinning
    it to global removes a subprocess per test and, more importantly, removes
    a dependency on where the tests happen to be invoked from.
    """
    monkeypatch.setattr(groups, "get_git_repo", lambda directory: None)
    return storage.GROUPS_GLOBAL_FILE


def run_promote(*args: str, input: str | None = None) -> str:
    """Invoke ``mem promote`` and return its stdout."""
    result = CliRunner().invoke(cli, ["promote", *args], input=input)
    assert result.exception is None, result.exception
    assert result.exit_code == 0, result.output
    return result.output


# --- the core rule ---------------------------------------------------------


class TestCleanRecurringSequence:
    """The happy path: the same three commands, three separate days."""

    def test_a_sequence_repeated_in_three_sessions_is_found(self) -> None:
        assert steps_of(mined(DEPLOY, DEPLOY, DEPLOY)) == [tuple(DEPLOY)]

    def test_the_candidate_records_how_often_and_when(self) -> None:
        (candidate,) = mined(DEPLOY, DEPLOY, DEPLOY)
        assert candidate.occurrences == 3
        assert candidate.last_seen > candidate.first_seen
        assert candidate.repo == REPO
        assert candidate.variables == ()

    def test_two_sessions_are_not_enough(self) -> None:
        """Two is compatible with coincidence; three is the threshold."""
        assert mined(DEPLOY, DEPLOY) == []

    def test_a_sequence_seen_once_yields_nothing(self) -> None:
        assert mined(DEPLOY) == []

    def test_repetition_inside_one_session_is_one_occasion(self) -> None:
        """A build loop run three times in an afternoon is one episode.

        The claim ``mem promote`` makes is "you did this on N occasions", so
        the count has to be a count of sessions. Counting raw repetitions
        would let a single afternoon manufacture the entire evidence base.
        """
        assert mined(DEPLOY + DEPLOY + DEPLOY) == []

    def test_a_single_command_is_not_a_sequence(self) -> None:
        """That is what ``mem save`` is for."""
        assert mined(["make build"], ["make build"], ["make build"]) == []


class TestVaryingArgument:
    """The case that makes this worth building: one token changes each run."""

    def test_a_changing_operand_becomes_a_variable(self) -> None:
        (candidate,) = mined(
            ["git checkout main", "make build"],
            ["git checkout staging", "make build"],
            ["git checkout hotfix", "make build"],
        )
        (variable,) = candidate.variables
        assert candidate.steps == (f"git checkout ${variable.name}", "make build")
        assert set(variable.values) == {"main", "staging", "hotfix"}

    def test_the_most_recent_value_is_listed_first(self) -> None:
        """It is the one that makes the variable recognisable."""
        (candidate,) = mined(
            ["git checkout main", "make build"],
            ["git checkout staging", "make build"],
            ["git checkout hotfix", "make build"],
        )
        assert candidate.variables[0].values[0] == "hotfix"

    def test_one_value_used_in_three_commands_is_one_variable(self) -> None:
        """Positions are grouped by the values they take, not by where they are.

        All three commands change together and to the same value, so this is
        one parameter used three times. Counting positions would reject the
        single most useful shape this feature has.
        """
        (candidate,) = mined(
            *[
                [
                    f"kubectl config use-context {ns}",
                    f"kubectl apply -f app.yaml -n {ns}",
                    f"kubectl rollout status deploy/api -n {ns}",
                ]
                for ns in ("prod", "staging", "dev")
            ]
        )
        (variable,) = candidate.variables
        assert len(variable.positions) == 3
        assert all(f"${variable.name}" in step for step in candidate.steps)

    def test_a_long_flag_names_the_variable(self) -> None:
        (candidate,) = mined(
            *[
                [f"helm upgrade api --namespace {ns}", "make smoke"]
                for ns in ("prod", "staging", "dev")
            ]
        )
        assert candidate.variables[0].name == "NAMESPACE"

    def test_an_attached_flag_value_names_the_variable_too(self) -> None:
        (candidate,) = mined(
            *[
                [f"helm upgrade api --namespace={ns}", "make smoke"]
                for ns in ("prod", "staging", "dev")
            ]
        )
        assert candidate.variables[0].name == "NAMESPACE"
        assert "--namespace=$NAMESPACE" in candidate.steps[0]

    def test_version_shaped_values_are_named_version(self) -> None:
        (candidate,) = mined(
            *[["npm version " + v, "npm publish"] for v in ("1.2.0", "1.3.0", "2.0.0")]
        )
        assert candidate.variables[0].name == "VERSION"

    def test_filename_shaped_values_are_named_file(self) -> None:
        (candidate,) = mined(
            *[
                ["make build", f"kubectl apply -f {name}.yaml"]
                for name in ("api", "web", "worker")
            ]
        )
        assert candidate.variables[0].name == "FILE"

    def test_a_registry_tag_is_not_called_a_file(self) -> None:
        """Found in calibration: ``registry/api:1.4.0`` ends in ``.0``.

        A laxer filename rule called that ``$FILE``, which is a name that
        actively misleads — the value is an image reference. The rule now
        requires the extension to start with a letter and the value to carry
        no ``:``.
        """
        assert not promote._FILENAME.match("registry.internal/api:1.4.0")
        assert promote._FILENAME.match("deploy.yaml")

    def test_a_name_colliding_with_a_shell_variable_is_avoided(self) -> None:
        """``--user`` would yield ``$USER``, which mem deliberately ignores.

        ``parse_variables`` excludes the common shell variables, so a group
        storing ``$USER`` would silently have no parameter at all. The name
        falls back rather than producing a runbook with a dead hole in it.
        """
        (candidate,) = mined(
            *[[f"curl -s --user {who} https://api", "make check"] for who in "abc"]
        )
        assert candidate.variables[0].name != "USER"

    def test_two_independent_variables_are_allowed(self) -> None:
        (candidate,) = mined(
            *[
                [f"make deploy {env}", f"make tag {ver}"]
                for env, ver in (("prod", "1.0"), ("dev", "2.0"), ("qa", "3.0"))
            ]
        )
        assert len(candidate.variables) == 2

    def test_three_independent_variables_are_refused(self) -> None:
        """Three holes is a template, not a workflow seen three times.

        The two-step windows inside it still qualify — they only need two
        variables — and that is the right outcome: the rule rejects the
        over-general candidate, not every sequence containing it.
        """
        found = steps_of(
            mined(
                *[
                    [f"make deploy {a}", f"make tag {b}", f"make notify {c}"]
                    for a, b, c in (
                        ("prod", "1.0", "x"),
                        ("dev", "2.0", "y"),
                        ("qa", "3.0", "z"),
                    )
                ]
            )
        )
        assert not any(len(steps) == 3 for steps in found)


class TestNotTheSameSequence:
    """Pairs mem must refuse to unify. The reason this can be trusted.

    Each case is two runs that pass every structural filter — same program,
    same token count, same session shape, three separate days — and are still
    two different things.
    """

    @pytest.mark.parametrize(
        "first,second,third,reason",
        [
            ("git push", "git pull", "git fetch", "the subcommand is the verb"),
            ("make build", "make test", "make clean", "different targets"),
            ("cd api", "cd web", "cd infra", "for two tokens the argument is all"),
            ("npm test", "yarn test", "pnpm test", "a different program entirely"),
            (
                "docker build --pull .",
                "docker build --no-cache .",
                "docker build --quiet .",
                "the flag name is structure, not a value",
            ),
            (
                "terraform plan",
                "terraform apply",
                "terraform destroy",
                "the next step is not the same step",
            ),
        ],
    )
    def test_a_shared_shape_is_not_a_shared_intent(
        self, first: str, second: str, third: str, reason: str
    ) -> None:
        assert (
            mined([first, "make check"], [second, "make check"], [third, "make check"])
            == []
        ), reason

    def test_a_privilege_prefix_shifts_the_protected_window(self) -> None:
        """``sudo systemctl restart`` protects the verb, not the ``sudo``."""
        assert (
            mined(
                *[
                    [f"sudo systemctl {verb} nginx", "make check"]
                    for verb in ("restart", "reload", "stop")
                ]
            )
            == []
        )

    def test_but_the_operand_after_it_may_still_vary(self) -> None:
        (candidate,) = mined(
            *[
                [f"sudo systemctl restart {unit}", "make check"]
                for unit in ("nginx", "redis", "postgres")
            ]
        )
        assert candidate.steps[0].startswith("sudo systemctl restart $")

    def test_a_pipeline_is_never_generalised(self) -> None:
        """mem does not parse shell, so it does not guess inside one."""
        assert (
            mined(
                *[[f"docker ps | grep api-{n}", "make check"] for n in ("1", "2", "3")]
            )
            == []
        )

    def test_but_an_identical_pipeline_still_matches_itself(self) -> None:
        (candidate,) = mined(
            *[["docker ps | grep api", "make check"] for _ in range(3)]
        )
        assert candidate.steps == ("docker ps | grep api", "make check")

    def test_two_sessions_are_never_spliced_together(self) -> None:
        """The tail of one day and the head of the next are not a procedure.

        Both halves recur three times and sit next to each other in capture
        order every single time, so the only thing stopping ``make package``
        → ``npm ci`` from being mined is the session boundary between them.
        """
        runs = []
        for _ in range(3):
            runs.append(["make build", "make package"])
            runs.append(["npm ci", "npm test"])
        found = steps_of(mined(*runs))
        assert ("make package", "npm ci") not in found
        assert ("make build", "make package") in found
        assert ("npm ci", "npm test") in found


class TestInterleavedNoise:
    """Looking at things is not a step, and must not break the sequence."""

    def test_inspection_commands_between_steps_are_skipped(self) -> None:
        noisy = [
            "git status",
            "make build",
            "ls -la",
            "cat Makefile",
            "make test",
            "pwd",
            "./scripts/deploy.sh",
            "git log",
        ]
        assert steps_of(mined(noisy, noisy, noisy)) == [tuple(DEPLOY)]

    @pytest.mark.parametrize(
        "command",
        ["ls", "cd ..", "git status", "git log --oneline", "cat notes.md", "clear"],
    )
    def test_these_are_inspection(self, command: str) -> None:
        assert promote.is_inspection(command)

    @pytest.mark.parametrize(
        "command",
        [
            "cat template.yaml > out.yaml",
            "echo hi >> log",
            "ls | wc -l",
            "make build",
            "git commit -m wip",
        ],
    )
    def test_these_are_not(self, command: str) -> None:
        """Shell grammar disqualifies a command from being called noise.

        ``cat x`` looks; ``cat x > y`` writes a file, and dropping it would
        remove a real step from the middle of a real procedure.
        """
        assert not promote.is_inspection(command)

    def test_a_failed_command_is_not_a_step(self) -> None:
        """A command that exited non-zero is not part of a working procedure."""
        commands: list[CapturedCommand] = []
        for index in range(3):
            ts = NOW - (3 - index) * BETWEEN_SESSIONS
            commands.append(cmd("make build", ts))
            commands.append(cmd("make tset", ts + 10, exit_code=2))
            commands.append(cmd("make test", ts + 20))
            commands.append(cmd("./scripts/deploy.sh", ts + 30))
        candidates = promote.dominant(
            promote.rank(promote.closed(promote.mine(KEY, commands)))
        )
        assert steps_of(candidates) == [tuple(DEPLOY)]

    def test_imported_commands_have_no_exit_code_and_are_kept(self) -> None:
        """Absence of an exit code is not evidence of failure.

        ``mem import`` records none, and dropping those would make this
        feature useless to the user who has just imported ten years of
        history — the one with the most to gain from it.
        """
        commands: list[CapturedCommand] = []
        for index in range(3):
            ts = NOW - (3 - index) * BETWEEN_SESSIONS
            for step in DEPLOY:
                commands.append(cmd(step, ts, exit_code=None, duration_ms=None))
                ts += 30
        assert steps_of(top_of(commands)) == [tuple(DEPLOY)]

    def test_steps_far_apart_in_think_time_are_not_one_procedure(self) -> None:
        """Removing noise must not invent adjacency across half an hour."""
        gap = promote.MAX_STEP_GAP_SECONDS + 60
        assert mined(DEPLOY, DEPLOY, DEPLOY, gap=gap) == []

    def test_a_slow_step_is_not_penalised_for_its_own_runtime(self) -> None:
        """The gap is think time, not wall clock — as in ``mem fix``.

        This is the case that made the raw-timestamp session rule untenable.
        A ``docker build`` that takes twenty minutes is a slow command, not
        twenty minutes of distraction, and measuring it as idle time cut every
        deploy sequence in half at precisely its slowest step — which is to
        say, cut exactly the sequences this feature exists to find.
        """
        slow = promote.SESSION_IDLE_SECONDS * 4
        commands: list[CapturedCommand] = []
        for index in range(3):
            ts = NOW - (3 - index) * BETWEEN_SESSIONS
            commands.append(cmd("make build", ts))
            commands.append(
                cmd("docker build .", ts + slow, duration_ms=(slow + 60) * 1000)
            )
        assert steps_of(top_of(commands)) == [("make build", "docker build .")]

    def test_but_a_genuine_pause_of_the_same_length_does_end_the_session(
        self,
    ) -> None:
        """The correction is to what is measured, not to the threshold."""
        slow = promote.SESSION_IDLE_SECONDS * 4
        commands: list[CapturedCommand] = []
        for index in range(3):
            ts = NOW - (3 - index) * BETWEEN_SESSIONS
            commands.append(cmd("make build", ts))
            commands.append(cmd("docker build .", ts + slow, duration_ms=100))
        assert top_of(commands) == []


class TestSessions:
    """Boundaries are re-derived from the history, not read from a file."""

    def test_an_idle_gap_ends_a_session(self) -> None:
        commands = [
            cmd("make build", NOW - 1000),
            cmd("make test", NOW - 1000 + promote.SESSION_IDLE_SECONDS + 1),
        ]
        assert len(promote.split_sessions(commands)) == 2

    def test_a_change_of_repository_ends_a_session(self) -> None:
        commands = [
            cmd("make build", NOW - 100),
            cmd("make test", NOW - 90, repo="/other"),
        ]
        assert len(promote.split_sessions(commands)) == 2

    def test_an_explicit_session_id_wins_over_the_timing_heuristic(self) -> None:
        commands = [
            cmd("make build", NOW - 100, session="a"),
            cmd("make test", NOW - 95, session="b"),
        ]
        assert len(promote.split_sessions(commands)) == 2

    def test_a_backwards_timestamp_starts_a_new_session(self) -> None:
        """Two shells interleaving their lines look exactly like this."""
        commands = [cmd("make build", NOW - 50), cmd("make test", NOW - 100)]
        assert len(promote.split_sessions(commands)) == 2


class TestOverlappingCandidates:
    """Every sub-sequence of a recurring sequence also recurs."""

    LONG = ["make build", "make test", "make package", "./scripts/deploy.sh"]

    def test_a_sub_sequence_with_no_extra_evidence_is_dropped(self) -> None:
        assert steps_of(mined(self.LONG, self.LONG, self.LONG)) == [tuple(self.LONG)]

    def test_a_sub_sequence_seen_more_often_survives_closure(self) -> None:
        """Then it really is a more common workflow than its parent."""
        head = self.LONG[:2]
        candidates = promote.closed(
            promote.mine(KEY, history(self.LONG, self.LONG, self.LONG, head, head))
        )
        assert tuple(head) in steps_of(candidates)
        assert tuple(self.LONG) in steps_of(candidates)

    def test_only_one_member_of_a_family_is_shown(self) -> None:
        """...and it is the one worth the most, not the one seen the most."""
        head = self.LONG[:2]
        shown = mined(self.LONG, self.LONG, self.LONG, head, head)
        assert steps_of(shown) == [tuple(self.LONG)]

    def test_ranking_on_occurrences_alone_would_get_this_backwards(self) -> None:
        """The measurement that justifies ranking on occurrences × length.

        A four-step procedure makes its own two-step head recur at least as
        often as the whole, so an occurrence-first order puts the fragment
        above the sequence it belongs to and the listing fills with pieces of
        one workflow. Pinned as a test because a future contributor reaching
        for "just sort by count" deserves to trip over it.

        On the calibration corpus this cost 8 points of workflow coverage in
        the default listing: 88% with ``steps_saved``, 80% without. See
        ADR-012.
        """
        head = self.LONG[:2]
        candidates = promote.closed(
            promote.mine(KEY, history(self.LONG, self.LONG, self.LONG, head, head))
        )
        by_count = sorted(candidates, key=lambda c: -c.occurrences)
        assert by_count[0].steps == tuple(head), "the fragment wins on count alone"
        assert promote.rank(candidates)[0].steps == tuple(self.LONG)

    def test_a_shared_command_is_not_a_shared_family(self) -> None:
        """Neither sequence contains the other, so both are real answers."""
        left = ["make build", "./deploy.sh"]
        right = ["make build", "./publish.sh"]
        shown = mined(left, left, left, right, right, right)
        assert sorted(steps_of(shown)) == sorted([tuple(left), tuple(right)])


class TestEmptyHistory:
    """Nothing to say, said without inventing anything."""

    def test_mining_nothing_is_nothing(self) -> None:
        assert promote.mine(KEY, []) == []

    def test_mining_an_empty_store_is_empty(self) -> None:
        assert promote.mine_all() == []

    def test_build_report_on_an_empty_store(self) -> None:
        report = promote.build_report()
        assert report.candidates == [] and report.names == []

    def test_the_command_says_so_plainly(self) -> None:
        assert "No repeated sequences" in run_promote()

    def test_the_json_payload_is_well_formed_not_an_error(self) -> None:
        payload = json.loads(run_promote("--json"))
        assert payload == {"count": 0, "candidates": []}

    def test_an_empty_command_line_is_not_a_step(self) -> None:
        assert promote.is_inspection("   ")

    def test_unbalanced_quotes_do_not_crash_the_tokenizer(self) -> None:
        assert promote.command_shape("echo 'unterminated")
        assert promote.tokenize('git commit -m "wip') is not None


class TestCredentials:
    """A secret must never become a stored runbook, and never be printed."""

    SECRET = [
        "make build",
        "curl -H 'Authorization: Bearer sk-ant-abcdefghijklmnopqrst' https://api",
        "./scripts/deploy.sh",
    ]

    def test_a_credential_shaped_step_flags_the_candidate(self) -> None:
        (candidate,) = mined(self.SECRET, self.SECRET, self.SECRET)
        assert candidate.has_credential

    def test_an_ordinary_candidate_is_not_flagged(self) -> None:
        (candidate,) = mined(DEPLOY, DEPLOY, DEPLOY)
        assert not candidate.has_credential

    def test_the_listing_redacts_the_secret(self) -> None:
        write_history(history(self.SECRET, self.SECRET, self.SECRET))
        output = run_promote()
        assert "sk-ant-abcdefghijklmnopqrst" not in output
        assert "REDACTED" in output

    def test_the_json_payload_is_redacted_too(self) -> None:
        write_history(history(self.SECRET, self.SECRET, self.SECRET))
        assert "sk-ant-abcdefghijklmnopqrst" not in run_promote("--json")

    def test_redaction_happens_in_the_payload_not_the_renderer(self) -> None:
        """One choke point, so a new output format cannot leak by omission."""
        write_history(history(self.SECRET, self.SECRET, self.SECRET))
        payload = promote.report_payload(promote.build_report())
        assert "sk-ant-abcdefghijklmnopqrst" not in json.dumps(payload)

    def test_promoting_it_is_refused_outright(self, global_scope: Path) -> None:
        write_history(history(self.SECRET, self.SECRET, self.SECRET))
        result = CliRunner().invoke(cli, ["promote", "1"], input="y\n")
        assert result.exit_code != 0
        assert "credential-shaped" in result.output
        assert not global_scope.exists()

    def test_there_is_no_flag_that_stores_it_anyway(self) -> None:
        """A ``--force`` here would defeat the entire point of the check."""
        help_text = CliRunner().invoke(cli, ["promote", "--help"]).output
        assert "--force" not in help_text

    def test_both_detectors_are_reused_not_reimplemented(self) -> None:
        """``variables`` owns credential detection; this module only calls it."""
        source = Path(promote.__file__).read_text(encoding="utf-8")
        assert "looks_like_credential" in source and "redact_secrets" in source
        assert "BEGIN" not in source, "a second credential rule set has appeared"


class TestNothingIsWrittenWithoutConsent:
    """The half of this feature that touches the disk."""

    def test_listing_writes_nothing(self, global_scope: Path) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        run_promote()
        assert not global_scope.exists()

    def test_declining_writes_nothing(self, global_scope: Path) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        output = run_promote("1", input="n\n")
        assert "Nothing saved" in output
        assert not global_scope.exists()

    def test_accepting_writes_the_group(self, global_scope: Path) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        run_promote("1", input="y\n")
        saved = storage.read_group_file(global_scope)
        (group,) = saved.groups.values()
        assert [c.cmd for c in group.commands] == DEPLOY
        assert group.description == "Promoted from 3 repeated runs"

    def test_the_stored_group_carries_the_variable(self, global_scope: Path) -> None:
        """The template's ``$VAR`` must arrive as a real declaration.

        Otherwise ``mem run`` would treat it as literal text and the runbook
        would deploy to a namespace called ``$NAMESPACE``.
        """
        write_history(
            history(
                *[
                    [f"helm upgrade api --namespace {ns}", "make smoke"]
                    for ns in ("prod", "staging", "dev")
                ]
            )
        )
        run_promote("1", input="y\n")
        (group,) = storage.read_group_file(global_scope).groups.values()
        assert group.commands[0].vars is not None
        assert [v.name for v in group.commands[0].vars] == ["NAMESPACE"]

    def test_a_promoted_sequence_is_not_suggested_again(
        self, global_scope: Path
    ) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        run_promote("1", input="y\n")
        assert "No repeated sequences" in run_promote()

    def test_the_module_cannot_execute_anything(self) -> None:
        """Structural, not behavioural: there is no import that could.

        The same guard ``mem fix`` carries. A suggestion engine that can run
        commands is one confirmation prompt away from being a hazard.
        """
        source = Path(promote.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "popen", "socket", "urllib"):
            assert forbidden not in source, forbidden


class TestPromoteCommand:
    """What the user actually sees."""

    def test_it_reports_the_sequence_the_count_and_the_variable(self) -> None:
        write_history(
            history(
                *[
                    [f"git checkout {branch}", "make build", "./scripts/deploy.sh"]
                    for branch in ("main", "staging", "hotfix")
                ]
            )
        )
        output = run_promote()
        assert "3 times" in output
        assert "make build" in output
        assert "./scripts/deploy.sh" in output
        assert "$CHECKOUT_ARG" in output
        assert "hotfix" in output

    def test_it_never_offers_to_run_anything(self) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        assert "runs nothing" in run_promote()

    def test_the_json_payload_carries_the_evidence(self) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        payload = json.loads(run_promote("--json"))
        assert payload["count"] == 1
        (entry,) = payload["candidates"]
        assert entry["index"] == 1
        assert entry["steps"] == DEPLOY
        assert entry["occurrences"] == 3
        assert entry["confidence"] == "moderate"
        assert entry["last_seen_iso"].endswith("Z")
        assert entry["has_credential"] is False

    def test_the_limit_caps_the_listing(self) -> None:
        left = ["make build", "./deploy.sh"]
        right = ["npm ci", "npm run release"]
        write_history(history(left, left, left, right, right, right))
        payload = json.loads(run_promote("--json", "-n", "1"))
        assert payload["count"] == 1

    def test_an_out_of_range_index_is_an_error_not_a_guess(self) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        result = CliRunner().invoke(cli, ["promote", "9"])
        assert result.exit_code != 0
        assert "No candidate 9" in result.output

    def test_the_name_can_be_overridden(self, global_scope: Path) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        run_promote("1", "--name", "ship-it", input="y\n")
        assert "ship-it" in storage.read_group_file(global_scope).groups

    def test_a_bad_name_is_rejected_before_anything_is_written(
        self, global_scope: Path
    ) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        result = CliRunner().invoke(cli, ["promote", "1", "--name", "Ship It"])
        assert result.exit_code != 0
        assert not global_scope.exists()

    def test_a_name_already_in_use_is_refused(self, global_scope: Path) -> None:
        write_history(history(DEPLOY, DEPLOY, DEPLOY))
        groups.save_command(global_scope, "echo hi", group_name="ship-it")
        result = CliRunner().invoke(cli, ["promote", "1", "--name", "ship-it"])
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_the_suggested_name_comes_from_the_last_step(self) -> None:
        candidate = promote.Candidate(
            steps=("make build", "./scripts/deploy-staging.sh"),
            variables=(),
            occurrences=3,
            first_seen=0,
            last_seen=0,
            repo=None,
            has_credential=False,
            shape=(),
        )
        assert promote.suggest_name(candidate) == "deploy-staging"

    def test_a_suggested_name_never_collides(self) -> None:
        candidate = promote.Candidate(
            steps=("make build", "terraform apply"),
            variables=(),
            occurrences=3,
            first_seen=0,
            last_seen=0,
            repo=None,
            has_credential=False,
            shape=(),
        )
        assert promote.suggest_name(candidate) == "terraform-apply"
        assert (
            promote.suggest_name(candidate, {"terraform-apply"}) == "terraform-apply-2"
        )

    def test_confidence_wording_tracks_the_count(self) -> None:
        assert promote.confidence(1) == "weak"
        assert promote.confidence(promote.MODERATE_EVIDENCE) == "moderate"
        assert promote.confidence(promote.STRONG_EVIDENCE) == "strong"


class TestRichMarkupSafety:
    """History is untrusted input; Rich reads square brackets as markup."""

    BRACKETS = ["sed 's/[a-z]//' in.txt", "make build", "awk '{print $1}' out"]

    def test_bracket_syntax_in_a_command_survives_verbatim(self) -> None:
        write_history(history(self.BRACKETS, self.BRACKETS, self.BRACKETS))
        assert "sed 's/[a-z]//' in.txt" in run_promote()

    def test_a_bare_closing_tag_does_not_raise(self) -> None:
        odd = ["sed 's|[/]|-|'", "make build", "./deploy.sh"]
        write_history(history(odd, odd, odd))
        assert "make build" in run_promote()

    def test_quoted_arguments_are_reassembled_exactly(self) -> None:
        """shlex would drop the quotes and change what the command means.

        ``git commit -m "wip thing"`` comes back from ``shlex.split`` as four
        tokens that rejoin into a five-word command line meaning something
        else. The tokenizer keeps the quoted run whole so a template is the
        original text with substrings replaced, not a reassembly.
        """
        tokens = promote.tokenize('git commit -m "wip thing"')
        assert [t.text for t in tokens] == ["git", "commit", "-m", '"wip thing"']
        runs = [['git commit -m "wip thing"', "git push"] for _ in range(3)]
        (candidate,) = mined(*runs)
        assert candidate.steps[0] == 'git commit -m "wip thing"'


class TestCalibration:
    """The measurement the whole design rests on, pinned.

    Full numbers and corpus in ADR-012. On eight hand-designed 45-day
    histories (~1,100 commands each, five planted workflows against ad-hoc
    exploration), the default five-candidate listing scored **0 false
    positives out of 40** and surfaced **88%** of the planted workflows.

    Removing the inspection filter took that to **80% false positives** — the
    feature stops working entirely. That is the one result a future
    contributor is most likely to undo by "simplifying", so it is pinned here
    in miniature.
    """

    NOISY = [
        "git status",
        "make build",
        "ls -la",
        "cat Makefile",
        "git diff",
        "make test",
        "pwd",
        "ls",
        "./scripts/deploy.sh",
        "git log",
    ]

    def test_with_the_filter_the_workflow_is_the_answer(self) -> None:
        assert steps_of(mined(self.NOISY, self.NOISY, self.NOISY)) == [tuple(DEPLOY)]

    def test_without_it_the_answer_is_buried_in_inspection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(promote, "is_inspection", lambda command: False)
        top = mined(self.NOISY, self.NOISY, self.NOISY)[0]
        assert top.steps != tuple(DEPLOY)
        assert any(step in {"ls", "ls -la", "pwd", "git status"} for step in top.steps)

    def test_the_default_listing_is_five(self) -> None:
        """Calibrated: recall of real workflows saturates there, FP is still 0.

        At eight the measured false-positive rate was 12.5%, and over the
        whole mined list (about twelve candidates per history) it was 42%.
        Ranking, not filtering, is what makes this output trustworthy — which
        is exactly why the default limit is part of the design and not a
        cosmetic choice.
        """
        parameter = next(p for p in cli.commands["promote"].params if p.name == "limit")
        assert parameter.default == 5
