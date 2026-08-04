"""Tests for the JSONL storage layer."""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import make_command
from mem import storage
from mem.models import (
    CommandPattern,
    PatternFile,
    StoredVariable,
    VarsFile,
    WorkSession,
)


# Captured at import time, before conftest's autouse isolation fixture runs,
# so tests can prove they are NOT looking at the developer's real home.
_REAL_HOME = Path.home()
_REAL_MEM_DIR = Path(storage.MEM_DIR)


def _boom_replace(src: object, dst: object) -> None:
    """Stand-in for ``os.replace`` that simulates a crash at commit time.

    Patched on ``os.replace`` rather than ``Path.rename`` because that is the
    call storage.atomic_write() commits with. The contract under test is
    unchanged: whatever the seam, a failure at commit time must leave the
    previous version of the file readable.
    """
    raise OSError("simulated crash during replace")


class TestIsolationHarness:
    """Meta-tests: prove the suite cannot reach the developer's real data.

    Two tests in ``test_patterns.py`` (``TestDeduplication`` and
    ``TestPatternCaching``) never requested the ``tmp_mem_dir`` fixture, so
    ``extract_patterns_for_tool`` happily read the real
    ``~/.mem/patterns/<tool>.json`` — meaning their results depended on the
    machine they ran on. Making the fixture autouse fixed that; these tests
    keep it fixed.
    """

    def test_mem_dir_is_a_temporary_directory(self, tmp_mem_dir, tmp_path):
        assert Path(storage.MEM_DIR) == tmp_path
        assert Path(storage.MEM_DIR) != _REAL_MEM_DIR

    def test_home_is_redirected(self):
        """Even code that resolves `~` on its own lands in a throwaway dir."""
        assert Path.home() != _REAL_HOME

    def test_derived_storage_constants_follow_mem_dir(self, tmp_path):
        """Constants computed at import time are re-pointed, not left dangling."""
        for path in (
            storage.GROUPS_DIR,
            storage.GROUPS_REPOS_DIR,
            storage.GROUPS_GLOBAL_FILE,
            storage.SYNC_COUNTER_FILE,
            storage.VARS_FILE,
        ):
            assert tmp_path in Path(path).parents

    def test_reading_patterns_never_falls_back_to_the_real_home(self):
        """A tool the developer really uses must still look unknown in tests.

        Skipped on machines with no real history, where the check would be
        vacuous.
        """
        if not (_REAL_MEM_DIR / "patterns" / "git.json").exists():
            pytest.skip("no real ~/.mem/patterns/git.json to be shadowed")
        assert storage.read_patterns("git") is None


class TestAppendAndRead:
    def test_append_and_read_command(self, tmp_mem_dir):
        """Append a command, read it back, verify all fields match."""
        cmd = make_command(
            command="docker compose up -d",
            repo="/Users/test/projects/myapp",
            exit_code=0,
            duration_ms=3200,
        )
        storage.append_command(cmd)

        results = list(
            storage.read_commands(storage.repo_key("/Users/test/projects/myapp"))
        )
        assert len(results) == 1
        assert results[0].command == "docker compose up -d"
        assert results[0].repo == "/Users/test/projects/myapp"
        assert results[0].exit_code == 0
        assert results[0].duration_ms == 3200

    def test_append_is_additive_and_ordered(self, tmp_mem_dir):
        """Appending never rewrites the file: earlier lines survive, in order."""
        for i in range(3):
            storage.append_command(
                make_command(command=f"cmd{i}", repo="/Users/test/projects/myapp")
            )

        results = list(
            storage.read_commands(storage.repo_key("/Users/test/projects/myapp"))
        )
        assert [c.command for c in results] == ["cmd0", "cmd1", "cmd2"]

    def test_one_json_object_per_line(self, tmp_mem_dir):
        """The on-disk format is JSONL: exactly one parsable object per line.

        Asserted on the raw bytes, not through ``read_commands``, because the
        format is a public contract (``cat``/``grep``/``jq`` must work on it).
        """
        storage.append_command(make_command(command="a", repo="/r/one"))
        storage.append_command(make_command(command="b", repo="/r/one"))

        raw = storage.repo_file(storage.repo_key("/r/one")).read_text(encoding="utf-8")
        assert raw.endswith("\n")
        lines = raw.splitlines()
        assert len(lines) == 2
        assert [json.loads(line)["command"] for line in lines] == ["a", "b"]

    def test_multiple_repos_isolated(self, tmp_mem_dir):
        """Commands in different repos stay in different files."""
        cmd_a = make_command(command="make test", repo="/Users/test/projects/repo-a")
        cmd_b = make_command(command="cargo build", repo="/Users/test/projects/repo-b")
        storage.append_command(cmd_a)
        storage.append_command(cmd_b)

        results_a = list(
            storage.read_commands(storage.repo_key("/Users/test/projects/repo-a"))
        )
        results_b = list(
            storage.read_commands(storage.repo_key("/Users/test/projects/repo-b"))
        )
        assert len(results_a) == 1
        assert results_a[0].command == "make test"
        assert len(results_b) == 1
        assert results_b[0].command == "cargo build"

    def test_global_fallback(self, tmp_mem_dir):
        """Commands with no repo go to _global.jsonl."""
        cmd = make_command(command="ls -la", repo=None)
        storage.append_command(cmd)

        results = list(storage.read_commands("_global"))
        assert len(results) == 1
        assert results[0].command == "ls -la"
        assert results[0].repo is None
        assert storage.repo_file("_global").exists()

    def test_read_nonexistent_returns_empty(self, tmp_mem_dir):
        """Reading a missing JSONL file yields an empty iterator."""
        results = list(storage.read_commands("nonexistent"))
        assert results == []

    def test_writes_stay_inside_mem_dir(self, tmp_mem_dir):
        """Every file a capture creates lives under MEM_DIR.

        Guards the isolation contract itself: if storage ever resolved a path
        from ``Path.home()`` at call time instead of from ``MEM_DIR``, this
        would catch it.
        """
        storage.append_command(make_command(command="x", repo="/Users/test/p/app"))
        written = [p for p in tmp_mem_dir.rglob("*.jsonl")]
        assert written
        for path in written:
            assert tmp_mem_dir in path.parents


class TestSanitizeRepoName:
    """Repo paths become filenames; the mapping must be total and safe."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("/Users/test/projects/myapp", "Users-test-projects-myapp"),
            ("already-fine", "already-fine"),
            ("/tmp/x/", "tmp-x"),
            ("my repo", "my-repo"),
            ("repo.with.dots", "repo-with-dots"),
            ("Ünïcödé", "n-c-d"),
        ],
    )
    def test_sanitized_forms(self, raw: str, expected: str):
        """Non-alphanumerics collapse to hyphens and edges are trimmed.

        The trailing ``.strip("-")`` matters: without it every absolute path
        would produce a filename starting with ``-``, which most Unix tools
        parse as an option flag.
        """
        assert storage.sanitize_repo_name(raw) == expected

    def test_result_is_a_bare_filename(self):
        """A sanitized name can never escape the repos/ directory."""
        for hostile in ("../../etc/passwd", "/", "..", "a/../../b"):
            name = storage.sanitize_repo_name(hostile)
            assert "/" not in name
            assert not name.startswith("-")
            assert ".." not in name

    def test_repo_file_lands_in_repos_subdir(self, tmp_mem_dir):
        """repo_file() composes MEM_DIR/repos/<name>.jsonl."""
        assert storage.repo_file("abc") == tmp_mem_dir / "repos" / "abc.jsonl"

    def test_repo_key_separates_paths_the_slug_conflates(self):
        """The slug is ambiguous by construction; the key must not be."""
        assert storage.sanitize_repo_name("/w/a-b/c") == storage.sanitize_repo_name(
            "/w/a/b/c"
        )
        assert storage.repo_key("/w/a-b/c") != storage.repo_key("/w/a/b/c")

    def test_repo_key_is_deterministic(self):
        """The same repo must resolve to the same file on every capture."""
        assert storage.repo_key("/w/app") == storage.repo_key("/w/app")

    def test_repo_key_is_a_bare_filename(self):
        """A key can never escape the repos/ directory either."""
        for hostile in ("../../etc/passwd", "/", "..", "a/../../b", "-"):
            key = storage.repo_key(hostile)
            assert key
            assert "/" not in key
            assert not key.startswith("-")
            assert ".." not in key


class TestPatterns:
    def test_write_then_read_roundtrip(self, tmp_mem_dir):
        """Pattern files survive a write/read cycle with every field intact."""
        pf = PatternFile(
            tool="kubectl",
            patterns=[
                CommandPattern(
                    pattern="kubectl get <resource>",
                    example="kubectl get pods",
                    frequency=42,
                ),
            ],
            last_updated=1700000000,
            processed_commands=["kubectl get pods"],
        )
        storage.write_patterns(pf)

        result = storage.read_patterns("kubectl")
        assert result is not None
        assert result.tool == "kubectl"
        assert result.last_updated == 1700000000
        assert result.processed_commands == ["kubectl get pods"]
        assert [(p.pattern, p.example, p.frequency) for p in result.patterns] == [
            ("kubectl get <resource>", "kubectl get pods", 42)
        ]

    def test_write_patterns_leaves_no_tmp_file(self, tmp_mem_dir):
        """The scratch file used for the atomic write is renamed away, not left."""
        storage.write_patterns(PatternFile(tool="kubectl", patterns=[], last_updated=1))
        assert list((tmp_mem_dir / "patterns").glob("*.tmp")) == []

    def test_tmp_file_is_a_sibling_of_the_target(self, tmp_mem_dir):
        """rename() is only atomic within one filesystem, so tmp must be adjacent.

        A tmp file in /tmp would turn the "atomic" write into a cross-device
        copy that can be observed half-finished.
        """
        target = storage.pattern_file("kubectl")
        tmp = target.with_suffix(".json.tmp")
        assert tmp.parent == target.parent

    def test_failed_write_preserves_previous_version(self, tmp_mem_dir, monkeypatch):
        """A crash at commit time must leave the OLD pattern file readable.

        This is the actual atomicity contract. The previous version of this
        test only checked that no ``.tmp`` file lingered and that the result
        parsed — both of which a plain ``path.write_text(...)`` would satisfy,
        so it could not distinguish an atomic implementation from a
        truncate-in-place one.
        """
        v1 = PatternFile(
            tool="kubectl",
            patterns=[
                CommandPattern(pattern="v1", example="kubectl get pods", frequency=1)
            ],
            last_updated=1,
        )
        storage.write_patterns(v1)

        v2 = PatternFile(
            tool="kubectl",
            patterns=[
                CommandPattern(pattern="v2", example="kubectl get svc", frequency=2)
            ],
            last_updated=2,
        )
        monkeypatch.setattr(os, "replace", _boom_replace)
        with pytest.raises(OSError):
            storage.write_patterns(v2)

        survivor = storage.read_patterns("kubectl")
        assert survivor is not None
        assert [p.pattern for p in survivor.patterns] == ["v1"]

    def test_read_nonexistent_pattern_returns_none(self, tmp_mem_dir):
        """Reading a missing pattern file returns None."""
        result = storage.read_patterns("nonexistent")
        assert result is None

    def test_read_corrupted_pattern_returns_none(self, tmp_mem_dir, capsys):
        """A truncated pattern file degrades to None with a warning, not a crash."""
        storage.ensure_dirs()
        storage.pattern_file("kubectl").write_text('{"tool": "kube', encoding="utf-8")

        assert storage.read_patterns("kubectl") is None
        assert "corrupted pattern file" in capsys.readouterr().err


class TestReadAll:
    def test_read_all_commands(self, tmp_mem_dir):
        """read_all_commands iterates across all repo files."""
        storage.append_command(
            make_command(command="cmd1", repo="/Users/test/projects/repo-a")
        )
        storage.append_command(
            make_command(command="cmd2", repo="/Users/test/projects/repo-b")
        )
        storage.append_command(make_command(command="cmd3", repo=None))

        results = list(storage.read_all_commands())
        commands = {r.command for r in results}
        assert commands == {"cmd1", "cmd2", "cmd3"}

    def test_read_all_commands_empty_when_no_storage(self, tmp_mem_dir):
        """No repos/ directory yields an empty iterator rather than an error."""
        assert list(storage.read_all_commands()) == []

    def test_read_all_commands_does_not_read_pattern_files(self, tmp_mem_dir):
        """Only *.jsonl under repos/ is command history."""
        storage.append_command(make_command(command="cmd1", repo=None))
        storage.write_patterns(
            PatternFile(tool="git", patterns=[], last_updated=1),
        )
        (tmp_mem_dir / "repos" / "notes.txt").write_text("junk", encoding="utf-8")

        assert [c.command for c in storage.read_all_commands()] == ["cmd1"]


class TestCorruptedLines:
    def test_skips_corrupted_lines(self, tmp_mem_dir, capsys):
        """Corrupted JSONL lines are skipped, not fatal."""
        storage.ensure_dirs()
        path = storage.repo_file(storage.repo_key("/Users/test/projects/myapp"))
        cmd = make_command(command="good command")
        with path.open("a") as f:
            f.write(cmd.to_jsonl() + "\n")
            f.write("THIS IS NOT JSON\n")
            f.write(cmd.to_jsonl() + "\n")

        results = list(
            storage.read_commands(storage.repo_key("/Users/test/projects/myapp"))
        )
        assert len(results) == 2
        assert {r.command for r in results} == {"good command"}
        err = capsys.readouterr().err
        assert "skipping corrupted line 2" in err

    def test_blank_lines_are_ignored_silently(self, tmp_mem_dir, capsys):
        """Empty lines are not corruption and must not produce warnings."""
        storage.ensure_dirs()
        path = storage.repo_file("r")
        path.write_text(
            make_command(command="ok").to_jsonl() + "\n\n\n", encoding="utf-8"
        )

        assert len(list(storage.read_commands("r"))) == 1
        assert capsys.readouterr().err == ""


class TestSessions:
    def test_session_written_to_file_named_by_utc_date(self, tmp_mem_dir):
        """A session is filed under the UTC date it STARTED."""
        started = 1700000000  # 2023-11-14T22:13:20Z
        session = WorkSession(
            id="abc",
            summary="s",
            started_at=started,
            ended_at=started + 60,
            dir="",
            repo="/r",
            commands=["git status"],
        )
        storage.append_session(session)

        expected_date = datetime.fromtimestamp(started, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        assert storage.session_file(expected_date).exists()
        assert [s.id for s in storage.read_sessions(expected_date)] == ["abc"]

    def test_read_sessions_missing_file(self, tmp_mem_dir):
        """An absent day yields nothing."""
        assert list(storage.read_sessions("1999-01-01")) == []

    def test_read_all_sessions_spans_days(self, tmp_mem_dir):
        """read_all_sessions walks every daily file."""
        day = 86400
        for i, ts in enumerate([1700000000, 1700000000 + day, 1700000000 + 2 * day]):
            storage.append_session(
                WorkSession(
                    id=f"s{i}",
                    summary="s",
                    started_at=ts,
                    ended_at=ts,
                    dir="",
                    commands=["c"],
                )
            )

        assert {s.id for s in storage.read_all_sessions()} == {"s0", "s1", "s2"}

    def test_corrupted_session_line_skipped(self, tmp_mem_dir, capsys):
        """A bad session line is skipped with a warning; the rest still load."""
        storage.ensure_dirs()
        path = storage.session_file("2023-11-14")
        good = WorkSession(
            id="ok", summary="s", started_at=1, ended_at=2, dir="", commands=["c"]
        )
        path.write_text("NOT JSON\n" + good.to_jsonl() + "\n", encoding="utf-8")

        assert [s.id for s in storage.read_sessions("2023-11-14")] == ["ok"]
        assert "skipping corrupted session line 1" in capsys.readouterr().err


class TestRotate:
    """Retention: commands expire after N days, session FILES after M days."""

    def _add(self, command: str, age_days: float, repo: str | None = "/r/app") -> None:
        ts = int(time.time()) - int(age_days * 86400)
        storage.append_command(make_command(command=command, ts=ts, repo=repo))

    def test_removes_only_commands_older_than_the_cutoff(self, tmp_mem_dir):
        """Commands older than keep_commands_days go; newer ones stay.

        Directly pins the direction of the comparison in ``rotate``: an
        inverted test (``ts < cutoff`` kept) would delete the recent history
        and archive the stale history, which is the worst possible outcome for
        a tool whose entire value is recent recall.
        """
        self._add("fresh", age_days=1)
        self._add("middling", age_days=45)
        self._add("stale", age_days=120)

        removed, session_files_removed = storage.rotate(
            keep_commands_days=90, keep_sessions_days=30
        )

        assert removed == 1
        assert session_files_removed == 0
        assert sorted(c.command for c in storage.read_all_commands()) == [
            "fresh",
            "middling",
        ]

    def test_returns_zero_and_keeps_everything_when_nothing_expired(self, tmp_mem_dir):
        """A no-op rotate reports 0 and leaves the file byte-identical."""
        self._add("fresh", age_days=1)
        before = storage.repo_file(storage.repo_key("/r/app")).read_bytes()

        assert storage.rotate() == (0, 0)
        assert storage.repo_file(storage.repo_key("/r/app")).read_bytes() == before

    def test_deletes_repo_file_when_every_command_expires(self, tmp_mem_dir):
        """An emptied repo file is unlinked, not left as a zero-byte husk."""
        self._add("ancient-1", age_days=200)
        self._add("ancient-2", age_days=300)

        removed, _ = storage.rotate(keep_commands_days=90)

        assert removed == 2
        assert not storage.repo_file(storage.repo_key("/r/app")).exists()

    def test_corrupted_lines_are_never_rotated_away(self, tmp_mem_dir):
        """Unparsable lines are preserved: we cannot prove they are stale."""
        self._add("ancient", age_days=200)
        with storage.repo_file(storage.repo_key("/r/app")).open(
            "a", encoding="utf-8"
        ) as f:
            f.write("NOT JSON\n")

        removed, _ = storage.rotate(keep_commands_days=90)

        assert removed == 1
        assert "NOT JSON" in storage.repo_file(storage.repo_key("/r/app")).read_text(
            encoding="utf-8"
        )

    def test_deletes_session_files_older_than_cutoff(self, tmp_mem_dir):
        """Old daily session files are unlinked; recent ones survive."""
        storage.ensure_dirs()
        today = datetime.now(tz=timezone.utc).date()
        recent = (today - timedelta(days=2)).isoformat()
        ancient = (today - timedelta(days=400)).isoformat()
        for date in (recent, ancient):
            storage.session_file(date).write_text("{}\n", encoding="utf-8")

        _, session_files_removed = storage.rotate(keep_sessions_days=30)

        assert session_files_removed == 1
        assert storage.session_file(recent).exists()
        assert not storage.session_file(ancient).exists()

    def test_pattern_files_are_never_rotated(self, tmp_mem_dir):
        """Extracted patterns are accumulated learning and outlive raw history."""
        self._add("ancient", age_days=500)
        storage.write_patterns(
            PatternFile(
                tool="git",
                patterns=[
                    CommandPattern(pattern="git <x>", example="git a", frequency=9)
                ],
                last_updated=0,
            )
        )

        storage.rotate(keep_commands_days=1, keep_sessions_days=1)

        surviving = storage.read_patterns("git")
        assert surviving is not None
        assert surviving.patterns[0].frequency == 9

    def test_rotate_on_empty_storage_is_a_noop(self, tmp_mem_dir):
        """Nothing on disk yet — rotate must not create or raise."""
        assert storage.rotate() == (0, 0)


class TestForgetCommands:
    """`mem forget` is a privacy primitive: no trace may survive anywhere."""

    def test_removes_matching_and_keeps_the_rest(self, tmp_mem_dir):
        """Only entries containing the query are deleted."""
        for cmd in ("export TOKEN=hunter2", "git status", "echo hunter2"):
            storage.append_command(make_command(command=cmd, repo="/r/app"))

        removed = storage.forget_commands("hunter2")

        assert removed == 2
        assert [c.command for c in storage.read_all_commands()] == ["git status"]

    def test_scrubs_matching_commands_from_sessions_too(self, tmp_mem_dir):
        """Session transcripts are rewritten, not just repo history."""
        storage.append_session(
            WorkSession(
                id="s1",
                summary="work",
                started_at=1700000000,
                ended_at=1700000100,
                dir="",
                commands=["git status", "export TOKEN=hunter2", "git diff"],
            )
        )

        storage.forget_commands("hunter2")

        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 1
        assert sessions[0].commands == ["git status", "git diff"]

    def test_session_dropped_when_all_its_commands_match(self, tmp_mem_dir):
        """A session left with zero commands is removed entirely."""
        storage.append_session(
            WorkSession(
                id="s1",
                summary="secret work",
                started_at=1700000000,
                ended_at=1700000100,
                dir="",
                commands=["export TOKEN=hunter2"],
            )
        )

        storage.forget_commands("hunter2")

        assert list(storage.read_all_sessions()) == []

    def test_no_match_leaves_everything_intact(self, tmp_mem_dir):
        """A query that matches nothing removes nothing."""
        storage.append_command(make_command(command="git status", repo="/r/app"))

        assert storage.forget_commands("nothing-like-this") == 0
        assert [c.command for c in storage.read_all_commands()] == ["git status"]

    def test_matching_is_substring_based(self, tmp_mem_dir):
        """Forgetting a token scrubs every command that embeds it."""
        for cmd in ("curl -H 'Bearer abc123'", "echo abc123 | wc -c", "ls"):
            storage.append_command(make_command(command=cmd, repo="/r/app"))

        assert storage.forget_commands("abc123") == 2
        assert [c.command for c in storage.read_all_commands()] == ["ls"]


class TestVarsFile:
    """The variable store may hold credentials; permissions are part of the API."""

    def test_roundtrip(self, tmp_mem_dir):
        """Values and metadata survive a write/read cycle."""
        storage.write_vars_file(
            VarsFile(vars={"API_TOKEN": StoredVariable(value="s3cret", last_used=99)})
        )

        data = storage.read_vars_file()
        assert data.vars["API_TOKEN"].value == "s3cret"
        assert data.vars["API_TOKEN"].last_used == 99

    def test_file_is_owner_read_write_only(self, tmp_mem_dir):
        """0600, always: the vars file can contain secrets.

        A 0644 vars file would expose every stored credential to any other
        account on a shared machine.
        """
        storage.write_vars_file(
            VarsFile(vars={"API_TOKEN": StoredVariable(value="s3cret")})
        )

        mode = stat.S_IMODE(storage.VARS_FILE.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_permissions_survive_a_rewrite(self, tmp_mem_dir):
        """Overwriting the store must not widen its permissions."""
        storage.write_vars_file(VarsFile(vars={"A": StoredVariable(value="1")}))
        storage.write_vars_file(VarsFile(vars={"A": StoredVariable(value="2")}))

        assert stat.S_IMODE(storage.VARS_FILE.stat().st_mode) == 0o600

    def test_failed_write_preserves_previous_version(self, tmp_mem_dir, monkeypatch):
        """A crash at commit time must not destroy the existing variables."""
        storage.write_vars_file(VarsFile(vars={"A": StoredVariable(value="original")}))

        monkeypatch.setattr(os, "replace", _boom_replace)
        with pytest.raises(OSError):
            storage.write_vars_file(VarsFile(vars={"A": StoredVariable(value="new")}))

        assert storage.read_vars_file().vars["A"].value == "original"

    def test_missing_file_reads_as_empty(self, tmp_mem_dir):
        """No store yet is not an error."""
        assert storage.read_vars_file().vars == {}

    def test_corrupted_file_reads_as_empty_with_warning(self, tmp_mem_dir, capsys):
        """A mangled store degrades to empty rather than breaking every command."""
        storage.VARS_FILE.write_text("{not json", encoding="utf-8")

        assert storage.read_vars_file().vars == {}
        assert "corrupted vars file" in capsys.readouterr().err


class TestSyncCounter:
    def test_starts_at_zero(self, tmp_mem_dir):
        """No counter file means no captures since the last sync."""
        assert storage.read_sync_counter() == 0

    def test_increment_persists(self, tmp_mem_dir):
        """The counter survives across calls (it lives on disk, not in memory)."""
        assert storage.increment_sync_counter() == 1
        assert storage.increment_sync_counter() == 2
        assert storage.read_sync_counter() == 2

    def test_reset_returns_to_zero(self, tmp_mem_dir):
        storage.increment_sync_counter()
        storage.reset_sync_counter()
        assert storage.read_sync_counter() == 0
        assert storage.increment_sync_counter() == 1

    def test_corrupted_counter_reads_as_zero(self, tmp_mem_dir):
        """Garbage in the counter file must not break capture."""
        storage.ensure_dirs()
        storage.SYNC_COUNTER_FILE.write_text("not-a-number", encoding="utf-8")

        assert storage.read_sync_counter() == 0
        assert storage.increment_sync_counter() == 1


class TestPrefilterNeedles:
    """`prefilter_needles` reduces terms to substrings safe to grep raw JSON.

    The rule it must satisfy is one-directional: a needle may admit lines that
    do not match (they get parsed and rejected properly afterwards), but it
    must never exclude a line that does. Everything below is a case where
    getting that backwards would silently shrink search results.
    """

    def test_ordinary_words_pass_through_unchanged(self):
        assert storage.prefilter_needles(["docker", "compose"]) == [
            "docker",
            "compose",
        ]

    def test_needles_are_lowercased(self):
        """The reader lowercases each line, so the needles must match that."""
        assert storage.prefilter_needles(["DOCKER"]) == ["docker"]

    @pytest.mark.parametrize(
        ("term", "needle"),
        [
            (r"\bword\b", "bword"),  # the backslash is escaped, the letters are not
            ('"quoted"', "quoted"),  # double quotes are escaped
            ("path/to/file", "path"),  # some encoders escape the solidus
            ("--flag=value", "--flag"),  # `=` is not stable, `-` is
            ("a.b-c_d", "a.b-c_d"),  # dot, hyphen and underscore are all safe
            ("café", "caf"),  # keep the ASCII run, drop the accented tail
        ],
    )
    def test_a_term_reduces_to_its_longest_json_stable_run(
        self, term: str, needle: str
    ):
        assert storage.prefilter_needles([term]) == [needle]

    @pytest.mark.parametrize("term", ["|", "$1", "ab", "日本語", ">>", ""])
    def test_terms_with_no_usable_run_contribute_no_needle(self, term: str):
        """Better to parse every line than to risk excluding a real match.

        A term made only of shell punctuation, or of non-ASCII with no ASCII
        run of its own, contributes nothing. The query still works — it just
        parses every line, which is the safe direction to fail in.
        """
        assert storage.prefilter_needles([term]) == []

    def test_a_needle_is_always_a_substring_of_its_term(self):
        """The property the whole optimisation rests on.

        If a command contains the term, it contains the needle; the needle is
        built only from characters no encoder rewrites, so it survives into
        the raw line verbatim. Break this and searches silently lose results.
        """
        terms = [
            r"grep \d+ access.log",
            '--output="report file"',
            "kubectl",
            "a.b-c_d",
            "café",
            "$HOME/bin",
        ]
        for term in terms:
            for needle in storage.prefilter_needles([term]):
                assert needle in term.lower(), f"{needle!r} is not inside {term!r}"

    def test_the_needle_actually_skips_lines(self, tmp_mem_dir):
        """Proof the filter is wired up and not quietly returning nothing.

        Without this, `prefilter_needles` could return `[]` for everything —
        every correctness test would still pass and the speedup would be zero.
        """
        storage.append_command(make_command(command="docker compose up", repo=None))
        storage.append_command(make_command(command="git status", repo=None))

        assert len(list(storage.read_commands("_global"))) == 2
        assert [c.command for c in storage.read_commands("_global", ["docker"])] == [
            "docker compose up"
        ]
        assert list(storage.read_commands("_global", ["nomatchhere"])) == []

    def test_every_needle_must_be_present(self, tmp_mem_dir):
        """Terms are conjunctive, so the needles are too."""
        storage.append_command(make_command(command="docker compose up", repo=None))
        storage.append_command(make_command(command="docker build .", repo=None))

        found = [
            c.command for c in storage.read_commands("_global", ["docker", "compose"])
        ]

        assert found == ["docker compose up"]


@pytest.mark.perf
class TestPrefilterIsFasterThanParsing:
    """The prefilter exists for one reason: 82% of query time was wasted.

    Answering a query used to cost ~140ms per 100k commands, of which ~135ms
    was `json.loads` on lines that were then discarded. Marked `perf` because
    it measures wall clock, which is too machine-dependent to gate a PR.
    """

    def test_a_selective_query_is_much_faster_than_a_full_parse(self, tmp_mem_dir):
        now = int(time.time())
        storage.ensure_dirs()
        path = storage.repo_file("_global")
        path.write_text(
            "\n".join(
                make_command(
                    command=f"tool-{i} run --flag", ts=now, repo=None
                ).to_jsonl()
                for i in range(20_000)
            )
            + "\n",
            encoding="utf-8",
        )

        start = time.perf_counter()
        parsed_everything = len(list(storage.read_commands("_global")))
        full = time.perf_counter() - start

        start = time.perf_counter()
        filtered = len(list(storage.read_commands("_global", ["tool-17999"])))
        prefiltered = time.perf_counter() - start

        assert parsed_everything == 20_000
        assert filtered == 1
        assert prefiltered * 3 < full, (
            f"prefilter gave no real speedup: {prefiltered:.3f}s vs {full:.3f}s"
        )

    def test_a_field_name_query_is_not_slower_than_no_filter(self, tmp_mem_dir):
        """The prefilter must never cost more than the work it skips.

        Two ways to get this wrong, and both happened. Scanning the whole raw
        line made `exit` match every record via `exit_code`, so the filter
        admitted everything and saved nothing. Narrowing the scan with a
        character-by-character Python loop then cost *more* than the
        `json.loads` it existed to avoid — 165ms per 100k records against
        137ms for simply parsing them all.
        """
        now = int(time.time())
        storage.ensure_dirs()
        storage.repo_file("_global").write_text(
            "\n".join(
                make_command(command=f"tool-{i} run", ts=now, repo=None).to_jsonl()
                for i in range(20_000)
            )
            + "\n",
            encoding="utf-8",
        )

        start = time.perf_counter()
        everything = len(list(storage.read_commands("_global")))
        full = time.perf_counter() - start

        # "exit" appears in the `exit_code` field name of every single record.
        needles = storage.prefilter_needles(["exit"])
        start = time.perf_counter()
        matched = len(list(storage.read_commands("_global", needles)))
        filtered = time.perf_counter() - start

        assert everything == 20_000
        assert matched == 0, "a field name still matches every record"
        assert filtered < full, (
            f"the prefilter cost more than parsing: {filtered:.3f}s vs {full:.3f}s"
        )


class TestCommandSpan:
    """The prefilter must scan the command, not the whole encoded record.

    Every record contains the field *names* `command`, `dir`, `exit_code`,
    `duration_ms`, `session` and `imported`, so scanning the raw line made a
    search for `exit` — or `dir`, or `port` — match every line in the store.
    The filter admitted everything and saved nothing, precisely for the
    queries most likely to be slow.
    """

    def test_it_returns_only_the_command(self):
        line = make_command(command="git push", repo="/r").to_jsonl()

        assert storage.command_span(line) == "git push"

    @pytest.mark.parametrize(
        "command",
        [
            'echo "hi"',  # escaped quotes must not end the span early
            r"grep \d+ log",
            r'sed "s/\\/x/g"',
            "echo café",
            "printf 'a\tb'",
            "curl -H 'X: {\"k\":1}'",
            "",
        ],
    )
    def test_it_survives_anything_a_command_can_contain(self, command: str):
        """The span is the *encoded* text, which is what the needles match.

        Deliberately not the decoded command: decoding is the expensive step
        this whole path exists to skip, and the needles are built from
        characters that survive JSON encoding unchanged precisely so they can
        be matched against encoded text. `json.dumps(x)[1:-1]` is that
        encoding with the surrounding quotes removed.
        """
        line = json.dumps({"command": command, "ts": 1, "dir": "/x"})

        assert storage.command_span(line) == json.dumps(command)[1:-1]

    @pytest.mark.parametrize(
        "line",
        [
            "not json at all",
            "{}",
            '{"ts": 1}',  # no command field
            '{"command": 42}',  # not a string
            '{"command": "unterminated',
            "",
        ],
    )
    def test_an_unrecognisable_line_is_scanned_whole(self, line: str):
        """Admitting too much is the safe direction; excluding a match is not."""
        assert storage.command_span(line) == line

    def test_whitespace_after_the_colon_is_tolerated(self):
        """These files are documented as hand-editable, so formatting varies."""
        assert storage.command_span('{"command" :  "ls -la", "ts": 1}') == "ls -la"

    def test_a_field_name_no_longer_matches_every_record(self, tmp_mem_dir):
        """The bug itself: `exit` matched every line because of `exit_code`."""
        storage.append_command(make_command(command="git push", repo=None))
        storage.append_command(make_command(command="exit", repo=None))

        found = [c.command for c in storage.read_commands("_global", ["exit"])]

        assert found == ["exit"]

    def test_a_directory_name_no_longer_matches_the_command(self, tmp_mem_dir):
        """`repo` and `dir` are stored on every record and are not the command."""
        storage.append_command(
            make_command(command="ls", repo="/Users/me/projects/kubernetes")
        )

        matched = list(
            storage.read_commands(
                storage.repo_key("/Users/me/projects/kubernetes"), ["kubernetes"]
            )
        )

        assert matched == [], "the prefilter matched the repo path, not the command"
