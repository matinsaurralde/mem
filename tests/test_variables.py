"""Tests for `mem.variables` — parsing, the resolution chain, substitution,
resolution status, and AI credential detection.

`variables.py` is the module where secrets live: values pulled from the
persistent store (`~/.mem/vars.json`), from the environment, or typed by the
user at a hidden prompt end up being spliced into a string that is later
handed to `subprocess.run(..., shell=True)`. This file pins down the two
properties that matter for that path:

1. **Resolution is deterministic** — the documented priority chain
   (inline > env > store > default > prompt) holds for every combination.
2. **A resolved value never escapes its intended sink** — it must not be
   printed to the terminal, must not be persisted to the group file, and
   must not be able to alter the structure of the command being executed.

Tests marked ``xfail(strict=True)`` assert the *correct* behaviour and
document that mem does not have it yet. When a fix lands, strict xfail turns
the XPASS into a CI failure so the marker gets removed deliberately.

Every test is isolated from the real ``~/.mem`` via the ``tmp_mem_dir``
fixture and from the real environment via ``monkeypatch``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from mem import groups, storage, variables
from mem.cli import cli
from mem.models import (
    Group,
    GroupCommand,
    GroupFile,
    StoredVariable,
    VarDeclaration,
    VarsFile,
)

# ---------------------------------------------------------------------------
# Ensure apple_fm_sdk is importable even when the real package is absent, so
# that `patch()` can resolve names inside mem.variables without ImportError.
# No test in this file ever reaches the real on-device model.
# ---------------------------------------------------------------------------

if "apple_fm_sdk" not in sys.modules:  # pragma: no cover - environment dependent
    _stub = ModuleType("apple_fm_sdk")
    _stub.LanguageModelSession = MagicMock  # type: ignore[attr-defined]
    sys.modules["apple_fm_sdk"] = _stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Variable names used across this file. Every test scrubs them from the real
# environment first so that a developer's exported shell vars cannot influence
# the outcome.
_TEST_VAR_NAMES = ("API", "API_KEY", "API_TOKEN", "TARGET", "DB_HOST", "NOPE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove test variable names from os.environ for every test.

    Without this, a real `export API_KEY=...` in the developer's shell would
    silently change which branch of the resolution chain is exercised.
    """
    for name in _TEST_VAR_NAMES:
        monkeypatch.delenv(name, raising=False)
    # Rich wraps long lines at the terminal width; a wrapped secret would make
    # "secret not in output" assertions pass for the wrong reason.
    monkeypatch.setenv("COLUMNS", "300")


def _decl(name: str, default: str | None = None) -> VarDeclaration:
    """Build a VarDeclaration without the model boilerplate."""
    return VarDeclaration(name=name, default=default)


def _store(**values: str) -> dict[str, StoredVariable]:
    """Build a stored-variable mapping shaped like `VarsFile.vars`."""
    return {name: StoredVariable(value=value) for name, value in values.items()}


def _write_global_group(
    commands: list[GroupCommand],
    name: str = "test",
    description: str | None = None,
) -> None:
    """Persist a single global group so the `mem run` CLI can find it."""
    storage.write_group_file(
        storage.GROUPS_GLOBAL_FILE,
        GroupFile(groups={name: Group(description=description, commands=commands)}),
    )


def _async_returning(value: Any) -> Callable[..., Any]:
    """Build an async stand-in for `_detect_credentials_async`."""

    async def _fake(_cmd: str) -> Any:
        return value

    return _fake


def _mock_fm(detections: Iterable[tuple[str, str, str]]) -> Any:
    """Context manager stack that fakes the whole Apple FM detection layer.

    Patches availability to True (so the test result does not depend on
    whether the SDK is installed) and replaces the async model call with a
    canned response. The real model is never invoked.
    """
    return patch.multiple(
        "mem.variables",
        _apple_fm_available=MagicMock(return_value=True),
        _detect_credentials_async=_async_returning(list(detections)),
    )


# ---------------------------------------------------------------------------
# 1. Detection of $VAR_NAME tokens
# ---------------------------------------------------------------------------


class TestParseVariables:
    """Contract: `$NAME` tokens are detected, shell builtins and escapes are not."""

    def test_detects_uppercase_tokens_in_order(self) -> None:
        """Contract: detection preserves first-occurrence order and dedupes."""
        assert variables.parse_variables("curl $HOST/$PATH_X -H $HOST") == [
            "HOST",
            "PATH_X",
        ]

    def test_excludes_common_shell_variables(self) -> None:
        """Contract: $HOME/$PATH/$USER belong to the shell, not to mem."""
        assert variables.parse_variables("cd $HOME && echo $PATH $USER") == []

    def test_single_letter_is_not_a_variable(self) -> None:
        """Contract: names need >= 2 chars, so `$A` stays literal shell text."""
        assert variables.parse_variables("echo $A") == []

    def test_lowercase_is_not_a_variable(self) -> None:
        """Contract: only UPPER_SNAKE tokens are mem variables."""
        assert variables.parse_variables("echo $path $Token") == []

    def test_subshell_and_arithmetic_are_not_variables(self) -> None:
        """Contract: `$(...)` and `$((...))` are shell syntax, not variables."""
        assert variables.parse_variables("echo $(DATE) $((COUNT + 1))") == []

    def test_double_dollar_escape_is_not_detected(self) -> None:
        """Contract: `$$API_KEY` is an escape — the shell owns it, not mem."""
        assert variables.parse_variables("echo $$API_KEY and $API") == ["API"]


class TestProcessEscapes:
    """Contract: `$$NAME` collapses to `$NAME` in the stored command text."""

    def test_escape_is_collapsed(self) -> None:
        assert variables.process_escapes("echo $$API_KEY") == "echo $API_KEY"

    def test_plain_variable_untouched(self) -> None:
        assert variables.process_escapes("echo $API_KEY") == "echo $API_KEY"

    def test_lone_double_dollar_untouched(self) -> None:
        """Contract: `$$` alone is the shell's PID, not an escape sequence."""
        assert variables.process_escapes("echo $$") == "echo $$"


# ---------------------------------------------------------------------------
# 2. Full truth table of the resolution chain
# ---------------------------------------------------------------------------

# Each row: (inline, env, store, default, expected_value, expected_source).
# `None` means "this source does not provide the variable".
_CHAIN_CASES: list[tuple[str | None, str | None, str | None, str | None, str, str]] = [
    # --- single source present -------------------------------------------
    ("inline_v", None, None, None, "inline_v", "arguments"),
    (None, "env_v", None, None, "env_v", "environment"),
    (None, None, "store_v", None, "store_v", "store"),
    (None, None, None, "default_v", "default_v", "default"),
    # --- inline beats everything below it ---------------------------------
    ("inline_v", "env_v", None, None, "inline_v", "arguments"),
    ("inline_v", None, "store_v", None, "inline_v", "arguments"),
    ("inline_v", None, None, "default_v", "inline_v", "arguments"),
    ("inline_v", "env_v", "store_v", "default_v", "inline_v", "arguments"),
    # --- env beats store and default --------------------------------------
    (None, "env_v", "store_v", None, "env_v", "environment"),
    (None, "env_v", None, "default_v", "env_v", "environment"),
    (None, "env_v", "store_v", "default_v", "env_v", "environment"),
    # --- store beats default ----------------------------------------------
    (None, None, "store_v", "default_v", "store_v", "store"),
]

_CHAIN_IDS = [
    "inline-only",
    "env-only",
    "store-only",
    "default-only",
    "inline-over-env",
    "inline-over-store",
    "inline-over-default",
    "inline-over-all",
    "env-over-store",
    "env-over-default",
    "env-over-store-and-default",
    "store-over-default",
]


class TestResolutionChain:
    """Contract: inline > environment > store > default > prompt, always."""

    @pytest.mark.parametrize(
        ("inline", "env", "store", "default", "expected_value", "expected_source"),
        _CHAIN_CASES,
        ids=_CHAIN_IDS,
    )
    def test_priority_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
        inline: str | None,
        env: str | None,
        store: str | None,
        default: str | None,
        expected_value: str,
        expected_source: str,
    ) -> None:
        """Contract: the highest-priority source that has a value wins."""
        if env is not None:
            monkeypatch.setenv("API_KEY", env)

        prompt = MagicMock(name="prompt_fn")
        resolved = variables.resolve_variables(
            [_decl("API_KEY", default=default)],
            inline_args={"API_KEY": inline} if inline is not None else {},
            stored_vars=_store(API_KEY=store) if store is not None else {},
            prompt_fn=prompt,
            allow_prompt=False,
        )

        assert resolved["API_KEY"] == (expected_value, expected_source)
        prompt.assert_not_called()

    def test_empty_env_value_wins_over_store_and_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract (pinned): an exported-but-empty env var is a real value.

        `resolve_variables` tests `os.environ.get(name) is not None`, so
        `export API_KEY=""` shadows both the store and the declared default and
        substitutes an empty string. This test pins that semantic so a future
        refactor cannot flip it silently — flipping it to shell-style
        `${VAR:-default}` fallback is a deliberate product decision, not an
        implementation detail.
        """
        monkeypatch.setenv("API_KEY", "")
        resolved = variables.resolve_variables(
            [_decl("API_KEY", default="default_v")],
            inline_args={},
            stored_vars=_store(API_KEY="store_v"),
            prompt_fn=MagicMock(),
            allow_prompt=False,
        )
        assert resolved["API_KEY"] == ("", "environment")

    def test_empty_inline_value_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract: `mem run g VAR=` is an explicit request for an empty value."""
        monkeypatch.setenv("API_KEY", "env_v")
        resolved = variables.resolve_variables(
            [_decl("API_KEY", default="default_v")],
            inline_args={"API_KEY": ""},
            stored_vars=_store(API_KEY="store_v"),
            prompt_fn=MagicMock(),
            allow_prompt=False,
        )
        assert resolved["API_KEY"] == ("", "arguments")

    def test_empty_stored_value_still_wins_over_default(self) -> None:
        """Contract: a stored empty string is a value the user chose to store."""
        resolved = variables.resolve_variables(
            [_decl("API_KEY", default="default_v")],
            inline_args={},
            stored_vars=_store(API_KEY=""),
            prompt_fn=MagicMock(),
            allow_prompt=False,
        )
        assert resolved["API_KEY"] == ("", "store")

    def test_prompt_is_last_resort(self) -> None:
        """Contract: with no source at all, the user is asked and tagged 'prompt'."""
        prompt = MagicMock(return_value="typed_v")
        resolved = variables.resolve_variables(
            [_decl("API_KEY")],
            inline_args={},
            stored_vars={},
            prompt_fn=prompt,
            allow_prompt=True,
        )
        assert resolved["API_KEY"] == ("typed_v", "prompt")
        prompt.assert_called_once()

    def test_prompt_accepting_default_is_tagged_default(self) -> None:
        """Contract: hitting Enter at the prompt yields the default, sourced 'default'."""
        prompt = MagicMock(return_value="")
        resolved = variables.resolve_variables(
            [_decl("API_KEY", default="default_v")],
            inline_args={},
            stored_vars={},
            prompt_fn=prompt,
            allow_prompt=True,
        )
        assert resolved["API_KEY"] == ("default_v", "default")

    def test_prompt_overriding_default_is_tagged_prompt(self) -> None:
        """Contract: typing over the offered default is sourced 'prompt'."""
        prompt = MagicMock(return_value="typed_v")
        resolved = variables.resolve_variables(
            [_decl("API_KEY", default="default_v")],
            inline_args={},
            stored_vars={},
            prompt_fn=prompt,
            allow_prompt=True,
        )
        assert resolved["API_KEY"] == ("typed_v", "prompt")

    def test_multiple_variables_resolved_independently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract: each declaration walks the chain on its own."""
        monkeypatch.setenv("DB_HOST", "db.internal")
        resolved = variables.resolve_variables(
            [_decl("API_KEY"), _decl("DB_HOST"), _decl("TARGET", default="prod")],
            inline_args={"API_KEY": "k"},
            stored_vars={},
            prompt_fn=MagicMock(),
            allow_prompt=False,
        )
        assert resolved == {
            "API_KEY": ("k", "arguments"),
            "DB_HOST": ("db.internal", "environment"),
            "TARGET": ("prod", "default"),
        }

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "allow_prompt=False is ignored for variables with no default: "
            "resolve_variables still calls prompt_fn, so `mem run --yes` can "
            "block on a hidden click.prompt in a non-interactive context"
        ),
    )
    def test_allow_prompt_false_never_prompts(self) -> None:
        """Contract: `allow_prompt=False` (i.e. `--yes`) must never prompt.

        The docstring of `resolve_variables` promises "If False, skip
        interactive prompts (for --yes mode)". Today the flag only short-
        circuits the *default* branch; a variable with no source and no default
        still falls through to `prompt_fn`, which in `--yes`/CI mode is
        `click.prompt` on a closed stdin.
        """
        prompt = MagicMock(return_value="whatever")
        variables.resolve_variables(
            [_decl("NOPE")],
            inline_args={},
            stored_vars={},
            prompt_fn=prompt,
            allow_prompt=False,
        )
        prompt.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Substitution: prefix collisions and escapes
# ---------------------------------------------------------------------------


class TestSubstituteVariables:
    """Contract: textual replacement must not corrupt neighbouring tokens."""

    def test_prefix_collision_longest_name_first(self) -> None:
        """Contract: `$API` and `$API_KEY` in one command resolve independently.

        Naive left-to-right replacement would turn `$API_KEY` into
        `<api-value>_KEY`. Longest-name-first ordering prevents that.
        """
        resolved = {
            "API": ("https://api.test", "arguments"),
            "API_KEY": ("k-123", "arguments"),
        }
        out = variables.substitute_variables("curl $API/v1 -H key:$API_KEY", resolved)
        assert out == "curl https://api.test/v1 -H key:k-123"
        assert "_KEY" not in out

    def test_prefix_collision_reversed_declaration_order(self) -> None:
        """Contract: the result does not depend on dict insertion order."""
        resolved = {
            "API_KEY": ("k-123", "arguments"),
            "API": ("https://api.test", "arguments"),
        }
        out = variables.substitute_variables("$API_KEY|$API|$API_KEY", resolved)
        assert out == "k-123|https://api.test|k-123"

    def test_three_way_prefix_chain(self) -> None:
        """Contract: nested prefixes ($API / $API_KEY / $API_KEY_ID) all survive."""
        resolved = {
            "API": ("a", "arguments"),
            "API_KEY": ("b", "arguments"),
            "API_KEY_ID": ("c", "arguments"),
        }
        out = variables.substitute_variables("$API $API_KEY $API_KEY_ID", resolved)
        assert out == "a b c"

    def test_repeated_token_replaced_everywhere(self) -> None:
        """Contract: every occurrence of a token is substituted."""
        resolved = {"TARGET": ("prod", "arguments")}
        out = variables.substitute_variables(
            "deploy $TARGET && verify $TARGET", resolved
        )
        assert out == "deploy prod && verify prod"

    def test_undeclared_token_left_alone(self) -> None:
        """Contract: tokens with no resolved value are handed to the shell as-is."""
        resolved = {"API": ("a", "arguments")}
        out = variables.substitute_variables("echo $API $HOME", resolved)
        assert out == "echo a $HOME"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "substitute_variables ignores $$ escapes: `$$API_KEY` becomes "
            "`$<value>` because .replace() matches the inner `$API_KEY`"
        ),
    )
    def test_escaped_token_is_not_substituted(self) -> None:
        """Contract: `$$NAME` must reach the shell as `$NAME`, unsubstituted.

        `process_escapes` collapses `$$NAME` to `$NAME` at save time, so in the
        normal flow the escaped command carries `vars=None` and is skipped.
        But `mem run` resolves variables once for the *whole group* and then
        substitutes into every command that has any `vars`, so an escaped token
        sharing a name with another command's variable gets clobbered.
        """
        resolved = {"API_KEY": ("s3cr3t", "store")}
        assert variables.substitute_variables("echo $$API_KEY", resolved) == (
            "echo $$API_KEY"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the $$ escape is destroyed by an export/import round-trip: "
            "process_escapes stores `$NAME` and _auto_detect_vars then "
            "re-detects it as a mem variable"
        ),
    )
    def test_escape_survives_export_import_round_trip(self) -> None:
        """Contract: a shell variable the user escaped stays a shell variable.

        `mem save 'echo $$API_KEY'` records the intent "the shell expands this".
        After export/import that intent must still hold — otherwise a runbook
        shared between machines starts prompting for, or substituting, a value
        the author never meant mem to own.
        """
        stored_cmd = variables.process_escapes("echo $$API_KEY")
        imported = [GroupCommand(cmd=stored_cmd, comment=None, vars=None)]
        groups._auto_detect_vars(imported)
        assert imported[0].vars is None


class TestMergeVarDeclarations:
    """Contract: detected tokens and explicit --var flags merge without dupes."""

    def test_detected_only(self) -> None:
        merged = variables.merge_var_declarations(["API_KEY"], [])
        assert merged == [_decl("API_KEY")]

    def test_explicit_default_enriches_detected(self) -> None:
        """Contract: `--var NAME=default` adds a default, it does not duplicate."""
        merged = variables.merge_var_declarations(["TARGET"], [("TARGET", "prod")])
        assert merged == [_decl("TARGET", "prod")]

    def test_explicit_only_is_added(self) -> None:
        """Contract: a --var not present in the text is still declared."""
        merged = variables.merge_var_declarations([], [("TARGET", None)])
        assert merged == [_decl("TARGET")]

    def test_detection_order_preserved(self) -> None:
        merged = variables.merge_var_declarations(
            ["API", "API_KEY"], [("API_KEY", "k"), ("DB_HOST", "h")]
        )
        assert [v.name for v in merged] == ["API", "API_KEY", "DB_HOST"]


# ---------------------------------------------------------------------------
# 4. check_resolution_status — what `mem list <group>` shows
# ---------------------------------------------------------------------------


class TestCheckResolutionStatus:
    """Contract: the listing status matches where a value would actually come from."""

    def test_environment_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("API_KEY", "v")
        assert variables.check_resolution_status([_decl("API_KEY")], {}) == [
            ("API_KEY", "resolved", "from environment")
        ]

    def test_store_source(self) -> None:
        assert variables.check_resolution_status(
            [_decl("API_KEY")], _store(API_KEY="v")
        ) == [("API_KEY", "resolved", "from store")]

    def test_default_source(self) -> None:
        assert variables.check_resolution_status([_decl("TARGET", "prod")], {}) == [
            ("TARGET", "resolved", "default: prod")
        ]

    def test_unset_hint_names_the_group(self) -> None:
        """Contract: the hint is a copy-pasteable command for the actual group."""
        assert variables.check_resolution_status(
            [_decl("API_KEY")], {}, group_name="deploy"
        ) == [
            (
                "API_KEY",
                "unset",
                "pass inline: mem run deploy API_KEY=<value>",
            )
        ]

    def test_status_priority_matches_resolution_priority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract: `mem list` must not promise a source `mem run` won't use.

        Env shadows store shadows default in both functions; if these two ever
        disagree the listing lies to the user about which value will be used.
        """
        monkeypatch.setenv("API_KEY", "env_v")
        decls = [_decl("API_KEY", default="default_v")]
        stored = _store(API_KEY="store_v")

        statuses = variables.check_resolution_status(decls, stored)
        resolved = variables.resolve_variables(
            decls, {}, stored, prompt_fn=MagicMock(), allow_prompt=False
        )

        assert statuses[0][2] == "from environment"
        assert resolved["API_KEY"][1] == "environment"

    def test_empty_env_var_reported_as_resolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract (pinned): status mirrors resolution for empty env values.

        Both use presence, not truthiness, so `export API_KEY=""` shows
        "resolved / from environment" and substitutes "". Consistent — but
        pinned here because it is the surprising half of the empty-value rule.
        """
        monkeypatch.setenv("API_KEY", "")
        assert variables.check_resolution_status([_decl("API_KEY")], {}) == [
            ("API_KEY", "resolved", "from environment")
        ]


# ---------------------------------------------------------------------------
# 5. P0-9 — stored secrets must not be echoed to the terminal
# ---------------------------------------------------------------------------

# A value shaped like a real secret but obviously fake. No Rich markup
# characters ("[", "]") so the assertion cannot be defeated by markup eating.
_SECRET = "sk-live-9Xq2TESTONLYnotarealkey"


class TestSecretLeakage:
    """Contract: a value the user typed behind a hidden prompt stays hidden."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "P0-9: `mem run` prints the fully substituted command "
            "(`console.print(f'  [dim]$ {run_cmd}[/]')`), so a secret pulled "
            "from the store is echoed in cleartext to the terminal"
        ),
    )
    def test_stored_secret_not_printed_by_run(self, tmp_mem_dir: Path) -> None:
        """Contract: `mem run` must not print a resolved secret in cleartext.

        The user stored the value with `mem vars set API_TOKEN` (hidden input)
        precisely so it never appears on screen. The execution preview should
        show the unresolved `$API_TOKEN`, or a mask — never the value. As
        written today the value lands in the terminal, in the scrollback, and
        in any CI log that captured the run.
        """
        storage.write_vars_file(
            VarsFile(vars={"API_TOKEN": StoredVariable(value=_SECRET)})
        )
        # `true` ignores its arguments, so the only possible source of the
        # secret in the captured output is mem's own preview line.
        _write_global_group(
            [GroupCommand(cmd="true --token=$API_TOKEN", vars=[_decl("API_TOKEN")])]
        )

        result = CliRunner().invoke(cli, ["run", "test", "--global", "--yes"])

        assert result.exit_code == 0, result.output
        assert _SECRET not in result.output

    def test_resolution_summary_names_the_source_not_the_value(
        self, tmp_mem_dir: Path
    ) -> None:
        """Contract: the "resolved from store" line reports provenance only."""
        storage.write_vars_file(
            VarsFile(vars={"API_TOKEN": StoredVariable(value=_SECRET)})
        )
        _write_global_group(
            [GroupCommand(cmd="true --token=$API_TOKEN", vars=[_decl("API_TOKEN")])]
        )

        result = CliRunner().invoke(cli, ["run", "test", "--global", "--yes"])

        assert result.exit_code == 0, result.output
        assert "$API_TOKEN resolved from store" in result.output

    def test_vars_list_never_prints_values(self, tmp_mem_dir: Path) -> None:
        """Contract: `mem vars list` shows names and metadata, never values."""
        storage.write_vars_file(
            VarsFile(vars={"API_TOKEN": StoredVariable(value=_SECRET)})
        )
        result = CliRunner().invoke(cli, ["vars", "list"])
        assert result.exit_code == 0, result.output
        assert "API_TOKEN" in result.output
        assert _SECRET not in result.output

    def test_resolved_value_never_written_to_group_file(
        self, tmp_mem_dir: Path
    ) -> None:
        """Contract: a runtime value must never be persisted into the runbook.

        Group files are the artefact users export, commit and share. A resolved
        value leaking into one turns a shared runbook into a credential leak.
        """
        _write_global_group(
            [GroupCommand(cmd="true --token=$API_TOKEN", vars=[_decl("API_TOKEN")])]
        )
        before = storage.GROUPS_GLOBAL_FILE.read_text(encoding="utf-8")

        result = CliRunner().invoke(
            cli, ["run", "test", "--global", "--yes", f"API_TOKEN={_SECRET}"]
        )

        assert result.exit_code == 0, result.output
        after = storage.GROUPS_GLOBAL_FILE.read_text(encoding="utf-8")
        assert _SECRET not in after
        assert after == before

    def test_inline_value_is_not_silently_persisted_to_the_store(
        self, tmp_mem_dir: Path
    ) -> None:
        """Contract: `mem run g VAR=value` is ephemeral, not an implicit `vars set`."""
        _write_global_group(
            [GroupCommand(cmd="true --token=$API_TOKEN", vars=[_decl("API_TOKEN")])]
        )

        result = CliRunner().invoke(
            cli, ["run", "test", "--global", "--yes", f"API_TOKEN={_SECRET}"]
        )

        assert result.exit_code == 0, result.output
        assert "API_TOKEN" not in storage.read_vars_file().vars
        if storage.VARS_FILE.exists():
            assert _SECRET not in storage.VARS_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 6. P0-2 — a variable value must not be able to inject shell syntax
# ---------------------------------------------------------------------------


class TestCommandInjection:
    """Contract: a variable supplies a *value*, never new command structure."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "P0-2: substitute_variables splices the raw value into the command "
            "string and cli.run executes it with shell=True, so `; touch X` in "
            "a value runs as a second command"
        ),
    )
    def test_inline_value_cannot_inject_a_second_command(
        self, tmp_mem_dir: Path
    ) -> None:
        """Contract: `; touch sentinel` inside a value is data, not a command.

        The sentinel file proves execution: if it exists after the run, the
        payload reached the shell as syntax rather than as an argument.
        """
        sentinel = tmp_mem_dir / "mem_pwned_inline"
        _write_global_group([GroupCommand(cmd="echo $TARGET", vars=[_decl("TARGET")])])

        result = CliRunner().invoke(
            cli,
            ["run", "test", "--global", "--yes", f"TARGET=safe; touch {sentinel}"],
        )

        assert result.exit_code == 0, result.output
        assert not sentinel.exists(), "variable value executed as a shell command"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "P0-2: command substitution `$(...)` inside a variable value is "
            "evaluated by the shell after textual splicing"
        ),
    )
    def test_inline_value_cannot_trigger_command_substitution(
        self, tmp_mem_dir: Path
    ) -> None:
        """Contract: `$(touch sentinel)` inside a value must not be evaluated."""
        sentinel = tmp_mem_dir / "mem_pwned_subshell"
        _write_global_group([GroupCommand(cmd="echo $TARGET", vars=[_decl("TARGET")])])

        result = CliRunner().invoke(
            cli,
            ["run", "test", "--global", "--yes", f"TARGET=$(touch {sentinel})"],
        )

        assert result.exit_code == 0, result.output
        assert not sentinel.exists(), "command substitution ran inside a value"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "P0-2 (imported runbook vector): the payload lives in the "
            "variable's `default`, which mem run never displays, and is "
            "spliced into the shell=True command in --yes mode"
        ),
    )
    def test_default_value_from_imported_runbook_cannot_inject(
        self, tmp_mem_dir: Path
    ) -> None:
        """Contract: a default shipped inside a runbook is data, not a command.

        This is the dangerous variant. The command listing at the top of
        `mem run` prints the *stored* text (`echo $TARGET`) — the default is
        never shown — so a user reviewing an imported runbook sees nothing
        suspicious while the payload executes.
        """
        sentinel = tmp_mem_dir / "mem_pwned_default"
        _write_global_group(
            [
                GroupCommand(
                    cmd="echo $TARGET",
                    vars=[_decl("TARGET", default=f"safe; touch {sentinel}")],
                )
            ]
        )

        result = CliRunner().invoke(cli, ["run", "test", "--global", "--yes"])

        assert result.exit_code == 0, result.output
        assert not sentinel.exists(), "runbook default executed as a shell command"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "P0-2: a value pulled from the persistent store is spliced into "
            "the command text with no quoting before shell=True execution"
        ),
    )
    def test_stored_value_cannot_inject(self, tmp_mem_dir: Path) -> None:
        """Contract: a corrupted/poisoned vars.json entry cannot run commands."""
        sentinel = tmp_mem_dir / "mem_pwned_store"
        storage.write_vars_file(
            VarsFile(vars={"TARGET": StoredVariable(value=f"safe; touch {sentinel}")})
        )
        _write_global_group([GroupCommand(cmd="echo $TARGET", vars=[_decl("TARGET")])])

        result = CliRunner().invoke(cli, ["run", "test", "--global", "--yes"])

        assert result.exit_code == 0, result.output
        assert not sentinel.exists(), "stored value executed as a shell command"

    def test_imported_runbook_default_is_hidden_from_the_run_preview(
        self, tmp_mem_dir: Path
    ) -> None:
        """Contract (pinned): the command listing shows stored text only.

        Documents *why* the default-injection vector is the severe one: the
        numbered listing `mem run` prints before asking for confirmation never
        reveals the default, so review is impossible from that screen alone.
        """
        _write_global_group(
            [
                GroupCommand(
                    cmd="echo $TARGET",
                    vars=[_decl("TARGET", default="payload-marker-1234")],
                )
            ]
        )

        with patch("mem.cli._is_interactive", return_value=True):
            result = CliRunner().invoke(cli, ["run", "test", "--global"], input="n\n")

        assert result.exit_code == 0, result.output
        assert "1. echo $TARGET" in result.output
        assert "payload-marker-1234" not in result.output


# ---------------------------------------------------------------------------
# 7. vars.json permissions (including the tmp file's TOCTOU window)
# ---------------------------------------------------------------------------


class TestVarsFilePermissions:
    """Contract: the file holding secrets is never readable by other users."""

    def test_vars_file_is_owner_only(self, tmp_mem_dir: Path) -> None:
        """Contract: vars.json ends up at mode 0600."""
        storage.write_vars_file(
            VarsFile(vars={"API_TOKEN": StoredVariable(value=_SECRET)})
        )
        mode = stat.S_IMODE(storage.VARS_FILE.stat().st_mode)
        assert mode == 0o600, f"vars.json is {oct(mode)}"

    def test_permissions_survive_rewrite(self, tmp_mem_dir: Path) -> None:
        """Contract: overwriting an existing store does not widen its mode."""
        storage.write_vars_file(VarsFile(vars={"API_TOKEN": StoredVariable(value="a")}))
        os.chmod(storage.VARS_FILE, 0o644)
        storage.write_vars_file(VarsFile(vars={"API_TOKEN": StoredVariable(value="b")}))
        mode = stat.S_IMODE(storage.VARS_FILE.stat().st_mode)
        assert mode == 0o600, f"vars.json is {oct(mode)} after rewrite"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "TOCTOU: write_vars_file writes vars.json.tmp with the process "
            "umask (0644) and only chmods it afterwards, so the secrets are "
            "world-readable for the duration of that window"
        ),
    )
    def test_tmp_file_is_never_world_readable(
        self, tmp_mem_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contract: secrets are never on disk with a mode wider than 0600.

        `write_vars_file` uses write-tmp-then-rename. The tmp file is created
        by `Path.write_text` with the default umask and only chmodded to 0600
        on the next line — any process on the box can read it in between. The
        fix is to create the file with the right mode from the start
        (`os.open(..., os.O_CREAT | os.O_WRONLY, 0o600)`).
        """
        observed: list[tuple[str, int]] = []
        original_write_text = Path.write_text

        def spy(self: Path, *args: Any, **kwargs: Any) -> int:
            result = original_write_text(self, *args, **kwargs)
            if self.name.endswith(".tmp"):
                observed.append((self.name, stat.S_IMODE(self.stat().st_mode)))
            return result

        monkeypatch.setattr(Path, "write_text", spy)
        storage.write_vars_file(
            VarsFile(vars={"API_TOKEN": StoredVariable(value=_SECRET)})
        )

        assert observed, "expected write_vars_file to go through a .tmp file"
        assert all(mode == 0o600 for _name, mode in observed), (
            f"tmp file exposed at {[(n, oct(m)) for n, m in observed]}"
        )


# ---------------------------------------------------------------------------
# 8. detect_credentials — Apple FM layer fully mocked
# ---------------------------------------------------------------------------


class TestDetectCredentialsPrefilter:
    """Contract: trivial commands never reach the on-device model."""

    @pytest.mark.parametrize(
        "cmd",
        ["ls -la", "cd ..", "git status", "echo hi"],
        ids=["ls", "cd", "git-status", "echo"],
    )
    def test_trivial_commands_skip_the_model(self, cmd: str) -> None:
        detector = AsyncMock(return_value=[])
        with (
            patch("mem.variables._apple_fm_available", return_value=True),
            patch("mem.variables._detect_credentials_async", detector),
        ):
            assert variables.detect_credentials(cmd) == []
        detector.assert_not_called()

    @pytest.mark.parametrize(
        "cmd",
        [
            'curl -H "Authorization: Bearer abc"',
            "mysql --password=hunter2",
            "export API_KEY=x",
            "deploy --flag AAAAAAAAAAAAAAAAAAAA",
        ],
        ids=["bearer", "password-flag", "api-key", "long-token"],
    )
    def test_plausible_commands_reach_the_model(self, cmd: str) -> None:
        detector = AsyncMock(return_value=[])
        with (
            patch("mem.variables._apple_fm_available", return_value=True),
            patch("mem.variables._detect_credentials_async", detector),
        ):
            variables.detect_credentials(cmd)
        detector.assert_called_once()

    def test_returns_empty_when_sdk_unavailable(self) -> None:
        """Contract: no Apple FM SDK means no detection, never a crash."""
        detector = AsyncMock(return_value=[("x", "Y_Z", "r")])
        with (
            patch("mem.variables._apple_fm_available", return_value=False),
            patch("mem.variables._detect_credentials_async", detector),
        ):
            assert variables.detect_credentials("curl -H 'Authorization: x'") == []
        detector.assert_not_called()


class TestDetectCredentialsFiltering:
    """Contract: the model's raw output is filtered before the user sees it."""

    def test_real_secret_is_reported(self) -> None:
        """Control test: proves the mocked pipeline actually reaches the filter.

        Without this, every "nothing was reported" assertion below could pass
        because the mock is wired wrong and `detect_credentials` swallowed an
        exception into an empty list.
        """
        cmd = 'curl -H "Authorization: Bearer ghp_ABCDEFGHIJKLMNOP1234"'
        with _mock_fm(
            [("ghp_ABCDEFGHIJKLMNOP1234", "GITHUB_TOKEN", "GitHub personal token")]
        ):
            assert variables.detect_credentials(cmd) == [
                ("ghp_ABCDEFGHIJKLMNOP1234", "GITHUB_TOKEN", "GitHub personal token")
            ]

    def test_flag_syntax_is_unwrapped_to_the_bare_secret(self) -> None:
        """Contract: `--password=hunter2horse` yields the value, not the flag."""
        cmd = "mysql -h db --password=hunter2horsebattery"
        with _mock_fm(
            [("--password=hunter2horsebattery", "DB_PASSWORD", "inline password")]
        ):
            assert variables.detect_credentials(cmd) == [
                ("hunter2horsebattery", "DB_PASSWORD", "inline password")
            ]

    def test_env_assignment_syntax_is_unwrapped(self) -> None:
        """Contract: `GITHUB_TOKEN=ghp_...` yields only the value part."""
        cmd = "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOP gh repo list"
        with _mock_fm([("GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOP", "GITHUB_TOKEN", "token")]):
            assert variables.detect_credentials(cmd) == [
                ("ghp_ABCDEFGHIJKLMNOP", "GITHUB_TOKEN", "token")
            ]

    def test_camelcase_acronym_is_split_by_normalization(self) -> None:
        """Contract (pinned): CamelCase splitting is naive about acronyms.

        `_normalize_var_name` inserts an underscore at every lower->upper
        boundary, so the model's "GitHubToken" becomes `GIT_HUB_TOKEN`, not
        `GITHUB_TOKEN`. Cosmetic, but pinned so the suggestion shown to the
        user cannot change without someone noticing.
        """
        cmd = "gh auth login --with-token ghp_ABCDEFGHIJKLMNOP"
        with _mock_fm([("ghp_ABCDEFGHIJKLMNOP", "GitHubToken", "token")]):
            assert variables.detect_credentials(cmd) == [
                ("ghp_ABCDEFGHIJKLMNOP", "GIT_HUB_TOKEN", "token")
            ]

    def test_hallucinated_value_is_dropped(self) -> None:
        """Contract: a value the model invented is not offered to the user."""
        cmd = "curl -H 'Authorization: Bearer realtokenvalue123'"
        with _mock_fm([("never-appeared-in-the-command", "SOME_TOKEN", "r")]):
            assert variables.detect_credentials(cmd) == []

    def test_url_is_not_a_credential(self) -> None:
        cmd = "curl --token abcdefghijklmnop https://api.example.com/v1/things"
        with _mock_fm([("https://api.example.com/v1/things", "API_URL", "r")]):
            assert variables.detect_credentials(cmd) == []

    def test_hostname_is_not_a_credential(self) -> None:
        cmd = "psql --password=supersecretvalue -h db.prod.example.com"
        with _mock_fm([("db.prod.example.com", "DB_HOST", "r")]):
            assert variables.detect_credentials(cmd) == []

    def test_short_value_is_not_a_credential(self) -> None:
        cmd = "mysql --password=abc --host longhostnamevaluehere"
        with _mock_fm([("abc", "DB_PASSWORD", "r")]):
            assert variables.detect_credentials(cmd) == []

    def test_duplicate_detections_collapse(self) -> None:
        cmd = "curl -H 'Authorization: Bearer tok_ABCDEFGHIJKLMNOP'"
        with _mock_fm(
            [
                ("tok_ABCDEFGHIJKLMNOP", "API_TOKEN", "first"),
                ("tok_ABCDEFGHIJKLMNOP", "OTHER_TOKEN", "second"),
            ]
        ):
            assert variables.detect_credentials(cmd) == [
                ("tok_ABCDEFGHIJKLMNOP", "API_TOKEN", "first")
            ]

    def test_substring_detection_is_dropped(self) -> None:
        """Contract: a fragment of another detected secret is not a second finding."""
        cmd = "curl -H 'Authorization: Bearer tok_ABCDEFGHIJKLMNOP'"
        with _mock_fm(
            [
                ("tok_ABCDEFGHIJKLMNOP", "API_TOKEN", "full"),
                ("ABCDEFGHIJKLMNOP", "API_TOKEN_PART", "fragment"),
            ]
        ):
            assert variables.detect_credentials(cmd) == [
                ("tok_ABCDEFGHIJKLMNOP", "API_TOKEN", "full")
            ]

    def test_suggested_name_is_normalized_to_upper_snake(self) -> None:
        cmd = "stripe --api-key sk_test_ABCDEFGHIJKLMNOP"
        with _mock_fm([("sk_test_ABCDEFGHIJKLMNOP", "stripeApiKey", "r")]):
            assert variables.detect_credentials(cmd) == [
                ("sk_test_ABCDEFGHIJKLMNOP", "STRIPE_API_KEY", "r")
            ]

    def test_model_failure_is_swallowed(self) -> None:
        """Contract: a model error degrades to "no detections", never a crash."""

        async def _boom(_cmd: str) -> list[tuple[str, str, str]]:
            raise RuntimeError("model unavailable")

        with (
            patch("mem.variables._apple_fm_available", return_value=True),
            patch("mem.variables._detect_credentials_async", _boom),
        ):
            assert variables.detect_credentials("curl -H 'Authorization: x'") == []

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "known false positive: `StrictHostKeyChecking=no` passes every "
            "filter in _deduplicate_detections (present in cmd, not a URL, "
            "not a hostname, >= 8 chars) so it is offered as DB_PASSWORD"
        ),
    )
    def test_ssh_option_is_not_reported_as_a_credential(self) -> None:
        """Contract: `-o StrictHostKeyChecking=no` is an SSH option, not a secret.

        This is the documented false positive: mem asks the user to replace a
        well-known SSH option with a `$DB_PASSWORD` variable. Beyond being
        wrong, accepting the suggestion rewrites the command into something
        that no longer disables host-key checking. `_deduplicate_detections`
        needs to reject `Key=Value` option syntax whose value is a known
        constant (`yes`/`no`/`accept-new`) — or reject any detection whose
        "secret" is a bare option token.
        """
        cmd = "ssh -o StrictHostKeyChecking=no deploy@example.com"
        with _mock_fm(
            [("StrictHostKeyChecking=no", "DB_PASSWORD", "looks like a password")]
        ):
            assert variables.detect_credentials(cmd) == []
