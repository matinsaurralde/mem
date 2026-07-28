"""Contract tests for the CLI layer (``mem.cli``).

This module is the safety net for ``src/mem/cli.py`` — the 1385-line
user-facing surface that had zero direct coverage. Every test here states a
contract the CLI must honour; tests marked ``xfail(strict=True)`` state a
contract that is currently **violated**, so the CI turns green-to-red the day
the bug is fixed and the test starts passing.

Two rules govern this file:

- All state lives in ``tmp_mem_dir``. Nothing ever reads the user's ``~/.mem``.
- Git detection is always mocked, so results do not depend on where the suite
  is run from.
"""

from __future__ import annotations

import json
import time
from typing import Iterator
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from conftest import make_command

from mem import storage
from mem.cli import cli
from mem.models import Group, GroupCommand, GroupFile, SavedCommand, WorkSession

# --- Payloads -------------------------------------------------------------
#
# Every payload embeds the token "payload" so a single search query reaches
# all of them, and every payload is a command a real user could plausibly run.

MARKUP_CMD = "echo [red]payload[/red] mundo"
"""Rich reads ``[red]``/``[/red]`` as styling tags and swallows them."""

CLOSE_TAG_CMD = "echo payload [/] done"
"""A stray ``[/]`` has nothing to close — Rich raises ``MarkupError``."""

CONCEAL_CMD = "echo [conceal]payload[/conceal]"
"""``conceal`` renders the text invisible in a real terminal."""

REGEX_CMD = "sed 's/[a-z]//g' payload.txt"
"""``[a-z]`` looks like a style tag to Rich and is eaten silently."""


# --- Fixtures and helpers -------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """A CliRunner with a wide terminal so Rich never wraps assertion targets."""
    return CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def outside_repo() -> Iterator[None]:
    """Pretend the process runs outside any git repo, with no background sync.

    Without this the CLI shells out to ``git`` against the real cwd, which
    would make results depend on the machine running the suite.
    """
    with (
        patch("mem.cli._current_repo", return_value=None),
        patch("mem.groups.get_git_repo", return_value=None),
        patch("mem.capture.get_git_repo", return_value=None),
        patch("mem.capture._spawn_background_sync"),
    ):
        yield


def _add_history(command: str, ts: int | None = None) -> None:
    """Append one command to the captured history of the global scope."""
    storage.append_command(make_command(command=command, ts=ts, repo=None, dir="/tmp"))


def _seed_every_surface(command: str) -> None:
    """Store ``command`` in every place the CLI can echo it back to the user.

    Covers captured history (search / forget / stats), the saved list and a
    named group (list), and a work session (session).
    """
    _add_history(command)
    storage.write_group_file(
        storage.GROUPS_GLOBAL_FILE,
        GroupFile(
            saved=[SavedCommand(cmd=command)],
            groups={"demo": Group(commands=[GroupCommand(cmd=command)])},
        ),
    )
    now = int(time.time())
    storage.append_session(
        WorkSession(
            id="session-1",
            summary="demo session",
            started_at=now,
            ended_at=now,
            dir="/tmp",
            repo=None,
            commands=[command],
        )
    )


# Every CLI surface that prints stored commands back to the user, as
# (invocation args, stdin fed to interactive prompts).
RENDERING_SURFACES: list[tuple[str, list[str], str]] = [
    ("search", ["payload"], ""),
    ("forget-preview", ["forget", "payload"], "n\n"),
    ("list-saved", ["list", "--global"], ""),
    ("list-group", ["list", "demo", "--global"], ""),
    ("stats", ["stats"], ""),
    ("session", ["session", "demo"], "n\n"),
]

SURFACE_IDS = [surface[0] for surface in RENDERING_SURFACES]


# ---------------------------------------------------------------------------
# P0-3 — Rich markup injection
# ---------------------------------------------------------------------------


class TestMarkupIsShownVerbatim:
    """A history tool that rewrites what you ran is worse than no tool.

    Contract: whatever bytes were captured must be printed back byte for byte.
    Rich markup inside a command is data, never formatting instructions.
    """

    @pytest.mark.xfail(
        strict=True, reason="P0-3: Rich interpreta el markup del comando"
    )
    @pytest.mark.parametrize(
        ("args", "stdin"),
        [(args, stdin) for _id, args, stdin in RENDERING_SURFACES],
        ids=SURFACE_IDS,
    )
    def test_markup_tags_survive_rendering(
        self,
        tmp_mem_dir,
        runner: CliRunner,
        outside_repo: None,
        args: list[str],
        stdin: str,
    ) -> None:
        """Every command-printing surface must show ``[red]...[/red]`` literally."""
        _seed_every_surface(MARKUP_CMD)

        result = runner.invoke(cli, args, input=stdin)

        assert result.exit_code == 0
        assert MARKUP_CMD in result.stdout

    @pytest.mark.xfail(
        strict=True, reason="P0-3: '[/]' sin apertura hace crashear el render de Rich"
    )
    @pytest.mark.parametrize(
        ("args", "stdin"),
        [(args, stdin) for _id, args, stdin in RENDERING_SURFACES],
        ids=SURFACE_IDS,
    )
    def test_stray_closing_tag_does_not_crash(
        self,
        tmp_mem_dir,
        runner: CliRunner,
        outside_repo: None,
        args: list[str],
        stdin: str,
    ) -> None:
        """A ``[/]`` in history must never take the whole command down.

        Today Rich raises ``MarkupError``, so a single innocuous line in the
        JSONL file bricks search, stats, list, session and forget at once.
        """
        _seed_every_surface(CLOSE_TAG_CMD)

        result = runner.invoke(cli, args, input=stdin)

        assert result.exception is None
        assert result.exit_code == 0
        assert CLOSE_TAG_CMD in result.stdout

    @pytest.mark.xfail(
        strict=True, reason="P0-3: '[conceal]' vuelve invisible el payload real"
    )
    def test_conceal_tag_cannot_hide_the_payload(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """``[conceal]`` must be shown as text, not honoured as a style.

        Otherwise a command can hide its own payload from the person auditing
        their history in a real terminal.
        """
        _add_history(CONCEAL_CMD)

        result = runner.invoke(cli, ["payload"])

        assert result.exit_code == 0
        assert CONCEAL_CMD in result.stdout

    @pytest.mark.xfail(
        strict=True, reason="P0-3: la clase de caracteres [a-z] se pierde en el render"
    )
    def test_regex_character_class_is_not_eaten(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """``sed 's/[a-z]//g'`` must not be displayed as ``sed 's///g'``.

        This is the most dangerous variant: the mangled line is still a valid
        command, so copy-pasting it silently runs something different.
        """
        _add_history(REGEX_CMD)

        result = runner.invoke(cli, ["payload"])

        assert result.exit_code == 0
        assert REGEX_CMD in result.stdout

    def test_json_output_is_never_mangled(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """The JSON path is markup-safe today — this pins that guarantee down."""
        _add_history(MARKUP_CMD)

        result = runner.invoke(cli, ["--json", "payload"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [entry["command"] for entry in payload] == [MARKUP_CMD]


# ---------------------------------------------------------------------------
# P1-2 — multi-word queries
# ---------------------------------------------------------------------------


class TestMultiWordQuery:
    """``mem docker compose`` must use both words, not just the first."""

    @pytest.mark.xfail(
        strict=True, reason="P1-2: solo se usa query_args[0], el resto se descarta"
    )
    def test_all_terms_filter_the_results(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """Every result must match every term of the query."""
        for _ in range(3):
            _add_history("docker build .")
        _add_history("docker compose up -d")

        result = runner.invoke(cli, ["--json", "docker", "compose"])

        assert result.exit_code == 0
        commands = [entry["command"] for entry in json.loads(result.stdout)]
        assert commands == ["docker compose up -d"]

    @pytest.mark.xfail(
        strict=True,
        reason="P1-2: el término descartado deja pasar ruido mejor rankeado",
    )
    def test_extra_terms_are_not_silently_dropped(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """A query with a term that matches nothing must return nothing.

        Today the second word is dropped, so the search happily answers with
        results for the first word only — the user cannot tell it was ignored.
        """
        _add_history("docker build .")

        result = runner.invoke(cli, ["--json", "docker", "zzzz-no-such-term"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# P1-11 — short flag consistency
# ---------------------------------------------------------------------------


def _walk_commands(
    command: click.Command, prefix: str = ""
) -> Iterator[tuple[str, click.Command]]:
    """Yield ``(full name, command)`` for every command in the CLI tree."""
    for name, sub in getattr(command, "commands", {}).items():
        full_name = f"{prefix}{name}"
        yield full_name, sub
        yield from _walk_commands(sub, f"{full_name} ")


def _short_flag_targets() -> dict[str, dict[str, list[str]]]:
    """Map each short flag to the destination names it binds to, per command."""
    targets: dict[str, dict[str, list[str]]] = {}
    for full_name, sub in _walk_commands(cli):
        for param in sub.params:
            if not isinstance(param, click.Option):
                continue
            for opt in [*param.opts, *param.secondary_opts]:
                if len(opt) == 2 and opt.startswith("-"):
                    targets.setdefault(opt, {}).setdefault(str(param.name), []).append(
                        full_name
                    )
    return targets


class TestShortFlagConsistency:
    """A short flag must mean one thing across the whole CLI."""

    @pytest.mark.xfail(
        strict=True,
        reason="P1-11: -g es --group en save/import y --global en el resto",
    )
    def test_no_short_flag_binds_to_two_different_options(self) -> None:
        """``-g`` cannot be ``--group`` here and ``--global`` there.

        Muscle memory is the whole point of a CLI: ``mem save -g deploy`` adds
        to a group while ``mem list -g deploy`` silently switches scope. The
        assertion message lists every offending flag.
        """
        collisions = {
            flag: dests
            for flag, dests in _short_flag_targets().items()
            if len(dests) > 1
        }
        assert collisions == {}

    def test_short_flags_are_registered_for_the_expected_options(self) -> None:
        """Sanity check for the introspection helper above."""
        targets = _short_flag_targets()
        assert "-y" in targets
        assert set(targets["-y"]) == {"yes"}


# ---------------------------------------------------------------------------
# Implicit search routing
# ---------------------------------------------------------------------------


class TestImplicitSearchRouting:
    """``mem <keyword>`` searches; ``mem <subcommand>`` runs the subcommand."""

    def test_unknown_word_is_treated_as_a_query(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """An unknown first argument routes to search."""
        _add_history("kubectl get pods")

        result = runner.invoke(cli, ["kubectl"])

        assert result.exit_code == 0
        assert "kubectl get pods" in result.stdout

    def test_known_subcommand_wins_over_search(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """``mem list`` runs the subcommand even when history matches "list"."""
        _add_history("ls -la list.txt")

        result = runner.invoke(cli, ["list", "--global"])

        assert result.exit_code == 0
        assert "No saved commands or groups yet." in result.stdout
        assert "ls -la list.txt" not in result.stdout

    @pytest.mark.parametrize("name", ["init", "save", "run", "export"])
    def test_subcommand_names_are_unsearchable(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None, name: str
    ) -> None:
        """Searching for a word that names a subcommand is impossible.

        Documented, not fixed: the router gives subcommands priority, so the
        invocation fails with click's usage error (exit 2) instead of
        searching. Any future fix must decide this deliberately.
        """
        _add_history(f"{name} something")

        result = runner.invoke(cli, [name])

        assert result.exit_code == 2
        assert "Missing argument" in result.stderr
        assert f"{name} something" not in result.stdout

    def test_no_arguments_prints_help(self, tmp_mem_dir, runner: CliRunner) -> None:
        """Bare ``mem`` is a help request, not an error."""
        result = runner.invoke(cli, [])

        assert result.exit_code == 0
        assert "Usage:" in result.stdout
        assert "Commands:" in result.stdout

    def test_query_with_no_matches_is_silent_and_succeeds(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """No matches is a valid answer: exit 0, no output, no traceback."""
        _add_history("git status")

        result = runner.invoke(cli, ["zzzz-nothing-matches-this"])

        assert result.exception is None
        assert result.exit_code == 0
        assert result.stdout == ""

    def test_query_against_empty_history_succeeds(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """Searching before anything was ever captured must not crash."""
        result = runner.invoke(cli, ["anything"])

        assert result.exception is None
        assert result.exit_code == 0

    def test_limit_option_caps_results(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """``-n`` limits the number of results returned."""
        for i in range(5):
            _add_history(f"git commit -m msg{i}")

        result = runner.invoke(cli, ["-n", "2", "--json", "git"])

        assert result.exit_code == 0
        assert len(json.loads(result.stdout)) == 2


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


class TestExitCodes:
    """Scripts pipe ``mem``; exit codes are part of the public API."""

    def test_empty_search_exits_zero(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """Zero results is not a failure."""
        result = runner.invoke(cli, ["nothing-here"])
        assert result.exit_code == 0

    def test_unknown_group_exits_nonzero(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """Asking for a group that does not exist is a user error."""
        result = runner.invoke(cli, ["list", "nope", "--global"])

        assert result.exit_code != 0
        assert "not found" in result.stderr

    def test_global_and_repo_together_exits_nonzero(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """``--global`` and ``--repo`` are mutually exclusive."""
        result = runner.invoke(cli, ["list", "--global", "--repo"])

        assert result.exit_code != 0
        assert "Cannot use --global and --repo together." in result.stderr

    def test_unsupported_shell_exits_nonzero(
        self, tmp_mem_dir, runner: CliRunner
    ) -> None:
        """``mem init powershell`` must fail loudly, not print a broken hook."""
        result = runner.invoke(cli, ["init", "powershell"])

        assert result.exit_code != 0
        assert "unsupported shell" in result.stderr
        assert result.stdout == ""

    @pytest.mark.parametrize("shell", ["zsh", "bash", "fish"])
    def test_supported_shells_exit_zero_with_hook_code(
        self, tmp_mem_dir, runner: CliRunner, shell: str
    ) -> None:
        """Supported shells print a hook that mentions ``mem _capture``."""
        result = runner.invoke(cli, ["init", shell])

        assert result.exit_code == 0
        assert "mem _capture" in result.stdout

    def test_export_of_unknown_group_exits_nonzero(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """Exporting a missing group is a user error, not an empty export."""
        result = runner.invoke(cli, ["export", "nope", "--global", "--stdout"])

        assert result.exit_code != 0

    def test_capture_never_fails_the_shell(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """``mem _capture`` runs inside the user's prompt: it must always exit 0."""
        with patch("mem.capture.capture_command", side_effect=OSError("disk full")):
            result = runner.invoke(cli, ["_capture", "git status", "/tmp", "0", "12"])

        assert result.exit_code == 0
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# --json across the CLI
# ---------------------------------------------------------------------------


# (id, args, whether the payload command should appear in the JSON body)
JSON_SURFACES: list[tuple[str, list[str], bool]] = [
    ("search", ["--json", "payload"], True),
    ("list", ["list", "--global", "--json"], True),
    ("list-group", ["list", "demo", "--global", "--json"], True),
    ("stats", ["stats", "--json"], True),
    ("session", ["session", "demo", "--json"], True),
    ("export", ["export", "demo", "--global", "--stdout", "-f", "json"], True),
    ("vars-list", ["vars", "list", "--json"], False),
]


class TestJsonOutput:
    """``--json`` must emit machine-readable output, uncontaminated by Rich."""

    @pytest.mark.parametrize(
        ("args", "expects_payload"),
        [(args, expects) for _id, args, expects in JSON_SURFACES],
        ids=[surface[0] for surface in JSON_SURFACES],
    )
    def test_json_output_is_parseable(
        self,
        tmp_mem_dir,
        runner: CliRunner,
        outside_repo: None,
        args: list[str],
        expects_payload: bool,
    ) -> None:
        """stdout must parse as JSON and keep the command byte-exact."""
        _seed_every_surface(MARKUP_CMD)

        result = runner.invoke(cli, args)

        assert result.exit_code == 0
        parsed = json.loads(result.stdout)
        assert parsed is not None
        if expects_payload:
            assert MARKUP_CMD in json.dumps(parsed)

    @pytest.mark.parametrize(
        "args",
        [args for _id, args, _expects in JSON_SURFACES],
        ids=[surface[0] for surface in JSON_SURFACES],
    )
    def test_json_output_is_not_polluted_by_rich(
        self,
        tmp_mem_dir,
        runner: CliRunner,
        outside_repo: None,
        args: list[str],
    ) -> None:
        """No panels, bullets or ANSI escapes may leak into the JSON stream."""
        _seed_every_surface(MARKUP_CMD)

        result = runner.invoke(cli, args)

        assert result.exit_code == 0
        assert result.stdout.lstrip()[0] in "[{"
        for artifact in ("\x1b[", "●", "─", "╭"):
            assert artifact not in result.stdout

    def test_json_search_on_empty_history_is_an_empty_list(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """An empty result set is ``[]``, never an empty string."""
        result = runner.invoke(cli, ["--json", "anything"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


class TestForget:
    """Deletion is irreversible: the preview is a promise about what dies."""

    def _remaining_commands(self) -> list[str]:
        return [cmd.command for cmd in storage.read_all_commands()]

    def test_declining_confirmation_deletes_nothing(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """Answering "n" must leave history untouched."""
        _add_history("aws s3 ls secret-bucket")
        _add_history("git status")

        result = runner.invoke(cli, ["forget", "secret"], input="n\n")

        assert result.exit_code == 0
        assert sorted(self._remaining_commands()) == [
            "aws s3 ls secret-bucket",
            "git status",
        ]

    def test_confirming_deletes_exactly_what_was_previewed(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """Everything listed in the preview is deleted; nothing else is."""
        _add_history("aws s3 ls secret-bucket")
        _add_history("aws s3 cp secret-bucket/f .")
        _add_history("git status")

        result = runner.invoke(cli, ["forget", "secret"], input="y\n")

        assert result.exit_code == 0
        assert "Found 2 matching commands" in result.stdout
        assert "aws s3 ls secret-bucket" in result.stdout
        assert "aws s3 cp secret-bucket/f ." in result.stdout
        assert "Deleted 2 commands." in result.stdout
        assert self._remaining_commands() == ["git status"]

    def test_yes_flag_skips_the_confirmation(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """``--yes`` deletes without prompting, with no stdin available."""
        _add_history("aws s3 ls secret-bucket")
        _add_history("git status")

        result = runner.invoke(cli, ["forget", "secret", "--yes"])

        assert result.exit_code == 0
        assert "Delete all" not in result.stdout
        assert "Deleted 1 commands." in result.stdout
        assert self._remaining_commands() == ["git status"]

    def test_no_matches_reports_and_keeps_history(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """A query that matches nothing must not touch the JSONL files."""
        _add_history("git status")

        result = runner.invoke(cli, ["forget", "nothing-matches"], input="n\n")

        assert result.exit_code == 0
        assert "No matching commands found." in result.stdout
        assert self._remaining_commands() == ["git status"]

    def test_preview_count_matches_deletion_count(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """The number announced in the preview is the number actually deleted."""
        for i in range(4):
            _add_history(f"curl https://api.example.com/{i} -H 'token: abc'")
        _add_history("git status")

        result = runner.invoke(cli, ["forget", "token: abc"], input="y\n")

        assert "Found 4 matching commands" in result.stdout
        assert "Deleted 4 commands." in result.stdout
        assert self._remaining_commands() == ["git status"]

    @pytest.mark.xfail(
        strict=True,
        reason="P0-3: el preview muestra el comando con el markup ya interpretado",
    )
    def test_preview_shows_the_command_that_will_be_deleted(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """You must be able to recognise what you are about to delete.

        The preview currently renders ``echo [red]payload[/red] mundo`` as
        ``echo payload mundo``, which is a different command than the one that
        gets removed from disk.
        """
        _add_history(MARKUP_CMD)

        result = runner.invoke(cli, ["forget", "payload"], input="n\n")

        assert result.exit_code == 0
        assert MARKUP_CMD in result.stdout


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


class TestStats:
    """Stats must degrade gracefully on a brand-new install."""

    def test_stats_on_empty_history(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """No history at all: report zero, do not crash."""
        result = runner.invoke(cli, ["stats"])

        assert result.exception is None
        assert result.exit_code == 0
        assert "0 total" in result.stdout

    def test_stats_json_on_empty_history(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """The JSON shape is stable even with nothing stored."""
        result = runner.invoke(cli, ["stats", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "total": 0,
            "top_commands": [],
            "top_repos": [],
        }

    def test_stats_counts_repeated_commands(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """Frequencies come from the raw history, duplicates included."""
        for _ in range(3):
            _add_history("git status")
        _add_history("git log")

        result = runner.invoke(cli, ["stats", "--json"])

        payload = json.loads(result.stdout)
        assert payload["total"] == 4
        assert payload["top_commands"][0] == {"command": "git status", "count": 3}


# ---------------------------------------------------------------------------
# Byte fidelity of captured commands
# ---------------------------------------------------------------------------


class TestCapturedCommandFidelity:
    """What the hook captures is what storage keeps and what search shows."""

    @pytest.mark.parametrize(
        ("label", "command"),
        [
            ("unicode", "echo 'héllo wörld — ñandú'"),
            ("emoji", "git commit -m '✅ ship it 🚀'"),
            ("quotes", 'git commit -m "it\'s a \\"test\\""'),
            ("backslash", "grep -E '\\\\d+' file.txt"),
            ("newline", "echo 'line1\nline2'"),
            ("tab", "printf 'a\tb\n'"),
        ],
    )
    def test_capture_roundtrips_bytes_exactly(
        self,
        tmp_mem_dir,
        runner: CliRunner,
        outside_repo: None,
        label: str,
        command: str,
    ) -> None:
        """The stored JSONL entry must equal the captured string exactly."""
        result = runner.invoke(cli, ["_capture", command, "/tmp", "0", "42"])

        assert result.exit_code == 0
        stored = [cmd.command for cmd in storage.read_all_commands()]
        assert stored == [command]

    @pytest.mark.parametrize(
        ("query", "command", "expected_fragments"),
        [
            ("héllo", "echo 'héllo wörld — ñandú'", ["héllo wörld — ñandú"]),
            ("ship", "git commit -m '✅ ship it 🚀'", ["✅ ship it 🚀"]),
            ("test", 'git commit -m "it\'s a \\"test\\""', ["it's a", "test"]),
            ("line1", "echo 'line1\nline2'", ["line1", "line2"]),
        ],
    )
    def test_search_displays_special_characters_intact(
        self,
        tmp_mem_dir,
        runner: CliRunner,
        outside_repo: None,
        query: str,
        command: str,
        expected_fragments: list[str],
    ) -> None:
        """Unicode, emoji, quotes and embedded newlines survive the renderer."""
        runner.invoke(cli, ["_capture", command, "/tmp", "0", "42"])

        result = runner.invoke(cli, [query])

        assert result.exit_code == 0
        for fragment in expected_fragments:
            assert fragment in result.stdout

    def test_unicode_survives_json_roundtrip(
        self, tmp_mem_dir, runner: CliRunner, outside_repo: None
    ) -> None:
        """``--json`` may escape non-ASCII, but decoding must give it back."""
        command = "echo '✅ héllo 🚀'"
        runner.invoke(cli, ["_capture", command, "/tmp", "0", "42"])

        result = runner.invoke(cli, ["--json", "héllo"])

        assert result.exit_code == 0
        assert [entry["command"] for entry in json.loads(result.stdout)] == [command]
