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
import re
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

# Commands sent to the model per sync run, across all tools.
#
# Measured on this machine: ~1.4s per command, so a first sync over an
# accumulated history is unbounded work — 3000 unique commands is roughly 70
# minutes of continuous on-device inference, kicked off from a precmd hook with
# no progress bar and no way to cancel. Because the background sync has never
# actually run (it spawned `python -m mem.cli`, and cli.py had no __main__
# block), *every* existing installation has a full backlog waiting. A cap is
# what keeps enabling it from being an incident; the next run continues.
SYNC_BUDGET = 50

# Per-command generalization prompt.
# Why this design:
# - One command at a time avoids dedup/counting errors from the LLM
# - Concrete examples anchor the model's understanding of "generalize"
# - "Keep subcommands, flags, and operators as-is" prevents hallucination
#   of extra flags (observed with minimal prompts)
# - Angle-bracket format is explicitly shown in examples
GENERALIZE_PROMPT = """Convert this {tool} command into a generalized pattern.
Replace each specific argument (names, IDs, paths, tags, values) with a short
name in angle brackets that says what kind of thing it was.
Keep subcommands, flags, and operators exactly as they are.

Examples:
  "git checkout main" -> "git checkout <branch>"
  "docker run -d -p 8080:80 myapp" -> "docker run -d -p <host_port>:<container_port> <image>"
  "kubectl get pods" -> "kubectl get <resource>"
  "kubectl logs -f deploy/api -n prod" -> "kubectl logs -f <workload> -n <namespace>"

Do not invent flags that are not in the input.

Command: {command}"""

# Matches a single <placeholder> token. Used both to read placeholder names and
# to reduce a pattern to its structural signature.
_PLACEHOLDER_RE = re.compile(r"<[^>]*>")

# Characters that change what a command *does* rather than what it acts on.
# A generalization has no business introducing any of them.
_SHELL_METACHARS = frozenset(";|&<>$`\n")

# Placeholder names the model produces by copying instruction wording rather
# than describing the argument. Observed on a real history: `kubectl logs -f
# <descriptive_placeholder>/api-server -n <descriptive_placeholder>` — the old
# prompt literally contained the word "descriptive_placeholder", so the model
# echoed it back. Patterns built from these carry no information, and two
# different arguments collapsing to the same meaningless name makes the pattern
# actively wrong.
_MEANINGLESS_PLACEHOLDERS = frozenset(
    {
        "descriptive_placeholder",
        "placeholder",
        "descriptive_id",
        "descriptive_name",
        "descriptive_value",
        "descriptive_namespace",
        "descriptive_time_range",
        "value",
        "arg",
        "argument",
    }
)

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


def _trim_invented_tokens(pattern: str, command: str) -> str:
    """Drop trailing tokens the model added that the command never had.

    Observed against the real model: ``kubectl get secrets`` came back as
    ``kubectl get <resource> <output_format>``, inventing an argument. Besides
    being wrong, the extra token stopped the command from merging into the
    ``kubectl get <resource>`` pattern it belongs to — the model is
    non-deterministic, so on one run a sibling merged and on the next it did
    not, and the pattern list fragmented for no reason the user could see.

    Generalizing can only replace tokens, never add them, so anything past the
    command's own token count is surplus and is cut. Rejecting the whole
    pattern instead was measurably worse: it fell back to the raw command,
    trading a slightly wrong abstraction for no abstraction at all.

    Patterns that are *shorter* are left alone — quoting legitimately collapses
    tokens, as in ``git commit -m 'fix bug'`` -> ``git commit -m '<message>'``.
    """
    pattern_tokens = pattern.split()
    limit = len(command.split())
    if len(pattern_tokens) <= limit:
        return pattern
    return " ".join(pattern_tokens[:limit])


def _is_corrupt(pattern: str, command: str) -> bool:
    """True if a pattern must not be trusted as a generalization.

    Prompt-echoed placeholder names. The old prompt literally contained the
    words "descriptive_placeholder", and the model echoed them back: ``kubectl
    logs -f <descriptive_placeholder>/api-server -n <descriptive_placeholder>``.
    Two different arguments collapsing to the same meaningless name makes the
    pattern actively misleading, not merely unhelpful.

    Introduced shell metacharacters. Observed: ``kubectl get secrets`` came back
    as ``kubectl get <resource>;name:``. Generalizing replaces arguments — it
    cannot introduce a command separator or a redirection that the input never
    had, so one appearing is proof the output is corrupt. Worth catching
    precisely because the junk sits *outside* the angle brackets, where it
    changes the pattern's structural signature and splits one pattern in two.

    A pattern with no placeholders at all is not corrupt: ``git status`` has
    nothing to abstract, so being its own pattern is correct.
    """
    names = _PLACEHOLDER_RE.findall(pattern)
    if any(n.strip("<>").strip().lower() in _MEANINGLESS_PLACEHOLDERS for n in names):
        return True
    # Compare against the command with placeholders removed, so a metacharacter
    # the user really typed is still allowed through.
    introduced = set(_PLACEHOLDER_RE.sub("", pattern)) - set(command)
    return bool(introduced & _SHELL_METACHARS)


async def _generalize_commands(tool: str, unique_commands: list[str]) -> dict[str, str]:
    """Generalize each unique command via Apple FM guided generation.

    Returns a mapping from concrete command -> generalized pattern. A command
    the model fails on, or answers with placeholders copied from the prompt,
    maps to itself: degrading one command is better than losing it.

    A fresh session per command keeps each prompt small, at the cost of one
    round trip each. That cost is why callers cap how many commands reach
    this function per run.
    """
    import apple_fm_sdk as fm

    GeneralizedCommand = _get_generable_types()
    cmd_to_pattern: dict[str, str] = {}

    for cmd in unique_commands:
        try:
            session = fm.LanguageModelSession()
            prompt = GENERALIZE_PROMPT.format(tool=tool, command=cmd)
            result = await session.respond(prompt, generating=GeneralizedCommand)
            pattern = (result.pattern or "").strip()
        except Exception:
            logger.debug("generalization failed for %r", cmd, exc_info=True)
            pattern = ""

        pattern = _trim_invented_tokens(pattern, cmd)
        if not pattern or _is_corrupt(pattern, cmd):
            pattern = cmd
        cmd_to_pattern[cmd] = pattern

    return cmd_to_pattern


async def extract_patterns_for_tool(
    tool: str,
    commands: list[str],
    cache: dict[str, str] | None = None,
    budget: int | None = None,
) -> PatternExtractionResult:
    """Extract abstract patterns from a list of concrete commands.

    Strategy:
    1. Deduplicate commands and count frequencies (code)
    2. Generalize each unique command not already in the cache (LLM)
    3. Aggregate frequencies by generalized pattern (code)

    ``cache`` is the {command -> pattern} mapping from the previous run. It is
    consulted directly instead of being reconstructed from the stored patterns,
    which only kept one example each — the reason patterns used to decay into
    raw commands on every resync. It replaces a ``set[str]`` of "commands
    already seen", which by construction could not say what they mapped to; the
    parameter is renamed so old callers fail loudly instead of silently missing
    every cache hit.

    ``budget`` caps how many commands are sent to the model in one call.
    Uncapped, the first run over an accumulated history is tens of minutes of
    continuous on-device inference triggered from a shell hook.

    Falls back to simple frequency grouping if the SDK is unavailable.
    """
    cache = dict(cache or {})

    # Step 1: Count raw frequencies (code — fast and exact)
    raw_freq = Counter(commands)
    unique_cmds = list(raw_freq.keys())

    if not _apple_fm_available():
        return _heuristic_patterns(tool, commands)

    # Step 2: Generalize only what the cache does not already cover. Most
    # frequent first, so a budget-limited run spends the model on the commands
    # the user actually repeats.
    new_cmds = [c for c in unique_cmds if c not in cache]
    new_cmds.sort(key=lambda c: raw_freq[c], reverse=True)
    if budget is not None:
        new_cmds = new_cmds[:budget]

    if new_cmds:
        cache.update(await _generalize_commands(tool, new_cmds))

    # Step 3: Aggregate (code — exact counting). A command left unprocessed
    # because the budget ran out simply waits for the next run.
    #
    # Grouped by structural signature rather than by the exact pattern string.
    # The model is non-deterministic about placeholder *names*, so the same
    # command shape came back as `kubectl get <resource>` on one run and
    # `kubectl get <resource_type>` on the next, splitting one pattern into two
    # for a reason invisible to the user. Signature merges them; the name shown
    # is whichever spelling covered the most commands.
    sig_freq: Counter[str] = Counter()
    sig_names: dict[str, Counter[str]] = defaultdict(Counter)
    sig_example: dict[str, str] = {}
    for cmd, count in raw_freq.items():
        pattern = cache.get(cmd)
        if pattern is None:
            continue
        signature = _PLACEHOLDER_RE.sub("<>", pattern)
        sig_freq[signature] += count
        sig_names[signature][pattern] += count
        if signature not in sig_example:
            sig_example[signature] = cmd

    patterns = [
        CommandPattern(
            pattern=sig_names[sig].most_common(1)[0][0],
            example=sig_example[sig],
            frequency=freq,
        )
        for sig, freq in sig_freq.most_common()
    ]
    return PatternExtractionResult(tool=tool, patterns=patterns, command_patterns=cache)


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

    # Identity mapping for every command, not just the ten reported: without a
    # model each command *is* its own pattern, and the cache has to record all
    # of them or the next run treats them as unseen.
    return PatternExtractionResult(
        tool=tool,
        patterns=patterns,
        command_patterns={cmd: cmd for cmd in freq},
    )


def _load_cache(tool: str) -> dict[str, str]:
    """Read the {command -> pattern} cache for a tool.

    Files written before ``command_patterns`` existed only recorded which
    commands had been seen, with no way to recover what they mapped to. Those
    commands are treated as uncached so they get generalized once more, rather
    than being resurrected as raw patterns.
    """
    existing = storage.read_patterns(tool)
    if existing is None:
        return {}
    return dict(existing.command_patterns)


def run_pattern_extraction(
    tool: str,
    commands: list[str] | None = None,
    budget: int | None = None,
) -> int:
    """Extract patterns for a single tool and save to storage.

    ``commands`` lets a caller that has already read the history pass the
    tool's commands in. Re-reading every JSONL once per tool turned a sync
    into O(tools x history): 50 tools over a 100k-command history meant five
    million line parses, every 20 captures, in a background process.

    Returns the number of commands sent to the model, so a caller can spend a
    shared budget across tools.
    """
    import asyncio

    if commands is None:
        commands = [
            cmd.command
            for cmd in storage.read_all_commands()
            # A whitespace-only command has no first token. Indexing [0]
            # unguarded raised IndexError, and the caller swallowed it, so one
            # blank line could silently abort the whole sync.
            if cmd.command.split()[:1] == [tool]
        ]

    if len(commands) < 5:
        return 0  # Not enough data for meaningful patterns

    cache = _load_cache(tool)
    uncached = {c for c in set(commands) if c not in cache}
    if not uncached and storage.read_patterns(tool) is not None:
        return 0  # Nothing new to process

    result = asyncio.run(
        extract_patterns_for_tool(tool, commands, cache, budget=budget)
    )

    pf = PatternFile(
        tool=tool,
        patterns=result.patterns,
        last_updated=int(time.time()),
        processed_commands=sorted(result.command_patterns),
        command_patterns=result.command_patterns,
    )
    storage.write_patterns(pf)
    return len(result.command_patterns) - len(cache)


def sync_all_patterns(silent: bool = False) -> tuple[int, int]:
    """Extract patterns for ALL tools with sufficient command history.

    Detects unique tools (first token of each command), runs extraction
    for each tool with >5 commands. Skips tools with insufficient data.

    Args:
        silent: If True, suppress all output (for background auto-sync).

    Returns (new_patterns, updated_patterns) counts.
    """
    # Collect all commands grouped by tool (first token). Read once: passing
    # each tool's slice down avoids re-reading the whole history per tool.
    tool_commands: dict[str, list[str]] = defaultdict(list)
    for cmd in storage.read_all_commands():
        parts = cmd.command.split()
        if parts:
            tool_commands[parts[0]].append(cmd.command)

    new_count = 0
    updated_count = 0
    remaining = SYNC_BUDGET

    # Busiest tools first, so a run that exhausts the budget spent it on the
    # tools the user actually lives in.
    for tool, commands in sorted(
        tool_commands.items(), key=lambda kv: len(kv[1]), reverse=True
    ):
        if len(commands) < 5:
            continue  # Skip tools with too few commands
        if remaining <= 0:
            break  # Out of budget; the next sync picks up where this stopped

        existing = storage.read_patterns(tool)
        spent = run_pattern_extraction(tool, commands=commands, budget=remaining)
        remaining -= max(spent, 0)

        if existing is None:
            new_count += 1
        else:
            updated_count += 1

    if not silent and not _apple_fm_available():
        print(
            "Tip: install AI support for smarter pattern extraction: "
            "pip install cli-mem[ai]",
            file=sys.stderr,
        )

    return new_count, updated_count
