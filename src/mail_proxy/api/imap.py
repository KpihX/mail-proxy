"""IMAP transport for mail-proxy — built on imapclient, ported from mail-mcp.

Design goals (unchanged from the mail-mcp content source):
- Context-manager lifecycle (connect on enter, logout on exit)
- Returns domain models (Message, MessageSummary, Folder) — no raw IMAP tuples
- Generic: works with any IMAP4rev1 server (Zimbra, Gmail, Outlook, Dovecot…)

All failures surface as `MailAPIError` with a clean, actionable message — no
raw imapclient exceptions, no stack traces, no credential leakage.
"""

from __future__ import annotations

import email
import email.header
import logging
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message as EmailMessage
from functools import wraps
from typing import Any, Self

import html2text
import imapclient
from imapclient.exceptions import IMAPClientError, LoginError

from ..config import AccountDef, api_timeout
from ..exceptions import MailAPIError
from .models import (
    Address,
    Attachment,
    Folder,
    Message,
    MessageSummary,
    SearchCriteria,
)

logger = logging.getLogger(__name__)


def _guard_imap(func: Callable[..., Any]) -> Callable[..., Any]:
    """Translate every raw imapclient/socket failure into a clean MailAPIError.

    The transport must never leak raw exceptions: a failed MOVE/STORE/SEARCH
    (e.g. a read-only mailbox) surfaces as a one-line actionable error, never a
    stack trace, and never credential data. Already-translated MailAPIErrors
    pass through untouched.

    Args:
        func (Callable[..., Any]): A public IMAPClient method.

    Returns:
        Callable[..., Any]: The wrapped method.

    Examples:
        >>> @_guard_imap
        ... def move_messages(self, uids, src, dst): ...
    """

    @wraps(func)
    def wrapper(self: IMAPClient, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(self, *args, **kwargs)
        except MailAPIError:
            raise
        except (IMAPClientError, OSError) as exc:
            raise MailAPIError(0, f"IMAP {func.__name__} failed: {exc}") from exc

    return wrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_header(raw: str | bytes | None) -> str:
    """Decode a possibly encoded RFC 2047 header value.

    Args:
        raw (str | bytes | None): Raw header value.

    Returns:
        str: Decoded text ("" for None).

    Examples:
        >>> _decode_header("=?utf-8?b?Qm9uam91cg==?=")
        'Bonjour'
        >>> _decode_header(None)
        ''
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    parts = email.header.decode_header(raw)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return " ".join(decoded)


def _parse_address_list(raw: str | None) -> list[Address]:
    """Parse a raw RFC 2822 address list ("Name <email>", "a@b.fr", …).

    Args:
        raw (str | None): Raw header value.

    Returns:
        list[Address]: Parsed addresses.

    Examples:
        >>> _parse_address_list("User <user@example.com>")[0].email
        'user@example.com'
        >>> _parse_address_list(None)
        []
    """
    if not raw:
        return []
    results = []
    for part in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", raw):
        part = part.strip()
        m = re.match(r'"?([^"<]*)"?\s*<([^>]+)>', part)
        if m:
            results.append(Address(name=m.group(1).strip(), email=m.group(2).strip()))
        elif "@" in part:
            results.append(Address(name="", email=part))
    return results


def _extract_text(msg: EmailMessage) -> tuple[str, str]:
    """Return (plain_text, html_text) from a parsed email.

    Args:
        msg (EmailMessage): Parsed message.

    Returns:
        tuple[str, str]: Plain and HTML bodies (HTML → plain fallback).

    Examples:
        >>> _extract_text(email.message_from_string("Subject: x\\n\\nHello"))[0]
        'Hello'
    """
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if "attachment" in cd:
                continue
            if ct == "text/plain" and not plain:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                plain = payload.decode(charset, errors="replace") if payload else ""
            elif ct == "text/html" and not html:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace") if payload else ""
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        body = payload.decode(charset, errors="replace") if payload else ""
        ct = msg.get_content_type()
        if ct == "text/html":
            html = body
        else:
            plain = body

    if html and not plain:
        h = html2text.HTML2Text()
        h.ignore_links = False
        plain = h.handle(html)

    return plain, html


def _extract_attachments(msg: EmailMessage) -> list[Attachment]:
    """Extract attachment metadata from a parsed email.

    Args:
        msg (EmailMessage): Parsed message.

    Returns:
        list[Attachment]: Metadata of every attachment/inline part.

    Examples:
        >>> len(_extract_attachments(email.message_from_string("x")))
        0
    """
    attachments = []
    if not msg.is_multipart():
        return attachments
    for part in msg.walk():
        cd = part.get("Content-Disposition", "")
        if "attachment" not in cd and "inline" not in cd:
            continue
        filename = _decode_header(part.get_filename())
        if not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            Attachment(
                filename=filename,
                content_type=part.get_content_type(),
                size_bytes=len(payload),
            )
        )
    return attachments


# ---------------------------------------------------------------------------
# IMAP client
# ---------------------------------------------------------------------------


class IMAPClient:
    """Thin wrapper around imapclient.IMAPClient with domain-model returns.

    Connect inside a `with` block (or call `connect()` explicitly); every IMAP
    failure is translated into a `MailAPIError` with the exact fix hint.

    Examples:
        >>> with IMAPClient(account) as c:
        ...     c.get_folder_status("INBOX").message_count
        128
    """

    def __init__(self, account: AccountDef) -> None:
        self.account = account
        self._client: imapclient.IMAPClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> IMAPClient:
        """Connect and log in to the IMAP server.

        Supports both password auth (``login()``) and OAuth2 (``XOAUTH2``
        via ``authenticate()``). For OAuth2, the access token is obtained
        from the stored token file with automatic refresh.

        Returns:
            IMAPClient: self, connected and authenticated.

        Raises:
            MailAPIError: On DNS/socket failure or login rejection.

        Examples:
            >>> IMAPClient(account).connect()._client is not None
            True
        """
        cfg = self.account.imap
        try:
            self._client = imapclient.IMAPClient(
                host=cfg.host,
                port=cfg.port,
                ssl=cfg.tls,
                timeout=api_timeout(),
            )
        except (TimeoutError, socket.gaierror, ConnectionRefusedError, OSError) as exc:
            raise MailAPIError(
                0,
                f"Cannot reach IMAP server {cfg.host}:{cfg.port} ({exc}).",
            ) from exc
        try:
            if self.account.auth_method == "oauth2":
                from ..oauth2 import get_valid_access_token

                access_token = get_valid_access_token(self.account.id)
                # imaplib.authenticate() expects raw bytes — it does base64 itself.
                # XOAUTH2 format: user=<email>\x01auth=Bearer <token>\x01\x01
                raw_xoauth2 = f"user={self.account.username}\x01auth=Bearer {access_token}\x01\x01".encode()
                self._client._imap.authenticate("XOAUTH2", lambda _: raw_xoauth2)
            else:
                self._client.login(self.account.username, self.account.password)
        except LoginError as exc:
            self._client = None
            raise MailAPIError(
                0,
                f"IMAP login rejected for account {self.account.id!r} — check "
                f"MAIL_{self.account.id.upper()}_LOGIN / _PASS or run "
                "'mail-proxy admin setup'.",
            ) from exc
        except IMAPClientError as exc:
            self._client = None
            raise MailAPIError(
                0, f"IMAP login failed for {self.account.id!r}: {exc}."
            ) from exc
        return self

    def disconnect(self) -> None:
        """Log out and release the connection.

        Returns:
            None

        Examples:
            >>> c = IMAPClient(account); c.connect(); c.disconnect()
            >>> c._client is None
            True
        """
        if self._client:
            try:
                self._client.logout()
            except Exception as exc:  # noqa: BLE001 - logout must never raise
                logger.debug("Logout error: %s", exc)
            self._client = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()

    def _c(self) -> imapclient.IMAPClient:
        """Return the raw imapclient instance (raises when not connected).

        Returns:
            imapclient.IMAPClient

        Raises:
            MailAPIError: When used outside a connection.

        Examples:
            >>> IMAPClient(account)._c()
            Traceback (most recent call last):
            ...
            mail_proxy.exceptions.MailAPIError: [0] IMAP client is not connected
        """
        if self._client is None:
            raise MailAPIError(0, "IMAP client is not connected.")
        return self._client

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    @_guard_imap
    def list_folders(self) -> list[Folder]:
        """List every IMAP folder of the account.

        Returns:
            list[Folder]: name, delimiter, attributes.

        Examples:
            >>> [f.name for f in IMAPClient(account).list_folders()]
            ['INBOX', 'Sent', 'Trash']
        """
        folders = []
        for flags, delimiter, name in self._c().list_folders():
            folders.append(
                Folder(
                    name=name,
                    delimiter=delimiter.decode()
                    if isinstance(delimiter, bytes)
                    else delimiter,
                    attributes=[
                        f.decode() if isinstance(f, bytes) else f for f in flags
                    ],
                )
            )
        return folders

    @_guard_imap
    def get_folder_status(self, folder: str = "INBOX") -> Folder:
        """Return MESSAGES and UNSEEN counts of a folder.

        Args:
            folder (str): Folder name.

        Returns:
            Folder: The folder with message_count/unseen_count populated.

        Examples:
            >>> IMAPClient(account).get_folder_status("INBOX").message_count
            128
        """
        status = self._c().folder_status(folder, ["MESSAGES", "UNSEEN"])
        return Folder(
            name=folder,
            message_count=status.get(b"MESSAGES"),
            unseen_count=status.get(b"UNSEEN"),
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def build_imap_criteria(self, criteria: SearchCriteria) -> list[object]:
        """Translate SearchCriteria into an imapclient-compatible criteria list.

        Args:
            criteria (SearchCriteria): Declarative search.

        Returns:
            list[object]: IMAP search keys, `["ALL"]` when empty.

        Examples:
            >>> IMAPClient(account).build_imap_criteria(SearchCriteria(unseen_only=True))
            ['UNSEEN']
            >>> IMAPClient(account).build_imap_criteria(SearchCriteria(query="x"))
            ['OR', ['SUBJECT', 'x'], ['BODY', 'x']]
        """
        c: list[object] = []
        if criteria.unseen_only:
            c.append("UNSEEN")
        if criteria.flagged_only:
            c.append("FLAGGED")
        if criteria.sender:
            c += ["FROM", criteria.sender]
        if criteria.subject_filter:
            c += ["SUBJECT", criteria.subject_filter]
        if criteria.to_filter:
            c += ["TO", criteria.to_filter]
        if criteria.cc_filter:
            c += ["CC", criteria.cc_filter]
        if criteria.since:
            c += ["SINCE", criteria.since.date()]
        if criteria.before:
            c += ["BEFORE", criteria.before.date()]
        if criteria.query:
            c += ["OR", ["SUBJECT", criteria.query], ["BODY", criteria.query]]
        if criteria.has_attachment:
            c += ["HEADER", "Content-Type", "multipart"]
        if criteria.min_size is not None:
            c += ["LARGER", criteria.min_size]
        if criteria.max_size is not None:
            c += ["SMALLER", criteria.max_size]
        if criteria.keyword:
            c += ["KEYWORD", criteria.keyword]
        return c or ["ALL"]

    @_guard_imap
    def search(self, criteria: SearchCriteria) -> list[int]:
        """Return UIDs matching the criteria (single folder, newest first).

        Args:
            criteria (SearchCriteria): Declarative search — `folder` selects.

        Returns:
            list[int]: Matching UIDs, limited to `criteria.limit`.

        Examples:
            >>> IMAPClient(account).search(SearchCriteria(folder="INBOX", limit=5))
            [128, 127, 126, 125, 124]
        """
        self._c().select_folder(criteria.folder, readonly=True)
        imap_criteria = self.build_imap_criteria(criteria)
        uids = self._c().search(imap_criteria, "UTF-8")  # type: ignore[reportArgumentType]
        uids = sorted(uids, reverse=True)
        return uids[: criteria.limit]

    @_guard_imap
    def message_exists(self, uid: int, folder: str) -> bool:
        """Whether a UID currently exists in a folder.

        The folder is re-selected before the UID probe: after a move the
        server-side selected mailbox is still the source folder, and searching
        there would report the moved UID as absent from its new home.

        Args:
            uid (int): The UID to probe.
            folder (str): Folder name.

        Returns:
            bool

        Examples:
            >>> IMAPClient(account).message_exists(42, "INBOX")
            True
            >>> IMAPClient(account).message_exists(999999, "INBOX")
            False
        """
        try:
            self._c().select_folder(folder, readonly=True)
            present = self._c().search(["UID", uid])  # type: ignore[reportArgumentType]
        except IMAPClientError:
            return False
        return bool(present)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    @_guard_imap
    def fetch_summaries(
        self, uids: list[int], folder: str = "INBOX"
    ) -> list[MessageSummary]:
        """Fetch lightweight summaries for UIDs (no bodies).

        Args:
            uids (list[int]): Folder-scoped UIDs.
            folder (str): Folder the UIDs belong to.

        Returns:
            list[MessageSummary]: Newest first.

        Examples:
            >>> IMAPClient(account).fetch_summaries([42], "INBOX")[0].uid
            42
        """
        if not uids:
            return []
        self._c().select_folder(folder, readonly=True)
        data = self._c().fetch(uids, ["ENVELOPE", "FLAGS", "BODYSTRUCTURE"])
        summaries = []
        for uid, msg_data in data.items():  # type: ignore[reportGeneralTypeIssues]
            envelope = msg_data.get(b"ENVELOPE")
            flags = [
                f.decode() if isinstance(f, bytes) else f
                for f in msg_data.get(b"FLAGS", [])  # type: ignore[reportGeneralTypeIssues]
            ]
            if envelope is None:
                continue
            subject = _decode_header(envelope.subject)
            sender = None
            if envelope.from_:
                addr = envelope.from_[0]
                name = _decode_header(addr.name) if addr.name else ""
                mailbox = addr.mailbox.decode() if addr.mailbox else ""
                host = addr.host.decode() if addr.host else ""
                sender = Address(name=name, email=f"{mailbox}@{host}")
            date = envelope.date if isinstance(envelope.date, datetime) else None
            has_attachments = bool(
                msg_data.get(b"BODYSTRUCTURE")
                and "attachment" in str(msg_data[b"BODYSTRUCTURE"])
            )

            summaries.append(
                MessageSummary(
                    uid=uid,
                    subject=subject,
                    sender=sender,
                    date=date,
                    flags=flags,
                    folder=folder,
                    has_attachments=has_attachments,
                    account_id=self.account.id,
                )
            )
        return sorted(
            summaries,
            key=lambda m: m.date or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )

    @_guard_imap
    def fetch_message(self, uid: int, folder: str = "INBOX") -> Message | None:
        """Fetch the full RFC822 message of a UID.

        Args:
            uid (int): Folder-scoped UID.
            folder (str): Folder name.

        Returns:
            Message | None: The full message, or None when the UID is absent.

        Examples:
            >>> IMAPClient(account).fetch_message(42).subject
            'Hello'
            >>> IMAPClient(account).fetch_message(999999) is None
            True
        """
        self._c().select_folder(folder, readonly=True)
        data = self._c().fetch([uid], ["RFC822", "FLAGS"])
        if uid not in data:  # type: ignore[reportGeneralTypeIssues]
            return None

        raw = data[uid].get(b"RFC822", b"")  # type: ignore[reportGeneralTypeIssues]
        flags = [
            f.decode() if isinstance(f, bytes) else f
            for f in data[uid].get(b"FLAGS", [])  # type: ignore[reportGeneralTypeIssues]
        ]

        msg = email.message_from_bytes(raw)  # type: ignore[reportArgumentType]
        plain, html = _extract_text(msg)
        attachments = _extract_attachments(msg)

        sender = None
        addrs = _parse_address_list(msg.get("From", ""))
        if addrs:
            sender = addrs[0]

        recipients = _parse_address_list(msg.get("To", ""))
        cc = _parse_address_list(msg.get("Cc", ""))

        date_str = msg.get("Date", "")
        date: datetime | None = None
        if date_str:
            from email.utils import parsedate_to_datetime

            try:
                date = parsedate_to_datetime(date_str)
            except (TypeError, ValueError, OverflowError) as exc:
                logger.debug("Unparsable message date %r: %s", date_str, exc)

        refs_raw = msg.get("References", "")
        references = [r.strip() for r in refs_raw.split() if r.strip()]

        return Message(
            uid=uid,
            message_id=msg.get("Message-ID", ""),
            subject=_decode_header(msg.get("Subject", "")),
            sender=sender,
            recipients=recipients,
            cc=cc,
            date=date,
            flags=flags,
            folder=folder,
            body_text=plain,
            body_html=html,
            attachments=attachments,
            in_reply_to=msg.get("In-Reply-To", ""),
            references=references,
            account_id=self.account.id,
        )

    @_guard_imap
    def fetch_bodies_for_pattern(
        self, uids: list[int], folder: str
    ) -> list[tuple[int, str, str, str]]:
        """Fetch (uid, from_str, subject, body_text) for client-side regex search.

        Args:
            uids (list[int]): UIDs to fetch.
            folder (str): Folder name.

        Returns:
            list[tuple[int, str, str, str]]: Raw rows for regex matching.

        Examples:
            >>> IMAPClient(account).fetch_bodies_for_pattern([42], "INBOX")[0][0]
            42
        """
        if not uids:
            return []
        self._c().select_folder(folder, readonly=True)
        data = self._c().fetch(uids, ["RFC822"])
        results = []
        for uid, msg_data in data.items():  # type: ignore[reportGeneralTypeIssues]
            raw = msg_data.get(b"RFC822", b"")  # type: ignore[reportGeneralTypeIssues]
            msg = email.message_from_bytes(raw)  # type: ignore[reportArgumentType]
            from_str = _decode_header(msg.get("From", ""))
            subject = _decode_header(msg.get("Subject", ""))
            plain, _ = _extract_text(msg)
            results.append((uid, from_str, subject, plain))
        return results

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    @_guard_imap
    def set_flags(
        self, uids: list[int], folder: str, flags: list[str], add: bool = True
    ) -> None:
        """Add or remove IMAP flags on UIDs.

        Args:
            uids (list[int]): Target UIDs.
            folder (str): Folder name.
            flags (list[str]): Flag names, e.g. `["\\\\Seen"]`.
            add (bool): True = add, False = remove.

        Returns:
            None

        Examples:
            >>> IMAPClient(account).set_flags([42], "INBOX", ["\\\\Seen"], add=True)
        """
        self._c().select_folder(folder)
        if add:
            self._c().add_flags(uids, flags)
        else:
            self._c().remove_flags(uids, flags)

    @_guard_imap
    def current_flags(self, uids: list[int], folder: str) -> dict[int, list[str]]:
        """Read back the current flags of UIDs (verification read).

        Args:
            uids (list[int]): Target UIDs.
            folder (str): Folder name.

        Returns:
            dict[int, list[str]]: {uid: flags}.

        Examples:
            >>> IMAPClient(account).current_flags([42], "INBOX")
            {42: ['\\\\Seen']}
        """
        self._c().select_folder(folder, readonly=True)
        data = self._c().fetch(uids, ["FLAGS"])
        return {
            uid: [  # type: ignore[reportGeneralTypeIssues]
                f.decode() if isinstance(f, bytes) else f
                for f in msg_data.get(b"FLAGS", [])  # type: ignore[reportGeneralTypeIssues]
            ]
            for uid, msg_data in data.items()  # type: ignore[reportGeneralTypeIssues]
        }

    @_guard_imap
    def move_messages(self, uids: list[int], src_folder: str, dst_folder: str) -> None:
        """Move UIDs between folders (MOVE when supported, else COPY+DELETE).

        Args:
            uids (list[int]): Target UIDs.
            src_folder (str): Source folder.
            dst_folder (str): Destination folder.

        Returns:
            None

        Examples:
            >>> IMAPClient(account).move_messages([42], "INBOX", "Archive")
        """
        self._c().select_folder(src_folder)
        capabilities = self._c().capabilities()
        if b"MOVE" in capabilities:
            self._c().move(uids, dst_folder)
        else:
            self._c().copy(uids, dst_folder)
            self._c().delete_messages(uids)
            self._c().expunge()

    @_guard_imap
    def delete_messages(self, uids: list[int], folder: str) -> None:
        """Permanently delete UIDs: mark `\\Deleted` and expunge immediately.

        Args:
            uids (list[int]): Target UIDs.
            folder (str): Folder name.

        Returns:
            None

        Examples:
            >>> IMAPClient(account).delete_messages([42], "INBOX")
        """
        self._c().select_folder(folder)
        self._c().delete_messages(uids)
        self._c().expunge()

    @_guard_imap
    def append_message(
        self, folder: str, raw_message: bytes, flags: list[str] | None = None
    ) -> int | None:
        """Append a raw RFC822 message to a folder (e.g. Drafts/Sent).

        Args:
            folder (str): Target folder.
            raw_message (bytes): Full RFC822 bytes.
            flags (list[str] | None): Flags for the appended message.

        Returns:
            int | None: Server-assigned UID when reported.

        Examples:
            >>> IMAPClient(account).append_message("Drafts", b"Subject: x")
            1
        """
        result = self._c().append(folder, raw_message, flags or [], None)
        return result if isinstance(result, int) else None

    # ------------------------------------------------------------------
    # Attachment download
    # ------------------------------------------------------------------

    @_guard_imap
    def download_attachment(
        self, uid: int, filename: str, folder: str = "INBOX"
    ) -> tuple[bytes, str]:
        """Download the raw bytes and content type of an attachment.

        Args:
            uid (int): Message UID.
            filename (str): Exact attachment filename.
            folder (str): Folder name.

        Returns:
            tuple[bytes, str]: (payload, content_type).

        Raises:
            MailAPIError: When the UID or filename does not exist.

        Examples:
            >>> IMAPClient(account).download_attachment(42, "report.pdf")
            (b'%PDF…', 'application/pdf')
        """
        self._c().select_folder(folder, readonly=True)
        data = self._c().fetch([uid], ["RFC822"])
        if uid not in data:
            raise MailAPIError(0, f"Message UID {uid} not found in {folder}.")
        raw = data[uid][b"RFC822"]  # type: ignore[reportGeneralTypeIssues]
        msg = email.message_from_bytes(raw)  # type: ignore[reportArgumentType]
        for part in msg.walk():
            fn = _decode_header(part.get_filename() or "")
            if fn == filename:
                payload = part.get_payload(decode=True)
                if payload is None:
                    raise MailAPIError(
                        0,
                        f"Attachment {filename!r} has an unsupported multipart payload.",
                    )
                return payload, part.get_content_type()  # type: ignore[reportReturnType]
        raise MailAPIError(
            0, f"Attachment {filename!r} not found in message UID {uid}."
        )

    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------

    @_guard_imap
    def create_folder(self, name: str) -> None:
        """Create a new IMAP folder.

        Args:
            name (str): Folder name ('/' separates sub-folders).

        Returns:
            None

        Examples:
            >>> IMAPClient(account).create_folder("Work/Project-X")
        """
        self._c().create_folder(name)

    @_guard_imap
    def delete_folder(self, name: str) -> None:
        """Delete an IMAP folder (must be empty on some servers).

        Args:
            name (str): Folder name.

        Returns:
            None

        Examples:
            >>> IMAPClient(account).delete_folder("Work/Project-X")
        """
        self._c().delete_folder(name)

    @_guard_imap
    def rename_folder(self, old_name: str, new_name: str) -> None:
        """Rename an IMAP folder.

        Args:
            old_name (str): Current name.
            new_name (str): New name.

        Returns:
            None

        Examples:
            >>> IMAPClient(account).rename_folder("Old", "New")
        """
        self._c().rename_folder(old_name, new_name)

    @_guard_imap
    def folder_exists(self, name: str) -> bool:
        """Whether a folder exists on the server.

        Args:
            name (str): Folder name.

        Returns:
            bool

        Examples:
            >>> IMAPClient(account).folder_exists("INBOX")
            True
            >>> IMAPClient(account).folder_exists("Nope")
            False
        """
        return any(f.name == name for f in self.list_folders())

    # ------------------------------------------------------------------
    # Keyword / label management
    # ------------------------------------------------------------------

    @_guard_imap
    def set_keyword(
        self, uids: list[int], folder: str, keyword: str, add: bool = True
    ) -> None:
        """Add or remove a custom IMAP keyword (user-defined label/tag).

        Args:
            uids (list[int]): Target UIDs.
            folder (str): Folder name.
            keyword (str): Keyword, e.g. `todo`.
            add (bool): True = add, False = remove.

        Returns:
            None

        Examples:
            >>> IMAPClient(account).set_keyword([42], "INBOX", "todo", add=True)
        """
        self._c().select_folder(folder)
        if add:
            self._c().add_flags(uids, [keyword])
        else:
            self._c().remove_flags(uids, [keyword])

    @_guard_imap
    def list_keywords(self, folder: str = "INBOX") -> list[str]:
        """Return user-defined keywords available on a folder (PERMANENTFLAGS).

        Args:
            folder (str): Folder name.

        Returns:
            list[str]: Custom keywords — system flags are excluded.

        Examples:
            >>> IMAPClient(account).list_keywords("INBOX")
            ['important', 'todo']
        """
        resp = self._c().select_folder(folder, readonly=True)
        raw_flags = resp.get(b"PERMANENTFLAGS", [])
        standard = {"\\Seen", "\\Answered", "\\Flagged", "\\Deleted", "\\Draft", "\\*"}
        result = []
        for f in raw_flags:
            s = f.decode() if isinstance(f, bytes) else str(f)
            if s not in standard:
                result.append(s)
        return result
