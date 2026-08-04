"""MCP server for mem — read-only shell memory for AI agents, over stdio.

An agent working in a terminal is missing the one thing the human next to it
has: the memory of what has actually been run in this repository. This module
lends it that memory, and nothing else.

Transport
---------

Newline-delimited JSON-RPC 2.0 over stdin/stdout. **Stdio only.** There is no
socket, no listener, no localhost port, and the HTTP/SSE transport is not
implemented and will not be. mem's first constitutional principle is that it
never touches the network, and "only on loopback" is still a listener that a
second process can connect to.

For the same reason the official MCP Python SDK is not a dependency: it pulls
in uvicorn, starlette and httpx — a full networking stack — to serve a
protocol that, over stdio, is a few hundred lines of `json.loads` and
`print`. That trade is not worth breaking the one promise mem makes. The
protocol is implemented here against the JSON-RPC 2.0 and MCP specifications
directly, and the dependency list stays click/rich/pydantic.

Because stdout *is* the protocol channel, anything printed to it corrupts the
stream and the client disconnects. :func:`serve` therefore rebinds
``sys.stdout`` to stderr for its whole lifetime, so a stray ``print``, a Rich
console write, or a corrupted-JSONL warning from the storage layer becomes a
harmless diagnostic instead of a protocol violation.

The tools, and why each one earns its place
-------------------------------------------

The bar for exposing a tool is that an agent working in a terminal is
measurably worse off without it. Four clear it:

``search_history``
    The core value. "What commands has this human actually run here" is
    information no model has and cannot infer: the real flags, the real
    service names, the real deploy incantation for *this* repo. Everything
    else in this module is a refinement of this one idea.

``list_runbooks`` / ``get_runbook``
    Named groups are human-curated and human-verified — someone typed the
    command, saw it work, and chose to keep it. Handing an agent that list is
    strictly safer than letting it invent a command from a model's prior, and
    it is the difference between "try `kubectl rollout restart`" and "your
    `deploy` runbook does exactly this, in this order". Two tools rather than
    one because listing is cheap and frequent while a full runbook body is
    neither.

``recent_failures``
    Mined deterministically from exit codes plus capture order: what broke,
    and what was run immediately afterwards. That correction pair is the
    highest-signal thing in a shell history and the hardest thing for an
    agent to reconstruct — it is the record of a human debugging the exact
    environment the agent is now standing in. No AI is involved; it is a
    scan of `exit_code != 0` and the following line.

Nothing here executes a command. That is not an oversight and it is not a
default to be relaxed later: an MCP tool that runs shell commands on an
agent's request is a remote code execution surface with a bow tie on. The
runbook tools hand the agent the *text* of a verified command so it can
propose it to its human, who still types Enter.

Privacy boundary
----------------

Three independent controls, because one is not enough:

1. **Opt-in.** Access is off until ``mem agent enable``. The flag is read on
   every request, not cached at startup, so ``mem agent disable`` takes
   effect on the next tool call without restarting the client.
2. **Redaction.** Every string that leaves this process passes through
   :func:`mem.variables.redact_secrets` at one choke point, so a tool added
   later cannot forget to apply it.
3. **Audit.** Every tool call, granted or refused, is appended to
   ``~/.mem/agent-audit.jsonl`` and is readable with ``mem agent log``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, TextIO

from mem import __version__, storage
from mem.models import AgentAuditEntry
from mem.variables import redact_secrets

# The MCP revisions this server speaks. The client names one in `initialize`;
# if it is on this list we echo it back, otherwise we answer with our default
# and let the client decide whether it can live with that — which is exactly
# what the specification prescribes.
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", PROTOCOL_VERSION)

SERVER_NAME = "mem"

# JSON-RPC 2.0 reserved error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Upper bound on how many stored commands a single tool call may consider.
# Not a performance guard — a guard against a `limit: 100000` turning one
# request into a full dump of the user's shell history.
MAX_LIMIT = 50
MAX_SCAN = 500

DISABLED_MESSAGE = (
    "mem agent access is disabled. No history is being served.\n"
    "The shell history stays private until its owner opts in explicitly:\n"
    "  mem agent enable     (and `mem agent disable` to revoke)\n"
    "  mem agent status     (to see the current state)\n"
    "  mem agent log        (to see what was requested)"
)

INSTRUCTIONS = (
    "mem is the user's own shell history, captured on this machine and never "
    "sent anywhere. Use search_history to find how the user actually runs a "
    "tool in this repository before proposing a command of your own. Use "
    "list_runbooks/get_runbook to find human-verified command sequences — "
    "prefer proposing a runbook step verbatim over inventing a command. Use "
    "recent_failures to see what recently broke and what was run next. "
    "All tools are read-only: mem never executes anything on your behalf, so "
    "propose commands to the user rather than expecting them to run."
)


class RpcError(Exception):
    """A JSON-RPC error to be reported to the client, not a crash.

    Every predictable failure — bad params, unknown tool, unreadable store —
    is raised as one of these so the dispatch loop can turn it into a proper
    error object. A server that dies on a bad frame is useless to an agent,
    which has no way to restart it and no way to know why it vanished.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# --- helpers ---------------------------------------------------------------


def _iso(ts: int) -> str:
    """Format an epoch timestamp as UTC ISO-8601.

    Alongside the raw epoch, never instead of it: a model reads dates far
    more reliably than it does integers, and a caller that wants to sort
    still gets the number.
    """
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


_repo_cache: list[str | None] = []


def _current_repo() -> str | None:
    """Git repo containing the server's working directory, detected once.

    MCP clients start the server with the project directory as cwd and never
    change it, so this is stable for the life of the process — and it costs a
    `git` subprocess, which is not something to pay on every request.
    """
    if not _repo_cache:
        from mem.capture import get_git_repo

        _repo_cache.append(get_git_repo(os.getcwd()))
    return _repo_cache[0]


def _redact(value: Any) -> Any:
    """Recursively redact every string in a JSON-shaped structure.

    Applied once to a whole tool payload rather than field by field: a
    per-field call is a rule a future tool can forget, and the failure mode
    of forgetting is a leaked credential.
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _require_str(args: dict[str, Any], name: str) -> str:
    """Read a required non-empty string argument or raise INVALID_PARAMS."""
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RpcError(INVALID_PARAMS, f"'{name}' must be a non-empty string")
    return value.strip()


def _optional_str(args: dict[str, Any], name: str) -> str | None:
    """Read an optional string argument or raise INVALID_PARAMS."""
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise RpcError(INVALID_PARAMS, f"'{name}' must be a non-empty string")
    return value.strip()


def _optional_limit(args: dict[str, Any], default: int) -> int:
    """Read an optional `limit`, clamped to MAX_LIMIT.

    A bool is rejected explicitly: in Python ``isinstance(True, int)`` is
    True, so ``{"limit": true}`` would otherwise silently mean ``limit=1``.
    """
    value = args.get("limit")
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise RpcError(INVALID_PARAMS, "'limit' must be an integer")
    if value < 1:
        raise RpcError(INVALID_PARAMS, "'limit' must be >= 1")
    return min(value, MAX_LIMIT)


# --- tools -----------------------------------------------------------------


def _tool_search_history(args: dict[str, Any]) -> dict[str, Any]:
    """Rank the user's captured commands against a query.

    ``repo`` is applied here as a filter rather than passed to
    ``search.search``, which treats the repo as a *ranking* signal and always
    scans every history file. Over-fetching and filtering keeps the tool's
    contract honest ("only commands from this repo") without changing the
    ranking engine.
    """
    from mem.search import search

    query = _require_str(args, "query")
    repo = _optional_str(args, "repo")
    limit = _optional_limit(args, 10)

    current = repo or _current_repo()
    fetch = MAX_SCAN if repo else limit
    ranked = search(query, current_repo=current, limit=fetch)

    if repo:
        sanitized = storage.sanitize_repo_name(repo)
        ranked = [
            (cmd, score)
            for cmd, score in ranked
            if cmd.repo
            and (cmd.repo == repo or storage.sanitize_repo_name(cmd.repo) == sanitized)
        ][:limit]

    return {
        "query": query,
        "repo": current,
        "count": len(ranked),
        "results": [
            {
                "command": cmd.command,
                "repo": cmd.repo,
                "ts": cmd.ts,
                "when": _iso(cmd.ts),
                "exit_code": cmd.exit_code,
                "duration_ms": cmd.duration_ms,
                "score": round(score, 4),
            }
            for cmd, score in ranked
        ],
    }


def _group_scopes() -> list[tuple[str, Any]]:
    """Every (scope_label, GroupFile) pair on disk, current repo first.

    Reads files directly instead of going through ``mem.groups``, whose
    resolution helpers raise ``click.ClickException`` — an exception class
    that means "print this and exit" and has no business terminating a
    long-lived protocol server.
    """
    scopes: list[tuple[str, Any]] = []
    current = _current_repo()
    current_stem = storage.sanitize_repo_name(current) if current else None

    repos_dir = storage.GROUPS_REPOS_DIR
    if repos_dir.exists():
        for path in sorted(repos_dir.glob("*.json")):
            try:
                scopes.append((path.stem, storage.read_group_file(path)))
            except ValueError:
                continue  # a malformed scope must not hide the healthy ones

    try:
        scopes.append(("global", storage.read_group_file(storage.GROUPS_GLOBAL_FILE)))
    except ValueError:
        pass

    scopes.sort(key=lambda item: item[0] != current_stem)
    return scopes


def _tool_list_runbooks(args: dict[str, Any]) -> dict[str, Any]:
    """List every named group across all scopes, current repo first."""
    current = _current_repo()
    runbooks = [
        {
            "name": name,
            "scope": label,
            "description": group.description,
            "commands": len(group.commands),
        }
        for label, data in _group_scopes()
        for name, group in data.groups.items()
    ]
    return {
        "current_repo": current,
        "count": len(runbooks),
        "runbooks": runbooks,
    }


def _tool_get_runbook(args: dict[str, Any]) -> dict[str, Any]:
    """Return one runbook's ordered commands, comments and variables.

    Scope resolution mirrors the CLI: the current repo shadows global, so an
    agent sees the same `deploy` the user sees when they type `mem run
    deploy` in this directory.
    """
    name = _require_str(args, "name")
    scope = _optional_str(args, "scope")

    for label, data in _group_scopes():
        if scope is not None and label != scope:
            continue
        group = data.groups.get(name)
        if group is None:
            continue
        return {
            "name": name,
            "scope": label,
            "description": group.description,
            "count": len(group.commands),
            "commands": [
                {
                    "cmd": c.cmd,
                    "comment": c.comment,
                    "vars": [
                        {"name": v.name, "default": v.default} for v in (c.vars or [])
                    ],
                }
                for c in group.commands
            ],
            "note": (
                "These commands are not executed by mem. Propose them to the "
                "user; $VARIABLES are placeholders the user supplies at run "
                "time with `mem run`."
            ),
        }

    where = f" in scope '{scope}'" if scope else ""
    raise RpcError(INVALID_PARAMS, f"runbook '{name}' not found{where}")


def _tool_recent_failures(args: dict[str, Any]) -> dict[str, Any]:
    """Report commands that exited non-zero and what was run next.

    Purely deterministic: a non-zero exit code plus capture order. The two
    commands that follow a failure are the human's attempt to fix it, and a
    later successful run of the *same* command is proof the fix worked —
    which is the single most useful thing a shell history can tell an agent
    and needs no inference to extract.

    The scan itself lives in :mod:`mem.fix`, which mines the same two facts
    for ``mem fix``. Sharing it is deliberate: two independent definitions of
    "a failure and what came after it" would drift, and this codebase has
    already been bitten twice by exactly that — a stale shell hook served to
    every pip user, and a ranking formula duplicated across two call sites.
    What stays here is what is genuinely this tool's own: the shape of the
    JSON, the two-command lookahead, and ``retried_successfully``.
    """
    from mem.fix import iter_failures, iter_histories

    limit = _optional_limit(args, 10)
    repo_filter = _optional_str(args, "repo")

    sanitized = storage.sanitize_repo_name(repo_filter) if repo_filter else None
    include = None if sanitized is None else (lambda stem: stem == sanitized)
    failures: list[dict[str, Any]] = []

    for _key, commands in iter_histories(include):
        succeeded_later: dict[str, int] = {}
        for index, cmd in enumerate(commands):
            if cmd.exit_code == 0:
                succeeded_later.setdefault(cmd.command, index)

        for failure in iter_failures(commands, lookahead=2):
            retry = succeeded_later.get(failure.command.command)
            failures.append(
                {
                    "command": failure.command.command,
                    "repo": failure.command.repo,
                    "ts": failure.command.ts,
                    "when": _iso(failure.command.ts),
                    "exit_code": failure.command.exit_code,
                    "followed_by": [
                        {"command": nxt.command, "exit_code": nxt.exit_code}
                        for nxt in failure.following
                    ],
                    "retried_successfully": retry is not None and retry > failure.index,
                }
            )

    failures.sort(key=lambda f: f["ts"], reverse=True)
    failures = failures[:limit]
    return {"count": len(failures), "failures": failures}


# The advertised tool surface. Descriptions are written for the model that
# reads them: they say when to reach for the tool, not merely what it returns.
TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_history",
        "description": (
            "Search the user's real shell history for commands matching a "
            "query, ranked by frequency, recency and repository context. Use "
            "this before proposing any command: it shows the flags, hosts, "
            "service names and conventions this user actually uses in this "
            "project, which no model can guess."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Words the command must all contain, e.g. "
                        "'docker compose' or 'kubectl prod'."
                    ),
                },
                "repo": {
                    "type": "string",
                    "description": (
                        "Restrict results to one repository, given as its "
                        "absolute path. Defaults to the repository containing "
                        "the server's working directory."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum results, 1-{MAX_LIMIT} (default 10).",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_runbooks",
        "description": (
            "List the user's named command groups (runbooks) across the "
            "current repository and the global scope. These are sequences the "
            "user curated by hand and verified by running them. Check here "
            "before composing a multi-step procedure yourself."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_runbook",
        "description": (
            "Return the ordered commands of one runbook, with the user's own "
            "comments and its variable placeholders. Propose these to the "
            "user verbatim rather than reconstructing an equivalent command; "
            "mem does not execute them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Runbook name as reported by list_runbooks.",
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Scope to read from: 'global' or a repository scope "
                        "name. Defaults to the current repository, falling "
                        "back to global — the same resolution `mem run` uses."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "recent_failures",
        "description": (
            "Recent commands that exited non-zero, each with the commands run "
            "immediately afterwards and whether the same command later "
            "succeeded. This is the record of the user debugging this exact "
            "environment: read it before diagnosing a failure yourself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": (
                        "Restrict to one repository, given as its absolute "
                        "path. Defaults to every repository."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum failures, 1-{MAX_LIMIT} (default 10).",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                },
            },
        },
    },
]

_TOOL_IMPLS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "search_history": _tool_search_history,
    "list_runbooks": _tool_list_runbooks,
    "get_runbook": _tool_get_runbook,
    "recent_failures": _tool_recent_failures,
}


# --- audit -----------------------------------------------------------------


def _audit(tool: str, args: dict[str, Any], results: int, error: str | None) -> None:
    """Record one agent request, redacted, never raising.

    An audit write that could fail the request would be an incentive to skip
    auditing under load, so a failure here is swallowed: the tool result is
    already computed and refusing to return it helps nobody. The write itself
    goes through the storage layer's lock and 0600 permissions.
    """
    safe_args = {
        str(k): redact_secrets(v if isinstance(v, str) else json.dumps(v))
        for k, v in list(args.items())[:10]
    }
    try:
        storage.append_agent_audit(
            AgentAuditEntry(
                ts=int(time.time()),
                tool=tool,
                arguments=safe_args,
                results=results,
                ok=error is None,
                error=error,
            )
        )
    except Exception:  # pragma: no cover - defensive, see docstring
        pass


# --- JSON-RPC dispatch -----------------------------------------------------


def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(
    request_id: Any, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """Build a JSON-RPC error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    """Answer the handshake, echoing a protocol version we both speak."""
    requested = params.get("protocolVersion")
    version = (
        requested
        if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
        else PROTOCOL_VERSION
    )
    enabled = storage.read_agent_access().enabled
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
        "instructions": INSTRUCTIONS if enabled else DISABLED_MESSAGE,
    }


def _handle_tools_list(params: dict[str, Any]) -> dict[str, Any]:
    """List the tools, or nothing at all while access is disabled.

    An empty list rather than an error: a well-behaved client then never
    calls a tool, and the reason is already in the `initialize` instructions.
    The flag is re-read here so `mem agent disable` takes effect immediately.
    """
    if not storage.read_agent_access().enabled:
        return {"tools": []}
    return {"tools": TOOLS}


def _handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    """Run one tool and wrap its payload as MCP text content.

    Two failure kinds, deliberately reported differently:

    - A *protocol* failure — missing argument, wrong type, tool that does not
      exist — is a JSON-RPC error (-32602/-32603). The client made a mistake
      and its own error handling should see it as one.
    - A *policy* refusal — access is disabled — comes back as ``isError``
      content, because the model is meant to read it. "Ask your human to run
      `mem agent enable`" is only useful if it reaches the agent as text it
      can relay, and a transport-level error usually does not.
    """
    name = params.get("name")
    if not isinstance(name, str) or not name:
        raise RpcError(INVALID_PARAMS, "'name' must be a non-empty string")

    args = params.get("arguments", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise RpcError(INVALID_PARAMS, "'arguments' must be an object")

    if not storage.read_agent_access().enabled:
        _audit(name, args, 0, "access disabled")
        return _text_content(DISABLED_MESSAGE, is_error=True)

    impl = _TOOL_IMPLS.get(name)
    if impl is None:
        _audit(name, args, 0, "unknown tool")
        raise RpcError(INVALID_PARAMS, f"unknown tool: {name}")

    try:
        payload = impl(args)
    except RpcError as exc:
        _audit(name, args, 0, exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the server
        _audit(name, args, 0, type(exc).__name__)
        raise RpcError(
            INTERNAL_ERROR, f"mem failed to read its store: {type(exc).__name__}"
        ) from exc

    _audit(name, args, int(payload.get("count", 0)), None)
    # The single choke point: nothing reaches the client without passing here.
    return _text_content(json.dumps(_redact(payload), indent=2))


def _text_content(text: str, is_error: bool = False) -> dict[str, Any]:
    """Wrap a string as an MCP tool result.

    Only ``content`` is emitted, never ``structuredContent``: the spec ties
    structured output to a declared ``outputSchema``, and shipping one
    without the other is the kind of detail that makes a strict client
    reject every response.
    """
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


_METHODS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": lambda params: {},
}

# Notifications carry no id and must never be answered — a response to a
# notification is a protocol violation, and clients are entitled to hang up
# over one.
_NOTIFICATIONS = frozenset(
    {
        "notifications/initialized",
        "notifications/cancelled",
        "notifications/progress",
    }
)


def handle_message(message: Any) -> dict[str, Any] | None:
    """Turn one decoded JSON-RPC message into a response, or None.

    Returns None for notifications and for responses the client sends us
    (which we never asked for, and must not answer).
    """
    if not isinstance(message, dict):
        # A batch (list) lands here too: MCP removed JSON-RPC batching, and
        # accepting it would mean guessing at framing the client cannot rely on.
        return _error(None, INVALID_REQUEST, "expected a JSON-RPC request object")

    if message.get("jsonrpc") != "2.0":
        return _error(
            message.get("id"), INVALID_REQUEST, "'jsonrpc' must be exactly '2.0'"
        )

    method = message.get("method")
    if not isinstance(method, str):
        # No method means this is a response to something we sent. We send no
        # requests, so there is nothing to correlate and nothing to reply.
        if "result" in message or "error" in message:
            return None
        return _error(message.get("id"), INVALID_REQUEST, "'method' must be a string")

    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error(message.get("id"), INVALID_PARAMS, "'params' must be an object")

    is_notification = "id" not in message
    request_id = message.get("id")

    if method in _NOTIFICATIONS or (is_notification and method not in _METHODS):
        return None

    handler = _METHODS.get(method)
    if handler is None:
        if is_notification:
            return None
        return _error(request_id, METHOD_NOT_FOUND, f"unknown method: {method}")

    try:
        payload = handler(params)
    except RpcError as exc:
        return _error(request_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # noqa: BLE001 - see class docstring for RpcError
        return _error(
            request_id, INTERNAL_ERROR, f"internal error: {type(exc).__name__}"
        )

    if is_notification:
        return None
    return _result(request_id, payload)


# --- transport -------------------------------------------------------------


def _write(stream: TextIO, message: dict[str, Any]) -> None:
    """Write one framed JSON-RPC message and flush.

    ``ensure_ascii`` is left at its default: escaping non-ASCII to ``\\uXXXX``
    makes the frame valid under any stdout encoding the client happens to
    give us, which matters because a shell history is full of accented paths
    and emoji. ``json.dumps`` also escapes newlines inside strings, so a
    captured multi-line command can never split one frame into two.
    """
    stream.write(json.dumps(message) + "\n")
    stream.flush()


def serve(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the JSON-RPC loop until stdin closes. Returns a process exit code.

    Streams are injectable so tests can drive the server the way a client
    does, and so the caller keeps a handle on the *real* stdout after
    ``sys.stdout`` is rebound.

    That rebinding is the important line in this function. Everything mem
    already does — the storage layer's "skipping corrupted line" warning,
    Rich consoles, a stray print in a future patch — writes to stdout by
    default, and on this transport stdout is the wire. Pointing it at stderr
    for the duration makes the invariant structural instead of a rule
    contributors have to remember.
    """
    inp = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    if not storage.read_agent_access().enabled:
        print(DISABLED_MESSAGE, file=err, flush=True)

    saved_stdout = sys.stdout
    sys.stdout = err
    try:
        while True:
            line = inp.readline()
            if not line:
                return 0
            line = line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(out, _error(None, PARSE_ERROR, f"invalid JSON: {exc.msg}"))
                continue

            response = handle_message(message)
            if response is not None:
                _write(out, response)
    except (BrokenPipeError, KeyboardInterrupt):
        # The client hung up or the user pressed Ctrl-C. Both are ordinary
        # ways for a stdio server to end its life, not errors.
        return 0
    finally:
        sys.stdout = saved_stdout


def main() -> int:
    """Entry point for `mem mcp`: send logging to stderr, then serve.

    ``force=True`` because a handler installed on stdout by any imported
    module would keep writing there after ``serve`` rebinds ``sys.stdout``
    (handlers capture the stream object, not the name) — and one log line on
    stdout is one corrupted frame.
    """
    import logging

    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)
    return serve()
