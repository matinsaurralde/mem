"""Tests for `mem.keychain` and the Keychain-backed variable store.

`mem vars set` used to write API tokens into `~/.mem/vars.json` in cleartext.
Since ADR-009 the value goes into the macOS Keychain and the file keeps only
the name. This file pins the four properties that make that worth doing:

1. **The secret never reaches argv.** `security -w <secret>` puts the value in
   the process table, where any user on the machine can read it with `ps`. mem
   sends it down stdin to `security -i` instead, and the test asserts on the
   argv the subprocess boundary actually received.
2. **The value survives the round trip byte for byte.** Shell metacharacters,
   newlines, non-ASCII, an empty value, and — the nasty one — a value that is
   itself valid hexadecimal, which the obvious reading path (`-w`) decodes
   into binary garbage.
3. **Migration cannot lose a value.** The plaintext copy is deleted only after
   the Keychain has been read back and agrees. Every failure mode leaves it
   where it was, and the next run retries.
4. **Failure is loud.** With the Keychain unavailable, `mem vars set` refuses
   and writes nothing — it never quietly falls back to plaintext under a
   promise of encryption — and `mem vars list` says which backend every value
   is actually in.

The `security` binary is never executed here: `conftest.FakeKeychain` replaces
`mem.keychain._run`, so everything above the process boundary (the command
line, the hex encoding, the output parsing) runs for real. The one test that
does drive the real binary is marked ``keychain_live``, is deselected by
default, and points `security` at a throwaway keychain file it creates and
deletes itself — run it with ``pytest -m keychain_live``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from mem import keychain, storage
from mem.cli import cli
from mem.models import Group, GroupCommand, GroupFile, StoredVariable, VarsFile

from conftest import FakeKeychain

# Captured at import time, before the autouse fake replaces it, so the two
# tests that need the real wrapper (with `subprocess.run` itself stubbed) can
# put it back.
REAL_RUN = keychain._run

# Values chosen to break a naive implementation. Each one killed a plausible
# design during development.
NASTY_VALUES: dict[str, str] = {
    "plain": "hunter2",
    "spaces": "  lead and trail  ",
    "metacharacters": "a; rm -rf / && echo $(whoami) `id` |pipe| 'q' \"d\"",
    "backslashes": "C:\\path\\to\\thing\\",
    "quotes": "say \"hi\" and 'bye'",
    "newlines": "-----BEGIN KEY-----\nline2\nline3\n-----END KEY-----",
    "non_ascii": "contrasña-☃-Ω-🔐",
    "empty": "",
    # Reads back from `security -w` as a bare hex string, indistinguishable
    # from binary password data. This is why mem reads with -g.
    "looks_like_hex": "deadbeefcafebabe0123456789abcdef",
    "tab_and_control": "a\tb\x0bc",
}


def _write_vars_json(**values: str) -> None:
    """Seed vars.json the way a pre-Keychain mem left it: values in the file."""
    storage.write_vars_file(
        VarsFile(vars={name: StoredVariable(value=v) for name, v in values.items()})
    )


def _raw_vars_json() -> str:
    """The vars.json bytes as they are on disk."""
    return storage.VARS_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. The secret must never appear in argv
# ---------------------------------------------------------------------------


class TestSecretStaysOutOfArgv:
    """Contract: `ps` must never be able to show a stored value."""

    def test_write_passes_the_value_on_stdin(self, fake_keychain: FakeKeychain) -> None:
        secret = "sk-live-9Xq2TESTONLYnotarealkey"
        keychain.set_secret("API_TOKEN", secret)

        argv, stdin = fake_keychain.calls[-1]
        assert argv == ["/usr/bin/security", "-i"], argv
        assert (
            secret in stdin.decode("utf-8") or secret.encode().hex() in stdin.decode()
        )

    @pytest.mark.parametrize("value", sorted(set(NASTY_VALUES.values())))
    def test_no_call_ever_carries_the_value_in_argv(
        self, fake_keychain: FakeKeychain, value: str
    ) -> None:
        """Not the write, not the read-back, not the delete."""
        keychain.set_secret("API_TOKEN", value)
        keychain.get_secret("API_TOKEN")
        keychain.delete_secret("API_TOKEN")

        if not value:
            return  # the empty string is a substring of everything
        for argv, _stdin in fake_keychain.calls:
            joined = " ".join(argv)
            assert value not in joined, f"value leaked into argv: {argv}"
            assert value.encode("utf-8").hex() not in joined

    def test_cli_set_never_puts_the_value_in_argv(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        secret = "sk-live-9Xq2TESTONLYnotarealkey"
        result = CliRunner().invoke(
            cli, ["vars", "set", "API_TOKEN"], input=f"{secret}\n"
        )

        assert result.exit_code == 0, result.output
        for argv, _stdin in fake_keychain.calls:
            assert secret not in " ".join(argv)


# ---------------------------------------------------------------------------
# 2. Values survive the round trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Contract: what comes out is exactly what went in."""

    @pytest.mark.parametrize(("label", "value"), sorted(NASTY_VALUES.items()))
    def test_value_round_trips(self, label: str, value: str) -> None:
        keychain.set_secret("API_TOKEN", value)
        assert keychain.get_secret("API_TOKEN") == value

    @pytest.mark.parametrize(("label", "value"), sorted(NASTY_VALUES.items()))
    def test_value_round_trips_through_the_store(
        self, tmp_mem_dir: Path, label: str, value: str
    ) -> None:
        storage.set_var("API_TOKEN", value)
        assert storage.get_var_value("API_TOKEN") == value
        assert value not in _raw_vars_json() or value == ""

    def test_overwriting_replaces_the_value(self) -> None:
        keychain.set_secret("API_TOKEN", "first")
        keychain.set_secret("API_TOKEN", "second")
        assert keychain.get_secret("API_TOKEN") == "second"

    def test_missing_item_is_none_not_an_error(self) -> None:
        assert keychain.get_secret("NEVER_SET") is None

    def test_item_is_labelled_for_keychain_access(
        self, fake_keychain: FakeKeychain
    ) -> None:
        """The Keychain Access row has to say which variable it is."""
        keychain.set_secret("API_TOKEN", "x")
        _argv, stdin = fake_keychain.calls[-1]
        line = stdin.decode()
        assert f"-s {keychain.SERVICE}" in line
        assert "-a API_TOKEN" in line
        assert f"-l {keychain.SERVICE}:API_TOKEN" in line


# ---------------------------------------------------------------------------
# 3. Parsing `security find-generic-password -g`
# ---------------------------------------------------------------------------


# Recorded verbatim from the real /usr/bin/security on macOS 15, by storing
# each value and reading it back. The decoder is only correct if it agrees
# with these bytes, so they are pinned rather than generated.
RECORDED_OUTPUT: list[tuple[str, bytes, str]] = [
    ("printable", b'password: "inter secret"\n', "inter secret"),
    ("hex_looking", b'password: "deadbeef"\n', "deadbeef"),
    ("embedded_quote", b'password: "a"b"\n', 'a"b'),
    ("punctuation", b'password: "~%^&*()|;<>"\n', "~%^&*()|;<>"),
    ("outer_spaces", b'password: "  lead and trail  "\n', "  lead and trail  "),
    ("empty", b"password: \n", ""),
    ("backslash", b'password: 0x6122625C63  "a"b\\134c"\n', 'a"b\\c'),
    (
        "newline",
        b'password: 0x6C696E65310A6C696E6532  "line1\\012line2"\n',
        "line1\nline2",
    ),
    (
        "non_ascii",
        b'password: 0x636F6E74726173C3B16120E29883  "contras\\303\\261a \\342\\230\\203"\n',
        "contrasña ☃",
    ),
    (
        "trailing_newline",
        b'password: 0x747261696C696E670A  "trailing\\012"\n',
        "trailing\n",
    ),
]


class TestPasswordOutputParsing:
    """Contract: mem decodes `security -g` output exactly, or refuses to guess."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(raw, expected) for _id, raw, expected in RECORDED_OUTPUT],
        ids=[case[0] for case in RECORDED_OUTPUT],
    )
    def test_recorded_real_output_decodes(self, raw: bytes, expected: str) -> None:
        assert keychain.parse_password_output(raw) == expected

    def test_hex_looking_value_is_not_hex_decoded(self) -> None:
        """The bug that rules out reading with `-w`.

        A 32-character hex API key is printable, so `security -w` prints it
        raw — byte for byte identical to how it prints *binary* data. Anything
        that hex-decodes on sight turns the user's key into 16 bytes of
        garbage. The `-g` form never does, because binary data always carries
        the `0x` prefix.
        """
        assert (
            keychain.parse_password_output(b'password: "deadbeefcafebabe"\n')
            == "deadbeefcafebabe"
        )

    def test_malformed_hex_raises_instead_of_guessing(self) -> None:
        with pytest.raises(keychain.KeychainError):
            keychain.parse_password_output(b'password: 0xZZTOP  "??"\n')

    def test_unparseable_output_raises_instead_of_guessing(self) -> None:
        with pytest.raises(keychain.KeychainError):
            keychain.parse_password_output(b"password: not-quoted-and-not-hex\n")

    def test_absent_password_line_raises(self) -> None:
        with pytest.raises(keychain.KeychainError):
            keychain.parse_password_output(b"keychain: nothing to see\n")

    def test_non_utf8_item_is_rejected(self) -> None:
        with pytest.raises(keychain.KeychainError):
            keychain.parse_password_output(b'password: 0xFFFE  "\\377\\376"\n')


# ---------------------------------------------------------------------------
# 4. Guards on what mem is willing to hand to `security`
# ---------------------------------------------------------------------------


class TestCommandLineGuards:
    """Contract: nothing user-controlled can restructure the command."""

    @pytest.mark.parametrize(
        "name",
        [
            "API TOKEN",
            "API\ndelete-generic-password -s mem-cli-vars -a OTHER",
            "API;OTHER",
            'API" -a OTHER "',
            "API$(whoami)",
            "",
        ],
    )
    def test_a_hostile_name_is_refused(
        self, fake_keychain: FakeKeychain, name: str
    ) -> None:
        """`security -i` parses a command line, so a name is a code path.

        A newline in an account name would be a second command for mem's own
        subprocess to run — the injection this module exists to avoid.
        """
        with pytest.raises(keychain.KeychainError):
            keychain.set_secret(name, "x")
        assert fake_keychain.calls == []

    def test_value_too_long_is_refused_not_truncated(
        self, fake_keychain: FakeKeychain
    ) -> None:
        """Real `security -i` truncates at 4096 chars and runs the head.

        Observed on the real binary: an 8 KB value produced a Keychain item
        containing a silently truncated secret. Refusing is the only safe
        answer available.
        """
        with pytest.raises(keychain.KeychainValueTooLong):
            keychain.set_secret("BIG", "A" * 4000)
        assert fake_keychain.calls == []

    def test_a_value_just_under_the_limit_still_works(self) -> None:
        value = "A" * 1800
        keychain.set_secret("BIG", value)
        assert keychain.get_secret("BIG") == value

    def test_a_named_keychain_is_used_for_every_operation(
        self, monkeypatch: pytest.MonkeyPatch, fake_keychain: FakeKeychain
    ) -> None:
        """MEM_KEYCHAIN must reach the write, the read and the delete alike.

        Missing it on one of the three would send that operation to the
        *default* keychain — which is how a probe of `security` put a
        truncated secret in a real login keychain during development.
        """
        monkeypatch.setenv(keychain.KEYCHAIN_ENV, "/tmp/mem-test.keychain")
        keychain.set_secret("API_TOKEN", "x")
        keychain.get_secret("API_TOKEN")
        keychain.delete_secret("API_TOKEN")

        for argv, stdin in fake_keychain.calls:
            assert "/tmp/mem-test.keychain" in " ".join(argv) + stdin.decode()

    def test_hostile_keychain_path_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, fake_keychain: FakeKeychain
    ) -> None:
        monkeypatch.setenv(keychain.KEYCHAIN_ENV, "/tmp/x; rm -rf /")
        with pytest.raises(keychain.KeychainError):
            keychain.set_secret("API_TOKEN", "x")


# ---------------------------------------------------------------------------
# 5. Availability
# ---------------------------------------------------------------------------


class TestAvailability:
    """Contract: mem knows when it cannot use the Keychain, and says so."""

    def test_available_on_macos_with_the_binary(self) -> None:
        if sys.platform == "darwin" and os.access(keychain.SECURITY_BIN, os.X_OK):
            assert keychain.is_available()
            assert keychain.unavailable_reason() is None

    def test_unavailable_off_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(keychain.sys, "platform", "linux")
        assert not keychain.is_available()
        assert "linux" in (keychain.unavailable_reason() or "")

    def test_unavailable_without_the_binary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(keychain.sys, "platform", "darwin")
        monkeypatch.setattr(keychain.os, "access", lambda *a, **kw: False)
        assert not keychain.is_available()
        assert keychain.SECURITY_BIN in (keychain.unavailable_reason() or "")

    def test_a_locked_keychain_raises_with_the_os_message(
        self, fake_keychain: FakeKeychain
    ) -> None:
        """`security`'s own wording explains more than mem could."""
        fake_keychain.failure = "User interaction is not allowed."
        with pytest.raises(keychain.KeychainUnavailable) as excinfo:
            keychain.set_secret("API_TOKEN", "x")
        assert "User interaction is not allowed." in str(excinfo.value)

    def test_a_missing_binary_at_call_time_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise FileNotFoundError(keychain.SECURITY_BIN)

        monkeypatch.setattr(keychain, "_run", REAL_RUN)
        monkeypatch.setattr(keychain.subprocess, "run", boom)
        with pytest.raises(keychain.KeychainUnavailable):
            keychain._run([keychain.SECURITY_BIN, "-i"])

    def test_an_unrunnable_security_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Out of file descriptors, no fork available — still not a crash."""

        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError(24, "Too many open files")

        monkeypatch.setattr(keychain, "_run", REAL_RUN)
        monkeypatch.setattr(keychain.subprocess, "run", boom)
        with pytest.raises(keychain.KeychainUnavailable):
            keychain._run([keychain.SECURITY_BIN, "-i"])

    def test_a_hung_security_is_unavailable_not_a_hang(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An authorization dialog nobody answers must not wedge the shell."""

        def timeout(*_args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="security", timeout=1)

        monkeypatch.setattr(keychain, "_run", REAL_RUN)
        monkeypatch.setattr(keychain.subprocess, "run", timeout)
        with pytest.raises(keychain.KeychainUnavailable):
            keychain._run([keychain.SECURITY_BIN, "-i"])


# ---------------------------------------------------------------------------
# 6. Migration off the plaintext file
# ---------------------------------------------------------------------------


class TestMigration:
    """Contract: values move into the Keychain, and none is ever lost."""

    def test_plaintext_values_move_into_the_keychain(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        _write_vars_json(API_TOKEN="sk-live-TESTONLY", DB_HOST="staging.db")

        result = storage.migrate_vars_to_keychain()

        assert result.migrated == 2
        assert result.failed == []
        assert fake_keychain.secret("API_TOKEN") == "sk-live-TESTONLY"
        assert fake_keychain.secret("DB_HOST") == "staging.db"

    def test_the_file_no_longer_holds_the_secret(self, tmp_mem_dir: Path) -> None:
        _write_vars_json(API_TOKEN="sk-live-TESTONLY")
        storage.migrate_vars_to_keychain()

        raw = _raw_vars_json()
        assert "sk-live-TESTONLY" not in raw
        assert "API_TOKEN" in raw, "the name must survive; only the value moves"
        assert json.loads(raw)["vars"]["API_TOKEN"]["backend"] == "keychain"

    def test_last_used_survives_the_move(self, tmp_mem_dir: Path) -> None:
        storage.write_vars_file(
            VarsFile(
                vars={"API_TOKEN": StoredVariable(value="x", last_used=1700000000)}
            )
        )
        storage.migrate_vars_to_keychain()
        assert storage.read_vars_file().vars["API_TOKEN"].last_used == 1700000000

    def test_migration_is_idempotent(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        _write_vars_json(API_TOKEN="sk-live-TESTONLY")
        assert storage.migrate_vars_to_keychain().migrated == 1

        before = _raw_vars_json()
        calls = len(fake_keychain.calls)

        second = storage.migrate_vars_to_keychain()

        assert second == storage.MigrationResult(0, [], None)
        assert _raw_vars_json() == before
        assert len(fake_keychain.calls) == calls, (
            "a clean store must cost no subprocess"
        )
        assert storage.get_var_value("API_TOKEN") == "sk-live-TESTONLY"

    def test_a_failed_write_leaves_the_plaintext_value_alone(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        """The Keychain is locked halfway through the upgrade."""
        _write_vars_json(API_TOKEN="sk-live-TESTONLY")
        fake_keychain.failure = (
            "The user name or passphrase you entered is not correct."
        )

        result = storage.migrate_vars_to_keychain()

        assert result.migrated == 0
        assert result.failed == ["API_TOKEN"]
        assert "not correct" in (result.reason or "")
        assert storage.read_vars_file().vars["API_TOKEN"].value == "sk-live-TESTONLY"
        assert storage.get_var_value("API_TOKEN") == "sk-live-TESTONLY"

    def test_a_write_that_cannot_be_read_back_is_not_trusted(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain, monkeypatch
    ) -> None:
        """Confirmation, not optimism, is what permits the delete.

        Here `security` accepts the write and reports success, but the item
        cannot be read back. If mem deleted the plaintext copy on the strength
        of the exit code alone, the value would now exist nowhere.
        """
        _write_vars_json(API_TOKEN="sk-live-TESTONLY")
        monkeypatch.setattr(keychain, "get_secret", lambda _name: None)

        result = storage.migrate_vars_to_keychain()

        assert result.failed == ["API_TOKEN"]
        assert storage.read_vars_file().vars["API_TOKEN"].value == "sk-live-TESTONLY"

    def test_migration_is_reported_when_it_happens(self, tmp_mem_dir: Path) -> None:
        _write_vars_json(API_TOKEN="sk-live-TESTONLY")
        result = CliRunner().invoke(cli, ["vars", "list"])

        assert result.exit_code == 0, result.output
        assert "into the macOS Keychain" in result.output
        assert "sk-live-TESTONLY" not in result.output

    def test_a_keychainless_platform_reports_instead_of_migrating(
        self, tmp_mem_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_vars_json(API_TOKEN="sk-live-TESTONLY")
        monkeypatch.setattr(keychain.sys, "platform", "linux")

        result = storage.migrate_vars_to_keychain()

        assert result.migrated == 0
        assert result.failed == ["API_TOKEN"]
        assert "linux" in (result.reason or "")
        assert storage.get_var_value("API_TOKEN") == "sk-live-TESTONLY"


# ---------------------------------------------------------------------------
# 7. Degradation is visible, and never silently plaintext
# ---------------------------------------------------------------------------


class TestDegradation:
    """Contract: mem fails loudly rather than writing a secret in cleartext."""

    def test_set_fails_when_the_keychain_is_unavailable(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        fake_keychain.failure = "User interaction is not allowed."

        result = CliRunner().invoke(
            cli, ["vars", "set", "API_TOKEN", "sk-live-TESTONLY"]
        )

        assert result.exit_code != 0
        assert "User interaction is not allowed." in result.output
        assert "does not fall back" in result.output

    def test_a_refused_set_writes_nothing_anywhere(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        fake_keychain.failure = "User interaction is not allowed."

        CliRunner().invoke(cli, ["vars", "set", "API_TOKEN", "sk-live-TESTONLY"])

        assert storage.read_vars_file().vars == {}
        if storage.VARS_FILE.exists():
            assert "sk-live-TESTONLY" not in _raw_vars_json()

    def test_list_names_the_backend_of_every_value(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        """A half-migrated store must not be described by one header line."""
        storage.set_var("SAFE_ONE", "in-the-keychain")
        data = storage.read_vars_file()
        data.vars["LEGACY_ONE"] = StoredVariable(value="still-plaintext")
        storage.write_vars_file(data)
        fake_keychain.failure = "User interaction is not allowed."

        result = CliRunner().invoke(cli, ["vars", "list"])

        assert result.exit_code == 0, result.output
        assert "SAFE_ONE" in result.output
        assert "keychain" in result.output
        assert "LEGACY_ONE" in result.output
        assert "plaintext" in result.output
        assert "still-plaintext" not in result.output
        assert "in-the-keychain" not in result.output

    def test_list_json_reports_the_backend(self, tmp_mem_dir: Path) -> None:
        storage.set_var("API_TOKEN", "sk-live-TESTONLY")
        result = CliRunner().invoke(cli, ["vars", "list", "--json"])

        payload = json.loads(result.output)
        assert payload["variables"] == [
            {"name": "API_TOKEN", "backend": "keychain", "last_used": 0}
        ]
        assert payload["keychain"]["service"] == keychain.SERVICE
        assert "sk-live-TESTONLY" not in result.output

    def test_inline_value_warns_that_it_was_visible(self, tmp_mem_dir: Path) -> None:
        """mem's own argv is the last place a secret can still leak."""
        result = CliRunner().invoke(cli, ["vars", "set", "API_TOKEN", "hunter2"])

        assert result.exit_code == 0, result.output
        assert "visible to `ps`" in result.output

    def test_the_prompt_hides_what_is_typed(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        """The README promises "hidden input (like sudo)"; assert the flag.

        Checking the captured output instead would prove nothing: CliRunner
        does not echo stdin either way, so the test would pass with the
        echoing prompt this replaced.
        """
        with patch("click.prompt", return_value="sk-live-TESTONLY") as prompt:
            result = CliRunner().invoke(cli, ["vars", "set", "API_TOKEN"])

        assert result.exit_code == 0, result.output
        assert prompt.call_args.kwargs["hide_input"] is True
        assert fake_keychain.secret("API_TOKEN") == "sk-live-TESTONLY"
        assert "sk-live-TESTONLY" not in result.output

    def test_an_unreadable_value_does_not_become_the_string_none(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        """A variable mem cannot read is unresolved, not resolved to garbage."""
        storage.set_var("API_TOKEN", "sk-live-TESTONLY")
        fake_keychain.failure = "User interaction is not allowed."

        entries, unreadable = storage.load_var_values(["API_TOKEN"])

        assert entries == {}
        assert unreadable == ["API_TOKEN"]

    def test_run_says_which_variable_it_could_not_read(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        storage.set_var("API_TOKEN", "sk-live-TESTONLY")
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "test": Group(
                        commands=[
                            GroupCommand(
                                cmd="true $API_TOKEN",
                                vars=[{"name": "API_TOKEN", "default": "fallback"}],
                            )
                        ]
                    )
                }
            ),
        )
        fake_keychain.failure = "User interaction is not allowed."

        result = CliRunner().invoke(cli, ["run", "test", "--global", "--yes"])

        assert "Could not read from the Keychain" in result.output
        assert "API_TOKEN" in result.output


# ---------------------------------------------------------------------------
# 8. Removal really removes
# ---------------------------------------------------------------------------


class TestRemoval:
    """Contract: after `mem vars remove`, the secret is gone from both halves."""

    def test_remove_deletes_the_keychain_item(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        storage.set_var("API_TOKEN", "sk-live-TESTONLY")

        result = CliRunner().invoke(cli, ["vars", "remove", "API_TOKEN"])

        assert result.exit_code == 0, result.output
        assert fake_keychain.items == {}
        assert storage.read_vars_file().vars == {}
        assert keychain.get_secret("API_TOKEN") is None

    def test_remove_reports_an_unknown_name(self, tmp_mem_dir: Path) -> None:
        result = CliRunner().invoke(cli, ["vars", "remove", "NOPE"])
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_remove_cleans_up_an_orphaned_keychain_item(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        """The index lost the name but the Keychain still holds the secret."""
        keychain.set_secret("API_TOKEN", "sk-live-TESTONLY")

        assert storage.remove_var("API_TOKEN") is False
        assert fake_keychain.items == {}

    def test_a_failed_delete_keeps_the_name_visible(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        """A name the user can retry beats a secret nothing points at."""
        storage.set_var("API_TOKEN", "sk-live-TESTONLY")
        fake_keychain.failure = "User interaction is not allowed."

        result = CliRunner().invoke(cli, ["vars", "remove", "API_TOKEN"])

        assert result.exit_code != 0
        assert "API_TOKEN" in storage.read_vars_file().vars

    def test_clear_empties_both_backends(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        storage.set_var("API_TOKEN", "one")
        storage.set_var("DB_HOST", "two")

        result = CliRunner().invoke(cli, ["vars", "clear", "--yes"])

        assert result.exit_code == 0, result.output
        assert fake_keychain.items == {}
        assert storage.read_vars_file().vars == {}

    def test_clear_keeps_what_it_could_not_delete(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        storage.set_var("API_TOKEN", "one")
        fake_keychain.failure = "User interaction is not allowed."

        result = CliRunner().invoke(cli, ["vars", "clear", "--yes"])

        assert result.exit_code != 0
        assert "API_TOKEN" in storage.read_vars_file().vars
        assert fake_keychain.secret("API_TOKEN") == "one"

    def test_forget_removes_a_keychain_backed_value(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        """`mem forget` promises no traces anywhere — the Keychain included."""
        storage.set_var("API_TOKEN", "sk-live-TESTONLY")

        storage.forget_commands("sk-live-TESTONLY")

        assert fake_keychain.items == {}
        assert storage.read_vars_file().vars == {}

    def test_forget_keeps_an_unrelated_value(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain
    ) -> None:
        storage.set_var("API_TOKEN", "sk-live-TESTONLY")

        storage.forget_commands("something-else")

        assert fake_keychain.secret("API_TOKEN") == "sk-live-TESTONLY"

    def test_forget_keeps_a_value_it_could_not_check(
        self, tmp_mem_dir: Path, fake_keychain: FakeKeychain, capsys
    ) -> None:
        """Deleting on a guess would throw away a credential."""
        storage.set_var("API_TOKEN", "sk-live-TESTONLY")
        fake_keychain.failure = "User interaction is not allowed."

        storage.forget_commands("sk-live-TESTONLY")

        assert "API_TOKEN" in storage.read_vars_file().vars
        assert "could not check" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 9. The real binary, deselected by default
# ---------------------------------------------------------------------------


@pytest.mark.keychain_live
class TestAgainstTheRealSecurityBinary:
    """End-to-end against `/usr/bin/security`, on a keychain we throw away.

    Deselected by default (`-m 'not keychain_live'`), because the default
    suite must never touch the developer's login keychain. Run deliberately
    with ``pytest -m keychain_live``. Everything here goes to a keychain file
    created in ``tmp_path`` and deleted afterwards; ``MEM_KEYCHAIN`` is what
    keeps `security` pointed at it.
    """

    @pytest.fixture()
    def throwaway_keychain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        if sys.platform != "darwin":
            pytest.skip("no macOS Keychain on this platform")
        path = tmp_path / "mem-test.keychain"
        subprocess.run(
            ["security", "create-keychain", "-p", "test-passphrase", str(path)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["security", "unlock-keychain", "-p", "test-passphrase", str(path)],
            check=True,
            capture_output=True,
        )
        monkeypatch.setenv(keychain.KEYCHAIN_ENV, str(path))
        yield path
        subprocess.run(
            ["security", "delete-keychain", str(path)], check=False, capture_output=True
        )

    @pytest.mark.parametrize(("label", "value"), sorted(NASTY_VALUES.items()))
    def test_real_round_trip(
        self, throwaway_keychain: Path, label: str, value: str
    ) -> None:
        keychain.set_secret("MEM_TEST_VAR", value)
        assert keychain.get_secret("MEM_TEST_VAR") == value
        assert keychain.delete_secret("MEM_TEST_VAR") is True
        assert keychain.get_secret("MEM_TEST_VAR") is None

    def test_real_migration(self, throwaway_keychain: Path, tmp_mem_dir: Path) -> None:
        _write_vars_json(MEM_TEST_VAR="sk-live-TESTONLY")

        assert storage.migrate_vars_to_keychain().migrated == 1
        assert "sk-live-TESTONLY" not in _raw_vars_json()
        assert storage.get_var_value("MEM_TEST_VAR") == "sk-live-TESTONLY"

        storage.remove_var("MEM_TEST_VAR")

    def test_real_secret_is_absent_from_the_process_table(
        self, throwaway_keychain: Path
    ) -> None:
        """The write is a real fork/exec; nothing about it may show in `ps`."""
        value = "sk-live-9Xq2TESTONLYnotarealkey"
        keychain.set_secret("MEM_TEST_VAR", value)
        listing = subprocess.run(
            ["ps", "-Ao", "args"], capture_output=True, text=True, check=True
        ).stdout
        assert value not in listing
        keychain.delete_secret("MEM_TEST_VAR")
