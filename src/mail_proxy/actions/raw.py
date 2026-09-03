"""Raw escape hatch — IMAP, SMTP RFC822, and Gmail API. Always HITL."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from ..api.smtp import SMTPClient
from ..config import AccountDef, api_timeout
from ..exceptions import MailProxyError
from ..oauth2 import get_valid_access_token
from .base import AccountScoped, action_def, require_approval


class RawPayload(AccountScoped):
    """Payload of `raw` — one arbitrary operation in one mail protocol.

    `raw` is the FOUNDATION action: every other `do` action is a thin,
    ergonomic specialisation of it.  For IMAP it dispatches the requested
    imapclient method directly on the shared connection (full protocol
    coverage + imapclient fallbacks).  For SMTP it submits arbitrary RFC822
    bytes.  For Gmail API it calls any REST endpoint.

    Attributes:
        protocol (str): Required: `imap`, `smtp`, or `gmail-api`.
        method (str): Required. `imap`: imapclient method name (`fetch`,
            `search`, `move`, `copy`, `set_flags`, `add_flags`, `remove_flags`,
            `get_flags`, `folder_status`, `list_folders`, `expunge`,
            `namespace`, `uid`, `append`, …). `smtp`: `send-rfc822`.
            `gmail-api`: HTTP verb.
        args (list): Positional arguments passed to the method.
        select (str | None): Folder to select first ("" = none).
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> RawPayload(protocol="imap", method="search", args=[["ALL"]]).method
        'search'
    """

    model_config = ConfigDict(extra="forbid")
    protocol: Literal["imap", "smtp", "gmail-api"] = Field(
        ..., description="Required protocol"
    )
    method: str = Field(..., description="Required protocol-native method")
    args: list[Any] = Field(
        default_factory=list, description="Positional arguments for the method"
    )
    select: str | None = Field(None, description='Folder to SELECT first ("" = none)')
    endpoint: str | None = Field(None, description="Gmail API path beginning with /")
    params: dict[str, Any] | None = Field(None, description="Protocol parameters")
    payload: Any = Field(None, description="Gmail API JSON request body")

    @model_validator(mode="after")
    def validate_protocol_shape(self) -> "RawPayload":
        if self.protocol == "imap":
            if (
                self.endpoint is not None
                or self.params is not None
                or self.payload is not None
            ):
                raise ValueError(
                    "imap accepts only protocol, method, args, select, account_id"
                )
        elif self.protocol == "smtp":
            if (
                self.method != "send-rfc822"
                or self.args
                or self.select is not None
                or self.endpoint is not None
                or self.payload is not None
            ):
                raise ValueError("smtp requires method='send-rfc822' and params only")
            if (
                not self.params
                or not {"recipients", "rfc822_base64"} <= self.params.keys()
            ):
                raise ValueError("smtp params require recipients and rfc822_base64")
        elif (
            self.method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
            or not self.endpoint
            or not self.endpoint.startswith("/")
            or self.args
            or self.select is not None
        ):
            raise ValueError(
                "gmail-api requires HTTP method, /endpoint, optional params/payload"
            )
        return self


def _sanitize(obj: Any) -> Any:
    """Recursively convert imapclient results to JSON-safe values.

    Handles bytes (keys and values), dicts, lists, tuples, and namedtuples
    (imapclient returns e.g. FolderInfo / QuotaInfo as namedtuples).
    """
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {_sanitize(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(item) for item in obj]
    if hasattr(obj, "_asdict"):  # namedtuple
        return {k: _sanitize(v) for k, v in obj._asdict().items()}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _raw_imap(conn: Any, p: RawPayload) -> dict:
    """Dispatch an imapclient method on the shared connection.

    `do raw` (IMAP) is the foundation: it forwards `method` + `args` to the
    raw imapclient instance — the same connection the curated `do` actions
    use — giving full protocol coverage.  It is a PURE passthrough: no
    routing, no fallback logic.  When a single imapclient call cannot express
    the operation (e.g. a server lacks the MOVE capability), compose multiple
    raw calls instead (e.g. `copy` + `delete_messages` + `expunge`).
    """
    raw = conn._c()  # raw imapclient.IMAPClient
    if p.select:
        # Read-write selection: raw is the foundation and must be able to
        # write (delete, expunge, store, move).  Read ops work read-write too.
        raw.select_folder(p.select, readonly=False)
    method = getattr(raw, p.method, None)
    if method is None or not callable(method):
        return {
            "typ": "NO",
            "data": [],
            "error": f"Unknown imapclient method: {p.method!r}",
        }
    try:
        result = method(*p.args)
    except Exception as exc:  # noqa: BLE001 - provider response belongs in raw data
        return {"typ": "NO", "data": [], "error": str(exc)}
    return {"typ": "OK", "data": _sanitize(result)}


def _raw_smtp(account: AccountDef, p: RawPayload) -> dict:
    """Submit arbitrary RFC822 bytes through the configured SMTP transport."""
    assert p.params is not None
    recipients = p.params["recipients"]
    rfc822_base64 = p.params["rfc822_base64"]
    if (
        not isinstance(recipients, list)
        or not all(isinstance(item, str) for item in recipients)
        or not isinstance(rfc822_base64, str)
    ):
        raise MailProxyError(
            "smtp params recipients must be strings and rfc822_base64 must be a string."
        )
    try:
        message = base64.b64decode(rfc822_base64, validate=True)
    except ValueError as exc:
        raise MailProxyError("raw smtp rfc822_base64 must be valid base64.") from exc
    sender = p.params.get("envelope_from") or account.from_address
    try:
        with SMTPClient(account)._connect() as server:
            refused = server.sendmail(
                sender, recipients, SMTPClient._normalize_crlf(message)
            )
    except Exception as exc:  # noqa: BLE001 - SMTP provider response belongs in raw data
        return {"typ": "NO", "data": {}, "error": str(exc)}
    return {
        "typ": "OK",
        "data": {
            "envelope_from": sender,
            "recipients": recipients,
            "refused": refused,
        },
    }


def _raw_gmail_api(account: AccountDef, p: RawPayload) -> dict:
    """Call any Gmail REST endpoint with the account's Google OAuth token."""
    if account.oauth2_provider != "google" or account.auth_method != "oauth2":
        raise MailProxyError("raw gmail-api requires a Google OAuth2 account.")
    if not p.endpoint or not p.endpoint.startswith("/"):
        raise MailProxyError("raw gmail-api endpoint must begin with '/'.")
    query = urllib.parse.urlencode(p.params or {})
    url = f"https://gmail.googleapis.com/gmail/v1{p.endpoint}" + (
        f"?{query}" if query else ""
    )
    body = json.dumps(p.payload).encode() if p.payload is not None else None
    request = urllib.request.Request(url, data=body, method=p.method.upper())
    request.add_header("Authorization", f"Bearer {get_valid_access_token(account.id)}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=api_timeout()) as response:
            raw_body = response.read().decode("utf-8")
            return {
                "typ": str(response.status),
                "data": json.loads(raw_body) if raw_body else {},
            }
    except urllib.error.HTTPError as exc:
        return {
            "typ": str(exc.code),
            "data": {},
            "error": exc.read().decode("utf-8", errors="replace"),
        }
    except OSError as exc:
        return {"typ": "NO", "data": {}, "error": str(exc)}


@require_approval()
def raw(client: Any, p: RawPayload) -> dict:
    """Run arbitrary IMAP, SMTP RFC822, or Gmail API operations. ALWAYS HITL.

    `raw` is the foundation action: every other `do` action is a thin
    ergonomic specialisation of it.  It is unlimited only inside the selected
    provider protocol.  Every call requires HITL and has no automatic
    verification.

    Parameters:
        - protocol (str): Required: `imap`, `smtp`, or `gmail-api`.
        - method (str): Required. `imap`: imapclient method name (`fetch`,
          `search`, `move`, `copy`, `set_flags`, `folder_status`,
          `list_folders`, `expunge`, `namespace`, `uid`, `append`, …).
          `smtp`: `send-rfc822`. `gmail-api`: HTTP verb.
        - args (list): Positional arguments passed to the method (nested lists
          allowed, e.g. `[[13519], ["FLAGS"]]` for `fetch`).
        - select (str | None): Folder to SELECT first ("" = none).
        - SMTP: `method="send-rfc822"` and `params` with recipients/rfc822_base64.
        - Gmail API: `method`, `endpoint`, `params`, `payload`.
        - account_id (str | None): Account id (omit → default).

    Examples:
        - IMAP SEARCH (subject quoted per IMAP):
            `mail-proxy do raw '{"protocol":"imap","method":"search","args":[["SUBJECT","\\"[raw-v2] test\\""]],"select":"INBOX"}'`
            → {"typ":"OK","data":[311,312]}

        - IMAP FETCH flags+size of a UID:
            `mail-proxy do raw '{"protocol":"imap","method":"fetch","args":[[312],["FLAGS","RFC822.SIZE"]],"select":"INBOX"}'`
            → {"typ":"OK","data":{"312":{"FLAGS":["\\\\Seen"],"RFC822.SIZE":53085}}}

        - IMAP MOVE to Archive (fallback COPY+STORE+EXPUNGE inherited):
            `mail-proxy do raw '{"protocol":"imap","method":"move","args":[[312],"Archive"],"select":"INBOX"}'`
            → {"typ":"OK","data":{"312":"Archive"}}

        - IMAP folder status:
            `mail-proxy do raw '{"protocol":"imap","method":"folder_status","args":["INBOX"]}'`
            → {"typ":"OK","data":{"MESSAGES":128,"UNSEEN":14}}

        - IMAP list folders:
            `mail-proxy do raw '{"protocol":"imap","method":"list_folders","args":[]}'`
            → {"typ":"OK","data":[{"name":"INBOX","flags":["\\\\HasNoChildren"]}]}

        - IMAP expunge:
            `mail-proxy do raw '{"protocol":"imap","method":"expunge","args":[],"select":"INBOX"}'`
            → {"typ":"OK","data":[1]}

        - Submit arbitrary MIME through SMTP:
            `mail-proxy do raw '{"protocol":"smtp","method":"send-rfc822","params":{"recipients":["a@b.fr"],"rfc822_base64":"RnJvbTogeEB5LmZyDQoNCkhp"}}'`
            → {"typ":"OK","data":{"recipients":["a@b.fr"],"refused":{}}}

        - Add Gmail's STARRED label through its canonical API:
            `mail-proxy do raw '{"protocol":"gmail-api","method":"post","endpoint":"/users/me/messages/ID/modify","payload":{"addLabelIds":["STARRED"]},"account_id":"gmail"}'`
            → {"typ":"200","data":{"id":"ID","labelIds":["SPAM","STARRED"]}}

    Note:
        Since the raw action is arbitrary and can execute mutations, it always
        requires HITL validation.  Run it from a tmux ops pane so you can
        receive and approve the browser review page:
        `mail-proxy do raw ./raw_payload.json`
    """
    account = client.account
    if p.protocol == "smtp":
        smtp_client = client.smtp()
        account = smtp_client.account
    if p.protocol == "imap":
        return _raw_imap(client.imap(), p)
    if p.protocol == "smtp":
        return _raw_smtp(account, p)
    return _raw_gmail_api(account, p)


ACTIONS = [action_def("raw", RawPayload, raw, group="Escape hatch")]
