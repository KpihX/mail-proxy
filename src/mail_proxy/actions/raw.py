"""Raw escape hatch — any IMAP command on a DEDICATED connection. HITL required.

The mail equivalent of tick-proxy's `raw`: instead of arbitrary HTTP endpoints
it runs arbitrary IMAP commands through a fresh, isolated imaplib connection
(so the shared imapclient state machine can never be corrupted). Expert-only.
"""

import imaplib
import logging
import socket
from typing import Any

from pydantic import Field

from ..config import AccountDef, api_timeout, get_account
from ..exceptions import MailAPIError
from .base import AccountScoped, action_def, require_approval

logger = logging.getLogger(__name__)


class RawPayload(AccountScoped):
    """Payload of `raw` — one arbitrary IMAP command.

    Attributes:
        command (str): IMAP command name, e.g. `STATUS` or `UID`.
        args (list[str]): Remaining command arguments, e.g.
            `["1:*", "(FLAGS)"]` after `UID`/`FETCH`.
        select (str | None): Folder to SELECT first ("" = none).
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> RawPayload(command="STATUS", args=["INBOX", "(MESSAGES)"]).command
        'STATUS'
    """

    command: str = Field(..., description='IMAP command, e.g. "STATUS" or "UID"')
    args: list[str] = Field(
        default_factory=list, description='Command arguments, e.g. ["1:*", "(FLAGS)"]'
    )
    select: str | None = Field(None, description='Folder to SELECT first ("" = none)')


def _decode_response(data: Any) -> list[Any]:
    """Decode raw imaplib response bytes into strings.

    Args:
        data (Any): imaplib response data (bytes or nested tuples).

    Returns:
        list[Any]: Decoded values.

    Examples:
        >>> _decode_response([b"OK"])
        ['OK']
        >>> _decode_response([(b"1", b"(\\Seen)")])
        ['1 (\\Seen)']
    """
    out: list[Any] = []
    for item in data:
        if isinstance(item, tuple):
            decoded = []
            for part in item:
                if isinstance(part, bytes):
                    decoded.append(part.decode("utf-8", errors="replace"))
                else:
                    decoded.append(part)
            out.append(b" ".join(decoded))
        elif isinstance(item, bytes):
            out.append(item.decode("utf-8", errors="replace"))
        else:
            out.append(item)
    return out


def _raw_connection(account: AccountDef) -> imaplib.IMAP4:
    """Open a dedicated imaplib connection to the account's IMAP server.

    Args:
        account (AccountDef): Resolved account.

    Returns:
        imaplib.IMAP4: Connected and authenticated (IMAP4_SSL or IMAP4+STARTTLS).

    Raises:
        MailAPIError: On network or login failure.

    Examples:
        >>> _raw_connection(account).state
        'AUTH'
    """
    cfg = account.imap
    try:
        if cfg.tls:
            imap = imaplib.IMAP4_SSL(cfg.host, cfg.port, timeout=api_timeout())
        else:
            imap = imaplib.IMAP4(cfg.host, cfg.port, timeout=api_timeout())
            imap.starttls()
    except (TimeoutError, socket.gaierror, ConnectionRefusedError, OSError) as exc:
        raise MailAPIError(
            0, f"Cannot reach IMAP server {cfg.host}:{cfg.port} ({exc})."
        ) from exc
    try:
        imap.login(account.username, account.password)
    except imaplib.IMAP4.error as exc:
        try:
            imap.logout()
        except Exception as logout_exc:  # noqa: BLE001 - best-effort cleanup
            logger.debug("Logout after failed login: %s", logout_exc)
        raise MailAPIError(
            0,
            f"IMAP login rejected for account {account.id!r} — check "
            f"MAIL_{account.id.upper()}_LOGIN / _PASS or run "
            "'mail-proxy admin setup'.",
        ) from exc
    return imap


@require_approval()
def raw(client: Any, p: RawPayload) -> dict:
    """Run ANY IMAP command directly — 100 % protocol coverage. HITL required.

    Whatever the 23 business actions do not cover is reachable here: arbitrary
    commands (STATUS, NAMESPACE, UID FETCH, UID EXPUNGE, …) on a dedicated
    imaplib connection that is closed right after the call — the shared
    connection is never touched. Because it is arbitrary, it always passes
    through the HITL review form and is never automatically verified.

    Parameters:
        - command (str): IMAP command name, e.g. `STATUS` or `UID`.
        - args (list[str]): Remaining command arguments — for `UID` commands
          the verb comes first, e.g. `["FETCH", "1:*", "(FLAGS)"]`.
        - select (str | None): Folder to SELECT first ("" = none).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Folder STATUS:
            `mail-proxy do raw '{"command":"STATUS","args":["INBOX","(MESSAGES UNSEEN)"]}'`
            → {"typ":"OK","data":["STATUS INBOX (MESSAGES 312 UNSEEN 14)"]}

        - UID FETCH flags of the newest message:
            `mail-proxy do raw '{"command":"UID","args":["FETCH","312","(FLAGS)"],"select":"INBOX"}'`
            → {"typ":"OK","data":[["312 (FLAGS (\\Seen))"]]}

        - NAMESPACE probe:
            `mail-proxy do raw '{"command":"NAMESPACE"}'`
            → {"typ":"OK","data":[["(\\"\\" \\"/\\") NIL NIL"]]}

        - UID SEARCH for a keyword:
            `mail-proxy do raw '{"command":"UID","args":["SEARCH","KEYWORD","todo"],"select":"INBOX"}'`
            → {"typ":"OK","data":[["311 312"]]}

        - Raw STATUS on a sub-folder:
            `mail-proxy do raw '{"command":"STATUS","args":["Archive","(MESSAGES)"]}'`
            → {"typ":"OK","data":["STATUS Archive (MESSAGES 120)"]}

    Note:
        Since the raw action is arbitrary and can execute mutations, it always
        requires HITL validation. Run it from a tmux ops pane so you can
        receive and approve the browser review page:
        `mail-proxy do raw ./raw_payload.json`
    """
    account = get_account(p.account_id)
    imap = _raw_connection(account)
    try:
        if p.select:
            typ, _ = imap.select(p.select, readonly=True)
            if typ != "OK":
                return {
                    "typ": typ,
                    "data": _decode_response(_),
                    "error": f"SELECT failed for {p.select!r}",
                }  # type: ignore[arg-type]
        typ, data = imap._simple_command(p.command, *p.args)
        # Collect the untagged responses so multi-line replies are not lost.
        if p.command.upper() in ("UID",):
            typ, data = imap.untagged_response(typ, data, "FETCH")
        return {"typ": typ, "data": _decode_response(data)}
    except (OSError, imaplib.IMAP4.error) as exc:
        return {"typ": "NO", "data": [], "error": str(exc)}
    finally:
        try:
            imap.logout()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            logger.debug("Logout error: %s", exc)


ACTIONS = [action_def("raw", RawPayload, raw, group="Escape hatch")]
