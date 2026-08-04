# ADR-006: Apple App Intents, Shortcuts and Spotlight — Evaluated and Deferred

**Status**: Accepted (deferred, with an explicit unblocking condition)
**Date**: 2026-08-04

## Context

mem is macOS-only by choice, and the stated strategy was to lean into Apple's platform: Foundation Models, App Intents, Shortcuts, and Spotlight. Research into the last three found a hard technical blocker that no amount of implementation effort removes.

All three frameworks are vended by a **registered application bundle**. The system discovers App Intents by scanning the intent metadata of installed, registered bundles; Shortcuts lists actions supplied by those bundles; Core Spotlight indexes items on behalf of a bundle identifier, with the index scoped and attributed to that bundle. A CLI installed with `pip install cli-mem` or `brew install mem` is an executable on `$PATH`. It has no `Info.plist`, no bundle identifier, no LaunchServices registration — there is nothing for the system to attach an intent, an action, or an index entry to.

Additionally, `SpotlightSearchTool` (the Foundation Models tool that reads the Spotlight index) is Swift-only and is not exposed by the Python SDK mem uses.

## What we would have gotten

- **App Intents**: `mem search`, `mem run <group>`, and `mem session` as first-class system actions with typed parameters — invocable from Siri, Spotlight, Focus filters, and Automations, without a terminal open.
- **Shortcuts**: users composing runbooks visually and sharing them, and mem participating in other people's automations — a distribution channel mem has no equivalent of today.
- **Spotlight (Core Spotlight)**: commands, patterns, and runbooks findable from ⌘-Space, ranked by the system, with the OS doing the indexing work described in ADR-005.

Each is genuinely valuable. None is reachable from a CLI, no matter how it is written.

## Decision

App Intents, Shortcuts and Spotlight integration are **deferred, not planned**. mem does not attempt partial or simulated versions of them.

**The Apple bet is Foundation Models only.** That is where a Python CLI has real, verified traction (ADR-002), and it is the part of the platform that does not require a bundle.

**The single condition that unblocks this decision**: mem ships a real, signed, notarized `.app` bundle that registers with LaunchServices, embeds the App Intents/Shortcuts extension in Swift, and carries the CLI inside it — with the CLI and the bundle sharing `~/.mem`. That is a second distribution channel and a second language in the build, i.e. a different project, not a feature. If that day comes, this ADR is superseded rather than amended.

## Alternatives Considered

- **Build the `.app` wrapper now.** Rejected: code signing, notarization, a Swift target, a cask, and an update path for a bundle — all before a single user-visible improvement to the CLI itself. The project has higher-value work that is fully within reach.
- **Ship the CLI *and* keep pretending the integration is coming.** Rejected explicitly: the original strategy named all four Apple technologies, and three of them are unreachable. Recording that in an ADR is cheaper than someone re-litigating it in six months.
- **`PrivateCloudComputeLanguageModel` for the parts on-device inference handles poorly.** Rejected: it sends data off the machine, which Principle I forbids without exception (and it is Swift-only besides). Zero network is the one property competitors cannot copy without cannibalizing themselves; trading it for better session summaries is the worst trade available.
- **The Shortcuts "Run Shell Script" action.** Not an alternative we implement — it is what users can already do today, with no work from us. Worth documenting for users; it is not App Intents, since mem supplies no typed actions and gets no Siri or Spotlight surface from it.

## Consequences

- mem has no Siri, Shortcuts, or Spotlight surface, and will not have one while it is distributed as a CLI. This is a known, accepted gap, not an oversight.
- `apple-fm-sdk` remains the only Apple dependency, and stays optional: without it, capture and search work exactly as before (ADR-002).
- Strategic note worth writing down while the decision is fresh: **on-device AI stops being a moat this year.** The defensible differentiator has to be *what* mem infers from your own history — patterns, failure→recovery pairs, runbook promotion — not *where* the model runs. Platform integration would have been convenience; it was never the moat.
