"""
CLI interface for mem — the user-facing command layer.

Every command here maps to a user story from the specification.
Click handles argument parsing; Rich handles output formatting.
"""

from __future__ import annotations

import json
import os
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
@click.option("--pattern", "-p", is_flag=True, help="Show extracted patterns instead of commands")
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
            console.print(f"No patterns found for \"{query}\".")
            return
        console.print(f"\nPatterns for \"{query}\":\n")
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
        click.echo(f'Error: unsupported shell "{shell}". Supported: zsh, bash, fish', err=True)
        sys.exit(1)

    if shell in future:
        click.echo(f'Error: {shell} support coming in v1.5. Currently supported: zsh', err=True)
        sys.exit(1)

    # Print hook code to stdout
    hook_path = Path(__file__).parent.parent.parent / "hooks" / f"mem.{shell}"
    if hook_path.exists():
        click.echo(hook_path.read_text())
    else:
        # Fallback: inline the hook if the file isn't found (e.g., installed via pip)
        click.echo(_ZSH_HOOK)


_ZSH_HOOK = '''# mem shell hook

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
'''


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

    for i, s in enumerate(results, 1):
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(s.started_at, tz=timezone.utc)
        header = f"Session: {dt.strftime('%Y-%m-%d %H:%M')}  {s.repo or 'global'}"

        lines = []
        for j, cmd in enumerate(s.commands, 1):
            lines.append(f"  {j:>2}  {cmd}")

        panel_content = "\n".join(lines)
        console.print(Panel(panel_content, title=header, border_style="dim"))
        console.print()

    # Replay prompt
    try:
        choice = click.prompt("Replay a session? [number/n]", default="n")
        if choice.lower() != "n":
            idx = int(choice) - 1
            if 0 <= idx < len(results):
                console.print()
                for cmd in results[idx].commands:
                    click.echo(cmd)
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
            console.print(f"  {i:>2}  {cmd.command:<40}  [dim cyan]{repo_text}[/]  [dim]{time_text}[/]")
        if len(matches) > 20:
            console.print(f"  ... and {len(matches) - 20} more")
        console.print()

        if not click.confirm(f"Delete all {len(matches)}?", default=False):
            return

    removed = storage.forget_commands(query)
    console.print(f"Deleted {removed} commands.")
