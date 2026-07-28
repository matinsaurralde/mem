"""
Terminal output for mem — the single place that turns data into text.

Why this module exists: every command string mem displays is
user-controlled, and Rich treats square brackets as markup. Interpolating a
command directly into a styled string therefore made mem display something
other than what the user actually ran, which for a history tool is the worst
possible defect — you copy a line and get a different command.

Three concrete failures this prevents:

- ``echo [red]hi[/red]`` rendered as ``echo hi``: the tags were consumed.
- ``sed 's/[a-z]//'`` lost the character class, silently.
- A bare ``[/]`` (ordinary in ``sed``/``awk``) raised ``MarkupError`` and
  crashed the command mid-output with a traceback.

The rule: chrome may be styled, data never is. Anything that came from the
user goes through :func:`safe` before it reaches a markup string, or is
passed as a :class:`rich.text.Text` object, which Rich never parses.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.text import Text

# highlight=False so Rich does not colourize things that look like numbers,
# paths or URLs inside a command. A command is text, not a value to prettify.
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


def safe(value: object) -> str:
    """Escape user-controlled text so Rich renders it literally.

    Use for anything interpolated into a string that Rich will parse for
    markup. ``escape`` backslash-escapes bracket sequences that would
    otherwise be read as tags, so the text survives byte-for-byte.
    """
    return escape(str(value))


def plain(value: object) -> Text:
    """Wrap user-controlled text as a Text object, immune to markup parsing.

    Preferred over :func:`safe` when the value is printed on its own, since
    it removes the escaping step entirely rather than relying on it.
    """
    return Text(str(value))


def fit(value: str, width: int) -> str:
    """Pad or truncate to an exact display width, with an ellipsis if cut.

    Applied to the raw string before escaping: escaping inserts backslashes
    that are not displayed, so measuring afterwards would misalign columns.
    """
    if width <= 0:
        return ""
    if len(value) <= width:
        return value.ljust(width)
    if width == 1:
        return "…"
    return value[: width - 1] + "…"
