"""
mail-proxy CLI — Single binary, two namespaces.

Usage:
    mail-proxy admin setup|status|reset|purge
    mail-proxy do <action> [payload] [--output-file/-o] [--format/-f]

All output in JSON (default) or table format.
Admin is ALWAYS JSON. 'do' defaults to JSON, can switch to table.
Verification is structural (`@require_verification` on the handler) — there is no flag.
"""

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import typer
from pydantic import BaseModel, ValidationError

from . import __version__, admin
from .actions.base import ActionDef
from .actions.registry import REGISTRY, by_group
from .client import MailClient
from .config import ensure_env
from .display import console, print_error, print_json, print_meta, print_table
from .doc import get_compact_help, get_full_help
from .exceptions import MailProxyError
from .hitl import request_approval
from .logger import setup_logging
from .models import Status, Verification, ok, rejected

AUTOSAVE_DIR = Path("/tmp/mail-proxy-autosave")

app = typer.Typer(
    name="mail-proxy",
    help="Mail administrative proxy — RPC CLI for IMAP/SMTP accounts, messages, folders and labels.",
    add_completion=False,
)
app_admin = typer.Typer(
    help="Admin commands: doctor, status, auth login|status|logout, reset, purge."
)
app_admin_auth = typer.Typer(help="Authentication commands: login, status, logout.")
app_do = typer.Typer(
    help="RPC actions: inbox-check, message-list, message-send, signature-list, …",
    add_completion=False,
    add_help_option=False,
)
app_admin.add_typer(app_admin_auth, name="auth")
app.add_typer(app_admin, name="admin")
app.add_typer(app_do, name="do")


# ─── Helpers ───


def parse_payload(payload_str: str | None) -> dict:
    """Convert a JSON string or a file path into a dict.

    Args:
        payload_str (str | None): Inline JSON, or a path to a `.json` file.

    Returns:
        dict: The parsed payload (empty when nothing was given).

    Raises:
        MailProxyError: When the string is neither valid JSON nor an existing file.

    Examples:
        >>> parse_payload('{"uid":42}')
        {'uid': 42}
        >>> parse_payload('/path/to/payload.json')
        {'uids': [42], 'folder': 'INBOX'}
    """
    if not payload_str:
        return {}
    try:
        return json.loads(payload_str)
    except json.JSONDecodeError:
        path = Path(payload_str)
        if path.exists():
            return json.loads(path.read_text())
        raise MailProxyError(f"Invalid JSON or file not found: {payload_str}") from None


def output_result(result: dict, fmt: str = "json") -> None:
    """Print the envelope in the requested format.

    Args:
        result (dict): The `{"meta": …, "data": …}` envelope.
        fmt (str): `json` (default) or `table`.

    Returns:
        None

    Examples:
        >>> output_result({"meta": {"status": "ok"}, "data": {"uid": 42}})
        {"meta": {"status": "ok"}, "data": {"uid": 42}}
        >>> output_result({"meta": {"status": "ok"}, "data": []}, "table")
        (Meta table + Data table)
    """
    if fmt == "table":
        console.print("[bold blue]Meta:[/]")
        print_meta(result.get("meta", {}))
        console.print("[bold blue]Data:[/]")
        print_table(result.get("data") or {})
    else:
        print_json(data=result)


def output_rejection(comment: str, edited: bool, fmt: str = "json") -> None:
    """Print the canonical HITL rejection envelope and terminate with code one.

    Args:
        comment (str): Reviewer reason or timeout explanation.
        edited (bool): Whether the reviewer changed the reviewed payload.
        fmt (str): Requested output format; rejected output remains an envelope.

    Returns:
        None: Always raises `typer.Exit(1)` after printing.

    Examples:
        >>> output_rejection("not now", False)  # doctest: +SKIP
        {"meta":{"status":"rejected",...},"data":null}
        >>> output_rejection("timeout", False, "table")  # doctest: +SKIP
        {"meta":{"status":"rejected",...},"data":null}
    """
    output_result(rejected(comment, edited), fmt)
    raise typer.Exit(1)


def _autosave(action: str, result: dict) -> Path:
    """Write the envelope to /tmp/mail-proxy-autosave and return the path.

    Args:
        action (str): The action name, used in the file name.
        result (dict): The envelope to persist.

    Returns:
        Path: The file that was written.

    Examples:
        >>> _autosave("message-send", {"meta": {}, "data": {}})
        PosixPath('/tmp/mail-proxy-autosave/message-send_20260812_112403.json')
        >>> _autosave("inbox-check", {"meta": {}, "data": []})
        PosixPath('/tmp/mail-proxy-autosave/inbox-check_20260812_112500.json')
    """
    AUTOSAVE_DIR.mkdir(parents=True, exist_ok=True)
    path = AUTOSAVE_DIR / f"{action}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(result, indent=2, default=str))
    return path


def _execute(
    action: ActionDef, payload_raw: str | None, output_file: str | None, fmt: str
) -> None:
    """Run one action end-to-end: validate → HITL → call → verify → print.

    Args:
        action (ActionDef): The registry entry to run.
        payload_raw (str | None): Inline JSON or a file path.
        output_file (str | None): Where to also write the envelope.
        fmt (str): Display format.

    Returns:
        None: Exits 1 on error, rejection or failed verification.

    Examples:
        >>> _execute(REGISTRY["inbox-check"], None, None, "json")
        {"meta": {...}, "data": {"unread_count": 14, ...}}
        >>> _execute(REGISTRY["message-delete"], '{"uids":[42]}', None, "json")
        (opens the HITL form, then prints the envelope)
    """
    params = parse_payload(payload_raw)
    try:
        ensure_env(params.get("account_id"))
    except MailProxyError as exc:
        print_error(str(exc))
        sys.exit(1)

    meta_status, comment, edited = "ok", "", False
    required_checks = tuple(getattr(action.handler, "__verification_checks__", ()))
    client: MailClient | None = None
    preflight_payload: BaseModel | None = None
    preflight = getattr(action.handler, "__preflight_check__", None)
    preflight_identity_fields = tuple(
        getattr(action.handler, "__preflight_identity_fields__", ())
    )
    if preflight is not None:
        try:
            preflight_payload = action.payload(**params) if action.payload else None
        except ValidationError as exc:
            print_error(f"Validation error: {exc}")
            sys.exit(1)
        client = MailClient(params.get("account_id"))
        try:
            preflight(client, preflight_payload)
        except MailProxyError as exc:
            client.close()
            print_error(str(exc))
            sys.exit(1)
    if action.hitl:
        response = request_approval(action.name, params)
        if response.status == "rejected":
            if client is not None:
                client.close()
            output_rejection(response.comment, response.edited, fmt)
        if isinstance(response.payload, dict):
            params = response.payload
        if preflight_payload is not None and any(
            params.get(field) != preflight_payload.model_dump().get(field)
            for field in preflight_identity_fields
        ):
            if client is not None:
                client.close()
            print_error(
                "Reviewed payload changed the preflighted target identity: "
                f"{', '.join(preflight_identity_fields)}."
            )
            sys.exit(1)
        meta_status, comment, edited = "approved", response.comment, response.edited

    try:
        validated = action.payload(**params) if action.payload else None
    except ValidationError as exc:
        if client is not None:
            client.close()
        print_error(f"Validation error: {exc}")
        sys.exit(1)

    if client is None:
        client = MailClient(params.get("account_id"))
    try:
        outcome = action.handler(client, validated)
        verification: Verification | None = None
        if isinstance(outcome, tuple):
            data, verification = outcome
        else:
            data = outcome
        if required_checks and verification is None:
            raise MailProxyError(
                f"{action.name} declares @require_verification but returned no proof."
            )
        if verification is not None and set(verification.checked) != set(
            required_checks
        ):
            raise MailProxyError(
                f"{action.name} verification checks do not match its declared policy."
            )
    except MailProxyError as exc:
        print_error(str(exc))
        sys.exit(1)
    finally:
        client.close()

    if verification is not None and isinstance(data, dict):
        data = {**data, "verification": verification.model_dump()}
    result = ok(data, edited=edited, comment=comment, status=meta_status)

    autosave_path = _autosave(action.name, result)
    if output_file:
        out = Path(output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str))
        print(f"📄 Written to: {out}", file=sys.stderr)
    else:
        print(f"💾 Autosave: {autosave_path}", file=sys.stderr)

    output_result(result, fmt)
    if verification is not None and not verification.ok:
        sys.exit(1)


# ─── Callbacks ───


def _version_callback(value: bool) -> None:
    """Print the version and exit.

    Args:
        value (bool): True when `--version` was passed.

    Returns:
        None

    Examples:
        >>> _version_callback(True)
        mail-proxy v0.1.0
        >>> _version_callback(False)     # no-op
    """
    if value:
        console.print(f"mail-proxy v{__version__}")
        raise typer.Exit()


def _do_help_callback(value: bool = True) -> None:
    """Print the compact catalog of all 30 actions, grouped.

    Args:
        value (bool): True when help was requested.

    Returns:
        None

    Examples:
        >>> _do_help_callback(True)
        (Inbox / Messages / Compose … with one compact docstring per action)
        >>> _do_help_callback(False)     # no-op
    """
    if not value:
        return
    console.print(
        "[bold yellow]For detailed information and examples on a specific"
        " action, run:[/bold yellow]"
    )
    console.print("  [bold]mail-proxy do <action> --help[/bold]\n")
    for group, actions in by_group().items():
        console.print(f"[bold magenta]── {group} ──[/bold magenta]")
        for action in actions:
            console.print(f"[bold cyan]{action.name}[/bold cyan]")
            console.print(get_compact_help(action.handler))
            console.print()
    raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
        None, "--version", callback=_version_callback, is_eager=True
    ),
) -> None:
    """Root callback — configures stderr logging.

    Args:
        version (bool | None): Handled by the eager `--version` callback.

    Returns:
        None

    Examples:
        >>> main(None)
        >>> main(True)      # prints the version and exits
    """
    setup_logging()


# ─── Admin ───


def _run_admin(command: Callable[[], tuple[dict | None, Status, bool, str]]) -> None:
    """Run one admin manager without applying the `do` configuration gate.

    Args:
        command (Callable[[], tuple[dict | None, Status, bool, str]]): Zero-argument
            admin callable returning normalized HITL metadata.

    Returns:
        None: Prints an approved envelope or the shared rejection envelope.

    Examples:
        >>> _run_admin(lambda: ({"status": "ok"}, "approved", False, ""))  # doctest: +SKIP
        {"meta":{"status":"approved",...},"data":{"status":"ok"}}
        >>> _run_admin(lambda: (None, "rejected", False, "not now"))  # doctest: +SKIP
        {"meta":{"status":"rejected",...},"data":null}
    """
    try:
        data, status, edited, comment = command()
    except MailProxyError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    if status == "rejected":
        output_rejection(comment, edited)
    print_json(data=ok(data, status=status, edited=edited, comment=comment))


@app_admin.command("doctor")
def admin_doctor() -> None:
    """Scan config and auto-fix permission / structural problems (ALWAYS JSON)."""
    print_json(data=ok(admin.doctor()))


@app_admin.command("status")
def admin_status() -> None:
    """Complete status: accounts, auth, permissions, probes, issues (ALWAYS JSON)."""
    print_json(data=ok(admin.status()))


@app_admin_auth.command("login")
def admin_auth_login() -> None:
    """Collect one full account (config + password) via HITL, write JSON + .env atomically."""
    _run_admin(admin.auth_login)


@app_admin_auth.command("status")
def admin_auth_status() -> None:
    """Show auth state of ALL accounts: JSON config + .env secret + IMAP/SMTP probes."""
    print_json(data=ok(admin.auth_status()))


@app_admin_auth.command("logout")
def admin_auth_logout() -> None:
    """Remove the password for ONE account (HITL-confirmed). Account stays in JSON."""
    _run_admin(admin.auth_logout)


@app_admin_auth.command("default")
def admin_auth_default() -> None:
    """Set the default account used when -a is omitted."""
    _run_admin(admin.auth_default)


@app_admin.command("reset")
def admin_reset() -> None:
    """Empty ALL passwords from .env (HITL-confirmed). Accounts in JSON untouched."""
    _run_admin(admin.reset)


@app_admin.command("purge")
def admin_purge() -> None:
    """Delete the entire config directory (HITL-confirmed). Both JSON and .env."""
    _run_admin(admin.purge)


# ─── do ───

OUTPUT_FILE_OPT = typer.Option(
    None, "--output-file", "-o", help="Write the envelope to a file."
)
FORMAT_OPT = typer.Option(
    "json", "--format", "-f", help="Output format: json (default) or table."
)


@app_do.callback(invoke_without_command=True)
def do_main(
    ctx: typer.Context,
    show_help: bool = typer.Option(
        False, "--help", "-h", help="Show help.", hidden=True
    ),
) -> None:
    """`do` callback — prints the catalog when no action is given.

    Args:
        ctx (typer.Context): Typer context.
        show_help (bool): True when `-h/--help` was passed.

    Returns:
        None

    Examples:
        >>> # mail-proxy do            → prints the 24-action catalog
        >>> # mail-proxy do inbox-check → runs the action
    """
    if show_help or ctx.invoked_subcommand is None:
        _do_help_callback(True)


def _register(action: ActionDef) -> None:
    """Attach one registry entry as a Typer command under `do`.

    Args:
        action (ActionDef): The action to expose.

    Returns:
        None

    Examples:
        >>> _register(REGISTRY["inbox-check"])   # adds `mail-proxy do inbox-check`
        >>> _register(REGISTRY["raw"])           # adds `mail-proxy do raw`
    """

    @app_do.command(action.name, help=get_full_help(action.handler))
    def _command(
        payload: str | None = typer.Argument(None, help="JSON payload or file path."),
        output_file: str | None = OUTPUT_FILE_OPT,
        fmt: str = FORMAT_OPT,
    ) -> None:
        try:
            _execute(action, payload, output_file, fmt)
        except MailProxyError as exc:
            print_error(str(exc))
            sys.exit(1)


for _action in REGISTRY.values():
    _register(_action)
