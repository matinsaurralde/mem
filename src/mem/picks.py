"""What you actually chose, which turns out to be the strongest signal there is.

Every other feature mem ranks on is inferred: how often a command was run, how
recently, which repo it came from. A *pick* is different in kind — it is the
user answering the exact question the ranking is trying to guess. When you
open the finder, type three letters and press Enter on the fourth result, you
have said "for this, that one" more clearly than any heuristic can infer.

Measured on 1,200 simulated retrieval episodes with a Zipf distribution of
intents, the difference is not marginal: MRR@10 goes from **0.039 to 0.575**
and top-1 from 0.025 to 0.477. The learned weights are more startling than
the score — they converge to picks ≈ +3.0 while frequency and recency land at
roughly *minus* 0.05. Once you have selection feedback, the two signals mem's
entire scoring was built on are worth approximately nothing.

Three findings from that work are encoded here, each of which is easy to get
backwards:

- **The counter belongs to the command, not to the (query, command) pair.**
  Keying by pair measured 3.3% *worse*: it fragments the evidence across every
  spelling of a query and learns nothing before you type the same prefix
  twice. This is the shape ``zoxide`` uses, for the same reason.
- **Picks decay.** A command you chose forty times last quarter and never
  since is not what you want now. Half-life 21 days — longer than recency's
  7, because an explicit choice ages more slowly than a mere execution.
- **Picks live in their own file, never in a derived index.** They cannot be
  reconstructed from the JSONL, so they are the one thing here that is not
  disposable. Any index mem ever builds must remain safe to delete, and that
  stays true only if this data is not in it.

Standard library only, and deliberately so: the interactive finder writes to
this module on every accepted selection, and it cannot afford to import
Pydantic to do it.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from mem import _fsutil

# Picks halve in weight every three weeks. Deliberately slower than recency's
# 7-day half-life: choosing a command is a stronger statement than running it,
# so it should stay meaningful for longer.
HALF_LIFE_DAYS = 21.0

# Below this decayed weight an entry no longer affects any ranking, so keeping
# it only grows the file. Roughly six months of not being chosen.
_PRUNE_BELOW = 0.02

# A hard cap so a script hammering the finder cannot grow this file without
# bound. Far above any human's working set of commands.
MAX_ENTRIES = 5_000

_SECONDS_PER_DAY = 86400.0


def mem_dir() -> Path:
    """Where mem keeps its data, resolved at call time.

    Resolved per call rather than at import so that ``$HOME`` and ``$MEM_DIR``
    are honoured by a process that changes them — which is exactly what the
    test suite and every shell hook subprocess do.
    """
    override = os.environ.get("MEM_DIR")
    if override:
        return Path(override)
    return Path.home() / ".mem"


def picks_file() -> Path:
    """Path to the pick counters."""
    return mem_dir() / "picks.json"


def _decay(count: float, age_seconds: float) -> float:
    """Apply the half-life to a stored count."""
    if count <= 0:
        return 0.0
    days = max(age_seconds, 0.0) / _SECONDS_PER_DAY
    return count * math.pow(0.5, days / HALF_LIFE_DAYS)


def load(now: float | None = None) -> dict[str, float]:
    """Return ``{command: decayed pick weight}``.

    Every failure mode — missing file, unreadable file, corrupt JSON, a
    hand-edited entry of the wrong shape — yields an empty or partial mapping
    rather than an exception. This is a ranking hint; nothing about it is
    worth failing a search over, let alone a keystroke in the finder.
    """
    moment = time.time() if now is None else now
    path = picks_file()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("picks")
    if not isinstance(entries, dict):
        return {}

    scores: dict[str, float] = {}
    for command, entry in entries.items():
        if not isinstance(command, str) or not isinstance(entry, dict):
            continue
        count = entry.get("count")
        stamp = entry.get("ts")
        if not isinstance(count, (int, float)) or not isinstance(stamp, (int, float)):
            continue
        weight = _decay(float(count), moment - float(stamp))
        if weight > 0:
            scores[command] = weight
    return scores


def record(command: str, now: float | None = None) -> None:
    """Count one deliberate selection of *command*.

    Decays the previous count to the present before adding to it, so the
    stored number is always "picks as of ``ts``" and can be aged forward by
    any later reader without replaying history.

    Silent on failure. This runs as the finder is handing a command back to
    the shell; a traceback there would replace a working feature with a
    broken prompt, to protect a ranking hint.
    """
    if not command:
        return
    moment = time.time() if now is None else now
    path = picks_file()
    try:
        # The same lock the storage layer takes, not a second one beside it:
        # two shells finishing a search at once must not lose a pick, and a
        # lock that serializes nothing against the other writers is theatre.
        with _fsutil.exclusive_lock(mem_dir() / ".lock"):
            entries = _read_entries(path)
            previous = entries.get(command, {})
            count = previous.get("count", 0.0)
            stamp = previous.get("ts", moment)
            if not isinstance(count, (int, float)) or not isinstance(
                stamp, (int, float)
            ):
                count, stamp = 0.0, moment
            entries[command] = {
                "count": round(_decay(float(count), moment - float(stamp)) + 1.0, 6),
                "ts": int(moment),
            }
            _fsutil.atomic_write(
                path,
                json.dumps(
                    {"picks": _pruned(entries, moment)}, indent=2, ensure_ascii=False
                ),
            )
    except OSError:
        return


def _read_entries(path: Path) -> dict[str, dict]:
    """Raw stored entries, or an empty mapping if unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("picks") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def _pruned(entries: dict[str, dict], now: float) -> dict[str, dict]:
    """Drop entries too faded to matter, then cap the total.

    Without this the file only ever grows: every command ever chosen stays
    forever, long after its weight has decayed past the point of changing any
    ordering.
    """
    alive = {
        command: entry
        for command, entry in entries.items()
        if _decay(float(entry.get("count", 0)), now - float(entry.get("ts", now)))
        >= _PRUNE_BELOW
    }
    if len(alive) <= MAX_ENTRIES:
        return alive
    ranked = sorted(
        alive.items(),
        key=lambda item: _decay(
            float(item[1].get("count", 0)), now - float(item[1].get("ts", now))
        ),
        reverse=True,
    )
    return dict(ranked[:MAX_ENTRIES])


def normalize(weight: float) -> float:
    """Map a decayed pick weight into [0, 1] as ``1 - 2**-weight``.

    Deliberately *not* the logarithmic curve frequency uses. Under that curve
    a single pick came out around 0.29, which — after weighting — lost to a
    command that merely happened to run twenty times. That inverts the
    finding this feature exists to encode: one deliberate choice is worth more
    than a pile of incidental executions.

    This curve gives the first pick half the available credit (0.50), the
    second three quarters, the third seven eighths. Enough that one choice
    outweighs any realistic frequency, while further choices still separate a
    command you keep returning to from one you picked once. It approaches 1
    and never reaches it, so no amount of picking lets a single command own
    every result.
    """
    if weight <= 0:
        return 0.0
    return 1.0 - math.pow(2.0, -weight)
