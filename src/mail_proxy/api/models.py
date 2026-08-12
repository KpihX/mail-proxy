"""Pydantic domain models for mail-proxy — transport-agnostic representations.

Ported from `mail_mcp/core/models.py` (the mail-mcp content source).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Flag(str, Enum):
    """Standard IMAP system flags used by the flag actions.

    Examples:
        >>> Flag.SEEN.value
        '\\\\Seen'
        >>> Flag.DRAFT.value
        '\\\\Draft'
    """

    SEEN = "\\Seen"
    ANSWERED = "\\Answered"
    FLAGGED = "\\Flagged"
    DELETED = "\\Deleted"
    DRAFT = "\\Draft"


class Address(BaseModel):
    """An e-mail address with an optional display name.

    Attributes:
        name (str): Display name, e.g. `Ivann KAMDEM POUOKAM`.
        email (str): Bare address, e.g. `ivann.kamdem-pouokam@polytechnique.edu`.

    Examples:
        >>> Address(name="Ivann KAMDEM POUOKAM", email="ivann@polytechnique.edu").email
        'ivann@polytechnique.edu'
        >>> Address(email="x@y.fr").name
        ''
    """

    name: str = ""
    email: str


class Attachment(BaseModel):
    """Attachment metadata of a message (content fetched on demand).

    Attributes:
        filename (str): Attachment file name.
        content_type (str): MIME type, e.g. `application/pdf`.
        size_bytes (int): Size in bytes.
        content_b64 (str | None): Base64 content — only set by the download
            ingestion path, never prefetched.

    Examples:
        >>> Attachment(filename="report.pdf", content_type="application/pdf",
        ...            size_bytes=2048).size_bytes
        2048
        >>> Attachment(filename="a.txt", content_type="text/plain", size_bytes=1).filename
        'a.txt'
    """

    filename: str
    content_type: str
    size_bytes: int
    content_b64: str | None = None


class Message(BaseModel):
    """A full fetched message.

    Attributes:
        uid (int): Folder-scoped UID.
        message_id (str): RFC 2822 Message-ID header.
        subject (str): Decoded subject line.
        sender (Address | None): From address.
        recipients (list[Address]): To list.
        cc (list[Address]): Cc list.
        date (datetime | None): Message date.
        flags (list[str]): IMAP flags.
        folder (str): Folder the message was read from.
        body_text (str): Plain-text body (html2text when needed).
        body_html (str): Raw HTML body when present.
        attachments (list[Attachment]): Attachment metadata.
        in_reply_to (str): In-Reply-To header.
        references (list[str]): References header tokens.
        account_id (str): Account the message belongs to.

    Examples:
        >>> Message(uid=42).uid
        42
        >>> Message(uid=1, flags=["\\\\Seen"]).is_seen
        True
    """

    uid: int
    message_id: str = ""
    subject: str = ""
    sender: Address | None = None
    recipients: list[Address] = Field(default_factory=list)
    cc: list[Address] = Field(default_factory=list)
    date: datetime | None = None
    flags: list[str] = Field(default_factory=list)
    folder: str = "INBOX"
    body_text: str = ""
    body_html: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    in_reply_to: str = ""
    references: list[str] = Field(default_factory=list)
    account_id: str = ""

    @property
    def is_seen(self) -> bool:
        """True when the message carries the `\\Seen` flag.

        Returns:
            bool

        Examples:
            >>> Message(uid=1, flags=["\\\\Seen"]).is_seen
            True
            >>> Message(uid=1).is_seen
            False
        """
        return Flag.SEEN in self.flags

    @property
    def is_flagged(self) -> bool:
        """True when the message carries the `\\Flagged` flag.

        Returns:
            bool

        Examples:
            >>> Message(uid=1, flags=["\\\\Flagged"]).is_flagged
            True
            >>> Message(uid=1).is_flagged
            False
        """
        return Flag.FLAGGED in self.flags

    @property
    def has_attachments(self) -> bool:
        """True when the message declares at least one attachment.

        Returns:
            bool

        Examples:
            >>> Message(uid=1, attachments=[Attachment(filename="a",
            ...     content_type="text/plain", size_bytes=1)]).has_attachments
            True
            >>> Message(uid=1).has_attachments
            False
        """
        return len(self.attachments) > 0


class MessageSummary(BaseModel):
    """Lightweight listing row — avoids fetching full bodies.

    Attributes:
        uid (int): Folder-scoped UID.
        message_id (str): RFC 2822 Message-ID header.
        subject (str): Decoded subject line.
        sender (Address | None): From address.
        date (datetime | None): Message date.
        flags (list[str]): IMAP flags.
        folder (str): Folder the message was read from.
        has_attachments (bool): True when the message declares attachments.
        account_id (str): Account the message belongs to.

    Examples:
        >>> MessageSummary(uid=7, subject="Hello").subject
        'Hello'
        >>> MessageSummary(uid=7).has_attachments
        False
    """

    uid: int
    message_id: str = ""
    subject: str = ""
    sender: Address | None = None
    date: datetime | None = None
    flags: list[str] = Field(default_factory=list)
    folder: str = "INBOX"
    has_attachments: bool = False
    account_id: str = ""


class Folder(BaseModel):
    """An IMAP folder.

    Attributes:
        name (str): Folder name, e.g. `INBOX` or `Work/Project-X`.
        delimiter (str): Hierarchy delimiter, usually `/`.
        attributes (list[str]): IMAP folder attributes, e.g. `\\HasNoChildren`.
        message_count (int | None): MESSAGES count when a STATUS was fetched.
        unseen_count (int | None): UNSEEN count when a STATUS was fetched.

    Examples:
        >>> Folder(name="INBOX").is_selectable
        True
        >>> Folder(name="Noselect", attributes=["\\\\Noselect"]).is_selectable
        False
    """

    name: str
    delimiter: str = "/"
    attributes: list[str] = Field(default_factory=list)
    message_count: int | None = None
    unseen_count: int | None = None

    @property
    def is_selectable(self) -> bool:
        """True when the folder can be selected (no `\\Noselect` attribute).

        Returns:
            bool

        Examples:
            >>> Folder(name="INBOX").is_selectable
            True
            >>> Folder(name="Noselect", attributes=["\\\\Noselect"]).is_selectable
            False
        """
        return "\\Noselect" not in self.attributes


class SearchCriteria(BaseModel):
    """Declarative search — translated to IMAP criteria + client-side regex.

    Attributes:
        folder (str): Single folder to search (default INBOX).
        folders (list[str] | None): Multi-folder search (overrides folder).
        query (str | None): IMAP OR SUBJECT+BODY text search.
        sender (str | None): IMAP FROM substring.
        subject_filter (str | None): IMAP SUBJECT substring.
        to_filter (str | None): IMAP TO substring.
        cc_filter (str | None): IMAP CC substring.
        since (datetime | None): SINCE date (inclusive).
        before (datetime | None): BEFORE date (exclusive).
        unseen_only (bool): Restrict to UNSEEN.
        flagged_only (bool): Restrict to FLAGGED.
        has_attachment (bool): Restrict to multipart messages.
        min_size (int | None): IMAP LARGER (bytes).
        max_size (int | None): IMAP SMALLER (bytes).
        keyword (str | None): IMAP KEYWORD (custom flag/label).
        sender_pattern (str | None): Client-side regex on From address.
        subject_pattern (str | None): Client-side regex on Subject.
        body_pattern (str | None): Client-side regex on body (expensive).
        limit (int): Maximum rows returned.
        account_id (str | None): Account to search.

    Examples:
        >>> SearchCriteria(folder="INBOX", unseen_only=True).unseen_only
        True
        >>> SearchCriteria(query="invoice").query
        'invoice'
    """

    folder: str = "INBOX"
    folders: list[str] | None = None
    query: str | None = None
    sender: str | None = None
    subject_filter: str | None = None
    to_filter: str | None = None
    cc_filter: str | None = None
    since: datetime | None = None
    before: datetime | None = None
    unseen_only: bool = False
    flagged_only: bool = False
    has_attachment: bool = False
    min_size: int | None = None
    max_size: int | None = None
    keyword: str | None = None
    sender_pattern: str | None = None
    subject_pattern: str | None = None
    body_pattern: str | None = None
    limit: int = 20
    account_id: str | None = None
