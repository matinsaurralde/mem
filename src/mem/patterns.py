"""
AI-powered pattern extraction using Apple Foundation Models.

This module is the ONLY place in mem that uses AI inference. Everything
else is deterministic. Pattern extraction exists because no regex or
heuristic can reliably generalize commands like:
    kubectl get pods, kubectl get services, kubectl get deployments
into the abstract pattern:
    kubectl get <resource>

Apple Foundation Models run entirely on-device via apple-fm-sdk.
No data ever leaves the machine.

What is guided generation:
    Guided generation constrains the LLM's output to conform to a
    Pydantic schema. Instead of free-form text, the model produces
    structured JSON that validates against PatternExtractionResult.
    This eliminates parsing errors and ensures reliable structured output.
"""

from __future__ import annotations

import logging
import sys
import time
from collections import defaultdict

from mem import storage
from mem.models import (
    CommandPattern,
    PatternExtractionResult,
    PatternFile,
)

logger = logging.getLogger(__name__)

# The prompt template for pattern extraction.
# Why this wording:
# - "analyzing shell command history" sets the domain context
# - Listing concrete commands gives the model examples to generalize from
# - "Replace specific names, values, and identifiers with <placeholder>"
#   explicitly instructs the model to use angle-bracket tokens
# - "Group similar commands into one pattern" prevents redundant patterns
# - "Return only the patterns, no explanation" keeps output clean for parsing
EXTRACTION_PROMPT = """You are analyzing shell command history for the tool: {tool}

Here are commands the user has run:
{command_list}

Extract the abstract structural patterns. Replace specific names, values, and identifiers with <placeholder> tokens in angle brackets. Group similar commands into one pattern.

Return only the patterns, no explanation."""


def _apple_fm_available() -> bool:
    """Check if Apple Foundation Models SDK is available."""
    try:
        import apple_fm_sdk  # noqa: F401
        return True
    except ImportError:
        return False


async def extract_patterns_for_tool(
    tool: str, commands: list[str]
) -> PatternExtractionResult:
    """Extract abstract patterns from a list of concrete commands.

    Uses Apple FM SDK with guided generation (Pydantic schema)
    to produce structured output. Falls back to a simple heuristic
    if the SDK is unavailable.
    """
    if _apple_fm_available():
        import apple_fm_sdk

        prompt = EXTRACTION_PROMPT.format(
            tool=tool,
            command_list="\n".join(f"  {cmd}" for cmd in commands),
        )

        # Guided generation: the model's output is constrained to match
        # the PatternExtractionResult Pydantic schema, producing valid
        # structured JSON instead of free-form text.
        result = await apple_fm_sdk.generate(
            prompt=prompt,
            schema=PatternExtractionResult,
        )
        return result
    else:
        # Fallback: simple frequency-based "patterns" without AI
        # Just return the most common commands as-is
        return _heuristic_patterns(tool, commands)


def _heuristic_patterns(tool: str, commands: list[str]) -> PatternExtractionResult:
    """Simple fallback when Apple FM SDK is unavailable.

    Groups identical commands and returns them as "patterns".
    Not as smart as AI extraction, but still useful for ranking.
    """
    freq: dict[str, int] = defaultdict(int)
    for cmd in commands:
        freq[cmd] += 1

    patterns = [
        CommandPattern(pattern=cmd, example=cmd, frequency=count)
        for cmd, count in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:10]
    ]

    return PatternExtractionResult(tool=tool, patterns=patterns)


def run_pattern_extraction(tool: str) -> None:
    """Extract patterns for a single tool and save to storage.

    Reads all commands starting with the tool name from storage,
    runs extraction, and writes the result to patterns/<tool>.json.
    """
    import asyncio

    commands = [
        cmd.command
        for cmd in storage.read_all_commands()
        if cmd.command.split()[0] == tool
    ]

    if len(commands) < 5:
        return  # Not enough data for meaningful patterns

    result = asyncio.run(extract_patterns_for_tool(tool, commands))

    pf = PatternFile(
        tool=tool,
        patterns=result.patterns,
        last_updated=int(time.time()),
    )
    storage.write_patterns(pf)


def sync_all_patterns() -> tuple[int, int]:
    """Extract patterns for ALL tools with sufficient command history.

    Detects unique tools (first token of each command), runs extraction
    for each tool with >5 commands. Skips tools with insufficient data.

    Returns (new_patterns, updated_patterns) counts.
    """
    # Collect all commands grouped by tool (first token)
    tool_commands: dict[str, list[str]] = defaultdict(list)
    for cmd in storage.read_all_commands():
        parts = cmd.command.split()
        if parts:
            tool_commands[parts[0]].append(cmd.command)

    new_count = 0
    updated_count = 0

    for tool, commands in tool_commands.items():
        if len(commands) < 5:
            continue  # Skip tools with too few commands

        existing = storage.read_patterns(tool)
        run_pattern_extraction(tool)

        if existing is None:
            new_count += 1
        else:
            updated_count += 1

    if not _apple_fm_available():
        print(
            "warning: apple-fm-sdk not available. "
            "Using heuristic patterns instead of AI extraction.",
            file=sys.stderr,
        )

    return new_count, updated_count
