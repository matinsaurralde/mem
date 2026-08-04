# ADR-011: A Hand-Written Concept Map, Not an Embedding and Not an LLM

**Status**: Accepted
**Date**: 2026-08-04

## Context

mem's search is substring matching: every word of the query must appear in the command. That is exactly right for the way the tool is used most of the time — `mem docker compose`, `mem kubectl apply` — and it is useless for the way people actually remember. Nobody recalls `openssl x509 -in cert.pem -noout -dates`. They recall *"the command I used to fix the certificate"*, and those two strings share no word at all.

The gap was measured, not assumed. Ten questions phrased the way a person would ask them ("how do I see what's listening on a port", "the command I used to fix the certificate") were run against a real shell history, scoring recall@5 — is the right command in the first five results:

| approach | recall@5 | latency |
|---|---|---|
| Substring search (what mem shipped) | **0/10** | 9.6 ms |
| Apple Foundation Models expanding the query | 2/10 | 2086 ms |
| Hybrid ranker, no synonyms | 2/10 | 2.4 ms |
| **Hybrid ranker + a ~30-line synonym map** | **8/10** | **3.9 ms** |

Zero out of ten. Not a ranking problem — the correct answer was not in the result set at all, because it could not be.

Two model-based approaches were tried against that baseline and both lost.

**On-device LLM query expansion (2/10, 2086 ms).** Asking the Apple Foundation Model to expand the query costs two seconds — five hundred times the map, and roughly twenty times mem's *entire* interactive budget — for a quarter of the accuracy. It also has no grounding in the user's history and invents accordingly: asked about fixing a certificate it suggested `certutil /rebuild`, a **Windows** tool, on a macOS-only product. A wrong answer delivered confidently after a two-second pause is worse than no answer delivered instantly.

**`NLContextualEmbedding` (0/5 top-1).** Apple's on-device sentence embedding scored zero on top-1 over five questions. The reason is structural, not a tuning failure: it is a natural-language BERT, and `lsof -i :8080` is not natural language. Shell commands are out of distribution — flags, punctuation, paths and binaries that never appear in the pretraining corpus. It maps "port" and "listening" close together happily; it has no idea `lsof` belongs anywhere near either.

## Decision

Ship a **curated concept map** at `src/mem/concepts.json`: ~200 entries from a natural-language concept to the shell vocabulary that expresses it, read through `importlib.resources` exactly like the shell hooks.

```json
"certificate": ["openssl", "x509", "cert", "pem", "tls", "certbot", "acme"],
"port":        ["lsof", "netstat", "listen", "port", "8080"],
"disk space":  ["df ", "du ", "ncdu", "diskutil", "df -h", "du -sh"]
```

`~/.mem/concepts.json` layers over it: a concept defined there replaces the shipped one, new concepts are added, `_stopwords` are unioned so the map translates. A malformed user file warns on stderr and falls back; it never raises.

Expansion is a **fallback, not a blend**. The literal pass runs first and unchanged; the map is consulted only when it returns nothing. Within the expanded pass a candidate must satisfy at least one concept *through a synonym*, and its score is scaled by idf-weighted coverage.

## Why a dictionary beats an embedding here (the general form)

An embedding is a compressed, lossy, unlabelled guess about a semantic space it was not trained on. A dictionary is 200 lines a developer can read in five minutes. For this problem the dictionary wins on every axis that matters:

- **It is auditable.** When a query returns the wrong thing you can see *why*, in a file, with `grep`. The embedding's answer is a number.
- **It is fixable.** A wrong synonym is a one-line pull request. A wrong neighbourhood in an embedding is a research project.
- **It is deterministic.** The same query returns the same results forever — the same reason ADR-001 chose JSONL and ADR-003 chose no daemon.
- **It is translatable and contributable.** Concepts and stopwords are both data, so a Spanish speaker adds `"certificado"` and `"para"` without touching Python.
- **It costs nothing.** No model download, no memory, no runtime, no dependency, and nothing that could ever want the network (Principle I).
- **It is small enough to be right.** The vocabulary of a working shell is a few hundred concepts, not a distribution to be learned. This is a domain where the map genuinely fits on a page.

The honest limit: a dictionary only knows what someone wrote down. That is a real cost, accepted below.

## Consequences

- **Curation is now a maintenance duty.** The map is data, and data rots quietly. `tests/test_concepts.py` enforces the curation rules that can be checked mechanically — every synonym must survive the search prefilter, no synonym may be a stopword, no duplicates, keys normalised — and pins recall@5 over a fixture history so the claim this feature rests on is verified rather than remembered.
- **A bad entry is inert, not harmful.** Expansion cannot demote, dilute or reorder anything the literal search returns, and idf weighting drives a synonym that matches half the candidates to a weight below 0.2. The worst a careless entry can do is waste a fallback that would otherwise have returned nothing.
- **A missing entry is silent.** The failure mode is a question with no answer, indistinguishable from a history that never contained one. `mem concepts > ~/.mem/concepts.json` is the escape hatch, and the reason the file is shipped as editable data rather than compiled into Python.
- **English until someone translates it.** The shipped stopwords are English question scaffolding. A user thinking in another language gets literal search until they add their own — which is a JSON edit, not a fork.
- **The fallback path is slower than the literal one**, because it cannot use the AND prefilter: measured at ~60 ms against 20k commands, versus ~11 ms for a literal query. It only runs for queries that would otherwise have returned nothing, so nobody waits for it in the common case — and it is still 30× faster than the LLM that scored a quarter as well.
- **This does not close the door on the model.** It sets the bar. Any future semantic search has to beat 8/10 at 3.9 ms, on device, with an answer a user can inspect. As with ADR-008: this is arithmetic, not optimism.
