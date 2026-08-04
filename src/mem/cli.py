"""
CLI interface for mem — the user-facing command layer.

Every command here maps to a user story from the specification.
Click handles argument parsing; Rich handles output formatting.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
from importlib import resources
from pathlib import Path

import click
from rich.panel import Panel
from rich.text import Text

from mem import __version__
from mem.capture import get_git_repo
from mem.history import SUPPORTED_SHELLS as IMPORTABLE_SHELLS
from mem.history import ImportPlan
from mem.render import console, err_console, fit, plain, safe


class MemGroup(click.Group):
    """Custom group that treats unknown commands as search queries."""

    def invoke(self, ctx):
        # If the first arg isn't a known subcommand, treat it as a search query
        args = list(ctx.protected_args) + list(ctx.args)
        if args and args[0] not in self.commands:
            ctx.ensure_object(dict)
            ctx.obj["query_args"] = args
            ctx.protected_args.clear()
            ctx.args.clear()
        return super().invoke(ctx)


def _current_repo() -> str | None:
    """Detect the git repo for the current working directory."""
    return get_git_repo(os.getcwd())


def _is_interactive() -> bool:
    """Check if stdin is connected to a terminal."""
    return sys.stdin.isatty()


def _relative_time(ts: int) -> str:
    """Format a timestamp as a human-readable relative time."""
    import time

    delta = int(time.time()) - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    d = delta // 86400
    if d == 1:
        return "1d ago"
    if d < 7:
        return f"{d}d ago"
    w = d // 7
    return f"{w}w ago"


@click.group(cls=MemGroup, invoke_without_command=True)
@click.version_option(__version__, prog_name="mem")
@click.option(
    "--pattern", "-p", is_flag=True, help="Show extracted patterns instead of commands"
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.option("--limit", "-n", default=10, help="Maximum results")
@click.pass_context
def cli(ctx: click.Context, pattern: bool, as_json: bool, limit: int) -> None:
    """mem — your shell history, understood."""
    if ctx.invoked_subcommand is not None:
        return

    ctx.ensure_object(dict)
    query_args = ctx.obj.get("query_args", [])
    # Join instead of taking [0]: every word the user typed is part of the
    # query. Keeping only the first silently answered a different question.
    query = " ".join(query_args) if query_args else None
    if query is None:
        click.echo(ctx.get_help())
        return

    from mem.search import search, search_patterns

    if pattern:
        # Show extracted patterns for the tool
        patterns = search_patterns(query)
        if as_json:
            click.echo(json.dumps([p.model_dump() for p in patterns], indent=2))
            return
        if not patterns:
            console.print(f'No patterns found for "{query}".')
            return
        console.print(f'\nPatterns for "{query}":\n')
        for p in patterns:
            # Highlight placeholders in yellow
            text = Text(f"  {p.pattern}")
            console.print(text, style="white")
        console.print()
        return

    # Default: search command history
    repo = _current_repo()
    results = search(query, current_repo=repo, limit=limit)

    if as_json:
        output = [
            {
                "command": cmd.command,
                "repo": cmd.repo,
                "timestamp": cmd.ts,
                "score": round(score, 4),
                "exit_code": cmd.exit_code,
                "duration_ms": cmd.duration_ms,
            }
            for cmd, score in results
        ]
        click.echo(json.dumps(output, indent=2))
        return

    if not results:
        return  # Empty results, no error (exit 0)

    for i, (cmd, score) in enumerate(results, 1):
        rank = f" {i:>2}"
        command_text = fit(cmd.command, 40)
        repo_text = fit(cmd.repo or "global", 12)
        time_text = _relative_time(cmd.ts)
        console.print(
            f"{rank}  {safe(command_text)}  [dim cyan]{safe(repo_text)}[/]"
            f"  [dim]{time_text}[/]"
        )


@cli.command(name="_capture", hidden=True)
@click.argument("command")
@click.argument("dir")
@click.argument("exit_code", type=int)
@click.argument("duration_ms", type=int)
def capture_cmd(command: str, dir: str, exit_code: int, duration_ms: int) -> None:
    """Internal: called by the shell hook after each command. Always silent, always exits 0."""
    try:
        from mem.capture import capture_command

        capture_command(command, dir, exit_code, duration_ms)
    except Exception:
        # Silent failure — never disrupt the user's shell
        pass


# Shells mem can emit a capture hook for. Deliberately distinct from
# `history.SUPPORTED_SHELLS` (imported above as IMPORTABLE_SHELLS), which is
# the shells whose *history file* mem knows how to parse. The two sets happen
# to match today and are different questions.
SUPPORTED_SHELLS = ("zsh", "bash", "fish")


def read_hook(shell: str) -> str:
    """Return the hook source for *shell*, as shipped inside the package.

    The hooks live at ``mem/hooks/mem.<shell>`` and are read through
    ``importlib.resources``, which resolves them the same way for an editable
    checkout, a wheel, and a zipimport. The previous implementation walked
    ``__file__.parent.parent.parent / "hooks"`` — a path that only exists in a
    source checkout — and fell back to a second, hand-maintained copy of every
    hook inlined in this module. So the code every pip and Homebrew user
    actually ran was the copy nobody edited, and nothing detected the drift.
    One file per shell, one reader, no fallback.
    """
    return (
        resources.files("mem")
        .joinpath("hooks", f"mem.{shell}")
        .read_text(encoding="utf-8")
    )


@cli.command()
@click.argument("shell")
def init(shell: str) -> None:
    """Print shell hook code for automatic command capture."""
    if shell not in SUPPORTED_SHELLS:
        click.echo(
            f'Error: unsupported shell "{shell}". Supported: zsh, bash, fish', err=True
        )
        sys.exit(1)

    click.echo(read_hook(shell))


@cli.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("query", nargs=-1)
def tui(query: tuple[str, ...]) -> None:
    """Interactive history finder (bound to Ctrl+R by the shell hook).

    Registered here so it shows up in ``mem --help`` and behaves like every
    other subcommand. It is not normally reached through this path:
    ``mem/_entry.py`` dispatches ``mem tui`` before Click is imported,
    because the finder's entire latency budget is smaller than that import.
    Reaching it through Click still works — it is just slower to appear.
    """
    from mem.tui import main as tui_main

    sys.exit(tui_main(list(query)))


@cli.command(name="_sync", hidden=True)
def sync_cmd() -> None:
    """Internal: background pattern extraction and data rotation.

    Triggered automatically every 20 captured commands. Runs silently —
    no output, no errors. Never called by the user directly.
    """
    from mem import storage

    # One sync at a time. Extraction takes seconds to minutes, while the
    # threshold that triggers it can be crossed by several terminals at once —
    # without this, N shells each start their own full pass over the same
    # history and compete for the neural engine.
    if not storage.try_sync_lock():
        return

    try:
        from mem.patterns import sync_all_patterns

        sync_all_patterns(silent=True)
        # Rotation lives here rather than in the capture path because it
        # rewrites every history file, which is far too much work to do on a
        # prompt. It also means retention only ever runs if this command does —
        # which for four months it did not.
        storage.rotate()
    except Exception:
        logging.getLogger("mem.sync").debug("background sync failed", exc_info=True)


@cli.command()
@click.argument("query")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def session(query: str, as_json: bool) -> None:
    """Search and replay past work sessions."""
    from mem.search import search_sessions

    results = search_sessions(query)

    if as_json:
        output = [
            {
                "id": s.id,
                "summary": s.summary,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "repo": s.repo,
                "commands": s.commands,
            }
            for s in results
        ]
        click.echo(json.dumps(output, indent=2))
        return

    if not results:
        console.print("No matching sessions found.")
        return

    from datetime import datetime, timezone

    for i, s in enumerate(results, 1):
        dt = datetime.fromtimestamp(s.started_at, tz=timezone.utc)
        header = safe(
            f"[{i}] Session: {dt.strftime('%Y-%m-%d %H:%M')}  {s.repo or 'global'}"
        )

        lines = []
        for j, cmd in enumerate(s.commands, 1):
            lines.append(f"  {j:>2}  {cmd}")

        # Text, not a markup string: Panel parses its renderable for tags.
        panel_content = plain("\n".join(lines))
        console.print(Panel(panel_content, title=header, border_style="dim"))
        console.print()

    # Replay prompt
    try:
        import subprocess as sp

        choice = click.prompt("Replay a session? [number/n]", default="n")
        if choice.lower() != "n":
            idx = int(choice) - 1
            if 0 <= idx < len(results):
                console.print()
                for cmd in results[idx].commands:
                    if not click.confirm(f"  Run: {cmd}?", default=True, err=True):
                        continue
                    console.print(f"  [dim]$ {safe(cmd)}[/]")
                    try:
                        sp.run(cmd, shell=True)
                    except KeyboardInterrupt:
                        console.print("\n  Interrupted.")
                        break
    except (ValueError, click.Abort):
        pass


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def stats(as_json: bool) -> None:
    """Show command-line usage statistics."""
    from collections import Counter

    from mem import storage

    commands: list[str] = []
    repos: list[str] = []
    for cmd in storage.read_all_commands():
        commands.append(cmd.command)
        if cmd.repo:
            repos.append(cmd.repo)

    total = len(commands)
    cmd_freq = Counter(commands).most_common(10)
    repo_freq = Counter(repos).most_common(5)

    if as_json:
        output = {
            "total": total,
            "top_commands": [{"command": c, "count": n} for c, n in cmd_freq],
            "top_repos": [{"repo": r, "count": n} for r, n in repo_freq],
        }
        click.echo(json.dumps(output, indent=2))
        return

    console.print(f"Commands: {total:,} total\n")

    if cmd_freq:
        console.print("Top commands:")
        for i, (cmd, count) in enumerate(cmd_freq, 1):
            console.print(f"  {i:>2}  {safe(fit(cmd, 40))} {count}")
        console.print()

    if repo_freq:
        console.print("Top repos:")
        for i, (repo, count) in enumerate(repo_freq, 1):
            console.print(f"  {i:>2}  {safe(fit(repo, 20))} {count}")


@cli.command()
@click.argument("query")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def forget(query: str, yes: bool) -> None:
    """Permanently delete commands matching a query."""
    from mem import storage

    # Preview matches
    matches = []
    for cmd in storage.read_all_commands():
        if query in cmd.command:
            matches.append(cmd)

    # Command history is only one of the six places `forget_commands` scrubs.
    # Previewing just that one meant text living solely in a saved runbook, a
    # stored variable, an extracted pattern or the agent audit log made this
    # command print "no matching commands found" and return without scrubbing
    # anything — so a command saved but never run was unforgettable, which is
    # exactly where someone is most likely to have pasted a credential.
    elsewhere = storage.forget_targets(query)

    if not matches and not elsewhere:
        console.print("No matching commands found.")
        return

    if not matches:
        console.print("No matching commands, but the text is still stored in:")
        for place in elsewhere:
            console.print(f"  [dim]·[/] {place}")
        console.print()
        if not yes and not click.confirm("Delete it from there?", default=False):
            return
        storage.forget_commands(query)
        console.print("Deleted.")
        return

    if not yes:
        console.print(f"Found {len(matches)} matching commands:")
        for i, cmd in enumerate(matches[:20], 1):
            repo_text = cmd.repo or "global"
            time_text = _relative_time(cmd.ts)
            console.print(
                f"  {i:>2}  {safe(fit(cmd.command, 40))}  [dim cyan]{safe(repo_text)}[/]"
                f"  [dim]{time_text}[/]"
            )
        if len(matches) > 20:
            console.print(f"  ... and {len(matches) - 20} more")
        console.print()

        if not click.confirm(f"Delete all {len(matches)}?", default=False):
            return

    removed = storage.forget_commands(query)
    console.print(f"Deleted {removed} commands.")


# --- Named Groups CLI commands ---


@cli.command()
@click.argument("command")
# `-g` means --global across the whole CLI. It used to mean --group here and
# in `import` while meaning --global in nine other commands, so the same
# keystroke did opposite things in adjacent commands.
@click.option(
    "--group",
    "--to",
    "-t",
    "group_name",
    default=None,
    help="Target group name",
)
@click.option(
    "--global", "-g", "global_flag", is_flag=True, help="Save to global scope"
)
@click.option("--comment", "-c", default=None, help="Inline annotation")
@click.option(
    "--var",
    "-v",
    "var_flags",
    multiple=True,
    help="Declare variable: NAME or NAME=default",
)
def save(
    command: str,
    group_name: str | None,
    global_flag: bool,
    comment: str | None,
    var_flags: tuple[str, ...],
) -> None:
    """Save a command to the saved list or to a named group."""
    from mem import groups
    from mem.variables import detect_credentials

    # Resolve ! to last captured command
    if command == "!":
        repo = _current_repo()
        command = groups.get_last_captured_command(repo)

    # Parse --var flags into (name, default) tuples
    import re as _re

    explicit_vars: list[tuple[str, str | None]] = []
    for v in var_flags:
        if "=" in v:
            name, default = v.split("=", 1)
        else:
            name, default = v, None
        if not _re.match(r"^[A-Z][A-Z0-9_]+$", name):
            raise click.ClickException(
                f"Invalid variable name '{name}'. "
                "Use uppercase letters, digits, and underscores (min 2 chars)."
            )
        explicit_vars.append((name, default))

    # AI credential detection (only if interactive and SDK available)
    if _is_interactive():
        credentials = detect_credentials(command)
        for original_value, suggested_name, reason in credentials:
            err_console.print(f"\n  Detected possible credential: {reason}")
            proposed = command.replace(original_value, f"${suggested_name}")
            err_console.print(f"  Suggested: {proposed}")
            # Prompt for variable name with validation loop
            while True:
                var_name = click.prompt(
                    "  Variable name (Enter to accept, or type to rename)",
                    default=suggested_name,
                    err=True,
                )
                if _re.match(r"^[A-Z][A-Z0-9_]+$", var_name):
                    break
                err_console.print(
                    f"  Invalid name '{var_name}'. "
                    "Must be UPPERCASE letters, digits, underscores (min 2 chars)."
                )
            if click.confirm("  Save with variable?", default=True, err=True):
                command = command.replace(original_value, f"${var_name}")

    scope_path = groups.resolve_scope(global_flag)

    def ask_description(name: str) -> str | None:
        if not _is_interactive():
            return None
        desc = click.prompt(
            f"Description for '{name}' (optional)",
            default="",
            show_default=False,
        )
        return desc or None

    saved, var_list = groups.save_command(
        scope_path,
        command,
        comment,
        group_name,
        description_callback=ask_description,
        explicit_vars=explicit_vars,
    )

    if saved:
        target = f"group '{group_name}'" if group_name else "saved commands"
        err_console.print(f"Saved to {safe(target)}: {safe(command)}")
        if var_list:
            var_strs = []
            for v in var_list:
                s = v.name
                if v.default is not None:
                    s += f" (default: {v.default})"
                var_strs.append(s)
            err_console.print(f"  Variables: {', '.join(var_strs)}")
    else:
        err_console.print(f"Already saved: {safe(command)}")


@cli.command(name="list")
@click.argument("group_name", required=False, default=None)
@click.option(
    "--global", "-g", "global_flag", is_flag=True, help="Show only global scope"
)
@click.option("--repo", "-r", "repo_flag", is_flag=True, help="Show only repo scope")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_cmd(
    group_name: str | None, global_flag: bool, repo_flag: bool, as_json: bool
) -> None:
    """List saved commands and groups, or show a group's commands."""
    from mem import groups, storage

    if global_flag and repo_flag:
        raise click.ClickException("Cannot use --global and --repo together.")

    repo = _current_repo()

    if repo_flag and not repo:
        raise click.ClickException("Not in a git repository. Cannot use --repo.")

    global_path = storage.GROUPS_GLOBAL_FILE
    repo_path = None

    if not global_flag and repo:
        sanitized = storage.sanitize_repo_name(repo)
        repo_path = storage.group_file_path(sanitized)

    # Show a specific group's commands
    if group_name is not None:
        if repo_flag:
            # --repo: only look in repo scope, never fall back to global
            if repo_path is None:
                raise click.ClickException(
                    f"Group '{group_name}' not found in repo scope."
                )
            repo_data = groups._load_group_file(repo_path)
            if group_name not in repo_data.groups:
                raise click.ClickException(
                    f"Group '{group_name}' not found in repo scope."
                )
            grp = repo_data.groups[group_name]
            scope_label = repo or repo_path.stem
            shadows = set()
        else:
            grp, scope_label, _file_path, shadows = groups.resolve_group(
                group_name,
                repo_path,
                global_path,
                force_global=global_flag,
            )
            # Use real repo path for display if scope is not global
            if scope_label != "global" and repo:
                scope_label = repo

        if as_json:
            click.echo(groups.export_json(group_name, grp))
            return

        console.print(f"\n● {safe(scope_label)} / {safe(group_name)}")
        if grp.description:
            console.print(f'  "{safe(grp.description)}"')
        if group_name in shadows and scope_label != "global":
            console.print(
                "  [dim](global group with same name exists — use --global to see it)[/]"
            )
        console.print("  " + "─" * 50)

        # Load variable store for status display
        from mem.variables import check_resolution_status

        vars_data = storage.read_vars_file()

        for i, cmd in enumerate(grp.commands, 1):
            comment_str = f"   # {cmd.comment}" if cmd.comment else ""
            console.print(f"  {i}. {safe(cmd.cmd)}{safe(comment_str)}")
            # Show variable resolution status if command has variables
            if cmd.vars:
                statuses = check_resolution_status(
                    cmd.vars,
                    vars_data.vars,
                    group_name,
                )
                for name, status, hint in statuses:
                    if status == "resolved":
                        console.print(f"     [green]✓[/] ${safe(name)}  {safe(hint)}")
                    else:
                        console.print(
                            f"     [yellow]⚠[/] ${safe(name)}  unset — {safe(hint)}"
                        )
        console.print()
        return

    result = groups.list_all(repo_path, global_path)

    # Use real repo path for display instead of sanitized filename
    repo_display = repo if not global_flag and repo_path else result["repo_name"]

    if as_json:
        output: dict = {}
        if result["repo_data"]:
            output["repo"] = {
                "name": repo_display,
                "saved": [s.model_dump() for s in result["repo_data"].saved],
                "groups": {
                    n: g.model_dump() for n, g in result["repo_data"].groups.items()
                },
            }
        if not repo_flag:
            output["global"] = {
                "saved": [s.model_dump() for s in result["global_data"].saved],
                "groups": {
                    n: g.model_dump() for n, g in result["global_data"].groups.items()
                },
            }
            if result["shadows"]:
                output["shadows"] = sorted(result["shadows"])
        click.echo(json.dumps(output, indent=2))
        return

    has_data = False

    # Repo saved commands
    if result["repo_data"] and result["repo_data"].saved:
        has_data = True
        console.print(f"\n● Saved commands in {safe(repo_display)}")
        for s in result["repo_data"].saved:
            comment_str = f"   # {s.comment}" if s.comment else ""
            console.print(f"  {safe(s.cmd)}{safe(comment_str)}")

    # Repo groups
    if result["repo_data"] and result["repo_data"].groups:
        has_data = True
        console.print(f"\n● Groups in {safe(repo_display)}")
        for name, grp in result["repo_data"].groups.items():
            count = len(grp.commands)
            desc = f'  "{grp.description}"' if grp.description else ""
            console.print(
                f"  {name:<20} {count} command{'s' if count != 1 else ''}{desc}"
            )

    # Global saved commands
    if not repo_flag and result["global_data"].saved:
        has_data = True
        console.print("\n● Saved commands (global)")
        for s in result["global_data"].saved:
            comment_str = f"   # {s.comment}" if s.comment else ""
            console.print(f"  {safe(s.cmd)}{safe(comment_str)}")

    # Global groups
    if not repo_flag and result["global_data"].groups:
        has_data = True
        shadows = result["shadows"]
        console.print("\n● Global groups")
        for name, grp in result["global_data"].groups.items():
            count = len(grp.commands)
            desc = f'  "{grp.description}"' if grp.description else ""
            shadow = "  ← shadowed in this repo" if name in shadows else ""
            console.print(
                f"  {name:<20} {count} command{'s' if count != 1 else ''}{desc}"
                f"[dim]{shadow}[/]"
            )

    if not has_data:
        console.print("\nNo saved commands or groups yet.")
        console.print('  Try: mem save "echo hello" --comment "test"')
        console.print('  Or:  mem save "echo hello" --group my-group')

    console.print()


@cli.command()
@click.argument("group_name", metavar="GROUP")
@click.argument("var_args", nargs=-1)
@click.option("--global", "-g", "global_flag", is_flag=True, help="Force global scope")
@click.option("--yes", "-y", is_flag=True, help="Skip all confirmation prompts")
def run(
    group_name: str, var_args: tuple[str, ...], global_flag: bool, yes: bool
) -> None:
    """Execute a group's commands interactively.

    Pass VAR=VALUE after the group name to set variables inline.
    """
    import subprocess as sp
    import time

    from mem import groups, storage
    from mem.models import VarDeclaration
    from mem.variables import process_escapes, resolve_variables

    if not _is_interactive() and not yes:
        raise click.ClickException(
            "Non-interactive mode detected. Use --yes to run without prompts."
        )

    # Parse inline VAR=VALUE arguments
    inline_args: dict[str, str] = {}
    for arg in var_args:
        if "=" in arg:
            name, value = arg.split("=", 1)
            inline_args[name] = value
        else:
            raise click.ClickException(
                f"Invalid argument '{arg}'. Use VAR=VALUE format."
            )

    global_path = storage.GROUPS_GLOBAL_FILE
    repo_path = None
    repo = _current_repo()
    if repo:
        sanitized = storage.sanitize_repo_name(repo)
        repo_path = storage.group_file_path(sanitized)

    grp, scope_label, _file_path, shadows = groups.resolve_group(
        group_name,
        repo_path,
        global_path,
        force_global=global_flag,
    )

    # Display header
    console.print(f"\n● {safe(scope_label)} / {safe(group_name)}")
    if grp.description:
        console.print(f'  "{safe(grp.description)}"')
    if group_name in shadows and scope_label != "global":
        console.print(
            "  [dim](global group with same name exists — use --global to see it)[/]"
        )
    console.print("  " + "─" * 50)

    # Display commands
    for i, cmd in enumerate(grp.commands, 1):
        comment_str = f"   # {cmd.comment}" if cmd.comment else ""
        console.print(f"  {i}. {safe(cmd.cmd)}{safe(comment_str)}")
    console.print("  " + "─" * 50)

    if not grp.commands:
        console.print("  (no commands)")
        return

    # Determine which commands to run
    run_all = yes
    if not yes:
        choice = click.prompt(
            f"  Run all? [y/N] or pick [1-{len(grp.commands)}]",
            default="n",
            show_default=False,
        )

        if choice.lower() == "n":
            return

        if choice.lower() == "y":
            run_all = True
            commands_to_run = list(enumerate(grp.commands, 1))
        else:
            try:
                idx = int(choice)
                if 1 <= idx <= len(grp.commands):
                    commands_to_run = [(idx, grp.commands[idx - 1])]
                else:
                    err_console.print(f"Invalid selection: {choice}")
                    return
            except ValueError:
                err_console.print(f"Invalid selection: {choice}")
                return
    else:
        commands_to_run = list(enumerate(grp.commands, 1))

    # Resolve all variables upfront (FR-006, FR-015)
    # Collect unique variables across all commands to run, resolve once
    all_vars: dict[str, VarDeclaration] = {}
    for _i, cmd in commands_to_run:
        if cmd.vars:
            for v in cmd.vars:
                if v.name not in all_vars:
                    all_vars[v.name] = v

    resolved: dict[str, tuple[str, str]] = {}
    if all_vars:
        vars_data = storage.read_vars_file()
        unique_var_list = list(all_vars.values())

        # In --yes mode, check for unresolvable variables first
        if yes:
            missing = []
            for v in unique_var_list:
                name = v.name
                if (
                    name not in inline_args
                    and name not in os.environ
                    and name not in vars_data.vars
                    and v.default is None
                ):
                    missing.append(name)
            if missing:
                raise click.ClickException(
                    f"Unresolved variables: {', '.join(missing)}\n"
                    f"Pass them inline: mem run {group_name} "
                    + " ".join(f"{n}=<value>" for n in missing)
                )

        resolved = resolve_variables(
            unique_var_list,
            inline_args,
            vars_data.vars,
            allow_prompt=not yes,
        )

        # Display resolution summary
        console.print()
        for name, (value, source) in resolved.items():
            console.print(f"  [green]✓[/] ${safe(name)} resolved from {safe(source)}")

        # Update last_used for store-resolved variables
        updated_store = False
        for name, (_value, source) in resolved.items():
            if source == "store" and name in vars_data.vars:
                vars_data.vars[name].last_used = int(time.time())
                updated_store = True
        if updated_store:
            storage.write_vars_file(vars_data)

    # Values are handed to the shell through the environment and the command is
    # run verbatim, so the shell expands $NAME itself. Splicing the value into
    # the command text instead made every value a potential injection: with
    # shell=True, `TARGET="safe; touch /tmp/x"` ran `touch` as a second command,
    # and `$(...)` in a value was evaluated. Parameter expansion does not
    # re-scan its result for operators, so the same value is inert.
    #
    # It also means a resolved secret is never rendered: the listing shows
    # `$API_TOKEN`, not the token. A value fetched from the store — entered with
    # hidden input precisely so it would stay unseen — used to be echoed in
    # plaintext to the terminal and left in the scrollback.
    child_env = {**os.environ, **{name: value for name, (value, _) in resolved.items()}}

    # Execute
    console.print()
    for i, cmd in commands_to_run:
        # $$NAME is stored verbatim so the escape survives export/import; it
        # collapses to $NAME only here, on the way to the shell.
        run_cmd = process_escapes(cmd.cmd)

        if not run_all and len(commands_to_run) > 1:
            if not click.confirm(f"  Run [{i}] {run_cmd}?", default=True, err=True):
                continue

        console.print(f"  [dim]$ {safe(run_cmd)}[/]")
        try:
            result = sp.run(run_cmd, shell=True, env=child_env)
        except KeyboardInterrupt:
            console.print("\n  Interrupted.")
            if yes:
                sys.exit(130)
            if not click.confirm("  Continue?", default=False, err=True):
                sys.exit(130)
            continue

        if result.returncode != 0:
            if yes:
                sys.exit(result.returncode)
            if not click.confirm(
                f"  Command failed (exit {result.returncode}). Continue?",
                default=False,
                err=True,
            ):
                sys.exit(result.returncode)
    console.print()


def _read_from_clipboard() -> str | None:
    """Read text from system clipboard. Returns None if unavailable or empty."""
    import shutil
    import subprocess as sp

    try:
        # macOS
        if shutil.which("pbpaste"):
            result = sp.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        # Linux (X11)
        if shutil.which("xclip"):
            result = sp.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        if shutil.which("xsel"):
            result = sp.run(
                ["xsel", "--clipboard", "--output"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
    except (sp.CalledProcessError, sp.TimeoutExpired):
        return None
    return None


def _copy_to_clipboard(text: str) -> bool:
    """Copy text to system clipboard. Returns True on success."""
    import shutil
    import subprocess as sp

    try:
        # macOS
        if shutil.which("pbcopy"):
            sp.run(["pbcopy"], input=text.encode(), check=True)
            return True
        # Linux (X11)
        if shutil.which("xclip"):
            sp.run(
                ["xclip", "-selection", "clipboard"], input=text.encode(), check=True
            )
            return True
        if shutil.which("xsel"):
            sp.run(["xsel", "--clipboard", "--input"], input=text.encode(), check=True)
            return True
    except sp.CalledProcessError:
        return False
    return False


@cli.command()
@click.argument("group_name", metavar="GROUP")
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["markdown", "json"]),
    default="json",
    help="Output format (default: json)",
)
@click.option(
    "--global", "-g", "global_flag", is_flag=True, help="Export from global scope"
)
@click.option(
    "--stdout", "use_stdout", is_flag=True, help="Print to stdout instead of clipboard"
)
def export(group_name: str, fmt: str, global_flag: bool, use_stdout: bool) -> None:
    """Export a group as markdown or JSON."""
    from mem import groups, storage

    global_path = storage.GROUPS_GLOBAL_FILE
    repo_path = None
    repo = _current_repo()
    if repo:
        sanitized = storage.sanitize_repo_name(repo)
        repo_path = storage.group_file_path(sanitized)

    grp, _, _, _ = groups.resolve_group(
        group_name,
        repo_path,
        global_path,
        force_global=global_flag,
    )

    if fmt == "markdown":
        output = groups.export_markdown(group_name, grp)
    else:
        output = groups.export_json(group_name, grp)

    if use_stdout:
        click.echo(output)
    else:
        if _copy_to_clipboard(output):
            err_console.print(f"Copied {fmt} to clipboard.")
        else:
            click.echo(output)
            err_console.print("(no clipboard tool found — printed to stdout)")


def _history_sources(
    shell: str | None, history_file: str | None
) -> list[tuple[str, Path]]:
    """Decide which history files to read, from the flags the user gave.

    ``--file`` names one file explicitly; its shell comes from ``--shell`` or,
    failing that, from the filename. Without ``--file`` the standard locations
    are probed and only the ones that exist are returned.
    """
    from mem import history

    if history_file is not None:
        path = Path(history_file)
        resolved = shell or history.shell_for_path(path)
        if resolved is None:
            raise click.ClickException(
                f"Cannot tell which shell wrote {path.name}. "
                f"Add --shell {{{','.join(IMPORTABLE_SHELLS)}}}."
            )
        return [(resolved, path)]

    return history.detect_history_files(shell)


def _print_history_plan(plan: ImportPlan) -> None:
    """Show, per file, what the import found. Chrome is styled, data is not."""
    console.print()
    for f in plan.files:
        location = plain(str(f.path))
        location.stylize("dim")
        if f.error:
            console.print(f"  [yellow]{f.shell:<5}[/] ", location, sep="")
            console.print(f"         [yellow]unreadable: {safe(f.error)}[/]")
            continue
        console.print(f"  [bold]{f.shell:<5}[/] ", location, sep="")
        console.print(
            f"         {len(f.commands):,} new   "
            f"[dim]{f.duplicates:,} already known   "
            f"{f.credentials:,} withheld as credentials   "
            f"{f.failed_lines:,} unparsed[/]"
        )
    console.print()


def _import_shell_history(
    shell: str | None, history_file: str | None, dry_run: bool, yes: bool
) -> None:
    """Run `mem import --from-shell-history` end to end.

    The plan is always computed and shown before anything is written, because
    this is the one mem command that adds thousands of lines to the store in a
    single step — the user should see the number before it happens, not after.
    """
    from mem import history

    sources = _history_sources(shell, history_file)
    if not sources:
        where = f" for {shell}" if shell else ""
        raise click.ClickException(
            f"No shell history file found{where}.\n"
            "Looked for ~/.zsh_history, ~/.bash_history and "
            "~/.local/share/fish/fish_history.\n"
            "Use --file to point at one directly."
        )

    plan = history.build_plan(sources)
    _print_history_plan(plan)

    if plan.total == 0:
        console.print("Nothing new to import.")
        return

    if dry_run:
        console.print(
            f"[bold]{plan.total:,}[/] commands would be imported. "
            "[dim](dry run — nothing was written)[/]"
        )
        return

    if not yes:
        if not _is_interactive():
            raise click.ClickException(
                "Non-interactive mode detected. Use --yes to import without prompts."
            )
        if not click.confirm(
            f"Import {plan.total:,} commands?", default=True, err=True
        ):
            return

    written = history.apply_plan(plan)
    console.print(
        f"Imported [bold]{written:,}[/] commands. "
        f"[dim]Skipped {plan.duplicates:,} already known and "
        f"{plan.credentials:,} that look like credentials; "
        f"{plan.failed_lines:,} lines could not be parsed.[/]"
    )


@cli.command(name="import")
@click.argument("file", type=click.Path(exists=True), required=False, default=None)
@click.option(
    "--group",
    "--to",
    "-t",
    "group_name",
    required=False,
    default=None,
    help="Target group name",
)
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["json", "markdown"]),
    default=None,
    help="Input format (auto-detected from extension if omitted)",
)
@click.option(
    "--global", "-g", "global_flag", is_flag=True, help="Import to global scope"
)
@click.option(
    "--from-shell-history",
    "from_shell_history",
    is_flag=True,
    help="Import your existing ~/.zsh_history, ~/.bash_history or fish history",
)
@click.option(
    "--shell",
    "shell",
    type=click.Choice(list(IMPORTABLE_SHELLS)),
    default=None,
    help="Limit the shell-history import to one shell",
)
@click.option(
    "--file",
    "history_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Import this history file instead of the auto-detected ones",
)
@click.option(
    "--dry-run", is_flag=True, help="Report what would be imported, write nothing"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def import_cmd(
    file: str | None,
    group_name: str | None,
    fmt: str | None,
    global_flag: bool,
    from_shell_history: bool,
    shell: str | None,
    history_file: str | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Import a group from a file or clipboard, or your shell history.

    When FILE is omitted, reads from the system clipboard and auto-detects
    the format and group name. Use -g to override the detected group name.

    With --from-shell-history, imports the commands your shell has already
    been recording, so mem is useful before it has captured anything itself.
    """
    if from_shell_history:
        if file is not None:
            raise click.ClickException(
                "--from-shell-history reads history files, not group files.\n"
                "Use --file to point at a specific history file."
            )
        _import_shell_history(shell, history_file, dry_run, yes)
        return

    if shell or history_file or dry_run:
        raise click.ClickException(
            "--shell, --file and --dry-run only apply to --from-shell-history."
        )

    from mem import groups, storage
    from mem.models import Group, GroupCommand

    detected_name: str | None = None

    if file is None:
        # --- Clipboard import mode ---
        content = _read_from_clipboard()
        if content is None:
            raise click.ClickException(
                "Clipboard is empty or unavailable.\n"
                "Usage: mem import                     (read from clipboard)\n"
                "       mem import runbook.json -g ops  (read from file)"
            )

        # Try JSON first, then markdown
        commands: list[GroupCommand] | None = None
        try:
            detected_name, commands = groups.import_from_json_str(content)
        except click.ClickException:
            pass

        if commands is None:
            try:
                detected_name, commands = groups.import_from_markdown_str(content)
            except click.ClickException:
                pass

        if not commands:
            raise click.ClickException(
                "No group data found in clipboard.\n"
                "Expected JSON (from mem export) or markdown with a command table.\n"
                "Try: mem export mygroup   then   mem import"
            )

        # Resolve group name: --group flag > auto-detected > prompt
        if group_name is None:
            group_name = detected_name
        if group_name is None:
            if _is_interactive():
                group_name = click.prompt("Group name")
            else:
                raise click.ClickException(
                    "Could not detect group name. Use -g to specify one."
                )

        groups.validate_group_name(group_name)

        # Confirm before importing
        if _is_interactive() and not yes:
            err_console.print(
                f"Found {len(commands)} commands. Import to group '{group_name}'?"
            )
            if not click.confirm("Proceed?", default=True, err=True):
                return
    else:
        # --- File import mode ---
        if group_name is None:
            raise click.ClickException(
                "Group name required for file import. Use -g to specify one."
            )
        groups.validate_group_name(group_name)

        file_path = Path(file)

        # Auto-detect format from extension if not specified
        if fmt is None:
            ext = file_path.suffix.lower()
            if ext == ".json":
                fmt = "json"
            elif ext in (".md", ".markdown"):
                fmt = "markdown"
            else:
                raise click.ClickException(
                    f"Cannot detect format from extension '{ext}'. Use --format to specify."
                )

        if fmt == "json":
            commands = groups.import_from_json(file_path)
        else:
            commands = groups.import_from_markdown(file_path)

    scope_path = groups.resolve_scope(global_flag)
    data = groups._load_group_file(scope_path)

    if group_name in data.groups:
        choice = click.prompt(
            f"Group '{group_name}' already exists. Merge or Replace?",
            type=click.Choice(["m", "r"], case_sensitive=False),
            default="r",
        )
        if choice.lower() == "r":
            data.groups[group_name] = Group(
                description=data.groups[group_name].description,
                commands=commands,
            )
            added = len(commands)
        else:
            existing_cmds = {c.cmd for c in data.groups[group_name].commands}
            added = 0
            for cmd in commands:
                if cmd.cmd not in existing_cmds:
                    data.groups[group_name].commands.append(cmd)
                    added += 1
    else:
        data.groups[group_name] = Group(commands=commands)
        added = len(commands)

    storage.write_group_file(scope_path, data)
    err_console.print(f"Imported {added} commands to group '{group_name}'.")


# --- Group management subgroup ---


@cli.group(name="group")
def group_grp() -> None:
    """Manage named groups."""


@group_grp.command(name="edit")
@click.argument("name")
@click.option("--global", "-g", "global_flag", is_flag=True, help="Edit global scope")
def group_edit(name: str, global_flag: bool) -> None:
    """Open the data file in your editor."""
    import subprocess as sp

    from mem import groups

    scope_path = groups.resolve_scope(global_flag)
    data = groups._load_group_file(scope_path)

    if name not in data.groups:
        raise click.ClickException(f"Group '{name}' not found.")

    editor = os.environ.get("EDITOR", "vi")
    try:
        sp.run([*shlex.split(editor), str(scope_path)])
    except FileNotFoundError:
        err_console.print(f"Editor '{editor}' not found. Edit manually: {scope_path}")


@group_grp.command(name="remove")
@click.argument("name")
@click.option(
    "--global", "-g", "global_flag", is_flag=True, help="Remove from global scope"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def group_remove(name: str, global_flag: bool, yes: bool) -> None:
    """Delete an entire group."""
    from mem import groups, storage

    scope_path = groups.resolve_scope(global_flag)
    data = groups._load_group_file(scope_path)

    if name not in data.groups:
        raise click.ClickException(f"Group '{name}' not found.")

    grp = data.groups[name]

    # Show contents before deleting
    console.print(f"\nGroup: {safe(name)}")
    if grp.description:
        console.print(f'  "{safe(grp.description)}"')
    for i, cmd in enumerate(grp.commands, 1):
        comment_str = f"   # {cmd.comment}" if cmd.comment else ""
        console.print(f"  {i}. {safe(cmd.cmd)}{safe(comment_str)}")
    console.print()

    if not yes:
        if not click.confirm(f"Delete group '{name}'?", default=False):
            return

    del data.groups[name]
    storage.write_group_file(scope_path, data)
    err_console.print(f"Deleted group '{name}'.")


@group_grp.command(name="rename")
@click.argument("old")
@click.argument("new")
@click.option(
    "--global", "-g", "global_flag", is_flag=True, help="Rename in global scope"
)
def group_rename(old: str, new: str, global_flag: bool) -> None:
    """Rename a group."""
    from mem import groups, storage

    groups.validate_group_name(new)
    scope_path = groups.resolve_scope(global_flag)
    data = groups._load_group_file(scope_path)

    if old not in data.groups:
        raise click.ClickException(f"Group '{old}' not found.")
    if new in data.groups:
        raise click.ClickException(f"Group '{new}' already exists.")

    data.groups[new] = data.groups.pop(old)
    storage.write_group_file(scope_path, data)
    err_console.print(f"Renamed '{old}' → '{new}'.")


@group_grp.command(name="copy")
@click.argument("name")
@click.option(
    "--global", "-g", "global_flag", is_flag=True, help="Copy to global scope"
)
@click.option("--repo", "repo_flag", is_flag=True, help="Copy to current repo scope")
def group_copy(name: str, global_flag: bool, repo_flag: bool) -> None:
    """Copy a group between scopes."""
    from mem import groups, storage

    if not global_flag and not repo_flag:
        raise click.ClickException("Specify --global or --repo as the target scope.")
    if global_flag and repo_flag:
        raise click.ClickException("Cannot specify both --global and --repo.")

    repo = _current_repo()
    if repo is None:
        raise click.ClickException("Not in a git repository.")

    sanitized = storage.sanitize_repo_name(repo)
    repo_path = storage.group_file_path(sanitized)
    global_path = storage.GROUPS_GLOBAL_FILE

    if global_flag:
        source_path, target_path = repo_path, global_path
    else:
        source_path, target_path = global_path, repo_path

    source_data = groups._load_group_file(source_path)
    target_data = groups._load_group_file(target_path)

    source_scope = "repo" if global_flag else "global"
    target_scope = "global" if global_flag else "repo"

    if name not in source_data.groups:
        raise click.ClickException(f"Group '{name}' not found in {source_scope} scope.")
    if name in target_data.groups:
        raise click.ClickException(
            f"Group '{name}' already exists in {target_scope} scope."
        )

    target_data.groups[name] = source_data.groups[name].model_copy(deep=True)
    storage.write_group_file(target_path, target_data)
    err_console.print(f"Copied group '{name}' to {target_scope} scope.")


# --- Saved commands subgroup ---


@cli.group(name="saved")
def saved_grp() -> None:
    """Manage saved commands."""


@saved_grp.command(name="edit")
@click.option("--global", "-g", "global_flag", is_flag=True, help="Edit global scope")
def saved_edit(global_flag: bool) -> None:
    """Open the data file in your editor."""
    import subprocess as sp

    from mem import groups

    scope_path = groups.resolve_scope(global_flag)

    if not scope_path.exists():
        raise click.ClickException(
            "No saved data yet. Save something first with 'mem save'."
        )

    editor = os.environ.get("EDITOR", "vi")
    try:
        sp.run([*shlex.split(editor), str(scope_path)])
    except FileNotFoundError:
        err_console.print(f"Editor '{editor}' not found. Edit manually: {scope_path}")


# --- Variable store subgroup ---


@cli.group(name="vars")
def vars_grp() -> None:
    """Manage persistent variables."""


@vars_grp.command(name="set")
@click.argument("name")
@click.argument("value", required=False, default=None)
def vars_set(name: str, value: str | None) -> None:
    """Set a persistent variable value."""
    import re

    from mem import storage
    from mem.models import StoredVariable

    if not re.match(r"^[A-Z][A-Z0-9_]+$", name):
        raise click.ClickException(
            f"Invalid variable name '{name}'. Use uppercase letters, digits, and underscores."
        )

    if value is None:
        value = click.prompt(f"  Value for {name}")

    vars_data = storage.read_vars_file()
    vars_data.vars[name] = StoredVariable(value=value, last_used=0)
    storage.write_vars_file(vars_data)
    err_console.print(f"Stored: {name}")


@vars_grp.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def vars_list(as_json: bool) -> None:
    """List stored variables (values hidden)."""
    from mem import storage

    vars_data = storage.read_vars_file()

    if as_json:
        output = {
            "variables": [
                {"name": name, "last_used": sv.last_used}
                for name, sv in sorted(vars_data.vars.items())
            ]
        }
        click.echo(json.dumps(output, indent=2))
        return

    if not vars_data.vars:
        console.print("No stored variables.")
        return

    console.print("\nStored variables (values hidden)")
    for name, sv in sorted(vars_data.vars.items()):
        if sv.last_used == 0:
            time_str = "never used"
        else:
            time_str = f"last used {_relative_time(sv.last_used)}"
        console.print(f"  {safe(fit(name, 20))} {time_str}")
    console.print()


@vars_grp.command(name="remove")
@click.argument("name")
def vars_remove(name: str) -> None:
    """Remove a stored variable."""
    from mem import storage

    vars_data = storage.read_vars_file()

    if name not in vars_data.vars:
        raise click.ClickException(f"Variable '{name}' not found.")

    del vars_data.vars[name]
    storage.write_vars_file(vars_data)
    err_console.print(f"Removed: {name}")


@vars_grp.command(name="clear")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def vars_clear(yes: bool) -> None:
    """Remove all stored variables."""
    from mem import storage

    vars_data = storage.read_vars_file()

    if not vars_data.vars:
        console.print("No stored variables to clear.")
        return

    count = len(vars_data.vars)

    if not yes:
        if not click.confirm(f"Clear all {count} variable(s)?", default=False):
            return

    from mem.models import VarsFile

    storage.write_vars_file(VarsFile())
    err_console.print(f"Cleared {count} variable(s).")


# --- Agent access (MCP) ---


@cli.group(name="agent")
def agent_grp() -> None:
    """Control AI agent access to your history over MCP."""


@agent_grp.command(name="enable")
def agent_enable() -> None:
    """Allow AI agents to read your history over MCP (stdio only)."""
    import time

    from mem import storage
    from mem.models import AgentAccess

    storage.write_agent_access(AgentAccess(enabled=True, updated_at=int(time.time())))
    err_console.print("Agent access enabled.")
    err_console.print(
        "  Agents can now read search results, runbooks and recent failures."
    )
    err_console.print("  Credentials are redacted; every request is logged.")
    err_console.print("  Review with: mem agent log     Revoke with: mem agent disable")


@agent_grp.command(name="disable")
def agent_disable() -> None:
    """Revoke AI agent access to your history."""
    import time

    from mem import storage
    from mem.models import AgentAccess

    storage.write_agent_access(AgentAccess(enabled=False, updated_at=int(time.time())))
    err_console.print("Agent access disabled.")
    err_console.print("  Takes effect on the next request — no restart needed.")


@agent_grp.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def agent_status(as_json: bool) -> None:
    """Show whether agents may read your history, and what they asked for."""
    from mem import storage

    access = storage.read_agent_access()
    requests = sum(1 for _ in storage.read_agent_audit())

    if as_json:
        click.echo(
            json.dumps(
                {
                    "enabled": access.enabled,
                    "updated_at": access.updated_at,
                    "requests": requests,
                    "audit_log": str(storage.agent_audit_file()),
                },
                indent=2,
            )
        )
        return

    state = "[green]enabled[/]" if access.enabled else "[yellow]disabled[/]"
    console.print(f"\nAgent access: {state}")
    if access.updated_at:
        console.print(f"  Changed {_relative_time(access.updated_at)}")
    console.print(f"  Requests logged: {requests}")
    console.print(f"  Audit log: {safe(storage.agent_audit_file())}")
    if not access.enabled:
        console.print("\n  Enable with: mem agent enable")
    console.print()


@agent_grp.command(name="log")
@click.option("--limit", "-n", default=20, help="Maximum entries (newest last)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def agent_log(limit: int, as_json: bool) -> None:
    """Show what an agent asked mem for, and when."""
    from mem import storage

    entries = list(storage.read_agent_audit())[-limit:] if limit > 0 else []

    if as_json:
        click.echo(json.dumps([e.model_dump() for e in entries], indent=2))
        return

    if not entries:
        console.print("No agent requests recorded.")
        return

    console.print("\nAgent requests")
    for entry in entries:
        args = " ".join(f"{k}={v}" for k, v in entry.arguments.items())
        mark = "[green]✓[/]" if entry.ok else "[yellow]✗[/]"
        detail = entry.error or f"{entry.results} result(s)"
        console.print(
            f"  {mark} {safe(fit(entry.tool, 16))} {safe(fit(args, 40))}"
            f"  [dim]{_relative_time(entry.ts)} — {safe(detail)}[/]"
        )
    console.print()


@cli.command(name="mcp")
def mcp_cmd() -> None:
    """Serve mem to AI agents over MCP (JSON-RPC 2.0 on stdin/stdout).

    Started by an MCP client, not by hand — stdin and stdout are the
    protocol channel, so running it in a terminal just looks like a hang.
    Register it with Claude Code:

        claude mcp add mem -- mem mcp

    or paste into claude_desktop_config.json / .mcp.json:

        {"mcpServers": {"mem": {"command": "mem", "args": ["mcp"]}}}

    Access is off until `mem agent enable`.
    """
    from mem.mcp import main

    sys.exit(main())


# The background sync is spawned as a subprocess, and `python -m mem.cli` only
# imports this module — without this block it defined every command and exited
# 0 having run none of them. That is how pattern extraction and data retention
# were both dead from the day auto-sync replaced the manual `mem sync` command:
# silently, with a zero exit code, inside a `try/except: pass`.
if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in e2e
    cli()
