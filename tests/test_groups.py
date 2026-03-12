"""Tests for Named Groups (active memory) feature.

Covers models, storage, business logic in groups.py, and CLI commands.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from mem import groups, storage
from mem.cli import cli
from mem.models import Group, GroupCommand, GroupFile, SavedCommand


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestSavedCommand:
    def test_basic(self):
        s = SavedCommand(cmd="git status")
        assert s.cmd == "git status"
        assert s.comment is None

    def test_with_comment(self):
        s = SavedCommand(cmd="git log", comment="recent history")
        assert s.comment == "recent history"

    def test_empty_cmd_rejected(self):
        with pytest.raises(ValidationError):
            SavedCommand(cmd="")

    def test_roundtrip_json(self):
        s = SavedCommand(cmd="echo hello", comment="test")
        data = s.model_dump_json()
        s2 = SavedCommand.model_validate_json(data)
        assert s == s2


class TestGroupCommand:
    def test_basic(self):
        c = GroupCommand(cmd="kubectl get pods")
        assert c.cmd == "kubectl get pods"
        assert c.comment is None

    def test_with_comment(self):
        c = GroupCommand(cmd="curl localhost", comment="health check")
        assert c.comment == "health check"

    def test_empty_cmd_rejected(self):
        with pytest.raises(ValidationError):
            GroupCommand(cmd="")


class TestGroup:
    def test_empty_group(self):
        g = Group()
        assert g.description is None
        assert g.commands == []

    def test_with_description_and_commands(self):
        g = Group(
            description="debug runbook",
            commands=[GroupCommand(cmd="echo 1"), GroupCommand(cmd="echo 2")],
        )
        assert g.description == "debug runbook"
        assert len(g.commands) == 2


class TestGroupFile:
    def test_empty(self):
        gf = GroupFile()
        assert gf.saved == []
        assert gf.groups == {}

    def test_with_data(self):
        gf = GroupFile(
            saved=[SavedCommand(cmd="ls")],
            groups={"deploy": Group(commands=[GroupCommand(cmd="echo deploy")])},
        )
        assert len(gf.saved) == 1
        assert "deploy" in gf.groups

    def test_roundtrip_json(self):
        gf = GroupFile(
            saved=[SavedCommand(cmd="git push", comment="push to remote")],
            groups={
                "test": Group(
                    description="testing",
                    commands=[GroupCommand(cmd="pytest", comment="run tests")],
                )
            },
        )
        data = gf.model_dump_json(indent=2)
        gf2 = GroupFile.model_validate_json(data)
        assert gf == gf2


# ---------------------------------------------------------------------------
# Storage tests
# ---------------------------------------------------------------------------


class TestGroupFilePath:
    def test_global_scope(self):
        path = storage.group_file_path(None)
        assert path == storage.GROUPS_GLOBAL_FILE

    def test_repo_scope(self):
        path = storage.group_file_path("my-repo")
        assert path == storage.GROUPS_REPOS_DIR / "my-repo.json"


class TestReadWriteGroupFile:
    def test_read_missing_returns_empty(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "nonexistent.json"
        result = storage.read_group_file(path)
        assert result == GroupFile()

    def test_write_and_read(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        gf = GroupFile(
            saved=[SavedCommand(cmd="echo hello")],
            groups={"g": Group(commands=[GroupCommand(cmd="echo g")])},
        )
        storage.write_group_file(path, gf)
        result = storage.read_group_file(path)
        assert result == gf

    def test_atomic_write_creates_parents(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "repos" / "deep" / "nested.json"
        gf = GroupFile()
        storage.write_group_file(path, gf)
        assert path.exists()

    def test_malformed_json_raises(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "bad.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NOT JSON", encoding="utf-8")
        with pytest.raises(ValueError, match="Malformed"):
            storage.read_group_file(path)


# ---------------------------------------------------------------------------
# Group name validation
# ---------------------------------------------------------------------------


class TestValidateGroupName:
    @pytest.mark.parametrize("name", ["deploy", "my-group", "a1", "test-123-abc"])
    def test_valid_names(self, name: str):
        groups.validate_group_name(name)  # should not raise

    @pytest.mark.parametrize(
        "name",
        [
            "Deploy",  # uppercase
            "my group",  # spaces
            "123abc",  # starts with number
            "-leading",  # starts with hyphen
            "special!",  # special chars
            "",  # empty
            "ALLCAPS",  # all caps
        ],
    )
    def test_invalid_names(self, name: str):
        with pytest.raises(click.BadParameter):
            groups.validate_group_name(name)


# ---------------------------------------------------------------------------
# Shadow detection
# ---------------------------------------------------------------------------


class TestDetectShadows:
    def test_no_shadows(self):
        repo = GroupFile(groups={"a": Group()})
        glob = GroupFile(groups={"b": Group()})
        assert groups.detect_shadows(repo, glob) == set()

    def test_single_shadow(self):
        repo = GroupFile(groups={"deploy": Group()})
        glob = GroupFile(groups={"deploy": Group(), "other": Group()})
        assert groups.detect_shadows(repo, glob) == {"deploy"}

    def test_multiple_shadows(self):
        repo = GroupFile(groups={"a": Group(), "b": Group()})
        glob = GroupFile(groups={"b": Group(), "a": Group(), "c": Group()})
        assert groups.detect_shadows(repo, glob) == {"a", "b"}

    def test_empty_scopes(self):
        assert groups.detect_shadows(GroupFile(), GroupFile()) == set()


# ---------------------------------------------------------------------------
# save_command
# ---------------------------------------------------------------------------


class TestSaveCommand:
    def test_save_to_saved_list(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        saved, _vars = groups.save_command(path, "git status")
        assert saved is True
        data = storage.read_group_file(path)
        assert len(data.saved) == 1
        assert data.saved[0].cmd == "git status"

    def test_save_with_comment(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        groups.save_command(path, "echo hi", comment="greeting")
        data = storage.read_group_file(path)
        assert data.saved[0].comment == "greeting"

    def test_duplicate_saved_returns_false(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        groups.save_command(path, "echo x")
        saved, _vars = groups.save_command(path, "echo x")
        assert saved is False
        data = storage.read_group_file(path)
        assert len(data.saved) == 1

    def test_save_to_group(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        saved, _vars = groups.save_command(path, "echo deploy", group_name="deploy")
        assert saved is True
        data = storage.read_group_file(path)
        assert "deploy" in data.groups
        assert len(data.groups["deploy"].commands) == 1
        assert data.groups["deploy"].commands[0].cmd == "echo deploy"

    def test_save_creates_group_with_description_callback(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        groups.save_command(
            path,
            "echo 1",
            group_name="my-group",
            description_callback=lambda n: f"Description for {n}",
        )
        data = storage.read_group_file(path)
        assert data.groups["my-group"].description == "Description for my-group"

    def test_save_to_existing_group_appends(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        groups.save_command(path, "echo 1", group_name="g")
        groups.save_command(path, "echo 2", group_name="g")
        data = storage.read_group_file(path)
        assert len(data.groups["g"].commands) == 2

    def test_duplicate_in_group_returns_false(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        groups.save_command(path, "echo same", group_name="g")
        saved, _vars = groups.save_command(path, "echo same", group_name="g")
        assert saved is False

    def test_invalid_group_name_raises(self, tmp_mem_dir: Path):
        path = tmp_mem_dir / "groups" / "test.json"
        with pytest.raises(click.BadParameter):
            groups.save_command(path, "echo x", group_name="BAD NAME")


# ---------------------------------------------------------------------------
# get_last_captured_command
# ---------------------------------------------------------------------------


class TestGetLastCapturedCommand:
    def test_reads_last_command(self, tmp_mem_dir: Path):
        repo_name = "test-repo"
        path = storage.repo_file(repo_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        from conftest import make_command

        cmd1 = make_command(command="first", repo="/test")
        cmd2 = make_command(command="second", repo="/test")
        with path.open("w", encoding="utf-8") as f:
            f.write(cmd1.to_jsonl() + "\n")
            f.write(cmd2.to_jsonl() + "\n")

        result = groups.get_last_captured_command("/test-repo")
        assert result == "second"

    def test_no_history_raises(self, tmp_mem_dir: Path):
        with pytest.raises(click.ClickException, match="No captured history"):
            groups.get_last_captured_command("/nonexistent")


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


class TestListAll:
    def test_global_only(self, tmp_mem_dir: Path):
        global_path = storage.GROUPS_GLOBAL_FILE
        storage.write_group_file(
            global_path,
            GroupFile(groups={"g": Group(commands=[GroupCommand(cmd="echo g")])}),
        )
        result = groups.list_all(None, global_path)
        assert result["repo_data"] is None
        assert "g" in result["global_data"].groups
        assert result["shadows"] == set()

    def test_both_scopes_with_shadows(self, tmp_mem_dir: Path):
        global_path = storage.GROUPS_GLOBAL_FILE
        repo_path = tmp_mem_dir / "groups" / "repos" / "myrepo.json"

        storage.write_group_file(
            global_path,
            GroupFile(groups={"shared": Group(), "global-only": Group()}),
        )
        storage.write_group_file(
            repo_path,
            GroupFile(groups={"shared": Group(), "repo-only": Group()}),
        )

        result = groups.list_all(repo_path, global_path)
        assert result["repo_data"] is not None
        assert "shared" in result["shadows"]
        assert result["repo_name"] == "myrepo"


# ---------------------------------------------------------------------------
# resolve_group
# ---------------------------------------------------------------------------


class TestResolveGroup:
    def _setup_scopes(self, tmp_mem_dir: Path):
        global_path = storage.GROUPS_GLOBAL_FILE
        repo_path = tmp_mem_dir / "groups" / "repos" / "testrepo.json"

        storage.write_group_file(
            repo_path,
            GroupFile(
                groups={
                    "deploy": Group(
                        description="repo deploy",
                        commands=[GroupCommand(cmd="echo repo")],
                    ),
                }
            ),
        )
        storage.write_group_file(
            global_path,
            GroupFile(
                groups={
                    "deploy": Group(
                        description="global deploy",
                        commands=[GroupCommand(cmd="echo global")],
                    ),
                    "ssh": Group(commands=[GroupCommand(cmd="ssh user@host")]),
                }
            ),
        )
        return repo_path, global_path

    def test_finds_in_repo_first(self, tmp_mem_dir: Path):
        repo_path, global_path = self._setup_scopes(tmp_mem_dir)
        grp, scope, _, shadows = groups.resolve_group("deploy", repo_path, global_path)
        assert grp.description == "repo deploy"
        assert scope == "testrepo"
        assert "deploy" in shadows

    def test_falls_back_to_global(self, tmp_mem_dir: Path):
        repo_path, global_path = self._setup_scopes(tmp_mem_dir)
        grp, scope, _, _ = groups.resolve_group("ssh", repo_path, global_path)
        assert grp.commands[0].cmd == "ssh user@host"
        assert scope == "global"

    def test_force_global(self, tmp_mem_dir: Path):
        repo_path, global_path = self._setup_scopes(tmp_mem_dir)
        grp, scope, _, _ = groups.resolve_group(
            "deploy", repo_path, global_path, force_global=True
        )
        assert grp.description == "global deploy"
        assert scope == "global"

    def test_not_found_raises(self, tmp_mem_dir: Path):
        repo_path, global_path = self._setup_scopes(tmp_mem_dir)
        with pytest.raises(click.ClickException, match="not found"):
            groups.resolve_group("nonexistent", repo_path, global_path)

    def test_force_global_not_found_raises(self, tmp_mem_dir: Path):
        _, global_path = self._setup_scopes(tmp_mem_dir)
        with pytest.raises(click.ClickException, match="not found in global"):
            groups.resolve_group("nonexistent", None, global_path, force_global=True)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExportMarkdown:
    def test_with_description(self):
        grp = Group(
            description="Debug commands",
            commands=[
                GroupCommand(cmd="curl localhost", comment="health check"),
                GroupCommand(cmd="tail -f log"),
            ],
        )
        md = groups.export_markdown("debug", grp)
        assert "## debug" in md
        assert "> Debug commands" in md
        assert "| `curl localhost` | health check |" in md
        assert "| `tail -f log` |  |" in md

    def test_without_description(self):
        grp = Group(commands=[GroupCommand(cmd="echo hi", comment="hello")])
        md = groups.export_markdown("test", grp)
        assert "## test" in md
        assert ">" not in md


class TestExportJson:
    def test_output_is_valid_json(self):
        grp = Group(
            description="deploy",
            commands=[GroupCommand(cmd="make deploy", comment="run deploy")],
        )
        output = groups.export_json("my-deploy", grp)
        data = json.loads(output)
        assert "my-deploy" in data
        assert data["my-deploy"]["description"] == "deploy"
        assert len(data["my-deploy"]["commands"]) == 1


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class TestImportFromJson:
    def test_export_format(self, tmp_path: Path):
        content = json.dumps(
            {
                "test": {
                    "description": "test group",
                    "commands": [
                        {"cmd": "echo 1", "comment": "first"},
                        {"cmd": "echo 2"},
                    ],
                }
            }
        )
        f = tmp_path / "input.json"
        f.write_text(content, encoding="utf-8")
        cmds = groups.import_from_json(f)
        assert len(cmds) == 2
        assert cmds[0].cmd == "echo 1"
        assert cmds[0].comment == "first"
        assert cmds[1].comment is None

    def test_flat_format(self, tmp_path: Path):
        content = json.dumps(
            {
                "commands": [{"cmd": "ls"}, {"cmd": "pwd"}],
            }
        )
        f = tmp_path / "flat.json"
        f.write_text(content, encoding="utf-8")
        cmds = groups.import_from_json(f)
        assert len(cmds) == 2

    def test_invalid_json_raises(self, tmp_path: Path):
        f = tmp_path / "bad.json"
        f.write_text("NOT JSON", encoding="utf-8")
        with pytest.raises(click.ClickException, match="Invalid JSON"):
            groups.import_from_json(f)

    def test_wrong_structure_raises(self, tmp_path: Path):
        f = tmp_path / "arr.json"
        f.write_text("[1,2,3]", encoding="utf-8")
        with pytest.raises(click.ClickException, match="Expected a JSON object"):
            groups.import_from_json(f)

    def test_commands_not_a_list_raises(self, tmp_path: Path):
        f = tmp_path / "bad.json"
        f.write_text('{"commands": "not-a-list"}', encoding="utf-8")
        with pytest.raises(
            click.ClickException, match="Expected 'commands' to be a list"
        ):
            groups.import_from_json(f)

    def test_malformed_command_entry_raises(self, tmp_path: Path):
        content = json.dumps({"commands": [{"no_cmd_key": "x"}]})
        f = tmp_path / "bad_entry.json"
        f.write_text(content, encoding="utf-8")
        with pytest.raises(click.ClickException, match="Malformed command entry"):
            groups.import_from_json(f)

    def test_non_dict_command_entry_raises(self, tmp_path: Path):
        content = json.dumps({"commands": ["just a string"]})
        f = tmp_path / "bad_type.json"
        f.write_text(content, encoding="utf-8")
        with pytest.raises(click.ClickException, match="Malformed command entry"):
            groups.import_from_json(f)


class TestImportFromMarkdown:
    def test_valid_table(self, tmp_path: Path):
        md = (
            "## test\n"
            "> Description\n\n"
            "| Command | Description |\n"
            "|---|---|\n"
            "| `echo hello` | greeting |\n"
            "| `ls -la` | list files |\n"
        )
        f = tmp_path / "table.md"
        f.write_text(md, encoding="utf-8")
        cmds = groups.import_from_markdown(f)
        assert len(cmds) == 2
        assert cmds[0].cmd == "echo hello"
        assert cmds[0].comment == "greeting"

    def test_blank_lines_between_rows(self, tmp_path: Path):
        md = (
            "| Command | Description |\n"
            "|---|---|\n"
            "| `echo 1` | first |\n"
            "\n"
            "| `echo 2` | second |\n"
        )
        f = tmp_path / "blanks.md"
        f.write_text(md, encoding="utf-8")
        cmds = groups.import_from_markdown(f)
        assert len(cmds) == 2
        assert cmds[1].cmd == "echo 2"

    def test_no_table_raises(self, tmp_path: Path):
        f = tmp_path / "empty.md"
        f.write_text("# Just a heading\nSome text.", encoding="utf-8")
        with pytest.raises(click.ClickException, match="No commands found"):
            groups.import_from_markdown(f)


class TestImportExportRoundTrip:
    def test_json_roundtrip(self, tmp_path: Path):
        grp = Group(
            description="test",
            commands=[
                GroupCommand(cmd="echo 1", comment="first"),
                GroupCommand(cmd="echo 2", comment="second"),
            ],
        )
        json_str = groups.export_json("roundtrip", grp)
        f = tmp_path / "roundtrip.json"
        f.write_text(json_str, encoding="utf-8")
        cmds = groups.import_from_json(f)
        assert len(cmds) == 2
        assert cmds[0].cmd == "echo 1"
        assert cmds[1].comment == "second"

    def test_markdown_roundtrip(self, tmp_path: Path):
        grp = Group(
            description="test",
            commands=[
                GroupCommand(cmd="echo a", comment="alpha"),
                GroupCommand(cmd="echo b", comment="beta"),
            ],
        )
        md = groups.export_markdown("roundtrip", grp)
        f = tmp_path / "roundtrip.md"
        f.write_text(md, encoding="utf-8")
        cmds = groups.import_from_markdown(f)
        assert len(cmds) == 2
        assert cmds[0].cmd == "echo a"
        assert cmds[1].comment == "beta"


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


FAKE_REPO = "/Users/test/projects/myapp"


def _mock_repo(repo: str = FAKE_REPO):
    """Return a context manager that mocks _current_repo and get_git_repo."""
    return patch("mem.cli._current_repo", return_value=repo)


class TestSaveCLI:
    def test_save_to_saved_list_global(self, tmp_mem_dir: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["save", "echo hello", "--global"])
        assert result.exit_code == 0
        assert "Saved to saved commands" in result.output

        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert len(data.saved) == 1
        assert data.saved[0].cmd == "echo hello"

    def test_save_to_group_global(self, tmp_mem_dir: Path):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["save", "echo deploy", "--global", "-g", "deploy", "-c", "run deploy"],
            input="\n",  # empty description prompt
        )
        assert result.exit_code == 0
        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "deploy" in data.groups
        assert data.groups["deploy"].commands[0].comment == "run deploy"

    def test_save_duplicate_shows_message(self, tmp_mem_dir: Path):
        runner = CliRunner()
        runner.invoke(cli, ["save", "echo x", "--global"])
        result = runner.invoke(cli, ["save", "echo x", "--global"])
        assert result.exit_code == 0
        assert "Already saved" in result.output

    def test_save_outside_repo_falls_back_to_global(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with (
            patch("mem.cli._current_repo", return_value=None),
            patch("mem.groups.get_git_repo", return_value=None),
        ):
            result = runner.invoke(cli, ["save", "echo hello"])
        assert result.exit_code == 0
        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert any(s.cmd == "echo hello" for s in data.saved)

    def test_save_in_repo(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["save", "git status"])
        assert result.exit_code == 0
        assert "Saved to saved commands" in result.output


class TestListCLI:
    def test_empty_state(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "No saved commands" in result.output

    def test_list_shows_global_data(self, tmp_mem_dir: Path):
        # Pre-populate global data
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                saved=[SavedCommand(cmd="echo global")],
                groups={
                    "deploy": Group(
                        description="deploy steps",
                        commands=[GroupCommand(cmd="make deploy")],
                    )
                },
            ),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "--global"])
        assert result.exit_code == 0
        assert "echo global" in result.output
        assert "deploy" in result.output

    def test_list_json_output(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(saved=[SavedCommand(cmd="echo test")]),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "--global", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "global" in data
        assert len(data["global"]["saved"]) == 1

    def test_list_shows_shadow_indicator(self, tmp_mem_dir: Path):
        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_path = storage.group_file_path(repo_name)
        storage.write_group_file(
            repo_path,
            GroupFile(
                groups={"shared": Group(commands=[GroupCommand(cmd="repo cmd")])}
            ),
        )
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={"shared": Group(commands=[GroupCommand(cmd="global cmd")])}
            ),
        )

        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert "shadowed" in result.output

    def test_list_specific_group(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "deploy": Group(
                        description="deploy steps",
                        commands=[
                            GroupCommand(cmd="make build", comment="compile"),
                            GroupCommand(cmd="make push"),
                        ],
                    )
                }
            ),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "deploy", "--global"])
        assert result.exit_code == 0
        assert "deploy" in result.output
        assert "make build" in result.output
        assert "compile" in result.output
        assert "make push" in result.output

    def test_list_specific_group_not_found(self, tmp_mem_dir: Path):
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "nope", "--global"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_list_specific_group_json(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "g": Group(
                        commands=[GroupCommand(cmd="echo hi")],
                    )
                }
            ),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "g", "--global", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "g" in data


class TestRunCLI:
    def _setup_group(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "test": Group(
                        description="test group",
                        commands=[
                            GroupCommand(cmd="echo 1", comment="first"),
                            GroupCommand(cmd="echo 2", comment="second"),
                            GroupCommand(cmd="echo 3", comment="third"),
                        ],
                    ),
                }
            ),
        )

    def test_run_yes_executes_all(self, tmp_mem_dir: Path):
        self._setup_group(tmp_mem_dir)
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "test", "--global", "--yes"])
        assert result.exit_code == 0
        assert "test group" in result.output

    def test_run_not_found_errors(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["run", "nonexistent", "--yes"])
        assert result.exit_code != 0
        assert (
            "not found" in result.output.lower()
            or "not found" in str(result.exception).lower()
        )

    def test_run_pick_single(self, tmp_mem_dir: Path):
        self._setup_group(tmp_mem_dir)
        runner = CliRunner()
        # CliRunner can't simulate TTY (C-level stdin is immutable),
        # so mock the _is_interactive helper and use input= for prompts.
        with patch("mem.cli._is_interactive", return_value=True):
            result = runner.invoke(cli, ["run", "test", "--global"], input="2\ny\n")
        assert result.exit_code == 0
        assert "echo 2" in result.output

    def test_run_decline(self, tmp_mem_dir: Path):
        self._setup_group(tmp_mem_dir)
        runner = CliRunner()
        with patch("mem.cli._is_interactive", return_value=True):
            result = runner.invoke(cli, ["run", "test", "--global"], input="n\n")
        assert result.exit_code == 0

    def test_run_all_skips_per_command_confirm(self, tmp_mem_dir: Path):
        self._setup_group(tmp_mem_dir)
        runner = CliRunner()
        with patch("mem.cli._is_interactive", return_value=True):
            # "y\n" = run all — should NOT prompt for each command
            result = runner.invoke(cli, ["run", "test", "--global"], input="y\n")
        assert result.exit_code == 0
        # All 3 commands should appear as executed ($ echo N)
        assert "$ echo 1" in result.output
        assert "$ echo 2" in result.output
        assert "$ echo 3" in result.output
        # Should NOT contain per-command confirmation prompts
        assert "Run [1]" not in result.output

    def test_run_non_tty_without_yes_errors(self, tmp_mem_dir: Path):
        self._setup_group(tmp_mem_dir)
        runner = CliRunner()
        with patch("mem.cli._is_interactive", return_value=False):
            result = runner.invoke(cli, ["run", "test", "--global"])
        assert result.exit_code != 0
        assert "non-interactive" in result.output.lower()


class TestExportCLI:
    def _setup_group(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "deploy": Group(
                        description="deploy steps",
                        commands=[
                            GroupCommand(cmd="make build", comment="build first"),
                            GroupCommand(cmd="make deploy", comment="then deploy"),
                        ],
                    ),
                }
            ),
        )

    def test_export_markdown(self, tmp_mem_dir: Path):
        self._setup_group(tmp_mem_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["export", "deploy", "--global", "-f", "markdown", "--stdout"]
        )
        assert result.exit_code == 0
        assert "## deploy" in result.output
        assert "| `make build` | build first |" in result.output

    def test_export_json(self, tmp_mem_dir: Path):
        self._setup_group(tmp_mem_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli, ["export", "deploy", "--global", "-f", "json", "--stdout"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "deploy" in data

    def test_export_not_found(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["export", "nonexistent"])
        assert result.exit_code != 0


class TestImportCLI:
    def test_import_json(self, tmp_mem_dir: Path, tmp_path: Path):
        content = json.dumps(
            {
                "commands": [
                    {"cmd": "echo 1", "comment": "first"},
                    {"cmd": "echo 2"},
                ]
            }
        )
        import_file = tmp_path / "import.json"
        import_file.write_text(content, encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["import", str(import_file), "-g", "imported", "--global"],
        )
        assert result.exit_code == 0
        assert "Imported 2 commands" in result.output

        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "imported" in data.groups
        assert len(data.groups["imported"].commands) == 2

    def test_import_merge(self, tmp_mem_dir: Path, tmp_path: Path):
        # Pre-populate existing group
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "existing": Group(commands=[GroupCommand(cmd="echo old")]),
                }
            ),
        )

        content = json.dumps(
            {
                "commands": [
                    {"cmd": "echo old"},  # duplicate
                    {"cmd": "echo new"},  # new
                ]
            }
        )
        import_file = tmp_path / "merge.json"
        import_file.write_text(content, encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["import", str(import_file), "-g", "existing", "--global"],
            input="m\n",  # merge
        )
        assert result.exit_code == 0
        assert "Imported 1 commands" in result.output  # only 1 new, not 2

        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        # Original + one new (duplicate skipped)
        assert len(data.groups["existing"].commands) == 2
        cmds = [c.cmd for c in data.groups["existing"].commands]
        assert cmds == ["echo old", "echo new"]

    def test_import_replace(self, tmp_mem_dir: Path, tmp_path: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "existing": Group(
                        description="keep this description",
                        commands=[GroupCommand(cmd="echo old")],
                    ),
                }
            ),
        )

        content = json.dumps(
            {
                "commands": [{"cmd": "echo new"}],
            }
        )
        import_file = tmp_path / "replace.json"
        import_file.write_text(content, encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["import", str(import_file), "-g", "existing", "--global"],
            input="r\n",  # replace
        )
        assert result.exit_code == 0

        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert len(data.groups["existing"].commands) == 1
        assert data.groups["existing"].commands[0].cmd == "echo new"
        # Description should be preserved on replace
        assert data.groups["existing"].description == "keep this description"

    def test_import_markdown(self, tmp_mem_dir: Path, tmp_path: Path):
        md = (
            "## test\n\n"
            "| Command | Description |\n"
            "|---|---|\n"
            "| `echo hello` | greeting |\n"
        )
        f = tmp_path / "import.md"
        f.write_text(md, encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["import", str(f), "-g", "from-md", "--global", "-f", "markdown"],
        )
        assert result.exit_code == 0
        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "from-md" in data.groups


class TestGroupRemoveCLI:
    def test_remove_with_confirmation(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "to-delete": Group(
                        description="will be deleted",
                        commands=[GroupCommand(cmd="echo bye")],
                    ),
                }
            ),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["group", "remove", "to-delete", "--global"],
            input="y\n",
        )
        assert result.exit_code == 0
        assert "Deleted" in result.output

        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "to-delete" not in data.groups

    def test_remove_with_yes_flag(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"g": Group(commands=[GroupCommand(cmd="x")])}),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["group", "remove", "g", "--global", "--yes"])
        assert result.exit_code == 0
        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "g" not in data.groups

    def test_remove_decline(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"keep": Group(commands=[GroupCommand(cmd="x")])}),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["group", "remove", "keep", "--global"],
            input="n\n",
        )
        assert result.exit_code == 0
        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "keep" in data.groups

    def test_remove_not_found(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["group", "remove", "nope", "--global"])
        assert result.exit_code != 0


class TestGroupRenameCLI:
    def test_rename_success(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "old-name": Group(
                        description="test",
                        commands=[GroupCommand(cmd="echo x")],
                    ),
                }
            ),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["group", "rename", "old-name", "new-name", "--global"],
        )
        assert result.exit_code == 0
        assert "Renamed" in result.output

        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "old-name" not in data.groups
        assert "new-name" in data.groups
        assert data.groups["new-name"].description == "test"

    def test_rename_source_not_found(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(
                cli,
                ["group", "rename", "nope", "new", "--global"],
            )
        assert result.exit_code != 0

    def test_rename_target_exists(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "a": Group(),
                    "b": Group(),
                }
            ),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["group", "rename", "a", "b", "--global"],
        )
        assert result.exit_code != 0
        assert (
            "already exists" in result.output.lower()
            or "already exists" in str(result.exception).lower()
        )

    def test_rename_invalid_new_name(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"valid": Group()}),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["group", "rename", "valid", "BAD NAME", "--global"],
        )
        assert result.exit_code != 0


class TestGroupCopyCLI:
    def test_copy_repo_to_global(self, tmp_mem_dir: Path):
        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_path = storage.group_file_path(repo_name)
        storage.write_group_file(
            repo_path,
            GroupFile(
                groups={
                    "deploy": Group(
                        description="repo deploy",
                        commands=[GroupCommand(cmd="make deploy")],
                    ),
                }
            ),
        )

        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["group", "copy", "deploy", "--global"])
        assert result.exit_code == 0
        assert "Copied" in result.output

        global_data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "deploy" in global_data.groups
        assert global_data.groups["deploy"].description == "repo deploy"

    def test_copy_global_to_repo(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "ssh": Group(commands=[GroupCommand(cmd="ssh host")]),
                }
            ),
        )

        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["group", "copy", "ssh", "--repo"])
        assert result.exit_code == 0

        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_data = storage.read_group_file(storage.group_file_path(repo_name))
        assert "ssh" in repo_data.groups

    def test_copy_no_scope_flag_errors(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["group", "copy", "test"])
        assert result.exit_code != 0

    def test_copy_target_exists_errors(self, tmp_mem_dir: Path):
        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_path = storage.group_file_path(repo_name)
        storage.write_group_file(
            repo_path,
            GroupFile(groups={"dup": Group(commands=[GroupCommand(cmd="repo")])}),
        )
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"dup": Group(commands=[GroupCommand(cmd="global")])}),
        )

        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["group", "copy", "dup", "--global"])
        assert result.exit_code != 0
        assert (
            "already exists" in result.output.lower()
            or "already exists" in str(result.exception).lower()
        )


class TestGroupCopyAdditionalCLI:
    def test_copy_both_flags_errors(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["group", "copy", "test", "--global", "--repo"])
        assert result.exit_code != 0
        assert "both" in result.output.lower() or "Cannot" in result.output

    def test_copy_source_not_found(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo(), patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            result = runner.invoke(cli, ["group", "copy", "nonexistent", "--global"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestRunCLIAdditional:
    def _setup_group(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "test": Group(
                        description="test group",
                        commands=[
                            GroupCommand(cmd="echo 1", comment="first"),
                            GroupCommand(cmd="echo 2", comment="second"),
                        ],
                    ),
                }
            ),
        )

    def test_run_yes_exits_on_failure(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "fail": Group(
                        commands=[
                            GroupCommand(cmd="exit 42"),
                        ]
                    ),
                }
            ),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "fail", "--global", "--yes"])
        assert result.exit_code == 42

    def test_run_decline_does_not_execute(self, tmp_mem_dir: Path):
        self._setup_group(tmp_mem_dir)
        runner = CliRunner()
        with patch("mem.cli._is_interactive", return_value=True):
            result = runner.invoke(cli, ["run", "test", "--global"], input="n\n")
        assert result.exit_code == 0
        # Should NOT have executed any command (no "$ echo" output)
        assert "$ echo" not in result.output

    def test_run_empty_group(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"empty": Group(description="nothing here")}),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["run", "empty", "--global", "--yes"])
        assert result.exit_code == 0
        assert "no commands" in result.output.lower()


class TestExportCLIAdditional:
    def test_export_json_has_commands(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "g": Group(
                        description="test",
                        commands=[
                            GroupCommand(cmd="echo a", comment="first"),
                            GroupCommand(cmd="echo b"),
                        ],
                    ),
                }
            ),
        )
        runner = CliRunner()
        result = runner.invoke(
            cli, ["export", "g", "--global", "-f", "json", "--stdout"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["g"]["commands"]) == 2
        assert data["g"]["commands"][0]["cmd"] == "echo a"


class TestSaveCLIDuplicateComment:
    def test_duplicate_with_different_comment_preserves_original(
        self, tmp_mem_dir: Path
    ):
        runner = CliRunner()
        runner.invoke(cli, ["save", "echo x", "--global", "-c", "original comment"])
        runner.invoke(cli, ["save", "echo x", "--global", "-c", "new comment"])

        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert len(data.saved) == 1
        assert data.saved[0].comment == "original comment"


class TestResolveScope:
    def test_global_flag(self, tmp_mem_dir: Path):
        path = groups.resolve_scope(global_flag=True)
        assert path == storage.GROUPS_GLOBAL_FILE

    def test_in_repo(self, tmp_mem_dir: Path):
        with patch("mem.groups.get_git_repo", return_value=FAKE_REPO):
            path = groups.resolve_scope(global_flag=False)
        expected_name = storage.sanitize_repo_name(FAKE_REPO)
        assert path == storage.group_file_path(expected_name)

    def test_outside_repo_falls_back_to_global(self, tmp_mem_dir: Path):
        with patch("mem.groups.get_git_repo", return_value=None):
            path = groups.resolve_scope(global_flag=False)
        assert path == storage.GROUPS_GLOBAL_FILE


class TestSaveNonInteractive:
    """Verify save --group skips description prompt in non-interactive mode."""

    def test_new_group_no_tty_skips_prompt(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with patch("mem.cli._is_interactive", return_value=False):
            result = runner.invoke(
                cli,
                ["save", "echo hi", "--global", "-g", "ci-group"],
            )
        assert result.exit_code == 0
        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert "ci-group" in data.groups
        assert data.groups["ci-group"].description is None

    def test_new_group_tty_prompts(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with patch("mem.cli._is_interactive", return_value=True):
            result = runner.invoke(
                cli,
                ["save", "echo hi", "--global", "-g", "my-group"],
                input="My description\n",
            )
        assert result.exit_code == 0
        data = storage.read_group_file(storage.GROUPS_GLOBAL_FILE)
        assert data.groups["my-group"].description == "My description"


class TestEditorParsing:
    """Verify EDITOR values with arguments are parsed correctly."""

    def test_editor_with_args(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"test": Group(commands=[GroupCommand(cmd="echo x")])}),
        )
        runner = CliRunner()
        with (
            patch.dict(os.environ, {"EDITOR": "code -w"}),
            patch("subprocess.run") as mock_run,
        ):
            result = runner.invoke(cli, ["group", "edit", "test", "--global"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert args[0] == "code"
        assert args[1] == "-w"

    def test_saved_edit_editor_with_args(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(saved=[SavedCommand(cmd="echo x")]),
        )
        runner = CliRunner()
        with (
            patch.dict(os.environ, {"EDITOR": "vim -u NONE"}),
            patch("subprocess.run") as mock_run,
        ):
            result = runner.invoke(cli, ["saved", "edit", "--global"])
        assert result.exit_code == 0
        args = mock_run.call_args[0][0]
        assert args[0] == "vim"
        assert args[1] == "-u"
        assert args[2] == "NONE"


# ---------------------------------------------------------------------------
# Fix 1: -g short flag for --global
# ---------------------------------------------------------------------------


class TestGlobalShortFlag:
    """Verify -g works as short flag for --global on applicable commands."""

    def test_list_short_g(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(saved=[SavedCommand(cmd="echo test")]),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["list", "-g"])
        assert result.exit_code == 0
        assert "echo test" in result.output

    def test_run_short_g(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"grp": Group(commands=[GroupCommand(cmd="echo hi")])}),
        )
        runner = CliRunner()
        with patch("mem.cli._is_interactive", return_value=True):
            result = runner.invoke(cli, ["run", "grp", "-g"], input="n\n")
        assert result.exit_code == 0

    def test_export_short_g(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"grp": Group(commands=[GroupCommand(cmd="echo hi")])}),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["export", "grp", "-g", "--stdout"])
        assert result.exit_code == 0

    def test_group_edit_short_g(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"grp": Group(commands=[GroupCommand(cmd="echo x")])}),
        )
        runner = CliRunner()
        with patch("subprocess.run"):
            result = runner.invoke(cli, ["group", "edit", "grp", "-g"])
        assert result.exit_code == 0

    def test_group_remove_short_g(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"grp": Group(commands=[GroupCommand(cmd="echo x")])}),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["group", "remove", "grp", "-g", "-y"])
        assert result.exit_code == 0

    def test_group_rename_short_g(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"old": Group(commands=[GroupCommand(cmd="echo x")])}),
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["group", "rename", "old", "new", "-g"])
        assert result.exit_code == 0

    def test_saved_edit_short_g(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(saved=[SavedCommand(cmd="echo x")]),
        )
        runner = CliRunner()
        with patch("subprocess.run"):
            result = runner.invoke(cli, ["saved", "edit", "-g"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Fix 2: mem list --repo
# ---------------------------------------------------------------------------


class TestListRepoFlag:
    """Verify --repo / -r flag on mem list."""

    def test_repo_flag_shows_only_repo(self, tmp_mem_dir: Path):
        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_path = storage.group_file_path(repo_name)
        storage.write_group_file(
            repo_path,
            GroupFile(saved=[SavedCommand(cmd="repo cmd")]),
        )
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(saved=[SavedCommand(cmd="global cmd")]),
        )
        runner = CliRunner()
        with _mock_repo():
            result = runner.invoke(cli, ["list", "--repo"])
        assert result.exit_code == 0
        assert "repo cmd" in result.output
        assert "global cmd" not in result.output

    def test_repo_short_r(self, tmp_mem_dir: Path):
        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_path = storage.group_file_path(repo_name)
        storage.write_group_file(
            repo_path,
            GroupFile(saved=[SavedCommand(cmd="repo cmd")]),
        )
        runner = CliRunner()
        with _mock_repo():
            result = runner.invoke(cli, ["list", "-r"])
        assert result.exit_code == 0
        assert "repo cmd" in result.output

    def test_repo_and_global_mutual_exclusion(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with _mock_repo():
            result = runner.invoke(cli, ["list", "--repo", "--global"])
        assert result.exit_code != 0
        assert "Cannot use --global and --repo together" in result.output

    def test_repo_outside_git_repo(self, tmp_mem_dir: Path):
        runner = CliRunner()
        with patch("mem.cli._current_repo", return_value=None):
            result = runner.invoke(cli, ["list", "--repo"])
        assert result.exit_code != 0
        assert "Not in a git repository" in result.output

    def test_repo_json_excludes_global(self, tmp_mem_dir: Path):
        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_path = storage.group_file_path(repo_name)
        storage.write_group_file(
            repo_path,
            GroupFile(saved=[SavedCommand(cmd="repo cmd")]),
        )
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(saved=[SavedCommand(cmd="global cmd")]),
        )
        runner = CliRunner()
        with _mock_repo():
            result = runner.invoke(cli, ["list", "--repo", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "repo" in data
        assert "global" not in data


# ---------------------------------------------------------------------------
# Fix 3: mem export defaults to JSON + clipboard
# ---------------------------------------------------------------------------


class TestExportClipboard:
    """Verify export defaults and clipboard behavior."""

    def _setup(self, tmp_mem_dir: Path):
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"grp": Group(commands=[GroupCommand(cmd="echo hi")])}),
        )

    def test_export_default_is_json(self, tmp_mem_dir: Path):
        self._setup(tmp_mem_dir)
        runner = CliRunner()
        result = runner.invoke(cli, ["export", "grp", "--global", "--stdout"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "grp" in data

    def test_export_stdout_flag(self, tmp_mem_dir: Path):
        self._setup(tmp_mem_dir)
        runner = CliRunner()
        result = runner.invoke(cli, ["export", "grp", "--global", "--stdout"])
        assert result.exit_code == 0
        assert result.output.strip()  # something was printed

    def test_export_clipboard_copies(self, tmp_mem_dir: Path):
        self._setup(tmp_mem_dir)
        runner = CliRunner()
        with patch("mem.cli._copy_to_clipboard", return_value=True) as mock_cp:
            result = runner.invoke(cli, ["export", "grp", "--global"])
        assert result.exit_code == 0
        mock_cp.assert_called_once()
        assert "Copied json to clipboard" in result.output

    def test_export_clipboard_fallback(self, tmp_mem_dir: Path):
        self._setup(tmp_mem_dir)
        runner = CliRunner()
        with patch("mem.cli._copy_to_clipboard", return_value=False):
            result = runner.invoke(cli, ["export", "grp", "--global"])
        assert result.exit_code == 0
        assert "no clipboard tool found" in result.output


# ---------------------------------------------------------------------------
# Fix 4: mem import auto-detect format
# ---------------------------------------------------------------------------


class TestImportAutoDetect:
    """Verify import auto-detects format from file extension."""

    def test_auto_detect_json(self, tmp_mem_dir: Path, tmp_path: Path):
        content = json.dumps({"commands": [{"cmd": "echo auto"}]})
        f = tmp_path / "data.json"
        f.write_text(content, encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["import", str(f), "-g", "auto-grp", "--global"],
        )
        assert result.exit_code == 0
        assert "Imported 1 commands" in result.output

    def test_auto_detect_markdown(self, tmp_mem_dir: Path, tmp_path: Path):
        md = (
            "## test\n\n| Command | Description |\n|---|---|\n| `echo md` | from md |\n"
        )
        f = tmp_path / "data.md"
        f.write_text(md, encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["import", str(f), "-g", "md-grp", "--global"],
        )
        assert result.exit_code == 0

    def test_auto_detect_unknown_extension_errors(
        self, tmp_mem_dir: Path, tmp_path: Path
    ):
        f = tmp_path / "data.txt"
        f.write_text("hello", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["import", str(f), "-g", "grp", "--global"],
        )
        assert result.exit_code != 0
        assert "Cannot detect format" in result.output

    def test_explicit_format_overrides(self, tmp_mem_dir: Path, tmp_path: Path):
        content = json.dumps({"commands": [{"cmd": "echo override"}]})
        f = tmp_path / "data.txt"
        f.write_text(content, encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["import", str(f), "-g", "grp", "--global", "-f", "json"],
        )
        assert result.exit_code == 0
        assert "Imported 1 commands" in result.output


# ---------------------------------------------------------------------------
# Review fix: clipboard failure falls back to stdout
# ---------------------------------------------------------------------------


class TestClipboardFailure:
    def test_clipboard_crash_falls_back(self, tmp_mem_dir: Path):
        """If clipboard tool exists but fails, fall back to stdout."""
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"grp": Group(commands=[GroupCommand(cmd="echo hi")])}),
        )
        runner = CliRunner()
        # _copy_to_clipboard returns False when CalledProcessError is caught
        with patch("mem.cli._copy_to_clipboard", return_value=False):
            result = runner.invoke(cli, ["export", "grp", "--global"])
        assert result.exit_code == 0
        assert "no clipboard tool found" in result.output


# ---------------------------------------------------------------------------
# Review fix: --repo with group_name restricts to repo scope
# ---------------------------------------------------------------------------


class TestListRepoGroupScope:
    def test_repo_flag_with_group_does_not_fallback_to_global(self, tmp_mem_dir: Path):
        """mem list <group> --repo should NOT show a global group."""
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={"deploy": Group(commands=[GroupCommand(cmd="echo global")])}
            ),
        )
        runner = CliRunner()
        with _mock_repo():
            result = runner.invoke(cli, ["list", "deploy", "--repo"])
        assert result.exit_code != 0
        assert "not found in repo scope" in result.output

    def test_repo_flag_with_group_shows_repo_group(self, tmp_mem_dir: Path):
        """mem list <group> --repo shows the repo group."""
        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_path = storage.group_file_path(repo_name)
        storage.write_group_file(
            repo_path,
            GroupFile(
                groups={"deploy": Group(commands=[GroupCommand(cmd="echo repo")])}
            ),
        )
        runner = CliRunner()
        with _mock_repo():
            result = runner.invoke(cli, ["list", "deploy", "--repo"])
        assert result.exit_code == 0
        assert "echo repo" in result.output


# ---------------------------------------------------------------------------
# UX fix: repo display uses real path, not sanitized name
# ---------------------------------------------------------------------------


class TestRepoDisplayPath:
    def test_list_shows_real_repo_path(self, tmp_mem_dir: Path):
        """mem list should show /Users/test/projects/myapp, not Users-test-projects-myapp."""
        repo_name = storage.sanitize_repo_name(FAKE_REPO)
        repo_path = storage.group_file_path(repo_name)
        storage.write_group_file(
            repo_path,
            GroupFile(saved=[SavedCommand(cmd="echo x")]),
        )
        runner = CliRunner()
        with _mock_repo():
            result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        assert FAKE_REPO in result.output
        # Sanitized name should NOT appear
        assert repo_name not in result.output
