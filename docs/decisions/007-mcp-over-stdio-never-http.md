# ADR-007: Zero Network Is Inviolable — MCP Is Exposed Over stdio, Never HTTP

**Status**: Accepted
**Date**: 2026-08-04

## Context

Principle I is absolute: mem does not import or depend on any networking library, and no shell data ever leaves the machine. It is also the only property of mem that a competitor cannot copy without giving up their own product.

At the same time, coding agents now live in the same terminal mem does, and mem already holds exactly the context an agent needs — command, cwd, repo, exit code, duration, session — with `--json` output almost everywhere. The Model Context Protocol is the way to hand that to an agent.

MCP defines two transports: **stdio** (newline-delimited JSON-RPC 2.0 over the process's own stdin/stdout, launched by the client as a child process) and **HTTP/SSE** (a server bound to a port). Only one of them is compatible with Principle I, and the difference is not a matter of degree.

## Decision

**mem exposes an MCP server over stdio only.**

- `mem mcp` reads newline-delimited JSON-RPC 2.0 requests on stdin and writes responses on stdout. It opens no socket, binds no port, and starts no listener. The parent process hands us two file descriptors and reads the answers back; there is no remote party and nothing addressable from outside the machine.
- **The HTTP/SSE (Streamable HTTP) transport is forbidden.** Not deferred — forbidden. It requires a listener, and a listener is networking whether or not it binds to `127.0.0.1`. Any future request for "just localhost", "just for the desktop app", or "just for sync" is answered by this ADR.
- **The official MCP Python SDK is not a dependency.** It transitively pulls in `uvicorn`, `starlette`, and `httpx` — an HTTP server and an HTTP client, present in the process even when only the stdio transport is used. That is precisely what Principle I forbids, and it undoes the work of getting `ssl`, `socket`, and `asyncio` out of mem's import graph. The stdio server is therefore **hand-rolled directly against the JSON-RPC 2.0 and MCP specs**: read a line, parse it with stdlib `json`, dispatch, write a line. It is the size of a CLI subcommand, and it costs zero new dependencies.

For the constitution's avoidance of doubt: **reading stdin and writing stdout is not networking.** A pipe we did not open, handed to us by the process that launched us, has no listener, no address, and no remote peer. A Unix domain socket would already be a different conversation, and is likewise not permitted — mem listens for nothing.

### The agent privacy boundary

Exposing history to an agent is a privacy decision, not just a protocol one. Three rules, binding:

1. **Opt-in, explicitly.** Agent access is off by default. The user turns it on with `mem agent enable` and off with `mem agent disable`. `mem mcp` refuses to serve while it is disabled — installing mem never silently makes your history readable by whatever agent is running in your terminal.
2. **Everything returned to an agent is redacted.** Every result crossing the MCP boundary passes through the same credential redactor used on capture, applied at the boundary rather than trusted from storage — so history captured before redaction existed is still covered.
3. **Agent activity is auditable.** Every request (timestamp, tool, arguments, number of results returned) is appended to an audit log the user can read with `mem agent log`. If an agent read your history, you can see exactly what it asked for.

## Alternatives Considered

- **MCP over HTTP/SSE.** Rejected: needs a listener and a networking stack, violating Principle I outright, and turns a private shell history into something that exists on a port. The convenience of remote agents is not worth the one property that defines the project.
- **Use the official MCP Python SDK with the stdio transport only.** Rejected: the dependency tree, not the code path, is what violates the principle — `uvicorn`/`starlette`/`httpx` end up installed and importable in mem's environment. Hand-rolling costs us spec conformance work; importing an HTTP stack costs us the principle.
- **No agent access at all.** Rejected: it is the largest gap on the competitive map (the closest thing to a competitor has 4 stars), roughly 70% of the work already exists in the data model and the `--json` envelope, and it converts the privacy argument from defensive to enabling — your agent learns from your history without your history leaving the machine.
- **A local Unix socket instead of stdio.** Rejected: it is a listener, with a lifecycle, permissions, and an attack surface. It also reintroduces the daemon that ADR-003 rejected. stdio has none of that and is what MCP clients launch by default anyway.

## Consequences

- We own protocol conformance. Hand-rolling means tracking MCP spec revisions ourselves and testing the wire format directly; there is no SDK to absorb a breaking change for us.
- Remote agents are impossible, permanently. That is the intended outcome, not a limitation to work around later.
- The MCP surface cannot ship before the credential redactor exists — rule 2 is a hard prerequisite, not a follow-up task.
- `mem mcp` must keep stdout clean: on the stdio transport, stdout *is* the protocol channel. All logging, diagnostics, and warnings go to stderr, which is what Principle III already requires.
- PHILOSOPHY.md is amended to say this out loud, so the feature is not blocked by its own constitution and no future contributor has to guess where the line is.
