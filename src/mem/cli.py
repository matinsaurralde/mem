"""
CLI interface for mem — the user-facing command layer.

Every command here maps to a user story from the specification.
Click handles argument parsing; Rich handles output formatting.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from mem import __version__
from mem.capture import get_git_repo

console = Console()
err_console = Console(stderr=True)


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
    query = query_args[0] if query_args else None
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
        command_text = cmd.command
        repo_text = cmd.repo or "global"
        time_text = _relative_time(cmd.ts)
        console.print(
            f"{rank}  {command_text:<40}  [dim cyan]{repo_text:<12}[/]  [dim]{time_text}[/]"
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


@cli.command()
@click.argument("shell")
def init(shell: str) -> None:
    """Print shell hook code for automatic command capture."""
    supported = {"zsh"}
    future = {"bash", "fish"}

    if shell not in supported and shell not in future:
        click.echo(
            f'Error: unsupported shell "{shell}". Supported: zsh, bash, fish', err=True
        )
        sys.exit(1)

    if shell in future:
        click.echo(
            f"Error: {shell} support coming in v1.5. Currently supported: zsh", err=True
        )
        sys.exit(1)

    # Print hook code to stdout
    hook_path = Path(__file__).parent.parent.parent / "hooks" / f"mem.{shell}"
    if hook_path.exists():
        click.echo(hook_path.read_text())
    else:
        # Fallback: inline the hook if the file isn't found (e.g., installed via pip)
        click.echo(_ZSH_HOOK)


_ZSH_HOOK = """# mem shell hook

_mem_preexec() {
  _mem_cmd="$1"
  _mem_start=$SECONDS
}

_mem_precmd() {
  local exit_code=$?
  if [[ -n "$_mem_cmd" ]]; then
    integer duration
    (( duration = (SECONDS - _mem_start) * 1000 ))
    mem _capture "$_mem_cmd" "$PWD" "$exit_code" "$duration" 2>/dev/null &!
    _mem_cmd=""
  fi
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _mem_preexec
add-zsh-hook precmd _mem_precmd
"""


@cli.command()
@click.option("--keep-commands", type=int, default=90, help="Days to retain commands")
@click.option("--keep-sessions", type=int, default=30, help="Days to retain sessions")
def sync(keep_commands: int, keep_sessions: int) -> None:
    """Extract patterns and rotate old data."""
    from rich.progress import Progress

    from mem.patterns import sync_all_patterns
    from mem import storage

    # Pattern extraction
    with Progress(console=console) as progress:
        task = progress.add_task("Extracting patterns...", total=None)
        new, updated = sync_all_patterns()
        progress.update(task, completed=100, total=100)

    console.print(f"Patterns: {new} new, {updated} updated\n")

    # Rotation
    cmd_removed, sess_removed = storage.rotate(keep_commands, keep_sessions)
    console.print("Rotation:")
    console.print(f"  Commands older than {keep_commands}d: {cmd_removed} removed")
    console.print(f"  Sessions older than {keep_sessions}d: {sess_removed} removed")


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
        header = f"[{i}] Session: {dt.strftime('%Y-%m-%d %H:%M')}  {s.repo or 'global'}"

        lines = []
        for j, cmd in enumerate(s.commands, 1):
            lines.append(f"  {j:>2}  {cmd}")

        panel_content = "\n".join(lines)
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
                    console.print(f"  [dim]$ {cmd}[/]")
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
            display = cmd[:35] + "..." if len(cmd) > 35 else cmd
            console.print(f"  {i:>2}  {display:<40} {count}")
        console.print()

    if repo_freq:
        console.print("Top repos:")
        for i, (repo, count) in enumerate(repo_freq, 1):
            console.print(f"  {i:>2}  {repo:<20} {count}")


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

    if not matches:
        console.print("No matching commands found.")
        return

    if not yes:
        console.print(f"Found {len(matches)} matching commands:")
        for i, cmd in enumerate(matches[:20], 1):
            repo_text = cmd.repo or "global"
            time_text = _relative_time(cmd.ts)
            console.print(
                f"  {i:>2}  {cmd.command:<40}  [dim cyan]{repo_text}[/]  [dim]{time_text}[/]"
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
@click.option("--group", "-g", "group_name", default=None, help="Target group name")
@click.option("--global", "global_flag", is_flag=True, help="Save to global scope")
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
                    "  Variable name",
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
        err_console.print(f"Saved to {target}: {command}")
        if var_list:
            var_strs = []
            for v in var_list:
                s = v.name
                if v.default is not None:
                    s += f" (default: {v.default})"
                var_strs.append(s)
            err_console.print(f"  Variables: {', '.join(var_strs)}")
    else:
        err_console.print(f"Already saved: {command}")


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

        console.print(f"\n● {scope_label} / {group_name}")
        if grp.description:
            console.print(f'  "{grp.description}"')
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
            console.print(f"  {i}. {cmd.cmd}{comment_str}")
            # Show variable resolution status if command has variables
            if cmd.vars:
                statuses = check_resolution_status(
                    cmd.vars,
                    vars_data.vars,
                    group_name,
                )
                for name, status, hint in statuses:
                    if status == "resolved":
                        console.print(f"     [green]✓[/] ${name}  {hint}")
                    else:
                        console.print(f"     [yellow]⚠[/] ${name}  unset — {hint}")
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
        console.print(f"\n● Saved commands in {repo_display}")
        for s in result["repo_data"].saved:
            comment_str = f"   # {s.comment}" if s.comment else ""
            console.print(f"  {s.cmd}{comment_str}")

    # Repo groups
    if result["repo_data"] and result["repo_data"].groups:
        has_data = True
        console.print(f"\n● Groups in {repo_display}")
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
            console.print(f"  {s.cmd}{comment_str}")

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
    from mem.variables import resolve_variables, substitute_variables

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
    console.print(f"\n● {scope_label} / {group_name}")
    if grp.description:
        console.print(f'  "{grp.description}"')
    if group_name in shadows and scope_label != "global":
        console.print(
            "  [dim](global group with same name exists — use --global to see it)[/]"
        )
    console.print("  " + "─" * 50)

    # Display commands
    for i, cmd in enumerate(grp.commands, 1):
        comment_str = f"   # {cmd.comment}" if cmd.comment else ""
        console.print(f"  {i}. {cmd.cmd}{comment_str}")
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
            console.print(f"  [green]✓[/] ${name} resolved from {source}")

        # Update last_used for store-resolved variables
        updated_store = False
        for name, (_value, source) in resolved.items():
            if source == "store" and name in vars_data.vars:
                vars_data.vars[name].last_used = int(time.time())
                updated_store = True
        if updated_store:
            storage.write_vars_file(vars_data)

    # Execute
    console.print()
    for i, cmd in commands_to_run:
        # Substitute variables in command
        run_cmd = cmd.cmd
        if cmd.vars and resolved:
            run_cmd = substitute_variables(run_cmd, resolved)

        if not run_all and len(commands_to_run) > 1:
            if not click.confirm(f"  Run [{i}] {run_cmd}?", default=True, err=True):
                continue

        console.print(f"  [dim]$ {run_cmd}[/]")
        try:
            result = sp.run(run_cmd, shell=True)
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


@cli.command(name="import")
@click.argument("file", type=click.Path(exists=True))
@click.option("--group", "-g", "group_name", required=True, help="Target group name")
@click.option(
    "--format",
    "-f",
    "fmt",
    type=click.Choice(["json", "markdown"]),
    default=None,
    help="Input format (auto-detected from extension if omitted)",
)
@click.option("--global", "global_flag", is_flag=True, help="Import to global scope")
def import_cmd(file: str, group_name: str, fmt: str | None, global_flag: bool) -> None:
    """Import a group from a file."""
    from mem import groups, storage
    from mem.models import Group

    groups.validate_group_name(group_name)
    scope_path = groups.resolve_scope(global_flag)
    data = groups._load_group_file(scope_path)

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
    console.print(f"\nGroup: {name}")
    if grp.description:
        console.print(f'  "{grp.description}"')
    for i, cmd in enumerate(grp.commands, 1):
        comment_str = f"   # {cmd.comment}" if cmd.comment else ""
        console.print(f"  {i}. {cmd.cmd}{comment_str}")
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
        console.print(f"  {name:<20} {time_str}")
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
