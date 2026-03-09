# ADR-002: Apple Foundation Models for Pattern Extraction

**Status**: Accepted
**Date**: 2026-03-08

## Context

mem extracts abstract command patterns from concrete shell history (e.g., `kubectl get pods`, `kubectl get services` → `kubectl get <resource>`). The extraction mechanism must run entirely on-device to preserve privacy, handle arbitrary CLI tools without per-tool rules, and produce structured output.

## Decision

Use Apple Foundation Models via `apple-fm-sdk` with Pydantic-based guided generation.

## Alternatives Considered

- **Regex heuristics**: Fails on novel tools and cannot generalize across argument variations. Requires per-tool maintenance that doesn't scale.
- **Local embeddings / Ollama**: Heavyweight. Requires separate model downloads, GPU memory management, and a running server process. Overkill for structured extraction.
- **Cloud LLM APIs**: Violates the Privacy First principle. Sending shell history to a remote server is a non-starter.

## Consequences

- All inference runs on the Mac's Neural Engine — zero network requests.
- Guided generation with a Pydantic schema produces validated, typed output directly.
- Requires macOS 26+ with Apple Intelligence enabled.
- mem degrades gracefully without the SDK: search and capture work fine, only pattern extraction is disabled.
