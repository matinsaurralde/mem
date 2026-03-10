"""Pydantic v2 data models for mem.

Why Pydantic: validation on deserialization, JSON serialization, and guided
generation schema for Apple FM SDK — one model definition serves three purposes.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CapturedCommand(BaseModel):
    """A single shell command captured by the shell hook."""

    command: str
    ts: int
    dir: str
    repo: str | None = None
    exit_code: int
    duration_ms: int = Field(ge=0)
    session: str | None = None

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
    """On-disk representation of extracted patterns for a single tool."""

    tool: str = Field(min_length=1)
    patterns: list[CommandPattern]
    last_updated: int


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
    """Guided generation output schema for Apple FM SDK."""

    tool: str = Field(min_length=1)
    patterns: list[CommandPattern]


class SessionState(BaseModel):
    """Ephemeral state for tracking current active session."""

    session_id: str
    last_command_ts: int
    last_repo: str | None = None
    commands: list[str]
