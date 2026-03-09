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

Architecture:
    The LLM handles GENERALIZATION (the hard part — turning concrete
    arguments into abstract placeholders). Code handles COUNTING and
    DEDUPLICATION (the easy part). This split plays to each system's
    strengths: the on-device model is good at semantic understanding
    but unreliable at counting and dedup.

    Flow:
    1. Deduplicate raw commands and count identical ones (code)
    2. Generalize each unique command via guided generation (LLM)
    3. Aggregate pattern frequencies from the mapping (code)
"""

from __future__ import annotations

import logging
import sys
import time
from collections import Counter, defaultdict

from mem import storage
from mem.models import (
    CommandPattern,
    PatternExtractionResult,
    PatternFile,
)

logger = logging.getLogger(__name__)

# Per-command generalization prompt.
# Why this design:
# - One command at a time avoids dedup/counting errors from the LLM
# - Concrete examples anchor the model's understanding of "generalize"
# - "Keep subcommands, flags, and operators as-is" prevents hallucination
#   of extra flags (observed with minimal prompts)
# - Angle-bracket format is explicitly shown in examples
GENERALIZE_PROMPT = """Convert this {tool} command into a generalized pattern.
Replace specific arguments (names, IDs, paths, tags, values) with <descriptive_placeholder> in angle brackets.
Keep subcommands, flags, and operators as-is.

Examples:
  "git checkout main" -> "git checkout <branch>"
  "docker run -d -p 8080:80 myapp" -> "docker run -d -p <host_port>:<container_port> <image>"
  "kubectl get pods" -> "kubectl get <resource>"

Command: {command}"""

# Session summary prompt (used by capture module).
SESSION_SUMMARY_PROMPT = (
    "Summarize this shell session in one short sentence:\n{commands}"
)


def _apple_fm_available() -> bool:
    """Check if Apple Foundation Models SDK is available."""
    try:
        import apple_fm_sdk  # noqa: F401
        return True
    except ImportError:
        return False


def _get_generable_types() -> type:
    """Lazily create @fm.generable type for guided generation.

    Returns the GeneralizedCommand class decorated with @fm.generable.
    Created on first call to avoid import-time dependency on apple-fm-sdk.
    """
    import apple_fm_sdk as fm

    @fm.generable("Generalized form of a shell command")
    class GeneralizedCommand:
        pattern: str = fm.guide(
            "The command with variable parts replaced by <placeholder>"
        )

    return GeneralizedCommand


async def _generalize_commands(
    tool: str, unique_commands: list[str]
) -> dict[str, str]:
    """Generalize each unique command via Apple FM guided generation.

    Returns a mapping from concrete command -> generalized pattern.
    Uses one LanguageModelSession per tool to share context.
    """
    import apple_fm_sdk as fm

    GeneralizedCommand = _get_generable_types()
    session = fm.LanguageModelSession()
    cmd_to_pattern: dict[str, str] = {}

    for cmd in unique_commands:
        prompt = GENERALIZE_PROMPT.format(tool=tool, command=cmd)
        result = await session.respond(prompt, generating=GeneralizedCommand)
        cmd_to_pattern[cmd] = result.pattern

    return cmd_to_pattern


async def extract_patterns_for_tool(
    tool: str, commands: list[str]
) -> PatternExtractionResult:
    """Extract abstract patterns from a list of concrete commands.

    Strategy:
    1. Deduplicate commands and count frequencies (code)
    2. Generalize each unique command via LLM (if available)
    3. Aggregate frequencies by generalized pattern (code)

    Falls back to simple frequency grouping if SDK is unavailable.
    """
    # Step 1: Count raw frequencies (code — fast and exact)
    raw_freq = Counter(commands)
    unique_cmds = list(raw_freq.keys())

    if _apple_fm_available():
        # Step 2: Generalize unique commands (LLM — semantic understanding)
        cmd_to_pattern = await _generalize_commands(tool, unique_cmds)

        # Step 3: Aggregate by pattern (code — exact counting)
        pattern_freq: Counter[str] = Counter()
        pattern_example: dict[str, str] = {}
        for cmd, count in raw_freq.items():
            p = cmd_to_pattern[cmd]
            pattern_freq[p] += count
            if p not in pattern_example:
                pattern_example[p] = cmd

        patterns = [
            CommandPattern(pattern=p, example=pattern_example[p], frequency=f)
            for p, f in pattern_freq.most_common()
        ]
        return PatternExtractionResult(tool=tool, patterns=patterns)
    else:
        return _heuristic_patterns(tool, commands)


async def generate_session_summary(commands: list[str]) -> str | None:
    """Generate a one-sentence session summary via Apple FM.

    Returns None if SDK is unavailable or generation fails.
    """
    if not _apple_fm_available():
        return None

    try:
        import apple_fm_sdk as fm

        session = fm.LanguageModelSession()
        prompt = SESSION_SUMMARY_PROMPT.format(commands="\n".join(commands))
        result = await session.respond(prompt)
        return str(result)
    except Exception:
        return None


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
