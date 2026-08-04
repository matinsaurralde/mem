"""Pydantic v2 data models for mem.

Why Pydantic: validation on deserialization, JSON serialization, and guided
generation schema for Apple FM SDK — one model definition serves three purposes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CapturedCommand(BaseModel):
    """A single shell command captured by the shell hook — or imported.

    ``imported`` marks a command that came from an existing shell history
    file (``mem import --from-shell-history``) rather than from the hook.
    It defaults to ``False``, so every JSONL line written before the field
    existed still validates and is correctly read back as hook-captured.

    ``exit_code`` and ``duration_ms`` are ``None`` for imported commands.
    A shell history file records neither, and there is no honest integer to
    put there: ``0`` would claim every imported command succeeded, and a
    ``0`` duration is indistinguishable from a genuinely fast command. They
    also default to ``None`` so old lines — which always carry both — keep
    validating unchanged.
    """

    command: str
    ts: int
    dir: str
    repo: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    session: str | None = None
    imported: bool = False

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line for append-only storage."""
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> CapturedCommand:
        """Deserialize from a JSONL line, stripping whitespace."""
        return cls.model_validate_json(line.strip())


class CommandPattern(BaseModel):
    """A recurring command pattern extracted by the AI layer."""

    pattern: str = Field(min_length=1)
    example: str = Field(min_length=1)
    frequency: int = Field(ge=1)


class PatternFile(BaseModel):
    """On-disk representation of extracted patterns for a single tool.

    ``command_patterns`` is the real cache: the full {command -> pattern}
    mapping the model produced. It replaces reconstructing that mapping from
    ``patterns``, which only ever stored one ``example`` per pattern — so on
    the next sync every other already-generalized command missed the cache,
    fell back to itself, and reappeared as its own raw "pattern". Patterns
    degraded into raw commands on every sync, and since sync runs every 20
    captures, that was the steady state for any real history.

    ``processed_commands`` is kept for backward compatibility with files
    written by earlier versions, where it was the only record of what had
    been seen.
    """

    tool: str = Field(min_length=1)
    patterns: list[CommandPattern]
    last_updated: int
    processed_commands: list[str] = []
    command_patterns: dict[str, str] = {}


class WorkSession(BaseModel):
    """A bounded work session grouping related commands."""

    id: str
    summary: str
    started_at: int
    ended_at: int
    dir: str
    repo: str | None = None
    commands: list[str]

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line for append-only storage."""
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> WorkSession:
        """Deserialize from a JSONL line, stripping whitespace."""
        return cls.model_validate_json(line.strip())


class PatternExtractionResult(BaseModel):
    """Result of one extraction pass over a tool's commands.

    ``command_patterns`` carries the {command -> pattern} cache forward so the
    caller can persist it, letting the next run skip commands the model has
    already generalized.
    """

    tool: str = Field(min_length=1)
    patterns: list[CommandPattern]
    command_patterns: dict[str, str] = {}


class SessionState(BaseModel):
    """Ephemeral state for tracking current active session."""

    session_id: str
    last_command_ts: int
    last_repo: str | None = None
    commands: list[str]


# --- Named Groups (active memory) ---


class VarDeclaration(BaseModel):
    """A variable placeholder in a saved command."""

    name: str = Field(min_length=2, pattern=r"^[A-Z][A-Z0-9_]+$")
    default: str | None = None


class StoredVariable(BaseModel):
    """A persistent variable value managed by mem vars."""

    value: str
    last_used: int = 0


class VarsFile(BaseModel):
    """On-disk representation of the persistent variable store."""

    vars: dict[str, StoredVariable] = {}


class SavedCommand(BaseModel):
    """A single bookmarked command in the saved list."""

    cmd: str = Field(min_length=1)
    comment: str | None = None
    vars: list[VarDeclaration] | None = None


class GroupCommand(BaseModel):
    """A single command entry within a named group."""

    cmd: str = Field(min_length=1)
    comment: str | None = None
    vars: list[VarDeclaration] | None = None


class Group(BaseModel):
    """A named, ordered collection of commands forming a runbook."""

    description: str | None = None
    commands: list[GroupCommand] = []


class GroupFile(BaseModel):
    """On-disk data file containing saved commands and named groups for a scope."""

    saved: list[SavedCommand] = []
    groups: dict[str, Group] = {}


# --- Agent access (MCP) ---


class AgentAccess(BaseModel):
    """Whether an AI agent may read mem's store over MCP.

    Default ``False`` is the whole point: history is captured for the human
    who typed it, and handing it to a model is a separate decision that has
    to be made explicitly with ``mem agent enable``. ``updated_at`` records
    when that decision was last changed so ``mem agent status`` can show it.
    """

    enabled: bool = False
    updated_at: int = 0


class AgentAuditEntry(BaseModel):
    """One line of the append-only record of what an agent asked mem for.

    Stored per tool call rather than per session: a session boundary is not
    observable over stdio (the client can keep the process alive for hours),
    while a call is the unit a user would actually want to review.

    ``arguments`` is redacted before it is written — an agent can put a
    secret in a query string, and an audit log that records secrets is a
    second copy of the thing this feature exists to protect.
    """

    ts: int
    tool: str
    arguments: dict[str, str] = {}
    results: int = 0
    ok: bool = True
    error: str | None = None

    def to_jsonl(self) -> str:
        """Serialize to a single JSONL line for append-only storage."""
        return self.model_dump_json()

    @classmethod
    def from_jsonl(cls, line: str) -> AgentAuditEntry:
        """Deserialize from a JSONL line, stripping whitespace."""
        return cls.model_validate_json(line.strip())
