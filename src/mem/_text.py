"""Text rules shared by both output surfaces, the CLI and the Ctrl+R finder.

Standard library only: the finder imports it before Rich is loaded.
"""

from __future__ import annotations


def printable(text: str) -> str:
    """Replace every character a terminal would act on, rather than show, with ``?``.

    History is untrusted input — whatever somebody pasted into a shell — and a
    stray escape sequence repaints the screen, sets a colour that bleeds into
    every later row, or rewrites the window title, purely by being listed.
    """
    return "".join(ch if ch.isprintable() or ch == " " else "?" for ch in text)
