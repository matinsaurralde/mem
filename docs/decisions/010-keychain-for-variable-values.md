# ADR-010: Variable Values Live in the macOS Keychain, Not in a 0600 File

**Status**: Accepted — amends PHILOSOPHY.md (Principle III) for the variable store only.
**Date**: 2026-08-04

## Context

`mem vars set API_TOKEN` wrote the value into `~/.mem/vars.json` as cleartext JSON, mode 0600:

```json
{"vars": {"API_TOKEN": {"value": "sk-live-…", "last_used": 0}}}
```

0600 stops *other users*. It stops nothing else, and nothing else is the actual threat model for a developer laptop:

- Time Machine, Backblaze, or any backup agent copies it verbatim, and the copy is not 0600 anywhere it lands.
- A `~/Dropbox`-style synced folder, or a dotfiles repo that globbed too widely, publishes it.
- Every process running as the user reads it without so much as a prompt — an `npm install` postinstall script, a VS Code extension, a `curl | sh` that went badly.
- A stolen machine that was awake or a disk image taken from it gives it up directly.

For a tool whose first principle is *privacy* and whose README says "your shell history never leaves the machine", a plaintext file of API tokens and database passwords was the weakest thing in it — and the one that would appear in the first security review.

macOS ships the purpose-built answer, and this project has already decided to lean into the platform (ADR-002, ADR-006, ADR-008): the **Keychain**. Items are encrypted at rest with a key derived from the login password, the login keychain locks with the session, and access is mediated by the OS rather than by file permissions.

The constraint is that `security(1)` is the only way in without a dependency, and `security` is a tool with sharp edges. Everything below was established by running it, not by reading the man page:

| Behaviour | Observed |
|---|---|
| `-w <secret>` | Documented, and puts the secret in `argv` — `security`'s own help says "Use of the -p or -w options is insecure" |
| `-w` as the last option (prompts) | Needs a controlling terminal: with a pipe on stdin it exits 2, and with `-w` omitted entirely it silently stores an **empty** password |
| `security -i` | Reads command lines from **stdin**; `argv` is just `["security", "-i"]` |
| `-X <hex>` | Takes the value as hexadecimal — no quoting, no metacharacters, no newlines |
| `find-generic-password -w` | Prints printable data raw and binary data as bare hex — **indistinguishable**, so a 32-character hex API key decodes into 16 bytes of garbage |
| `find-generic-password -g` | Unambiguous: `password: 0x<HEX>  "<escaped>"` for anything non-printable, `password: "<value>"` otherwise |
| `security -i` line length | Truncates at 4096 characters **and executes the truncated head**. An 8 KB value produced a Keychain item holding a silently truncated secret |

## Decision

**Variable values are stored in the macOS Keychain as generic passwords.** `~/.mem/vars.json` remains, demoted to an index: which variables exist and when each was last used.

Concretely:

- **Service** `mem-cli-vars`, **account** = the variable name, **label** `mem-cli-vars:API_TOKEN`. One `grep` in Keychain Access finds everything mem owns and nothing else.
- **Writes go over stdin**, via `security -i`, hex-encoded with `-X`. The secret never appears in `argv`, so `ps` never shows it; the hex sidesteps the interactive tokenizer entirely, so a value containing `"`, `\`, `$(…)` or a newline cannot be misparsed. The item still holds the **real bytes**, so `security find-generic-password -s mem-cli-vars -a API_TOKEN -w` returns the user's own secret without mem in the loop.
- **Reads use `-g`**, never `-w`, for the disambiguation reason in the table above.
- **A value that would not fit in one command line is refused** (`KeychainValueTooLong`), because the alternative is a truncated secret stored under a name that claims to be intact. That caps a value at roughly 2 KB.
- **Migration is confirm-then-delete, per variable.** Write to the Keychain → read it back → compare → only then rewrite `vars.json` without the value, under the existing `exclusive_lock` and `atomic_write`. It runs on first use of any `mem vars` command and on the `mem run` resolution path, it is idempotent, and every failure mode leaves the plaintext copy exactly where it was for the next attempt to find.
- **There is no plaintext fallback for new values.** When the Keychain is unavailable, `mem vars set` fails, says why in `security`'s own words, and stores nothing.
- **Values that could not be migrated keep working**, and are reported as `plaintext` in `mem vars list` with a warning, every time. Refusing to read them would punish the user for the state of their keychain, and deleting them would be data loss.
- **The backend is derived, not declared.** An entry is Keychain-backed *because* it has no value in the file. A `backend` field that could disagree with the file's contents would eventually claim "keychain" over a cleartext secret.

## Cost of this decision (stated plainly)

- **A subprocess per read.** ~15 ms per variable, on `mem run` only, and only for the variables that run actually needs. Listing a runbook deliberately does not read values.
- **A possible OS prompt.** `security` is trusted to read what `security` created, so in practice mem does not trigger the "wants to access your keychain" dialog — but a locked keychain, a keychain-access reset, or a login-password change can, and mem cannot predict it. Every call is therefore bounded by a 60 s timeout, so a dialog nobody sees fails the command instead of wedging the shell.
- **macOS only.** The variable store now genuinely does not work off macOS, where before it degraded to a file that worked everywhere. mem is a macOS tool by decision; this makes it true of one more subsystem.
- **`mem forget` costs one `security` call per stored variable**, because matching a value requires reading it. It is rare, explicit and destructive; the alternative — matching on names only — would report success while leaving the secret in the Keychain.
- **mem now writes outside `~/.mem`.** Uninstalling by `rm -rf ~/.mem` leaves the Keychain items behind. `mem vars clear` is the way to take them back, and the items are visible and deletable in Keychain Access.
- **The old plaintext may survive on disk.** Migration replaces `vars.json` atomically, but the previous contents can persist in unallocated blocks. The Keychain protects future writes; it cannot un-write the past.

## Alternatives Considered

- **Do nothing — keep the 0600 file.** Rejected. It is the threat model above, unanswered, in the one project whose entire pitch is privacy. "Nobody else can read your files" is a claim about multi-user Unix in 1985, not about a laptop running a package manager.
- **Encrypt the file with a passphrase mem asks for.** Rejected on three counts. It would need a KDF and an AEAD (`hashlib.pbkdf2_hmac` exists; a well-reviewed AEAD in the stdlib does not — rolling our own is exactly the mistake this ADR is trying to avoid). It would prompt on every `mem run`, which Principle V ("a tool that nags will be uninstalled within a day") rules out, and caching the derived key to avoid that recreates a smaller version of the original problem. And the passphrase would in practice be the login password, typed into a CLI instead of into the OS — strictly worse than letting the OS hold it.
- **Keychain via a library (`keyring`, `pyobjc`).** Rejected: new dependencies, which is a hard constitutional rule, and both are far larger than the ~250 lines of `subprocess` this replaces. `keyring` also brings a plugin architecture whose backends are decided at runtime by what is installed — the opposite of a tool that can state where a secret is.
- **`security add-generic-password -w <secret>` (the documented form).** Rejected: `argv` is world-readable through `ps`. Any process on the machine can watch for it. This is the single detail that most implementations get wrong.
- **The `-w` prompt, driven through a pty.** Rejected after testing: with a pipe on stdin it is a usage error, and a pty in canonical mode caps a line at 1024 bytes and cannot carry a newline — so it is both smaller and less capable than `security -i`, for more machinery.
- **Store our own hex encoding as the password text** (so `-w` reads unambiguously). Rejected: it makes the item unreadable to anything but mem. `security find-generic-password -w` and Keychain Access would show the user hexadecimal instead of their own token, which turns a credential store into lock-in. Parsing `-g` is a few lines of ours; opacity would be the user's forever.
- **Split long values across several Keychain items.** Rejected as complexity for a case that does not exist: a variable is a shell value, and 2 KB is far past any token or password. A private key belongs in a file whose *path* is the variable.
- **Silently fall back to plaintext when the Keychain is unavailable.** Rejected, emphatically. It is the most dangerous option on this list: the user reads "values are stored in the Keychain", believes it, and stops treating `~/.mem` as sensitive — while mem quietly writes cleartext on exactly the machines (CI, a locked laptop, a shared box) where it matters most. A failure the user sees is worth more than a success that is not true.

## Consequences

- `vars.json` becomes an index, and its `value` field is `null` for every entry mem writes from now on. It stays 0600: the *list* of variable names is worth keeping private on its own, and any not-yet-migrated entry is still a cleartext secret.
- Principle III says the plain-text files are the only source of truth. For variable **values** that is no longer true, and this ADR is the public amendment rather than a quiet exception. The properties the principle exists to protect still hold: there is exactly one source of truth for a value, it is not mem's private format, and the user can read it without mem (`security find-generic-password`, Keychain Access). No mem-owned second copy exists anywhere.
- The test suite must never touch the developer's login keychain, so `conftest.FakeKeychain` replaces the single function that spawns `security`, and the genuine end-to-end tests are marked `keychain_live` — deselected by default, run with `pytest -m keychain_live`, and pointed at a throwaway keychain via `MEM_KEYCHAIN`.
- `mem vars set NAME` now hides what is typed, as the README always claimed it did. Passing the value inline still works and now warns that it was visible to `ps` and captured into the user's own shell history — mem's `argv` is the last place a secret can still leak, and mem is the thing recording it.
- Anyone porting mem beyond macOS inherits a clear seam: `mem/keychain.py` is the whole backend, and `unavailable_reason()` is where another platform's answer would go.
