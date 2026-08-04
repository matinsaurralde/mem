"""The console script entry point, and the one place a fast path can exist.

``mem`` used to point straight at ``mem.cli:cli``, which meant Click, Rich and
Pydantic were imported before the first line of any command ran — ~180ms on a
warm cache, of which ~58ms is Pydantic alone. For ``mem search`` that is
merely wasteful. For the Ctrl+R finder it is fatal: the whole budget for
feeling instant is a few tens of milliseconds, and it was gone before the
process did anything.

So the console script lands here instead. This module imports nothing beyond
``sys``; it looks at the first argument and imports only the module that
argument needs. ``mem tui`` reaches the finder in ~25ms; everything else takes
the Click path exactly as before.

Adding a second console script (``mem-tui``) would have been simpler, but it
splits one tool into two names in the user's PATH and in every set of
instructions. The dispatch is four lines and keeps the surface at one binary.
"""

from __future__ import annotations

import sys

# Subcommands that must not pay for the Click/Rich/Pydantic import graph.
# Keep this list short and obvious: anything here bypasses argument parsing,
# so it has to handle its own arguments and its own --help.
_FAST_PATHS = {"tui"}


def main() -> None:
    """Dispatch to the fast path when possible, otherwise to the full CLI."""
    if len(sys.argv) > 1 and sys.argv[1] in _FAST_PATHS:
        from mem.tui import main as tui_main

        raise SystemExit(tui_main(sys.argv[2:]))

    from mem.cli import cli

    cli()
