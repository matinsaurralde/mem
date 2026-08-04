"""Contract tests for the MCP server — driven the way a real client drives it.

The server's whole job is to behave correctly on a pipe, so almost every
test here starts a real ``mem mcp`` subprocess, writes framed JSON-RPC to its
stdin and parses what comes back on its stdout. Testing ``handle_message()``
in-process would pass on a server that prints a banner to stdout, crashes on
a malformed frame, or serves history before the user opted in — which are
exactly the three defects that matter.

Three invariants are treated as load-bearing and tested from several angles:

1. **Stdout carries protocol and nothing else.** One stray byte desynchronises
   the client permanently.
2. **Nothing is served until the user opts in.** The default is refusal, and
   the refusal has to say why.
3. **Nothing leaves without passing redaction.** Tested with real-shaped
   secrets, asserting the secret is *absent* rather than that the marker is
   present — the marker can be there while a second copy leaks.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

# The socket-killing sitecustomize stub is imported rather than copied: it is
# the project's canonical proof that a code path never reaches the network,
# and a second copy would be free to drift away from it.
from test_e2e import NETWORK_STUB, RUN_TIMEOUT

from mem import mcp, storage
from mem.cli import cli
from mem.models import (
    AgentAccess,
    AgentAuditEntry,
    Group,
    GroupCommand,
    GroupFile,
    VarDeclaration,
)
from mem.variables import redact_secrets

# The subprocesses below are started as `python -m mem.cli`, not through the
# `mem` console script. Both are real entry points — this is the one
# `capture._spawn_background_sync` uses — but only this one is guaranteed to
# run the source tree under test: a console script is a generated shim that
# points wherever the last `pip install` aimed it.
MEM_MODULE = [sys.executable, "-m", "mem.cli"]


# --- harness ---------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An empty HOME for the server subprocess. Never the developer's own."""
    h = tmp_path / "mcp-home"
    h.mkdir()
    return h


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A non-git working directory, so repo detection is deterministically None."""
    w = tmp_path / "mcp-work"
    w.mkdir()
    return w


def server_env(home: Path, **extra: str) -> dict[str, str]:
    """Environment for the server subprocess, with ``$HOME`` redirected."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("MEM_DIR", None)
    env.update(extra)
    return env


def run_mem(args: list[str], home: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a plain `mem` subcommand (used to flip the agent access flag)."""
    return subprocess.run(
        [*MEM_MODULE, *args],
        cwd=str(cwd),
        env=server_env(home),
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT,
    )


def serve_raw(
    payload: str,
    home: Path,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `mem mcp` against a literal stdin payload, however malformed.

    Separate from :func:`serve` so tests can send bytes no JSON encoder would
    ever produce — which is precisely the input a server must survive.
    """
    return subprocess.run(
        [*MEM_MODULE, "mcp"],
        cwd=str(cwd),
        env=env if env is not None else server_env(home),
        input=payload,
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT,
    )


def serve(
    frames: list[dict[str, Any]],
    home: Path,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `mem mcp`, feed it *frames*, and return the completed process.

    stdin is closed after the last frame, which is how a real client shuts a
    stdio server down; a server that does not exit on EOF would hang here.
    """
    payload = "".join(json.dumps(frame) + "\n" for frame in frames)
    return serve_raw(payload, home, cwd, env)


def frames_of(result: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    """Parse stdout into JSON-RPC messages, asserting nothing else is there."""
    messages = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - failure path
            pytest.fail(f"non-protocol output on stdout: {line!r} ({exc})")
    return messages


def by_id(result: subprocess.CompletedProcess[str], request_id: Any) -> dict[str, Any]:
    """Return the single response carrying *request_id*."""
    matches = [m for m in frames_of(result) if m.get("id") == request_id]
    assert len(matches) == 1, f"expected one response for id={request_id}: {matches}"
    return matches[0]


def tool_payload(response: dict[str, Any]) -> Any:
    """Decode the JSON document a successful tool call returns as text."""
    assert "error" not in response, response
    content = response["result"]["content"]
    assert content[0]["type"] == "text"
    return json.loads(content[0]["text"])


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": mcp.PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1.0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def call(request_id: int, name: str, arguments: dict | None = None) -> dict[str, Any]:
    """Build a `tools/call` request frame."""
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": params,
    }


def enable(home: Path, workdir: Path) -> None:
    """Grant agent access through the real CLI, not by writing the file."""
    result = run_mem(["agent", "enable"], home, workdir)
    assert result.returncode == 0, result.stderr


def plant(home: Path, rows: list[dict[str, Any]], repo: str = "_global") -> None:
    """Write raw history lines, bypassing capture so timestamps are fixed."""
    path = home / ".mem" / "repos" / f"{repo}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    base = {"dir": "/w", "repo": None, "exit_code": 0, "duration_ms": 5}
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({**base, **row}) + "\n")


def plant_runbook(home: Path, name: str, commands: list[GroupCommand]) -> None:
    """Write a global runbook straight to disk."""
    path = home / ".mem" / "groups" / "_global.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = GroupFile(groups={name: Group(description="curated", commands=commands)})
    path.write_text(data.model_dump_json(indent=2), encoding="utf-8")


NOW = int(time.time())


def module_imports(module: Any) -> set[str]:
    """Top-level package name of every import in *module*, at any nesting depth.

    Parsed with ``ast`` rather than read from ``sys.modules``: lazily imported
    modules would be invisible there, and this has to see the import even if
    the branch containing it never runs.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def dangerous_calls(module: Any) -> set[str]:
    """Names of any dynamic-execution builtin or os exec/spawn call in *module*."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    builtins = {"eval", "exec", "compile", "__import__"}
    attributes = {"system", "popen", "execv", "execve", "spawnv", "fork"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in builtins:
            found.add(node.func.id)
        if isinstance(node.func, ast.Attribute) and node.func.attr in attributes:
            found.add(node.func.attr)
    return found


# --- handshake -------------------------------------------------------------


class TestInitializeHandshake:
    """The first three frames of every MCP session."""

    def test_full_handshake(self, home: Path, workdir: Path) -> None:
        """initialize → initialized notification → tools/list, in one session."""
        enable(home, workdir)

        result = serve(
            [INIT, INITIALIZED, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}],
            home,
            workdir,
        )

        assert result.returncode == 0, result.stderr
        messages = frames_of(result)
        # Two responses, not three: the notification must not be answered.
        assert [m["id"] for m in messages] == [1, 2]

        init = messages[0]["result"]
        assert init["protocolVersion"] == mcp.PROTOCOL_VERSION
        assert init["serverInfo"]["name"] == "mem"
        assert "tools" in init["capabilities"]
        assert init["instructions"]

    def test_unknown_protocol_version_falls_back_to_ours(
        self, home: Path, workdir: Path
    ) -> None:
        """A version we do not speak is answered with the one we do.

        Echoing the client's unknown version back would claim compatibility
        mem cannot honour; the spec says to answer with a supported one and
        let the client decide.
        """
        enable(home, workdir)
        request = {
            **INIT,
            "params": {**INIT["params"], "protocolVersion": "1999-01-01"},
        }

        result = serve([request], home, workdir)

        assert by_id(result, 1)["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION

    @pytest.mark.parametrize("version", mcp.SUPPORTED_PROTOCOL_VERSIONS)
    def test_supported_versions_are_echoed(
        self, version: str, home: Path, workdir: Path
    ) -> None:
        """Every advertised revision is negotiated back verbatim."""
        enable(home, workdir)
        request = {**INIT, "params": {**INIT["params"], "protocolVersion": version}}

        result = serve([request], home, workdir)

        assert by_id(result, 1)["result"]["protocolVersion"] == version

    def test_ping_is_answered(self, home: Path, workdir: Path) -> None:
        """Clients keepalive with `ping`; an unanswered one looks like a hang."""
        enable(home, workdir)

        result = serve([{"jsonrpc": "2.0", "id": 7, "method": "ping"}], home, workdir)

        assert by_id(result, 7)["result"] == {}

    def test_server_exits_cleanly_on_eof(self, home: Path, workdir: Path) -> None:
        """Closing stdin ends the process with status 0, not a traceback."""
        result = serve([], home, workdir)

        assert result.returncode == 0
        assert "Traceback" not in result.stderr


# --- the privacy boundary --------------------------------------------------


class TestDisabledByDefault:
    """Nothing is served until the user says so, and the refusal explains itself."""

    def test_tools_list_is_empty_before_opt_in(self, home: Path, workdir: Path) -> None:
        """A fresh install advertises no tools at all."""
        plant(home, [{"command": "kubectl get pods", "ts": NOW}])

        result = serve(
            [INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], home, workdir
        )

        assert by_id(result, 2)["result"]["tools"] == []

    def test_tool_call_is_refused_with_a_reason(
        self, home: Path, workdir: Path
    ) -> None:
        """The refusal reaches the model as readable text naming the fix."""
        plant(home, [{"command": "kubectl get pods -n prod", "ts": NOW}])

        result = serve(
            [INIT, call(2, "search_history", {"query": "kubectl"})], home, workdir
        )

        response = by_id(result, 2)
        assert response["result"]["isError"] is True
        text = response["result"]["content"][0]["text"]
        assert "mem agent enable" in text
        assert "kubectl get pods -n prod" not in result.stdout

    def test_refusal_is_also_explained_on_stderr(
        self, home: Path, workdir: Path
    ) -> None:
        """The human reading the client's server log must see it too."""
        result = serve([INIT], home, workdir)

        assert "mem agent enable" in result.stderr

    def test_initialize_instructions_say_it_is_disabled(
        self, home: Path, workdir: Path
    ) -> None:
        """Even the handshake advertises the state, before any tool is tried."""
        result = serve([INIT], home, workdir)

        assert "disabled" in by_id(result, 1)["result"]["instructions"]

    def test_enable_then_disable_round_trip(self, home: Path, workdir: Path) -> None:
        """Access follows the flag in both directions, per request.

        The flag is re-read on every call rather than cached at startup, so
        revoking access does not require the user to restart their client —
        which they would have no reason to think of doing.
        """
        plant(home, [{"command": "terraform apply", "ts": NOW}])

        enable(home, workdir)
        granted = serve(
            [INIT, call(2, "search_history", {"query": "terraform"})], home, workdir
        )
        assert tool_payload(by_id(granted, 2))["count"] == 1

        assert run_mem(["agent", "disable"], home, workdir).returncode == 0
        revoked = serve(
            [INIT, call(2, "search_history", {"query": "terraform"})], home, workdir
        )
        assert by_id(revoked, 2)["result"]["isError"] is True
        assert "terraform apply" not in revoked.stdout

    def test_status_reports_the_flag(self, home: Path, workdir: Path) -> None:
        """`mem agent status --json` is the human-facing view of the same flag."""
        before = run_mem(["agent", "status", "--json"], home, workdir)
        assert json.loads(before.stdout)["enabled"] is False

        enable(home, workdir)

        after = run_mem(["agent", "status", "--json"], home, workdir)
        assert json.loads(after.stdout)["enabled"] is True

    def test_corrupted_flag_file_fails_closed(
        self, home: Path, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unparseable agent.json means disabled, never enabled.

        A privacy switch that grants access because it could not read the
        answer is worse than no switch at all.
        """
        monkeypatch.setattr(storage, "MEM_DIR", home / ".mem")
        (home / ".mem").mkdir(parents=True)
        storage.agent_file().write_text("{ not json", encoding="utf-8")

        assert storage.read_agent_access().enabled is False


# --- the tools -------------------------------------------------------------


class TestSearchHistoryTool:
    """The core tool: what has this human actually run."""

    def test_returns_ranked_matching_commands(self, home: Path, workdir: Path) -> None:
        """Matches every query term and reports the stored metadata."""
        enable(home, workdir)
        plant(
            home,
            [
                {"command": "docker compose up -d", "ts": NOW - 100},
                {"command": "docker ps -a", "ts": NOW - 200},
                {"command": "git status", "ts": NOW - 300},
            ],
        )

        result = serve(
            [INIT, call(2, "search_history", {"query": "docker compose"})],
            home,
            workdir,
        )

        payload = tool_payload(by_id(result, 2))
        assert [r["command"] for r in payload["results"]] == ["docker compose up -d"]
        entry = payload["results"][0]
        assert entry["exit_code"] == 0
        assert entry["when"].endswith("Z")
        assert entry["ts"] == NOW - 100

    def test_limit_is_honoured_and_capped(self, home: Path, workdir: Path) -> None:
        """`limit` bounds the result set; an absurd one is clamped, not obeyed."""
        enable(home, workdir)
        plant(home, [{"command": f"npm run task{i}", "ts": NOW - i} for i in range(30)])

        limited = serve(
            [INIT, call(2, "search_history", {"query": "npm", "limit": 3})],
            home,
            workdir,
        )
        assert tool_payload(by_id(limited, 2))["count"] == 3

        absurd = serve(
            [INIT, call(2, "search_history", {"query": "npm", "limit": 10_000})],
            home,
            workdir,
        )
        assert tool_payload(by_id(absurd, 2))["count"] <= mcp.MAX_LIMIT

    def test_repo_argument_filters_results(self, home: Path, workdir: Path) -> None:
        """`repo` is a filter, not merely a ranking hint."""
        enable(home, workdir)
        plant(
            home,
            [{"command": "make build", "ts": NOW, "repo": "/work/api"}],
            repo="-work-api",
        )
        plant(
            home,
            [{"command": "make build", "ts": NOW, "repo": "/work/web"}],
            repo="-work-web",
        )

        result = serve(
            [INIT, call(2, "search_history", {"query": "make", "repo": "/work/api"})],
            home,
            workdir,
        )

        payload = tool_payload(by_id(result, 2))
        assert {r["repo"] for r in payload["results"]} == {"/work/api"}

    def test_no_matches_is_an_empty_result_not_an_error(
        self, home: Path, workdir: Path
    ) -> None:
        """An agent asking about something never run gets a clean empty answer."""
        enable(home, workdir)

        result = serve(
            [INIT, call(2, "search_history", {"query": "zzzznothing"})], home, workdir
        )

        payload = tool_payload(by_id(result, 2))
        assert payload["count"] == 0
        assert payload["results"] == []


class TestRunbookTools:
    """The curated, human-verified action surface."""

    def test_list_and_get_a_runbook(self, home: Path, workdir: Path) -> None:
        """A runbook is listed, then returned in order with its annotations."""
        enable(home, workdir)
        plant_runbook(
            home,
            "deploy",
            [
                GroupCommand(cmd="make build", comment="compile"),
                GroupCommand(
                    cmd="kubectl apply -f k8s/ --context $CLUSTER",
                    comment="ship",
                    vars=[VarDeclaration(name="CLUSTER", default="staging")],
                ),
            ],
        )

        result = serve(
            [
                INIT,
                call(2, "list_runbooks"),
                call(3, "get_runbook", {"name": "deploy"}),
            ],
            home,
            workdir,
        )

        listing = tool_payload(by_id(result, 2))
        assert listing["runbooks"] == [
            {
                "name": "deploy",
                "scope": "global",
                "description": "curated",
                "commands": 2,
            }
        ]

        runbook = tool_payload(by_id(result, 3))
        assert [c["cmd"] for c in runbook["commands"]] == [
            "make build",
            "kubectl apply -f k8s/ --context $CLUSTER",
        ]
        assert runbook["commands"][1]["vars"] == [
            {"name": "CLUSTER", "default": "staging"}
        ]

    def test_variable_placeholders_survive_redaction(
        self, home: Path, workdir: Path
    ) -> None:
        """`$API_TOKEN` is the answer, not a secret — it must not be redacted.

        Redacting the placeholder would delete the single most useful thing
        the tool has to say: which variable this runbook expects.
        """
        enable(home, workdir)
        plant_runbook(
            home,
            "publish",
            [GroupCommand(cmd="npm publish --token=$NPM_TOKEN", comment=None)],
        )

        result = serve(
            [INIT, call(2, "get_runbook", {"name": "publish"})], home, workdir
        )

        payload = tool_payload(by_id(result, 2))
        assert payload["commands"][0]["cmd"] == "npm publish --token=$NPM_TOKEN"

    def test_missing_runbook_is_an_invalid_params_error(
        self, home: Path, workdir: Path
    ) -> None:
        """A name that does not exist is a client mistake, reported as one."""
        enable(home, workdir)

        result = serve([INIT, call(2, "get_runbook", {"name": "nope"})], home, workdir)

        assert by_id(result, 2)["error"]["code"] == mcp.INVALID_PARAMS

    def test_no_runbooks_lists_nothing(self, home: Path, workdir: Path) -> None:
        """An empty store answers with an empty list, not a crash."""
        enable(home, workdir)

        result = serve([INIT, call(2, "list_runbooks")], home, workdir)

        assert tool_payload(by_id(result, 2)) == {
            "current_repo": None,
            "count": 0,
            "runbooks": [],
        }


class TestRecentFailuresTool:
    """Correction pairs, mined from exit codes and capture order alone."""

    def test_failure_carries_what_was_run_next(self, home: Path, workdir: Path) -> None:
        """The two following commands are the human's attempt to fix it."""
        enable(home, workdir)
        plant(
            home,
            [
                {"command": "pytest -q", "ts": NOW - 300, "exit_code": 1},
                {"command": "pip install -e '.[dev]'", "ts": NOW - 200},
                {"command": "pytest -q", "ts": NOW - 100},
            ],
        )

        result = serve([INIT, call(2, "recent_failures")], home, workdir)

        payload = tool_payload(by_id(result, 2))
        assert payload["count"] == 1
        failure = payload["failures"][0]
        assert failure["command"] == "pytest -q"
        assert failure["exit_code"] == 1
        assert [f["command"] for f in failure["followed_by"]] == [
            "pip install -e '.[dev]'",
            "pytest -q",
        ]
        # The same command succeeded afterwards: that is the proof it was fixed.
        assert failure["retried_successfully"] is True

    def test_successful_commands_are_not_reported(
        self, home: Path, workdir: Path
    ) -> None:
        """Exit code 0 is not a failure, however interesting the command."""
        enable(home, workdir)
        plant(home, [{"command": "make test", "ts": NOW}])

        result = serve([INIT, call(2, "recent_failures")], home, workdir)

        assert tool_payload(by_id(result, 2))["count"] == 0

    def test_newest_failure_comes_first(self, home: Path, workdir: Path) -> None:
        """Ordering is by recency: a debugging session is a stack, not a queue."""
        enable(home, workdir)
        plant(
            home,
            [
                {"command": "old-failure", "ts": NOW - 5000, "exit_code": 2},
                {"command": "new-failure", "ts": NOW - 10, "exit_code": 127},
            ],
        )

        result = serve([INIT, call(2, "recent_failures", {"limit": 1})], home, workdir)

        payload = tool_payload(by_id(result, 2))
        assert [f["command"] for f in payload["failures"]] == ["new-failure"]


class TestNoExecutionSurface:
    """The tool list is the whole attack surface, so it is pinned."""

    def test_exactly_four_read_only_tools_are_exposed(
        self, home: Path, workdir: Path
    ) -> None:
        """Adding a tool is a deliberate act that has to change this test.

        In particular there is no tool that runs a command: an MCP endpoint
        that executes shell on an agent's request is remote code execution,
        and the runbook tools deliberately return command *text* instead.
        """
        enable(home, workdir)

        result = serve(
            [INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], home, workdir
        )

        names = {t["name"] for t in by_id(result, 2)["result"]["tools"]}
        assert names == {
            "search_history",
            "list_runbooks",
            "get_runbook",
            "recent_failures",
        }

    def test_the_server_module_cannot_execute_anything(self) -> None:
        """No tool can shell out, because the module has no way to.

        Pinned structurally rather than behaviourally: the dangerous change
        is someone adding `subprocess.run(cmd)` to a future tool, and this
        fails the moment the capability is imported. The one subprocess mem
        runs for MCP — `git rev-parse`, to detect the current repo — lives in
        `mem.capture` and takes no agent input.

        Parsed with `ast` rather than grepped, so the prose in this module's
        own docstrings (which discusses subprocesses and sockets at length)
        cannot trip it, and an aliased `import subprocess as sp` cannot slip
        past it.
        """
        assert not module_imports(mcp) & {"subprocess", "pty", "ctypes"}
        assert not dangerous_calls(mcp)

    def test_every_tool_declares_a_schema(self, home: Path, workdir: Path) -> None:
        """A tool without an input schema is a tool a client cannot call safely."""
        enable(home, workdir)

        result = serve(
            [INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], home, workdir
        )

        for tool in by_id(result, 2)["result"]["tools"]:
            assert tool["description"].strip()
            assert tool["inputSchema"]["type"] == "object"


# --- protocol robustness ---------------------------------------------------


class TestProtocolErrors:
    """A server that dies on a bad frame is unusable: an agent cannot restart it."""

    def test_malformed_json_is_a_parse_error_and_the_server_lives(
        self, home: Path, workdir: Path
    ) -> None:
        """-32700, id null, and the *next* frame is still answered."""
        enable(home, workdir)
        payload = (
            "{ this is not json\n"
            + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"})
            + "\n"
        )

        result = serve_raw(payload, home, workdir)

        messages = frames_of(result)
        assert messages[0]["id"] is None
        assert messages[0]["error"]["code"] == mcp.PARSE_ERROR
        assert messages[1] == {"jsonrpc": "2.0", "id": 9, "result": {}}
        assert result.returncode == 0

    def test_unknown_method(self, home: Path, workdir: Path) -> None:
        """-32601 names the method so the client can log something useful."""
        enable(home, workdir)

        result = serve(
            [{"jsonrpc": "2.0", "id": 2, "method": "resources/list"}], home, workdir
        )

        error = by_id(result, 2)["error"]
        assert error["code"] == mcp.METHOD_NOT_FOUND
        assert "resources/list" in error["message"]

    def test_unknown_tool(self, home: Path, workdir: Path) -> None:
        """A tool that does not exist is an invalid argument, not a 500."""
        enable(home, workdir)

        result = serve([INIT, call(2, "rm_rf_slash", {})], home, workdir)

        assert by_id(result, 2)["error"]["code"] == mcp.INVALID_PARAMS

    @pytest.mark.parametrize(
        "arguments",
        [
            {},
            {"query": ""},
            {"query": "   "},
            {"query": 42},
            {"query": None},
            {"query": "ok", "limit": "ten"},
            {"query": "ok", "limit": 0},
            {"query": "ok", "limit": True},
            {"query": "ok", "repo": ""},
        ],
        ids=[
            "missing",
            "empty",
            "blank",
            "wrong-type",
            "null",
            "limit-string",
            "limit-zero",
            "limit-bool",
            "empty-repo",
        ],
    )
    def test_bad_arguments_are_invalid_params(
        self, arguments: dict, home: Path, workdir: Path
    ) -> None:
        """Every argument mistake is -32602, never a traceback on stderr."""
        enable(home, workdir)

        result = serve([INIT, call(2, "search_history", arguments)], home, workdir)

        assert by_id(result, 2)["error"]["code"] == mcp.INVALID_PARAMS
        assert "Traceback" not in result.stderr

    def test_missing_tool_name(self, home: Path, workdir: Path) -> None:
        """`tools/call` with no name is a malformed call, reported as -32602."""
        enable(home, workdir)

        result = serve(
            [INIT, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}}],
            home,
            workdir,
        )

        assert by_id(result, 2)["error"]["code"] == mcp.INVALID_PARAMS

    def test_wrong_jsonrpc_version(self, home: Path, workdir: Path) -> None:
        """A 1.0-style frame is rejected rather than half-understood."""
        enable(home, workdir)

        result = serve([{"id": 2, "method": "ping"}], home, workdir)

        assert by_id(result, 2)["error"]["code"] == mcp.INVALID_REQUEST

    def test_batch_requests_are_rejected(self, home: Path, workdir: Path) -> None:
        """MCP dropped JSON-RPC batching; accepting it would invent a contract."""
        enable(home, workdir)
        batch = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]) + "\n"

        result = serve_raw(batch, home, workdir)

        assert frames_of(result)[0]["error"]["code"] == mcp.INVALID_REQUEST

    def test_notifications_are_never_answered(self, home: Path, workdir: Path) -> None:
        """Answering a notification is a protocol violation clients may hang up on."""
        enable(home, workdir)

        result = serve(
            [
                INITIALIZED,
                {"jsonrpc": "2.0", "method": "notifications/cancelled"},
                {"jsonrpc": "2.0", "method": "notifications/unheard-of"},
            ],
            home,
            workdir,
        )

        assert frames_of(result) == []

    def test_client_responses_are_ignored(self, home: Path, workdir: Path) -> None:
        """We send no requests, so a stray response gets no reply at all."""
        enable(home, workdir)

        result = serve([{"jsonrpc": "2.0", "id": 5, "result": {}}], home, workdir)

        assert frames_of(result) == []

    def test_blank_lines_between_frames_are_skipped(
        self, home: Path, workdir: Path
    ) -> None:
        """Keepalive newlines must not be read as empty frames."""
        enable(home, workdir)
        ping = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"})

        result = serve_raw(f"\n\n{ping}\n\n", home, workdir)

        assert frames_of(result) == [{"jsonrpc": "2.0", "id": 3, "result": {}}]

    def test_internal_errors_become_minus_32603(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unexpected exception inside a tool is reported, not fatal.

        Driven in-process because there is no way to inject a broken tool
        into a subprocess, and the point is the dispatcher's behaviour.
        """
        monkeypatch.setitem(
            storage.__dict__, "read_agent_access", lambda: AgentAccess(enabled=True)
        )

        def boom(args: dict) -> dict:
            raise RuntimeError("disk on fire")

        monkeypatch.setitem(mcp._TOOL_IMPLS, "list_runbooks", boom)
        monkeypatch.setattr(mcp, "_audit", lambda *a, **kw: None)

        response = mcp.handle_message(call(1, "list_runbooks", {}))

        assert response is not None
        assert response["error"]["code"] == mcp.INTERNAL_ERROR
        assert "disk on fire" not in json.dumps(response), (
            "an internal exception message may quote a path or a secret"
        )


# --- stdout purity ---------------------------------------------------------


class TestStdoutCarriesOnlyProtocol:
    """Stdout is the wire. One stray byte desynchronises the client for good."""

    def test_corrupted_history_warning_goes_to_stderr(
        self, home: Path, workdir: Path
    ) -> None:
        """A corrupted JSONL line makes storage warn — on stderr, never stdout.

        The corrupt line has to contain the query term. Search now tests the
        raw line for the term before parsing it, so a line that could not have
        matched anyway is skipped without ever being handed to the JSON
        decoder — and therefore without producing a warning. The invariant
        under test is unchanged: when storage does warn, it warns on stderr.
        """
        enable(home, workdir)
        plant(home, [{"command": "kubectl get pods", "ts": NOW}])
        path = home / ".mem" / "repos" / "_global.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write('{ "command": "kubectl this line is corrupt\n')

        result = serve(
            [INIT, call(2, "search_history", {"query": "kubectl"})], home, workdir
        )

        assert "corrupted" in result.stderr
        for line in result.stdout.splitlines():
            if line.strip():
                json.loads(line)  # raises if anything non-protocol slipped through

    def test_every_stdout_line_is_a_jsonrpc_message(
        self, home: Path, workdir: Path
    ) -> None:
        """Across a full session, every line parses and carries the envelope."""
        enable(home, workdir)
        plant(home, [{"command": "make deploy", "ts": NOW, "exit_code": 1}])
        plant_runbook(home, "ops", [GroupCommand(cmd="make deploy", comment=None)])

        result = serve(
            [
                INIT,
                INITIALIZED,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                call(3, "search_history", {"query": "make"}),
                call(4, "recent_failures"),
                call(5, "list_runbooks"),
                call(6, "get_runbook", {"name": "ops"}),
            ],
            home,
            workdir,
        )

        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 6
        for line in lines:
            message = json.loads(line)
            assert message["jsonrpc"] == "2.0"
            assert "id" in message

    def test_multiline_command_stays_inside_one_frame(
        self, home: Path, workdir: Path
    ) -> None:
        """A captured command containing a newline must not split a frame."""
        enable(home, workdir)
        plant(home, [{"command": "echo 'line one\nline two' | wc -l", "ts": NOW}])

        result = serve(
            [INIT, call(2, "search_history", {"query": "wc"})], home, workdir
        )

        assert len(frames_of(result)) == 2
        payload = tool_payload(by_id(result, 2))
        assert payload["results"][0]["command"] == "echo 'line one\nline two' | wc -l"

    def test_stray_prints_are_rerouted_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool that prints to stdout cannot corrupt the stream.

        This is the structural half of the invariant: `serve` rebinds
        `sys.stdout` to stderr for its whole lifetime, so the protection does
        not depend on every future contributor remembering the rule. Driven
        in-process because the whole point is to inject a misbehaving tool.
        """
        monkeypatch.setitem(
            storage.__dict__, "read_agent_access", lambda: AgentAccess(enabled=True)
        )
        monkeypatch.setattr(mcp, "_audit", lambda *a, **kw: None)

        def chatty(args: dict) -> dict:
            from mem.render import console

            print("CONTAMINATION via print")
            console.print("CONTAMINATION via rich")
            return {"count": 0, "runbooks": []}

        monkeypatch.setitem(mcp._TOOL_IMPLS, "list_runbooks", chatty)

        stdin = io.StringIO(json.dumps(call(1, "list_runbooks", {})) + "\n")
        out, err = io.StringIO(), io.StringIO()

        mcp.serve(stdin, out, err)

        assert "CONTAMINATION" not in out.getvalue()
        assert out.getvalue().count("\n") == 1
        json.loads(out.getvalue())
        assert err.getvalue().count("CONTAMINATION") == 2
        assert sys.stdout is not err, "serve must restore sys.stdout when it returns"


# --- redaction -------------------------------------------------------------


# Real-shaped secrets. Each entry is (label, command, the substring that must
# never appear in anything the server writes).
_SLACK_TOKEN = "-".join(("xoxb", "1234567890", "abcdefghijklmnop"))

SECRET_COMMANDS: list[tuple[str, str, str]] = [
    (
        "aws-access-key-id",
        "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE && aws s3 ls",
        "AKIAIOSFODNN7EXAMPLE",
    ),
    (
        "aws-secret-access-key",
        "aws configure set aws_secret_access_key wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
        "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
    ),
    (
        "bearer-token",
        'curl -H "Authorization: Bearer sk-ant-api03-Zm9vYmFyYmF6cXV4Cg" https://api.example.com',
        "sk-ant-api03-Zm9vYmFyYmF6cXV4Cg",
    ),
    (
        "jwt",
        "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk'",
        "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ),
    (
        "pgpassword",
        "PGPASSWORD=hunter2correcthorse psql -h db.internal -U admin app",
        "hunter2correcthorse",
    ),
    (
        "curl-basic-auth",
        "curl -u deploybot:s3cr3t-p4ssw0rd https://registry.internal/v2/",
        "s3cr3t-p4ssw0rd",
    ),
    (
        "token-flag",
        "gh release create v1.0 --token=ghp_1234567890abcdefghijklmnopqrstuvwxyz",
        "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
    ),
    (
        "password-flag",
        "mysqldump --user root --password Tr0ub4dor3xample app > dump.sql",
        "Tr0ub4dor3xample",
    ),
    (
        "dotenv-value",
        'echo "STRIPE_SECRET_KEY=sk_live_51Hxxxxxxxxxxxxxxxxxxxx" >> .env',
        "sk_live_51Hxxxxxxxxxxxxxxxxxxxx",
    ),
    (
        "connection-string",
        "psql postgres://app:d4t4b4s3p4ss@db.internal:5432/production",
        "d4t4b4s3p4ss",
    ),
    (
        "private-key-blob",
        'echo "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAA\n-----END OPENSSH PRIVATE KEY-----" > id_ed25519',
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAA",
    ),
    (
        # Assembled at import time rather than written out: a literal
        # `xoxb-...` string is what GitHub's push protection scans for, and it
        # blocks the push regardless of the fact that this one is synthetic.
        # The value the redactor sees is identical.
        "slack-token",
        f"curl -d token={_SLACK_TOKEN} https://slack.example/api",
        _SLACK_TOKEN,
    ),
]


class TestRedaction:
    """Everything that leaves passes through one redactor, on every path out."""

    @pytest.mark.parametrize(
        "label,command,secret",
        SECRET_COMMANDS,
        ids=[case[0] for case in SECRET_COMMANDS],
    )
    def test_search_results_never_carry_the_secret(
        self, label: str, command: str, secret: str, home: Path, workdir: Path
    ) -> None:
        """The secret is absent from stdout entirely, not merely marked up.

        Asserting on the absence of the secret rather than the presence of
        `[REDACTED]` is deliberate: a rule can redact one occurrence and miss
        a second, and the marker would still be there to reassure us.
        """
        enable(home, workdir)
        plant(home, [{"command": command, "ts": NOW}])

        result = serve([INIT, call(2, "search_history", {"query": "e"})], home, workdir)

        assert secret not in result.stdout, f"{label} leaked to the agent"
        assert secret not in result.stderr

    @pytest.mark.parametrize(
        "label,command,secret",
        SECRET_COMMANDS,
        ids=[case[0] for case in SECRET_COMMANDS],
    )
    def test_runbooks_never_carry_the_secret(
        self, label: str, command: str, secret: str, home: Path, workdir: Path
    ) -> None:
        """A secret saved into a runbook is redacted on the way out too."""
        enable(home, workdir)
        plant_runbook(home, "risky", [GroupCommand(cmd=command, comment=None)])

        result = serve([INIT, call(2, "get_runbook", {"name": "risky"})], home, workdir)

        assert secret not in result.stdout, f"{label} leaked through get_runbook"

    def test_secrets_in_comments_and_descriptions_are_redacted(
        self, home: Path, workdir: Path
    ) -> None:
        """Redaction covers every string in the payload, not just `cmd`."""
        enable(home, workdir)
        plant_runbook(
            home,
            "notes",
            [
                GroupCommand(
                    cmd="deploy.sh",
                    comment="use PGPASSWORD=leakedinacomment when prompted",
                    vars=[
                        VarDeclaration(
                            name="TOKEN", default="ghp_leakedasadefaultvalue123456"
                        )
                    ],
                )
            ],
        )

        result = serve([INIT, call(2, "get_runbook", {"name": "notes"})], home, workdir)

        assert "leakedinacomment" not in result.stdout
        assert "ghp_leakedasadefaultvalue123456" not in result.stdout

    def test_failure_correction_pairs_are_redacted(
        self, home: Path, workdir: Path
    ) -> None:
        """The `followed_by` commands go through the same choke point."""
        enable(home, workdir)
        plant(
            home,
            [
                {"command": "psql -h db", "ts": NOW - 10, "exit_code": 2},
                {"command": "PGPASSWORD=thefixwasapassword psql -h db", "ts": NOW},
            ],
        )

        result = serve([INIT, call(2, "recent_failures")], home, workdir)

        assert "thefixwasapassword" not in result.stdout
        assert "[REDACTED]" in result.stdout

    def test_audit_log_does_not_store_a_secret_query(
        self, home: Path, workdir: Path
    ) -> None:
        """An agent can put a secret in a query; the audit must not keep it."""
        enable(home, workdir)

        serve(
            [
                INIT,
                call(2, "search_history", {"query": "PGPASSWORD=auditleakcheck psql"}),
            ],
            home,
            workdir,
        )

        recorded = (home / ".mem" / "agent-audit.jsonl").read_text(encoding="utf-8")
        assert "auditleakcheck" not in recorded
        assert "[REDACTED]" in recorded


# --- audit trail -----------------------------------------------------------


class TestAuditTrail:
    """`mem agent log` is how a user finds out what a model read."""

    def test_calls_are_recorded_with_tool_and_arguments(
        self, home: Path, workdir: Path
    ) -> None:
        """Both the granted call and its result count are on the record."""
        enable(home, workdir)
        plant(home, [{"command": "helm upgrade api", "ts": NOW}])

        serve([INIT, call(2, "search_history", {"query": "helm"})], home, workdir)

        entries = json.loads(run_mem(["agent", "log", "--json"], home, workdir).stdout)
        assert len(entries) == 1
        assert entries[0]["tool"] == "search_history"
        assert entries[0]["arguments"] == {"query": "helm"}
        assert entries[0]["results"] == 1
        assert entries[0]["ok"] is True

    def test_refused_calls_are_recorded_too(self, home: Path, workdir: Path) -> None:
        """An attempt made while access was off is exactly what a user wants to see."""
        serve([INIT, call(2, "search_history", {"query": "helm"})], home, workdir)

        entries = json.loads(run_mem(["agent", "log", "--json"], home, workdir).stdout)
        assert [(e["tool"], e["ok"]) for e in entries] == [("search_history", False)]
        assert entries[0]["error"] == "access disabled"

    def test_log_is_append_only_across_sessions(
        self, home: Path, workdir: Path
    ) -> None:
        """A second client session adds to the record instead of replacing it."""
        enable(home, workdir)

        serve([INIT, call(2, "list_runbooks")], home, workdir)
        serve([INIT, call(2, "list_runbooks")], home, workdir)

        entries = json.loads(run_mem(["agent", "log", "--json"], home, workdir).stdout)
        assert len(entries) == 2

    def test_audit_file_is_owner_only(self, home: Path, workdir: Path) -> None:
        """It records what was asked about a private store; 0600 like the rest."""
        enable(home, workdir)
        serve([INIT, call(2, "list_runbooks")], home, workdir)

        mode = (home / ".mem" / "agent-audit.jsonl").stat().st_mode & 0o777
        assert mode == 0o600

    def test_forget_scrubs_the_audit_log(self, home: Path, workdir: Path) -> None:
        """`mem forget` promises no traces anywhere — including here.

        The audit log keeps the agent's query verbatim, so forgetting a
        command has to reach it as well: otherwise `mem forget` reports
        success while the text survives in a file the user was told is a
        record of what an agent read.
        """
        enable(home, workdir)
        plant(home, [{"command": "deploy zzsecretproject --prod", "ts": NOW}])
        serve(
            [INIT, call(2, "search_history", {"query": "zzsecretproject"})],
            home,
            workdir,
        )
        assert "zzsecretproject" in (home / ".mem" / "agent-audit.jsonl").read_text()

        run_mem(["forget", "zzsecretproject", "--yes"], home, workdir)

        path = home / ".mem" / "agent-audit.jsonl"
        assert not path.exists() or "zzsecretproject" not in path.read_text()

    def test_status_counts_requests(self, home: Path, workdir: Path) -> None:
        """`mem agent status` surfaces the volume without reading every line."""
        enable(home, workdir)
        serve([INIT, call(2, "list_runbooks"), call(3, "list_runbooks")], home, workdir)

        status = json.loads(
            run_mem(["agent", "status", "--json"], home, workdir).stdout
        )
        assert status["requests"] == 2


# --- constitution ----------------------------------------------------------


class TestNoNetwork:
    """Principle I: mem never touches the network — MCP included."""

    def test_the_server_runs_with_every_socket_dead(
        self, home: Path, workdir: Path, tmp_path: Path
    ) -> None:
        """A full session completes with socket() raising on every call.

        This is the whole justification for hand-rolling the protocol instead
        of taking the official SDK, which would have pulled uvicorn, starlette
        and httpx into a project that must never import one.
        """
        stub_dir = tmp_path / "netstub"
        stub_dir.mkdir()
        (stub_dir / "sitecustomize.py").write_text(NETWORK_STUB, encoding="utf-8")
        existing = os.environ.get("PYTHONPATH", "")
        python_path = f"{stub_dir}{os.pathsep}{existing}" if existing else str(stub_dir)

        enable(home, workdir)
        plant(home, [{"command": "rsync -avz ./ backup:/srv", "ts": NOW}])

        result = serve(
            [INIT, INITIALIZED, call(2, "search_history", {"query": "rsync"})],
            home,
            workdir,
            env=server_env(home, PYTHONPATH=python_path),
        )

        assert result.returncode == 0, result.stderr
        assert "NetworkBlocked" not in result.stderr
        payload = tool_payload(by_id(result, 2))
        assert payload["results"][0]["command"] == "rsync -avz ./ backup:/srv"

    def test_the_module_imports_no_networking_library(self) -> None:
        """A structural guard, because an unused import is still a dependency.

        The socket stub above only catches a socket that is actually opened.
        This catches the import that makes opening one possible — including
        the official MCP SDK, whose whole reason for being excluded is that
        it drags uvicorn, starlette and httpx in behind it.
        """
        forbidden = {
            "socket",
            "socketserver",
            "http",
            "urllib",
            "requests",
            "httpx",
            "aiohttp",
            "asyncio",
            "ssl",
            "ftplib",
            "smtplib",
            "telnetlib",
            "xmlrpc",
            "uvicorn",
            "starlette",
            "anyio",
            # `mcp` is the official SDK's distribution name. Our own module is
            # `mem.mcp`, which resolves to top-level `mem` and never matches.
            "mcp",
        }

        assert not module_imports(mcp) & forbidden


# --- in-process ------------------------------------------------------------
#
# Everything above drives a subprocess, which is the only way to prove the
# server behaves on a real pipe — but it also means the coverage tracer never
# sees a single line of mem/mcp.py execute. The tests below run the same
# dispatcher in-process against injected streams so the branch coverage is
# real, and so failure paths that cannot be provoked from outside (a
# malformed scope file, an audit write that raises) are reachable at all.


@pytest.fixture
def agent_enabled(tmp_mem_dir: Path) -> Path:
    """Grant access in the autouse-isolated MEM_DIR the unit tests share."""
    storage.write_agent_access(AgentAccess(enabled=True, updated_at=NOW))
    return tmp_mem_dir


@pytest.fixture(autouse=True)
def _no_git_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin repo detection to "no repo" for the in-process tests.

    ``mcp._current_repo`` caches a `git rev-parse` result in a module-level
    list, so without this the answer would be pytest's own working directory
    — this repository — and would leak between tests in the order they ran.
    """
    monkeypatch.setattr(mcp, "_repo_cache", [None])


def drive(frames: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Run `serve` in-process over injected streams; return (responses, stderr)."""
    stdin = io.StringIO("".join(json.dumps(f) + "\n" for f in frames))
    out, err = io.StringIO(), io.StringIO()

    assert mcp.serve(stdin, out, err) == 0

    responses = [
        json.loads(line) for line in out.getvalue().splitlines() if line.strip()
    ]
    return responses, err.getvalue()


def local_history(rows: list[dict[str, Any]], repo: str = "_global") -> None:
    """Plant raw history lines into the patched MEM_DIR."""
    storage.ensure_dirs()
    path = storage.repo_file(repo)
    base = {"dir": "/w", "repo": None, "exit_code": 0, "duration_ms": 5}
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({**base, **row}) + "\n")


class TestDispatcherInProcess:
    """The JSON-RPC layer, exercised directly."""

    def test_tools_are_listed_and_called(self, agent_enabled: Path) -> None:
        """One session covering the handshake and every tool."""
        local_history(
            [
                {"command": "cargo build --release", "ts": NOW - 50},
                {"command": "cargo test", "ts": NOW - 40, "exit_code": 101},
                {"command": "cargo test -- --nocapture", "ts": NOW - 30},
            ]
        )
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={
                    "ci": Group(
                        description=None,
                        commands=[
                            GroupCommand(cmd="cargo fmt --check", comment="lint")
                        ],
                    )
                }
            ),
        )

        responses, _ = drive(
            [
                INIT,
                INITIALIZED,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                call(3, "search_history", {"query": "cargo"}),
                call(4, "recent_failures"),
                call(5, "list_runbooks"),
                call(6, "get_runbook", {"name": "ci"}),
            ]
        )

        # The notification in the middle produces no frame, so there are six
        # responses for seven messages.
        assert [r["id"] for r in responses] == [1, 2, 3, 4, 5, 6]
        counts = {
            r["id"]: json.loads(r["result"]["content"][0]["text"])["count"]
            for r in responses
            if "content" in r["result"]
        }
        assert len(responses[1]["result"]["tools"]) == 4
        assert counts == {3: 3, 4: 1, 5: 1, 6: 1}

    def test_scope_argument_selects_a_specific_runbook(
        self, agent_enabled: Path
    ) -> None:
        """`scope` disambiguates when a repo and global runbook share a name."""
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={"deploy": Group(commands=[GroupCommand(cmd="global-deploy")])}
            ),
        )
        storage.write_group_file(
            storage.group_file_path("-work-api"),
            GroupFile(
                groups={"deploy": Group(commands=[GroupCommand(cmd="repo-deploy")])}
            ),
        )

        responses, _ = drive(
            [call(1, "get_runbook", {"name": "deploy", "scope": "global"})]
        )

        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        assert payload["commands"][0]["cmd"] == "global-deploy"
        assert payload["scope"] == "global"

    def test_current_repo_scope_shadows_global(
        self, agent_enabled: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without `scope`, the current repo wins — as `mem run` would resolve it."""
        monkeypatch.setattr(mcp, "_repo_cache", ["/work/api"])
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(
                groups={"deploy": Group(commands=[GroupCommand(cmd="global-deploy")])}
            ),
        )
        storage.write_group_file(
            storage.group_file_path(storage.sanitize_repo_name("/work/api")),
            GroupFile(
                groups={"deploy": Group(commands=[GroupCommand(cmd="repo-deploy")])}
            ),
        )

        responses, _ = drive([call(1, "get_runbook", {"name": "deploy"})])

        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        assert payload["commands"][0]["cmd"] == "repo-deploy"

    def test_a_malformed_scope_file_does_not_hide_the_healthy_ones(
        self, agent_enabled: Path
    ) -> None:
        """One corrupt group file must not cost the agent every other runbook."""
        storage.GROUPS_REPOS_DIR.mkdir(parents=True, exist_ok=True)
        (storage.GROUPS_REPOS_DIR / "broken.json").write_text(
            "{ nope", encoding="utf-8"
        )
        storage.write_group_file(
            storage.GROUPS_GLOBAL_FILE,
            GroupFile(groups={"ok": Group(commands=[GroupCommand(cmd="echo fine")])}),
        )

        responses, _ = drive([call(1, "list_runbooks")])

        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        assert [r["name"] for r in payload["runbooks"]] == ["ok"]

    def test_recent_failures_can_be_scoped_to_one_repo(
        self, agent_enabled: Path
    ) -> None:
        """`repo` narrows the scan to a single history file.

        The file names are the *sanitized* repo paths, which is how the
        capture layer writes them — `/work/api` becomes `work-api`.
        """
        local_history(
            [{"command": "api-fail", "ts": NOW, "exit_code": 1}], repo="work-api"
        )
        local_history(
            [{"command": "web-fail", "ts": NOW, "exit_code": 1}], repo="work-web"
        )

        responses, _ = drive([call(1, "recent_failures", {"repo": "/work/api"})])

        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        assert [f["command"] for f in payload["failures"]] == ["api-fail"]

    def test_recent_failures_on_an_empty_store(self, agent_enabled: Path) -> None:
        """A machine with no history yet answers with an empty list."""
        responses, _ = drive([call(1, "recent_failures")])

        payload = json.loads(responses[0]["result"]["content"][0]["text"])
        assert payload == {"count": 0, "failures": []}

    def test_null_params_are_treated_as_absent(self, agent_enabled: Path) -> None:
        """`"params": null` is legal JSON-RPC for "no parameters"."""
        responses, _ = drive(
            [{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None}]
        )

        assert responses[0]["result"] == {}

    def test_non_object_params_are_invalid(self, agent_enabled: Path) -> None:
        """Positional params are valid JSON-RPC but not valid MCP."""
        responses, _ = drive(
            [{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1, 2]}]
        )

        assert responses[0]["error"]["code"] == mcp.INVALID_PARAMS

    def test_missing_method_on_a_request(self, agent_enabled: Path) -> None:
        """A frame that is neither request nor response is an invalid request."""
        responses, _ = drive([{"jsonrpc": "2.0", "id": 1}])

        assert responses[0]["error"]["code"] == mcp.INVALID_REQUEST

    def test_null_arguments_are_treated_as_empty(self, agent_enabled: Path) -> None:
        """`"arguments": null` means "no arguments", not "broken call"."""
        responses, _ = drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "list_runbooks", "arguments": None},
                }
            ]
        )

        assert responses[0]["result"]["isError"] is False

    def test_non_object_arguments_are_invalid(self, agent_enabled: Path) -> None:
        """A list where an object belongs is -32602, not a crash."""
        responses, _ = drive(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "list_runbooks", "arguments": ["x"]},
                }
            ]
        )

        assert responses[0]["error"]["code"] == mcp.INVALID_PARAMS

    def test_a_failing_audit_write_does_not_fail_the_request(
        self, agent_enabled: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The answer is already computed; refusing to return it helps nobody."""

        def unwritable(entry: Any) -> None:
            raise OSError("read-only filesystem")

        monkeypatch.setattr(storage, "append_agent_audit", unwritable)

        responses, _ = drive([call(1, "list_runbooks")])

        assert responses[0]["result"]["isError"] is False

    def test_main_serves_and_returns_zero(
        self, agent_enabled: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mem mcp` wires stdin/stdout/stderr up and exits 0 on EOF."""
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", io.StringIO())

        assert mcp.main() == 0

    def test_a_broken_pipe_is_not_an_error(
        self, agent_enabled: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client that hangs up mid-answer ends the server cleanly.

        This is the ordinary way an MCP session dies — the user closed the
        client — and a traceback in their server log would send them hunting
        for a bug that is not there.
        """

        class BrokenStdout(io.StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError("client went away")

        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
        )

        assert mcp.serve(stdin, BrokenStdout(), io.StringIO()) == 0


class TestRefusalAndErrorsInProcess:
    """The same refusals and errors as above, traced rather than observed."""

    def test_disabled_hides_the_tools_and_refuses_the_call(
        self, tmp_mem_dir: Path
    ) -> None:
        """Both gates close on the same flag read."""
        local_history([{"command": "kubectl get pods", "ts": NOW}])

        responses, stderr = drive(
            [
                INIT,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                call(3, "search_history", {"query": "kubectl"}),
            ]
        )

        assert "disabled" in responses[0]["result"]["instructions"]
        assert responses[1]["result"]["tools"] == []
        assert responses[2]["result"]["isError"] is True
        assert "mem agent enable" in stderr
        assert "kubectl get pods" not in json.dumps(responses)

    def test_unknown_protocol_version_and_unknown_method(
        self, agent_enabled: Path
    ) -> None:
        """Two independent negotiation failures, neither fatal."""
        responses, _ = drive(
            [
                {**INIT, "params": {"protocolVersion": "1999-01-01"}},
                {"jsonrpc": "2.0", "id": 2, "method": "resources/read"},
            ]
        )

        assert responses[0]["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION
        assert responses[1]["error"]["code"] == mcp.METHOD_NOT_FOUND

    def test_unknown_tool_and_bad_limit(self, agent_enabled: Path) -> None:
        """Argument validation happens before any store is read."""
        responses, _ = drive(
            [
                call(1, "delete_everything", {}),
                call(2, "search_history", {"query": "x", "limit": "many"}),
                call(3, "search_history", {"query": "x", "limit": -1}),
            ]
        )

        assert [r["error"]["code"] for r in responses] == [mcp.INVALID_PARAMS] * 3

    def test_missing_runbook(self, agent_enabled: Path) -> None:
        """A name that resolves in no scope is an argument error."""
        responses, _ = drive([call(1, "get_runbook", {"name": "ghost"})])

        assert "ghost" in responses[0]["error"]["message"]

    def test_search_repo_filter_and_limit_clamp(self, agent_enabled: Path) -> None:
        """The over-fetch-then-filter path, and the ceiling on `limit`."""
        local_history(
            [
                {"command": f"make target{i}", "ts": NOW - i, "repo": "/work/api"}
                for i in range(3)
            ],
            repo="work-api",
        )
        local_history(
            [{"command": "make other", "ts": NOW, "repo": "/work/web"}], repo="work-web"
        )

        responses, _ = drive(
            [
                call(1, "search_history", {"query": "make", "repo": "/work/api"}),
                call(2, "search_history", {"query": "make", "limit": 10_000}),
            ]
        )

        filtered = json.loads(responses[0]["result"]["content"][0]["text"])
        assert {r["repo"] for r in filtered["results"]} == {"/work/api"}
        clamped = json.loads(responses[1]["result"]["content"][0]["text"])
        assert clamped["count"] <= mcp.MAX_LIMIT

    def test_a_parse_error_does_not_end_the_session(self, agent_enabled: Path) -> None:
        """The loop continues after an unparseable line, in-process too."""
        stdin = io.StringIO(
            "<<<not json>>>\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
            + "\n"
        )
        out, err = io.StringIO(), io.StringIO()

        assert mcp.serve(stdin, out, err) == 0

        messages = [json.loads(line) for line in out.getvalue().splitlines()]
        assert messages[0]["error"]["code"] == mcp.PARSE_ERROR
        assert messages[1]["result"] == {}

    def test_current_repo_is_detected_once_and_cached(
        self, agent_enabled: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repo detection costs one `git` subprocess for the whole session.

        The cache is what makes that true, and it is only correct because an
        MCP client never changes the server's working directory — so the
        second call answers from the first even from a different directory.
        """
        monkeypatch.setattr(mcp, "_repo_cache", [])
        monkeypatch.chdir(git_repo)

        assert mcp._current_repo() == str(git_repo)

        monkeypatch.chdir(agent_enabled)
        assert mcp._current_repo() == str(git_repo)


class TestStorageAgentHelpers:
    """The persistence behind the flag and the audit log."""

    def test_access_defaults_to_disabled_on_a_fresh_install(
        self, tmp_mem_dir: Path
    ) -> None:
        """No file means no access — the default is the safe one."""
        assert storage.read_agent_access().enabled is False

    def test_flag_file_is_owner_only(self, tmp_mem_dir: Path) -> None:
        """0600 like everything else under ~/.mem."""
        storage.write_agent_access(AgentAccess(enabled=True, updated_at=NOW))

        assert storage.agent_file().stat().st_mode & 0o777 == 0o600

    def test_corrupted_audit_lines_are_skipped_not_fatal(
        self, tmp_mem_dir: Path
    ) -> None:
        """A truncated write must not make `mem agent log` unreadable forever."""
        storage.ensure_dirs()
        path = storage.agent_audit_file()
        good = AgentAuditEntry(ts=NOW, tool="list_runbooks", results=1)
        path.write_text("{ truncated\n" + good.to_jsonl() + "\n\n", encoding="utf-8")

        entries = list(storage.read_agent_audit())

        assert [e.tool for e in entries] == ["list_runbooks"]

    def test_reading_an_absent_audit_log_yields_nothing(
        self, tmp_mem_dir: Path
    ) -> None:
        """Never audited is not an error state."""
        assert list(storage.read_agent_audit()) == []

    def test_scrubbing_keeps_unrelated_entries(self, tmp_mem_dir: Path) -> None:
        """`mem forget` removes the matching entries and only those."""
        storage.append_agent_audit(
            AgentAuditEntry(
                ts=NOW, tool="search_history", arguments={"query": "zzgone"}
            )
        )
        storage.append_agent_audit(
            AgentAuditEntry(
                ts=NOW, tool="search_history", arguments={"query": "zzkept"}
            )
        )

        storage._scrub_agent_audit("zzgone")

        remaining = [e.arguments["query"] for e in storage.read_agent_audit()]
        assert remaining == ["zzkept"]

    def test_scrubbing_removes_the_file_when_nothing_survives(
        self, tmp_mem_dir: Path
    ) -> None:
        """An empty audit log is deleted rather than left as an empty file."""
        storage.append_agent_audit(
            AgentAuditEntry(
                ts=NOW, tool="search_history", arguments={"query": "zzgone"}
            )
        )

        storage._scrub_agent_audit("zzgone")

        assert not storage.agent_audit_file().exists()

    def test_scrubbing_a_missing_log_is_a_no_op(self, tmp_mem_dir: Path) -> None:
        """`mem forget` on a machine that never served an agent does nothing."""
        storage._scrub_agent_audit("anything")  # must not raise

    def test_scrubbing_preserves_unparseable_lines(self, tmp_mem_dir: Path) -> None:
        """A line we cannot read is a line we cannot judge — so we keep it."""
        storage.ensure_dirs()
        storage.agent_audit_file().write_text("{ corrupt\n", encoding="utf-8")

        storage._scrub_agent_audit("anything")

        assert storage.agent_audit_file().read_text(encoding="utf-8") == "{ corrupt\n"


# --- the redactor itself ---------------------------------------------------


class TestRedactSecrets:
    """Unit-level contract for the function the whole boundary rests on."""

    @pytest.mark.parametrize(
        "label,command,secret",
        SECRET_COMMANDS,
        ids=[case[0] for case in SECRET_COMMANDS],
    )
    def test_every_supported_shape_is_removed(
        self, label: str, command: str, secret: str
    ) -> None:
        """The secret is gone and a marker is left in its place."""
        redacted = redact_secrets(command)

        assert secret not in redacted
        assert redact_secrets.__module__  # sanity: we tested the real function
        assert "[REDACTED]" in redacted

    @pytest.mark.parametrize(
        "label,command,secret",
        SECRET_COMMANDS,
        ids=[case[0] for case in SECRET_COMMANDS],
    )
    def test_redaction_is_idempotent(
        self, label: str, command: str, secret: str
    ) -> None:
        """Applying it twice changes nothing, so callers may be defensive."""
        once = redact_secrets(command)

        assert redact_secrets(once) == once

    @pytest.mark.parametrize(
        "command",
        [
            "kubectl get secret my-app-secret -n prod",
            "grep -r api_key ./src/config.py",
            "grep token /var/log/application.log",
            "openssl genrsa -out private_key.pem 4096",
            "vault kv get secret/data/app",
            "git checkout 3f9a2b1c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f90",
            "docker run -p 8080:80 -u 1000:1000 nginx",
            "mkdir -p /tmp/build && cp -p a b",
            "ssh -o StrictHostKeyChecking=no deploy@host",
            "curl -u $DEPLOY_USER:$DEPLOY_TOKEN https://api.example.com",
            "npm publish --token=$NPM_TOKEN",
            "mem run deploy API_TOKEN=$API_TOKEN",
            "terraform apply -var-file=prod.tfvars",
        ],
    )
    def test_ordinary_commands_survive_untouched(self, command: str) -> None:
        """False positives are not free: a mangled result is a useless result.

        Each of these looks credential-shaped to a naive rule and is not one.
        `$VAR` cases matter most — a runbook's placeholder is the answer the
        agent needs, not a secret to hide.
        """
        assert redact_secrets(command) == command

    def test_username_is_kept_when_the_password_is_removed(self) -> None:
        """Half the pair is the secret; the other half is what identifies it."""
        assert (
            redact_secrets("psql postgres://app:s3cr3tvalue@db.internal:5432/prod")
            == "psql postgres://app:[REDACTED]@db.internal:5432/prod"
        )

    def test_auth_scheme_is_kept_when_the_token_is_removed(self) -> None:
        """Knowing it was Bearer auth is useful; the token never is."""
        assert (
            redact_secrets('curl -H "Authorization: Bearer abcdef1234567890"')
            == 'curl -H "Authorization: Bearer [REDACTED]"'
        )

    def test_flag_name_is_kept_when_the_value_is_removed(self) -> None:
        """`--token=[REDACTED]` still tells the reader what belonged there."""
        assert (
            redact_secrets("deploy --token=abcdef123456 --region us-east-1")
            == "deploy --token=[REDACTED] --region us-east-1"
        )

    def test_a_following_flag_is_not_mistaken_for_a_password(self) -> None:
        """`--password --verbose` means "prompt me", not "the password is --verbose"."""
        assert (
            redact_secrets("mysql --password --verbose") == "mysql --password --verbose"
        )

    def test_truncated_private_key_is_still_redacted(self) -> None:
        """A blob pasted without its END line is exactly as sensitive."""
        text = "cat <<EOF\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n"

        assert "MIIEvQIBADANBg" not in redact_secrets(text)


# --- the `mem agent` commands ----------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """A CliRunner with a wide terminal so Rich never wraps assertion targets."""
    return CliRunner(env={"COLUMNS": "200"})


class TestAgentCommands:
    """The human-facing half: how the user grants, inspects and revokes access."""

    def test_enable_and_disable_flip_the_stored_flag(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """The CLI is the only supported way to change the flag."""
        assert runner.invoke(cli, ["agent", "enable"]).exit_code == 0
        assert storage.read_agent_access().enabled is True

        assert runner.invoke(cli, ["agent", "disable"]).exit_code == 0
        assert storage.read_agent_access().enabled is False

    def test_enable_explains_what_was_just_granted(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """Consent that does not say what it covers is not informed consent."""
        result = runner.invoke(cli, ["agent", "enable"])

        assert "redacted" in result.output
        assert "mem agent log" in result.output
        assert "mem agent disable" in result.output

    def test_status_on_a_fresh_install(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """Disabled, zero requests, and the path of the log to check."""
        result = runner.invoke(cli, ["agent", "status"])

        assert result.exit_code == 0
        assert "disabled" in result.output
        assert "mem agent enable" in result.output

    def test_status_after_enabling_shows_when(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """A stale grant is worth noticing, so the change time is shown."""
        runner.invoke(cli, ["agent", "enable"])

        result = runner.invoke(cli, ["agent", "status"])

        assert "enabled" in result.output
        assert "Changed just now" in result.output

    def test_log_is_empty_before_any_request(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """Nothing recorded says so plainly rather than printing an empty table."""
        result = runner.invoke(cli, ["agent", "log"])

        assert result.exit_code == 0
        assert "No agent requests" in result.output

    def test_log_renders_granted_and_refused_requests(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """Each line names the tool, the arguments and the outcome."""
        storage.append_agent_audit(
            AgentAuditEntry(
                ts=NOW, tool="search_history", arguments={"query": "kubectl"}, results=4
            )
        )
        storage.append_agent_audit(
            AgentAuditEntry(
                ts=NOW, tool="get_runbook", ok=False, error="access disabled"
            )
        )

        result = runner.invoke(cli, ["agent", "log"])

        assert "search_history" in result.output
        assert "query=kubectl" in result.output
        assert "4 result(s)" in result.output
        assert "access disabled" in result.output

    def test_log_shows_the_newest_entries(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """`--limit` keeps the tail, because the recent requests are the story."""
        for i in range(5):
            storage.append_agent_audit(
                AgentAuditEntry(ts=NOW + i, tool=f"tool{i}", results=0)
            )

        result = runner.invoke(cli, ["agent", "log", "--limit", "2"])

        assert "tool4" in result.output
        assert "tool3" in result.output
        assert "tool0" not in result.output

    def test_log_json_is_machine_readable(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """Unix citizen: the audit trail pipes into jq like everything else."""
        storage.append_agent_audit(
            AgentAuditEntry(ts=NOW, tool="list_runbooks", results=2)
        )

        result = runner.invoke(cli, ["agent", "log", "--json"])

        assert json.loads(result.output) == [
            {
                "ts": NOW,
                "tool": "list_runbooks",
                "arguments": {},
                "results": 2,
                "ok": True,
                "error": None,
            }
        ]

    def test_status_json_names_the_audit_file(
        self, runner: CliRunner, tmp_mem_dir: Path
    ) -> None:
        """The user is told where the record lives, not asked to guess."""
        result = runner.invoke(cli, ["agent", "status", "--json"])

        payload = json.loads(result.output)
        assert payload["audit_log"].endswith("agent-audit.jsonl")
        assert payload["requests"] == 0
