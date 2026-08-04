# ADR-009: Ranking Learns From What You Select

**Status**: Accepted
**Date**: 2026-08-04

## Context

Every feature mem ranked on was an *inference about* the user: how often a command was run, how recently, which repo it came from. None of them was the user saying what they wanted.

The interactive finder changed that. When someone opens it, types three letters and presses Enter on the fourth result, they have answered — for that command, unambiguously — the exact question the ranking formula spends the rest of its life guessing at. That answer was being thrown away.

Simulated over 1,200 retrieval episodes with a Zipf distribution of intents (the shape real command usage has), the difference is not incremental:

| ranking | MRR@10 | top-1 | latency |
|---|---|---|---|
| frequency + recency + context, as shipped | 0.039 | 0.025 | 7.7 ms/query |
| + decayed selection counts, learned weights | **0.575** | **0.477** | 4.80 ms/query |

**14× better and 1.6× faster.** The learned weights say something more uncomfortable than the score does. They converge to `picks=+3.02, cov=+1.60, prefix=+1.04, session=+0.82, repoctx=+0.16` — and `freq=-0.06, recency=-0.05`. Once selection feedback exists, frequency and recency, the two signals mem's entire scoring was built on, are worth approximately zero.

## Decision

Record every command accepted in the finder, decayed over time, and make it the highest-weighted feature in the ranking formula.

```
score = 0.40*picks + 0.21*frequency + 0.21*recency + 0.09*prefix + 0.09*context
```

Four choices inside that, each of which is easy to get backwards:

**The counter belongs to the command, not to the (query, command) pair.** Keying by pair measured **3.3% worse**. It fragments the evidence across every spelling of a query and learns nothing until the same prefix is typed twice. This is the shape `zoxide` uses, for the same reason.

**Picks decay, with a 21-day half-life.** Deliberately slower than recency's 7 days: choosing a command is a stronger statement than running it, so it should stay meaningful longer. A command picked forty times last quarter and never since is not what you want now.

**The remaining 0.60 is split in exactly the proportions the four original features had among themselves** (35/35/15/15 → 21/21/9/9). With no picks recorded, every score is the old score scaled by 0.60, so the *ordering is identical*. Introducing a signal that requires data nobody has yet must be a no-op until they have it — and the entire existing test suite passing unchanged is the evidence that it is.

**A single pick outweighs any realistic frequency.** Normalised as `1 - 2**-weight` rather than the logarithmic curve frequency uses: under the logarithmic curve one pick came out at 0.29 and *lost* to a command that merely happened to run twenty times, inverting the finding this feature exists to encode. The chosen curve gives the first pick half the available credit, the second three quarters, the third seven eighths — approaching 1 and never reaching it, so no amount of picking lets one command own every result.

Verified end to end: with `git commit --amend` run 20 times and `git commit -v` run once, `mem "git commit"` ranks `--amend` first. After a single selection of `git commit -v` in the finder, it ranks first.

## Where it is stored, and why that matters

`~/.mem/picks.json`, owner-only, written atomically under the same lock as everything else.

**Never inside a derived index.** ADR-005 permits a local index on the binding condition that it stays discardable — that `rm ~/.mem/index.db` is always a safe answer to any problem. Pick counters cannot be reconstructed from the JSONL. Putting them in the index would quietly turn the universal fix into data loss, and nobody would notice until they had already run it.

Consequently the file lives in a standard-library-only module: the finder writes to it on every accepted selection, and it cannot afford to import Pydantic to do so. The shared file primitives were extracted to `mem/_fsutil.py` rather than duplicated, because a second atomic-write implementation is the same defect pattern that put a stale shell hook in front of every pip user.

## Cost of this decision (stated plainly)

- **A cold start is unchanged, not better.** A new user, or one who only uses `mem <query>` and never the finder, gets exactly today's ranking. This feature is worth nothing to them until they use it. That is a real limitation and the reason frequency and recency were kept meaningful rather than set to the measured-optimal ≈0.
- **It creates data that cannot be regenerated.** Everything else under `~/.mem` can be rebuilt from the JSONL; this cannot. It is now the one file worth backing up.
- **A mistaken selection is learned.** Picking the wrong result teaches the wrong lesson. The 21-day decay bounds how long that costs, and `mem forget` reaches the file, but there is no undo for a single pick.
- **The measurement is a simulation, not a field study.** The 0.039 → 0.575 figure comes from synthetic episodes with a Zipf intent distribution. The distribution is realistic and the mechanism is sound, but nobody has yet measured it against a real user's real week.

## Alternatives rejected

**Do nothing.** The strongest available signal is generated by a feature that already exists, and was being discarded.

**Key on (query, command) pairs.** Measured 3.3% worse — see above.

**Online weight learning (RankNet-lite, pairwise).** Measured at 1.67 ms/episode, converging in ~1,500 selections, and reaching 0.528 against 0.529 for fixed weights when intents *do not* repeat — i.e. no gain for the user it would help least. Deferred: it is strictly more machinery for a result the fixed weights already reach, and it would need to be opt-in.

**Adopt the learned weights wholesale** (`freq=-0.06`, `recency=-0.05`). Rejected because those weights are optimal *given* accumulated picks. Applying them on day one would make ranking worse for every user until they had built up selection history — optimising the endgame at the expense of the opening.
