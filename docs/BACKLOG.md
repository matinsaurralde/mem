# Backlog

One line per idea. Nothing here is planned; it is where a feature idea goes
so it does not turn into a change during an audit or a bug fix.

- `mem forget --noise`: remove lines matching `capture.looks_like_terminal_noise` from an existing store, for users who captured mouse reports before 0.5.1.
- `mem import` runs the same noise predicate as capture, so a polluted `~/.zsh_history` is not re-imported verbatim.
- Put `mem _capture` on the `_entry.py` fast path (stdlib-only append), taking the per-prompt cost from ~150 ms to a few tens of ms; only then is the "<5 ms" claim about more than `fork`.
- `MEM_DIR` honoured by `storage.py` as well as by `picks.py`/`tui.py`, and documented, or removed from the two.
- The concept-map fallback in the finder (`tui.py` does not import `concepts`, so Ctrl+R and `mem <query>` answer a question differently).
- `read_key` support for `~`-terminated and `;`-modified escape sequences (Delete, F-keys, Ctrl+arrows), bracketed paste, and a SIGTERM handler that restores the terminal.
- A `mem doctor` that reports what `mem` on `$PATH` actually is (Cellar version vs `--version`), whether the rc hook matches `mem init`, and whether the installed hooks are the shipped ones.
- Runbook files keyed by `repo_key` (hash-suffixed) with the same migration ADR-004 did for history.
- `mem vars` refusing to overwrite a corrupt `vars.json` (raise like `read_group_file`), plus a `mem vars repair`.
- A timed first-frame assertion for the finder in a real pty, so ADR-008's 50 ms budget is a test and not a sentence.
- Run the `perf` group in CI on a schedule, uninstrumented, with the numbers recorded.
- PyPI Trusted Publishing instead of the long-lived `PYPI_TOKEN` (standing TODO in CLAUDE.md).
