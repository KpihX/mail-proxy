"""
Rich output helpers for mail-proxy.

stdout carries the JSON envelope; these helpers render it (json or table).
Warnings, errors and HITL prompts are printed by the caller on stderr.
"""

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
stderr_console = Console(stderr=True)


def print_json(data: Any) -> None:
    """Print data as formatted JSON on stdout.

    Args:
        data (Any): Any JSON-serialisable object — normally the full
            `{"meta": …, "data": …}` envelope.

    Returns:
        None: Writes to stdout.

    Examples:
        >>> print_json({"meta": {"status": "ok"}, "data": {"uid": 42}})
        {"meta": {"status": "ok"}, "data": {"uid": 42}}
        >>> print_json({"data": []})
        {"data": []}
    """
    console.print_json(data=data)


def print_table(data: list[dict] | dict) -> None:
    """Print a dict or a list of dicts as a Rich table.

    Args:
        data (list[dict] | dict): Mapping rendered as Key/Value rows, or a list
            of uniform dicts rendered as a column per key.

    Returns:
        None: Writes to stdout.

    Examples:
        >>> print_table({"status": "ok", "edited": False})
        ┏━━━━━━━━┳━━━━━━━┓ … (Key / Value table)
        >>> print_table([{"uid": 42, "subject": "Hello"}])
        ┏━━━━━┳━━━━━━━━━┓ … (uid / subject table)
    """
    if isinstance(data, dict):
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Key", style="dim")
        table.add_column("Value")
        for k, v in data.items():
            table.add_row(str(k), str(v))
        console.print(table)
    elif isinstance(data, list) and data:
        keys = list(data[0].keys()) if isinstance(data[0], dict) else ["Value"]
        table = Table(show_header=True, header_style="bold cyan")
        for k in keys:
            table.add_column(str(k))
        for item in data:
            if isinstance(item, dict):
                table.add_row(*[str(item.get(k, "")) for k in keys])
            else:
                table.add_row(str(item))
        console.print(table)
    else:
        console.print(data)


def print_warning(message: str) -> None:
    """Print a yellow warning on the stderr console.

    Args:
        message (str): Warning text.

    Returns:
        None

    Examples:
        >>> print_warning("IMAP server unreachable — check your network")
        ⚠️  IMAP server unreachable — check your network
        >>> print_warning("Credentials missing for account 'poly'")
        ⚠️  Credentials missing for account 'poly'
    """
    stderr_console.print(f"[bold yellow]⚠️  {message}[/bold yellow]")


def print_error(message: str) -> None:
    """Print a red error on the stderr console.

    Args:
        message (str): Error text (already user-facing, never a traceback).

    Returns:
        None

    Examples:
        >>> print_error("Config not found. Run 'mail-proxy admin setup'.")
        ❌ Config not found. Run 'mail-proxy admin setup'.
        >>> print_error("[0] IMAP login rejected for 'poly'")
        ❌ [0] IMAP login rejected for 'poly'
    """
    stderr_console.print(f"[bold red]❌ {message}[/bold red]")


def print_success(message: str) -> None:
    """Print a green success line on the stderr console.

    Args:
        message (str): Success text.

    Returns:
        None

    Examples:
        >>> print_success("Credentials saved")
        ✅ Credentials saved
        >>> print_success("mail-proxy configured")
        ✅ mail-proxy configured
    """
    stderr_console.print(f"[bold green]✅ {message}[/bold green]")


def print_meta(meta: dict) -> None:
    """Render the `meta` section of an envelope as a panel.

    Args:
        meta (dict): The envelope meta — `status`, `comment`, and `edited`.

    Returns:
        None

    Examples:
        >>> print_meta({"status": "ok", "comment": "", "edited": False})
        ╭─ Output Meta ─╮ Status: ok · Comment: (empty) · Edited: ❌ No
        >>> print_meta({"status": "rejected", "comment": "wrong recipient", "edited": True})
        ╭─ Output Meta ─╮ Status: rejected · Comment: wrong recipient · Edited: ✅ Yes
    """
    status = meta.get("status", "ok")
    color = "green" if status in ("ok", "approved") else "red"
    console.print(
        Panel(
            f"[bold {color}]Status:[/] {status}\n"
            f"[bold]Comment:[/] {meta.get('comment', '') or '(empty)'}\n"
            f"[bold]Edited:[/] {'✅ Yes' if meta.get('edited') else '❌ No'}",
            title="[bold blue]Output Meta[/]",
            border_style=color,
        )
    )
