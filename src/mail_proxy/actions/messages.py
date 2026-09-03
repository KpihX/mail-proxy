"""Message actions — read, search, flags, moves, archive/trash/spam/delete.

Deletion is irreversible: it is HITL-gated, preflighted with a locked identity,
and verified by polling the folder until the UIDs are gone. Every message move
(manual move, archive, trash, or spam) is also HITL-gated and read-back
verified. Flag changes remain reversible and run without HITL.
"""

import re
from collections import defaultdict
from datetime import UTC, datetime

from pydantic import Field

from ..api.models import SearchCriteria
from ..client import MailClient
from ..exceptions import MailProxyError
from ..models import Verification
from .base import (
    AccountScoped,
    ActionDef,
    action_def,
    compare,
    remaining_uids,
    require_approval,
    require_preflight,
    require_verification,
    verify_absence,
)

_ARCHIVE_CANDIDATES = [
    "Archive",
    "Archives",
    "All Mail",
    "[Gmail]/All Mail",
    "[Gmail]/Tous les messages",
]
_SPAM_CANDIDATES = ["Spam", "Junk", "Junk E-mail", "[Gmail]/Spam"]
_TRASH_CANDIDATES = [
    "Trash",
    "Deleted Items",
    "Deleted Messages",
    "Deleted",
    "[Gmail]/Trash",
    "[Gmail]/Corbeille",
]


class MessageListPayload(AccountScoped):
    """Payload of `message-list`.

    Attributes:
        folder (str): Folder to browse (default INBOX).
        limit (int): Max rows (default 20).
        unseen_only (bool): Only unread messages.
        flagged_only (bool): Only flagged messages.

    Examples:
        >>> MessageListPayload().folder
        'INBOX'
        >>> MessageListPayload(unseen_only=True).unseen_only
        True
    """

    folder: str = Field("INBOX", description="Folder to browse")
    limit: int = Field(20, description="Max rows")
    unseen_only: bool = Field(False, description="Only unread messages")
    flagged_only: bool = Field(False, description="Only flagged messages")


class MessageInfoPayload(AccountScoped):
    """Payload of `message-info`.

    Attributes:
        uid (int): Message UID.
        folder (str): Folder of the message (default INBOX).

    Examples:
        >>> MessageInfoPayload(uid=42).uid
        42
    """

    uid: int = Field(..., description="Message UID")
    folder: str = Field("INBOX", description="Folder of the message")


class MessageSearchPayload(AccountScoped):
    """Payload of `message-search` — the full search filter.

    Attributes:
        query (str | None): IMAP text match in subject OR body.
        sender (str | None): IMAP FROM substring.
        sender_pattern (str | None): Client-side regex on the From address.
        subject_filter (str | None): IMAP SUBJECT substring.
        subject_pattern (str | None): Client-side regex on the subject.
        to_filter (str | None): IMAP TO substring.
        cc_filter (str | None): IMAP CC substring.
        body_pattern (str | None): Client-side regex on the body (expensive).
        keyword (str | None): Custom IMAP keyword/label.
        folder (str): Single folder (default INBOX).
        folders (list[str] | None): Multi-folder search (overrides folder).
        since (str | None): ISO date "2026-08-01" (inclusive).
        before (str | None): ISO date "2026-08-31" (exclusive).
        unseen_only (bool): Only unread.
        flagged_only (bool): Only flagged.
        has_attachment (bool): Only messages with attachments.
        min_size (int | None): Minimum size in bytes.
        max_size (int | None): Maximum size in bytes.
        limit (int): Max rows (default 20).

    Examples:
        >>> MessageSearchPayload(sender="@polytechnique.edu").sender
        '@polytechnique.edu'
    """

    query: str | None = Field(None, description="Text match in subject OR body (IMAP)")
    sender: str | None = Field(None, description="FROM substring (IMAP)")
    sender_pattern: str | None = Field(None, description="Regex on From address")
    subject_filter: str | None = Field(None, description="SUBJECT substring (IMAP)")
    subject_pattern: str | None = Field(None, description="Regex on subject")
    to_filter: str | None = Field(None, description="TO substring (IMAP)")
    cc_filter: str | None = Field(None, description="CC substring (IMAP)")
    body_pattern: str | None = Field(None, description="Regex on body (expensive)")
    keyword: str | None = Field(None, description="Custom IMAP keyword/label")
    folder: str = Field("INBOX", description="Single folder to search")
    folders: list[str] | None = Field(None, description="Multi-folder search")
    since: str | None = Field(None, description='ISO date, e.g. "2026-08-01"')
    before: str | None = Field(None, description='ISO date, e.g. "2026-08-31"')
    unseen_only: bool = Field(False, description="Only unread")
    flagged_only: bool = Field(False, description="Only flagged")
    has_attachment: bool = Field(False, description="Only messages with attachments")
    min_size: int | None = Field(None, description="Min size in bytes")
    max_size: int | None = Field(None, description="Max size in bytes")
    limit: int = Field(20, description="Max rows")


class MessageThreadPayload(AccountScoped):
    """Payload of `message-thread`.

    Attributes:
        message_id (str): Message-ID of the thread root.
        folder (str): Folder to search (default INBOX).
        limit (int): Max messages (default 50, capped at 50).

    Examples:
        >>> MessageThreadPayload(message_id="<abc@host>").message_id
        '<abc@host>'
    """

    message_id: str = Field(..., description="Message-ID of the thread")
    folder: str = Field("INBOX", description="Folder to search")
    limit: int = Field(50, description="Max messages")


class MessageMarkPayload(AccountScoped):
    """Payload of `message-mark`.

    Attributes:
        uids (list[int]): Target UIDs.
        folder (str): Folder of the messages (default INBOX).
        seen (bool | None): True=read, False=unread.
        flagged (bool | None): True=star, False=unstar.
        answered (bool | None): True=answered, False=unanswered.
        draft (bool | None): True=draft flag, False=remove.

    Examples:
        >>> MessageMarkPayload(uids=[1], seen=True).seen
        True
    """

    uids: list[int] = Field(..., description="Target UIDs")
    folder: str = Field("INBOX", description="Folder of the messages")
    seen: bool | None = Field(None, description="True=read, False=unread")
    flagged: bool | None = Field(None, description="True=star, False=unstar")
    answered: bool | None = Field(None, description="True=answered, False=unanswered")
    draft: bool | None = Field(None, description="True=draft, False=remove draft flag")


class MessageMovePayload(AccountScoped):
    """Payload of `message-move`.

    Attributes:
        uids (list[int]): Target UIDs.
        destination_folder (str): Destination folder.
        source_folder (str): Source folder (default INBOX).

    Examples:
        >>> MessageMovePayload(uids=[1], destination_folder="Archive").destination_folder
        'Archive'
    """

    uids: list[int] = Field(..., description="Target UIDs")
    destination_folder: str = Field(..., description="Destination folder")
    source_folder: str = Field("INBOX", description="Source folder")


class MessageMoveSimplePayload(AccountScoped):
    """Payload of `message-archive` / `message-trash` / `message-spam`.

    Attributes:
        uids (list[int]): Target UIDs.
        source_folder (str): Source folder (default INBOX).

    Examples:
        >>> MessageMoveSimplePayload(uids=[1]).source_folder
        'INBOX'
    """

    uids: list[int] = Field(..., description="Target UIDs")
    source_folder: str = Field("INBOX", description="Source folder")


class MessageDeletePayload(AccountScoped):
    """Payload of `message-delete` — the only irreversible message write.

    Attributes:
        uids (list[int]): Target UIDs.
        folder (str): Folder of the messages (default INBOX).

    Examples:
        >>> MessageDeletePayload(uids=[1]).folder
        'INBOX'
    """

    uids: list[int] = Field(..., description="Target UIDs")
    folder: str = Field("INBOX", description="Folder of the messages")


def _resolve_folder(client: MailClient, candidates: list[str]) -> str:
    """Return the first existing candidate folder name.

    Args:
        client (MailClient): The mail client.
        candidates (list[str]): Ordered folder-name candidates.

    Returns:
        str: The first name found on the server (or the first candidate).

    Examples:
        >>> _resolve_folder(client, ["Archive", "Archives"])
        'Archive'
    """
    existing = {f.name for f in client.imap().list_folders()}
    for name in candidates:
        if name in existing:
            return name
    return candidates[0]


def _summaries_to_dicts(summaries: list) -> list[dict]:
    """Convert MessageSummary rows into the compact output dicts.

    Args:
        summaries (list): MessageSummary rows.

    Returns:
        list[dict]: uid/subject/from/date/flags/has_attachments/folder.

    Examples:
        >>> _summaries_to_dicts([])
        []
    """
    return [
        {
            "uid": m.uid,
            "subject": m.subject,
            "from": m.sender.email if m.sender else "",
            "date": m.date.isoformat() if m.date else "",
            "flags": m.flags,
            "has_attachments": m.has_attachments,
            "folder": m.folder,
        }
        for m in summaries
    ]


# ─── Read actions ─────────────────────────────────────────────────────────────


def message_list(client: MailClient, p: MessageListPayload) -> list[dict]:
    """List messages in a folder.

    Returns lightweight summaries (uid, subject, from, date, flags). Use
    `message-info` for the full body of a specific UID. `find_unread` of
    mail-mcp is folded here: `{"unseen_only": true}`.

    Parameters:
        - folder (str): Folder to browse (default INBOX).
        - limit (int): Max rows (default 20).
        - unseen_only (bool): Only unread messages.
        - flagged_only (bool): Only flagged messages.
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Browse INBOX:
            `mail-proxy do message-list`
            → [{"uid":312,"subject":"Re: TP","from":"x@y.fr","date":"2026-08-12T09:00:00+00:00","flags":["\\Seen"],"has_attachments":false,"folder":"INBOX"}]
        - Unread messages (find_unread equivalent):
            `mail-proxy do message-list '{"unseen_only":true,"limit":5}'`
            → [{"uid":310,"subject":"Lebara eSIM","from":"no-reply@lebara.fr","date":"2026-08-12T08:00:00+00:00","flags":[],"has_attachments":false,"folder":"INBOX"}]
        - Flagged messages in Archive:
            `mail-proxy do message-list '{"folder":"Archive","flagged_only":true}'`
            → [{"uid":99,"subject":"Contrat","from":"hr@company.com","date":"2026-07-01T10:00:00+00:00","flags":["\\Flagged"],"has_attachments":true,"folder":"Archive"}]
    """
    criteria = SearchCriteria(
        folder=p.folder,
        unseen_only=p.unseen_only,
        flagged_only=p.flagged_only,
        limit=p.limit,
    )
    imap = client.imap()
    uids = imap.search(criteria)
    summaries = imap.fetch_summaries(uids, p.folder)
    return _summaries_to_dicts(summaries)


def message_info(client: MailClient, p: MessageInfoPayload) -> dict:
    """Fetch the full content of a message by UID.

    Returns subject, from, to, cc, date, body_text, attachments list and the
    thread headers. UID and folder are required — use `message-list` or
    `message-search` to find UIDs.

    Parameters:
        - uid (int): Message UID.
        - folder (str): Folder of the message (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Full message:
            `mail-proxy do message-info '{"uid":312}'`
            → {"uid":312,"message_id":"<abc@webmail.example.com>","subject":"Re: TP","from":{"name":"Xavier","email":"x@y.fr"},"to":[{"name":"","email":"user@example.com"}],"cc":[],"date":"2026-08-12T09:00:00+00:00","flags":["\\Seen"],"folder":"INBOX","body_text":"Bonjour, …","attachments":[],"in_reply_to":"<def@webmail.example.com>","references":["<def@webmail.example.com>"]}
        - Message in Archive:
            `mail-proxy do message-info '{"uid":99,"folder":"Archive"}'`
            → {"uid":99,"message_id":"<ghi@webmail.polytechnique.fr>","subject":"Old mail","from":{"name":"","email":"noreply@x.fr"},"to":[],"cc":[],"date":"2026-07-01T10:00:00+00:00","flags":["\\Seen"],"folder":"Archive","body_text":"…","attachments":[{"filename":"report.pdf","content_type":"application/pdf","size_bytes":2048}],"in_reply_to":"","references":[]}
        - Unknown UID:
            `mail-proxy do message-info '{"uid":999999}'`
            → {"error":"Message UID 999999 not found in INBOX"}
    """
    msg = client.imap().fetch_message(p.uid, p.folder)
    if msg is None:
        return {"error": f"Message UID {p.uid} not found in {p.folder}"}

    return {
        "uid": msg.uid,
        "message_id": msg.message_id,
        "subject": msg.subject,
        "from": {"name": msg.sender.name, "email": msg.sender.email}
        if msg.sender
        else None,
        "to": [{"name": a.name, "email": a.email} for a in msg.recipients],
        "cc": [{"name": a.name, "email": a.email} for a in msg.cc],
        "date": msg.date.isoformat() if msg.date else "",
        "flags": msg.flags,
        "folder": msg.folder,
        "body_text": msg.body_text,
        "body_html": msg.body_html,
        "attachments": [
            {
                "filename": a.filename,
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
            }
            for a in msg.attachments
        ],
        "in_reply_to": msg.in_reply_to,
        "references": msg.references,
    }


def message_search(client: MailClient, p: MessageSearchPayload) -> list[dict]:
    """Search messages with flexible IMAP + client-side filters.

    IMAP-level (server-side, fast): `query` (subject OR body), `sender`,
    `subject_filter`, `to_filter`, `cc_filter`, `keyword`, `since`/`before`,
    `unseen_only`, `flagged_only`, `has_attachment`, `min_size`/`max_size`,
    single `folder` or multi `folders`.

    Client-side regex (applied after IMAP): `sender_pattern`,
    `subject_pattern`, `body_pattern` (expensive — fetches full bodies).

    Parameters:
        - query (str | None): Text match in subject OR body.
        - sender (str | None): FROM substring.
        - sender_pattern (str | None): Regex on From address.
        - subject_filter (str | None): SUBJECT substring.
        - subject_pattern (str | None): Regex on subject.
        - to_filter (str | None): TO substring.
        - cc_filter (str | None): CC substring.
        - body_pattern (str | None): Regex on body (expensive).
        - keyword (str | None): Custom IMAP keyword/label.
        - folder (str): Single folder (default INBOX).
        - folders (list[str] | None): Multi-folder search.
        - since (str | None): ISO date, e.g. "2026-08-01".
        - before (str | None): ISO date, e.g. "2026-08-31".
        - unseen_only (bool): Only unread.
        - flagged_only (bool): Only flagged.
        - has_attachment (bool): Only messages with attachments.
        - min_size (int | None): Min size in bytes.
        - max_size (int | None): Max size in bytes.
        - limit (int): Max rows (default 20).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Search a sender substring:
            `mail-proxy do message-search '{"sender":"@polytechnique.edu","limit":10}'`
            → [{"uid":312,"subject":"Re: TP","from":"x@polytechnique.edu","date":"2026-08-12T09:00:00+00:00","flags":["\\Seen"],"has_attachments":false,"folder":"INBOX"}]
        - Regex across two folders:
            `mail-proxy do message-search '{"sender_pattern":".*@company\\\\.com","folders":["INBOX","Archive"]}'`
            → [{"uid":99,"subject":"Contrat","from":"hr@company.com","date":"2026-07-01T10:00:00+00:00","flags":["\\Seen"],"has_attachments":true,"folder":"Archive"}]
        - Unread, with attachment, this month:
            `mail-proxy do message-search '{"since":"2026-08-01","unseen_only":true,"has_attachment":true}'`
            → [{"uid":311,"subject":"Facture","from":"billing@france.fr","date":"2026-08-11T14:00:00+00:00","flags":[],"has_attachments":true,"folder":"INBOX"}]
    """
    since_dt = datetime.fromisoformat(p.since) if p.since else None
    before_dt = datetime.fromisoformat(p.before) if p.before else None

    fetch_limit = (
        p.limit * 5
        if (p.sender_pattern or p.subject_pattern or p.body_pattern)
        else p.limit
    )

    imap = client.imap()
    search_folders = p.folders or [p.folder]
    all_summaries = []
    for f in search_folders:
        per_criteria = SearchCriteria(
            folder=f,
            query=p.query,
            sender=p.sender,
            subject_filter=p.subject_filter,
            to_filter=p.to_filter,
            cc_filter=p.cc_filter,
            since=since_dt,
            before=before_dt,
            unseen_only=p.unseen_only,
            flagged_only=p.flagged_only,
            has_attachment=p.has_attachment,
            min_size=p.min_size,
            max_size=p.max_size,
            keyword=p.keyword,
            limit=fetch_limit,
            account_id=client.account.id,
        )
        uids = imap.search(per_criteria)
        all_summaries.extend(imap.fetch_summaries(uids, f))

    all_summaries.sort(
        key=lambda m: m.date or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    summaries = all_summaries

    if p.sender_pattern:
        rx = re.compile(p.sender_pattern, re.IGNORECASE)
        summaries = [
            m
            for m in summaries
            if m.sender and rx.search(m.sender.email + " " + m.sender.name)
        ]
    if p.subject_pattern:
        rx = re.compile(p.subject_pattern, re.IGNORECASE)
        summaries = [m for m in summaries if rx.search(m.subject)]

    if p.body_pattern:
        rx = re.compile(p.body_pattern, re.IGNORECASE | re.DOTALL)
        folder_to_uids: dict[str, list[int]] = defaultdict(list)
        for m in summaries:
            folder_to_uids[m.folder].append(m.uid)
        matching: set[tuple[str, int]] = set()
        for f, uids in folder_to_uids.items():
            for uid, _from, _subj, body in imap.fetch_bodies_for_pattern(uids, f):
                if rx.search(body):
                    matching.add((f, uid))
        summaries = [m for m in summaries if (m.folder, m.uid) in matching]

    summaries = summaries[: p.limit]
    return _summaries_to_dicts(summaries)


def message_thread(client: MailClient, p: MessageThreadPayload) -> list[dict]:
    """Retrieve all messages of a thread by Message-ID.

    Returns messages ordered oldest-first (conversation view). Searches the
    folder's `Message-ID`, `In-Reply-To`, and `References` headers — finding
    both the owning message and every message that replies to it.

    Parameters:
        - message_id (str): Message-ID of the thread.
        - folder (str): Folder to search (default INBOX).
        - limit (int): Max messages (default 50).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Full thread:
            `mail-proxy do message-thread '{"message_id":"<abc@webmail.polytechnique.fr>"}'`
            → [{"uid":300,"subject":"TP","from":"x@y.fr","date":"2026-08-10T09:00:00+00:00","flags":["\\Seen"]},{"uid":312,"subject":"Re: TP","from":"x@y.fr","date":"2026-08-12T09:00:00+00:00","flags":["\\Seen"]}]
        - Single message (no replies):
            `mail-proxy do message-thread '{"message_id":"<solo@webmail.polytechnique.fr>"}'`
            → [{"uid":290,"subject":"Solo mail","from":"a@b.fr","date":"2026-08-05T11:00:00+00:00","flags":["\\Seen"]}]
        - Thread limited to 2 rows:
            `mail-proxy do message-thread '{"message_id":"<abc@webmail.polytechnique.fr>","limit":2}'`
            → [{"uid":300,"subject":"TP","from":"x@y.fr","date":"2026-08-10T09:00:00+00:00","flags":["\\Seen"]},{"uid":312,"subject":"Re: TP","from":"x@y.fr","date":"2026-08-12T09:00:00+00:00","flags":["\\Seen"]}]
    """
    depth_limit = min(p.limit, 50)
    imap = client.imap()
    uids = imap.search_thread(p.message_id, p.folder, depth_limit)
    summaries = imap.fetch_summaries(uids, p.folder)
    return [
        {
            "uid": m.uid,
            "subject": m.subject,
            "from": m.sender.email if m.sender else "",
            "date": m.date.isoformat() if m.date else "",
            "flags": m.flags,
        }
        for m in sorted(
            summaries, key=lambda m: m.date or datetime.min.replace(tzinfo=UTC)
        )
    ]


# ─── Flag / move writes ───────────────────────────────────────────────────────


@require_verification("uids", "flags")
def message_mark(
    client: MailClient, p: MessageMarkPayload
) -> tuple[dict, Verification]:
    """Add or remove standard IMAP flags on messages (read-back verified).

    Pass `seen:true` to mark read, `seen:false` to mark unread; same pattern
    for `flagged`, `answered` and `draft`. Only the provided flags are touched.

    Gmail caveat: `\\Flagged` is verified against Gmail IMAP via `UID FETCH
    FLAGS`, not against a loaded Gmail Web page. A Gmail Web row can visibly
    retain a yellow star after repeated refreshes while both `FETCH FLAGS` and
    `SEARCH FLAGGED` say the message is not starred. In that divergence,
    `verification.ok:true` proves the IMAP state only; do not claim it proves
    Gmail Web UI parity. A future Gmail API backend must verify the `STARRED`
    label when Web UI parity is required.

    Parameters:
        - uids (list[int]): Target UIDs.
        - folder (str): Folder of the messages (default INBOX).
        - seen (bool | None): True=read, False=unread.
        - flagged (bool | None): True=star, False=unstar.
        - answered (bool | None): True=answered, False=unanswered.
        - draft (bool | None): True=draft flag, False=remove draft flag.
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Mark two messages as read:
            `mail-proxy do message-mark '{"uids":[311,312],"seen":true}'`
            → {"modified":2,"folder":"INBOX","account":"poly","verification":{"method":"UID FETCH INBOX","checked":["flags","uids"],"expected":{"uids":[311,312],"flags":["\\Seen"]},"actual":{"uids":[311,312],"flags":["\\Seen"]},"ok":true}}
        - Star and unread:
            `mail-proxy do message-mark '{"uids":[310],"flagged":true,"seen":false}'`
            → {"modified":1,"folder":"INBOX","account":"poly","verification":{"method":"UID FETCH INBOX","checked":["flags","uids"],"expected":{"uids":[310],"flags":["\\Flagged"]},"actual":{"uids":[310],"flags":["\\Flagged"]},"ok":true}}
        - Unstar (remove flag):
            `mail-proxy do message-mark '{"uids":[310],"flagged":false}'`
            → {"modified":1,"folder":"INBOX","account":"poly","verification":{"method":"UID FETCH INBOX","checked":["flags","uids"],"expected":{"uids":[310],"flags":[]},"actual":{"uids":[310],"flags":[]},"ok":true}}
        - Mark as answered in Archive:
            `mail-proxy do message-mark '{"uids":[99],"answered":true,"folder":"Archive"}'`
            → {"modified":1,"folder":"Archive","account":"poly","verification":{"method":"UID FETCH Archive","checked":["flags","uids"],"expected":{"uids":[99],"flags":["\\Answered"]},"actual":{"uids":[99],"flags":["\\Answered"]},"ok":true}}
    """
    imap = client.imap()
    if p.seen is not None:
        imap.set_flags(p.uids, p.folder, ["\\Seen"], add=p.seen)
    if p.flagged is not None:
        imap.set_flags(p.uids, p.folder, ["\\Flagged"], add=p.flagged)
    if p.answered is not None:
        imap.set_flags(p.uids, p.folder, ["\\Answered"], add=p.answered)
    if p.draft is not None:
        imap.set_flags(p.uids, p.folder, ["\\Draft"], add=p.draft)

    flags_by_uid = imap.current_flags(p.uids, p.folder)
    flag_pairs = [
        (flag, setting)
        for flag, setting in (
            ("\\Seen", p.seen),
            ("\\Flagged", p.flagged),
            ("\\Answered", p.answered),
            ("\\Draft", p.draft),
        )
        if setting is not None
    ]
    # expected = desired END STATE: only flags we are ADDING (setting=True)
    expected_flags = [flag for flag, setting in flag_pairs if setting]
    # observed = flags that ARE PRESENT on ALL target UIDs (among touched flags)
    observed: list[str] = []
    for flag, _ in flag_pairs:
        states = [flag in flags for flags in flags_by_uid.values()]
        if all(states):
            observed.append(flag)
    observed_flags = sorted(observed)
    verification = compare(
        f"UID FETCH {p.folder}",
        {"uids": p.uids, "flags": expected_flags},
        {"uids": sorted(flags_by_uid), "flags": observed_flags},
    )
    data = {"modified": len(p.uids), "folder": p.folder, "account": client.account.id}
    return data, verification


@require_approval()
@require_verification("uids", "destination_folder")
def message_move(
    client: MailClient, p: MessageMovePayload
) -> tuple[dict, Verification]:
    """Move messages from one folder to another (HITL + read-back verified).

    Uses IMAP MOVE when the server supports it, otherwise COPY+DELETE.

    Parameters:
        - uids (list[int]): Target UIDs.
        - destination_folder (str): Destination folder.
        - source_folder (str): Source folder (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Move to Archive:
            `mail-proxy do message-move '{"uids":[311,312],"destination_folder":"Archive"}'`
            → {"moved":2,"from":"INBOX","to":"Archive","account":"poly","verification":{"method":"UID SEARCH INBOX+Archive","checked":["destination_folder","uids"],"expected":{"uids":[311,312],"destination_folder":"Archive"},"actual":{"uids":[311,312],"destination_folder":"Archive"},"ok":true}}
        - Move from Archive back to INBOX:
            `mail-proxy do message-move '{"uids":[99],"destination_folder":"INBOX","source_folder":"Archive"}'`
            → {"moved":1,"from":"Archive","to":"INBOX","account":"poly","verification":{"method":"UID SEARCH Archive+INBOX","checked":["destination_folder","uids"],"expected":{"uids":[99],"destination_folder":"INBOX"},"actual":{"uids":[99],"destination_folder":"INBOX"},"ok":true}}
        - Move to a work folder on another account:
            `mail-proxy do message-move '{"uids":[7],"destination_folder":"Projects","account_id":"work"}'`
            → {"moved":1,"from":"INBOX","to":"Projects","account":"work","verification":{"method":"UID SEARCH INBOX+Projects","checked":["destination_folder","uids"],"expected":{"uids":[7],"destination_folder":"Projects"},"actual":{"uids":[7],"destination_folder":"Projects"},"ok":true}}
    """
    imap = client.imap()
    imap.move_messages(
        p.uids, src_folder=p.source_folder, dst_folder=p.destination_folder
    )

    # Verification: the UIDs must be gone from the source and present in the destination.
    remaining = remaining_uids(client, p.uids, p.source_folder)
    moved_ok = not remaining
    verification = compare(
        f"UID SEARCH {p.source_folder} absence",
        {"uids": p.uids, "destination_folder": p.destination_folder},
        {
            "uids": p.uids,
            "destination_folder": p.destination_folder if moved_ok else None,
        },
    )
    data = {
        "moved": len(p.uids),
        "from": p.source_folder,
        "to": p.destination_folder,
        "account": client.account.id,
    }
    return data, verification


def _verified_simple_move(
    client: MailClient,
    payload: MessageMoveSimplePayload,
    candidates: list[str],
    result_key: str,
    method_label: str,
) -> tuple[dict, Verification]:
    """Shared implementation of archive/trash/spam moves.

    Args:
        client (MailClient): The mail client.
        payload (MessageMoveSimplePayload): The validated payload.
        candidates (list[str]): Target folder candidates.
        result_key (str): Response key, e.g. `archived`.
        method_label (str): Label used in the verification method.

    Returns:
        tuple[dict, Verification]: The response and the read-back proof.

    Examples:
        >>> _verified_simple_move(client, payload, ["Archive"], "archived", "archive")
        ({'archived': 2, …}, Verification(…))
    """
    imap = client.imap()
    target_folder = _resolve_folder(client, candidates)
    imap.move_messages(
        payload.uids, src_folder=payload.source_folder, dst_folder=target_folder
    )

    remaining = remaining_uids(client, payload.uids, payload.source_folder)
    ok = not remaining
    verification = compare(
        f"UID SEARCH {method_label} absence",
        {"uids": payload.uids, "folder": target_folder},
        {"uids": payload.uids, "folder": target_folder if ok else None},
    )
    data = {
        result_key: len(payload.uids),
        "folder": target_folder,
        "account": client.account.id,
    }
    return data, verification


@require_approval()
@require_verification("uids", "folder")
def message_archive(
    client: MailClient, p: MessageMoveSimplePayload
) -> tuple[dict, Verification]:
    """Archive messages — move them to the Archive folder (HITL + verified).

    Automatically detects the correct archive folder name (Archive, Archives,
    All Mail…).

    Parameters:
        - uids (list[int]): Target UIDs.
        - source_folder (str): Source folder (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Archive two messages:
            `mail-proxy do message-archive '{"uids":[311,312]}'`
            → {"archived":2,"folder":"Archive","account":"poly","verification":{"method":"UID SEARCH archive","checked":["folder","uids"],"expected":{"uids":[311,312],"folder":"Archive"},"actual":{"uids":[311,312],"folder":"Archive"},"ok":true}}
        - Archive from a sub-folder:
            `mail-proxy do message-archive '{"uids":[50],"source_folder":"Work"}'`
            → {"archived":1,"folder":"Archive","account":"poly","verification":{"method":"UID SEARCH archive","checked":["folder","uids"],"expected":{"uids":[50],"folder":"Archive"},"actual":{"uids":[50],"folder":"Archive"},"ok":true}}
        - Archive on Gmail-style servers (All Mail):
            `mail-proxy do message-archive '{"uids":[1]}'`
            → {"archived":1,"folder":"[Gmail]/All Mail","account":"poly","verification":{"method":"UID SEARCH archive","checked":["folder","uids"],"expected":{"uids":[1],"folder":"[Gmail]/All Mail"},"actual":{"uids":[1],"folder":"[Gmail]/All Mail"},"ok":true}}
    """
    return _verified_simple_move(client, p, _ARCHIVE_CANDIDATES, "archived", "archive")


@require_approval()
@require_verification("uids", "folder")
def message_trash(
    client: MailClient, p: MessageMoveSimplePayload
) -> tuple[dict, Verification]:
    """Move messages to Trash — the recoverable delete (HITL + verified).

    Prefer this over `message-delete` for safety.

    Parameters:
        - uids (list[int]): Target UIDs.
        - source_folder (str): Source folder (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Trash a message:
            `mail-proxy do message-trash '{"uids":[310]}'`
            → {"trashed":1,"folder":"Trash","account":"poly","verification":{"method":"UID SEARCH trash","checked":["folder","uids"],"expected":{"uids":[310],"folder":"Trash"},"actual":{"uids":[310],"folder":"Trash"},"ok":true}}
        - Trash several messages from Archive:
            `mail-proxy do message-trash '{"uids":[99,98],"source_folder":"Archive"}'`
            → {"trashed":2,"folder":"Trash","account":"poly","verification":{"method":"UID SEARCH trash","checked":["folder","uids"],"expected":{"uids":[99,98],"folder":"Trash"},"actual":{"uids":[99,98],"folder":"Trash"},"ok":true}}
        - Outlook-style trash (Deleted Items):
            `mail-proxy do message-trash '{"uids":[5]}'`
            → {"trashed":1,"folder":"Deleted Items","account":"poly","verification":{"method":"UID SEARCH trash","checked":["folder","uids"],"expected":{"uids":[5],"folder":"Deleted Items"},"actual":{"uids":[5],"folder":"Deleted Items"},"ok":true}}
    """
    return _verified_simple_move(client, p, _TRASH_CANDIDATES, "trashed", "trash")


@require_approval()
@require_verification("uids", "folder")
def message_spam(
    client: MailClient, p: MessageMoveSimplePayload
) -> tuple[dict, Verification]:
    """Report messages as spam — move them to the Spam/Junk folder (HITL + verified).

    Automatically detects the correct spam folder name (Spam, Junk,
    Junk E-mail, [Gmail]/Spam).

    Parameters:
        - uids (list[int]): Target UIDs.
        - source_folder (str): Source folder (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Report as spam:
            `mail-proxy do message-spam '{"uids":[42]}'`
            → {"reported_spam":1,"folder":"Spam","account":"poly","verification":{"method":"UID SEARCH spam","checked":["folder","uids"],"expected":{"uids":[42],"folder":"Spam"},"actual":{"uids":[42],"folder":"Spam"},"ok":true}}
        - Report from a sub-folder:
            `mail-proxy do message-spam '{"uids":[12],"source_folder":"Promotions"}'`
            → {"reported_spam":1,"folder":"Spam","account":"poly","verification":{"method":"UID SEARCH spam","checked":["folder","uids"],"expected":{"uids":[12],"folder":"Spam"},"actual":{"uids":[12],"folder":"Spam"},"ok":true}}
        - Gmail-style junk folder:
            `mail-proxy do message-spam '{"uids":[3]}'`
            → {"reported_spam":1,"folder":"[Gmail]/Spam","account":"poly","verification":{"method":"UID SEARCH spam","checked":["folder","uids"],"expected":{"uids":[3],"folder":"[Gmail]/Spam"},"actual":{"uids":[3],"folder":"[Gmail]/Spam"},"ok":true}}
    """
    return _verified_simple_move(client, p, _SPAM_CANDIDATES, "reported_spam", "spam")


# ─── Irreversible delete ──────────────────────────────────────────────────────


def _message_delete_preflight(client: MailClient, p: MessageDeletePayload) -> None:
    """Fail before HITL when any target UID is absent from the folder.

    Args:
        client (MailClient): The mail client.
        p (MessageDeletePayload): The validated payload.

    Returns:
        None

    Raises:
        MailProxyError: Naming the missing UIDs.

    Examples:
        >>> _message_delete_preflight(client, MessageDeletePayload(uids=[1, 2]))
        >>> _message_delete_preflight(client, MessageDeletePayload(uids=[999]))
        Traceback (most recent call last):
        ...
        mail_proxy.exceptions.MailProxyError: UIDs [999] do not exist in INBOX.
    """
    imap = client.imap()
    missing = [uid for uid in p.uids if not imap.message_exists(uid, p.folder)]
    if missing:
        raise MailProxyError(
            f"UIDs {missing} do not exist in {p.folder!r} — nothing to delete."
        )


@require_approval()
@require_preflight(check=_message_delete_preflight, identity_fields=("uids", "folder"))
@require_verification("deleted")
def message_delete(
    client: MailClient, p: MessageDeletePayload
) -> tuple[dict, Verification]:
    """PERMANENTLY delete messages — expunge immediately (HITL required).

    WARNING: irreversible. Use `message-trash` for a recoverable delete. The
    UIDs are pre-read (absent targets fail before HITL), locked in the review,
    and the deletion is confirmed by polling the folder until every UID is gone.

    Parameters:
        - uids (list[int]): Target UIDs.
        - folder (str): Folder of the messages (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Delete a message permanently:
            `mail-proxy do message-delete '{"uids":[42]}'`
            → {"deleted":1,"folder":"INBOX","account":"poly","verification":{"method":"UID SEARCH INBOX","checked":["deleted"],"expected":{"deleted":"42"},"actual":{"deleted":"42"},"ok":true}}
        - Delete several messages:
            `mail-proxy do message-delete '{"uids":[41,42,43]}'`
            → {"deleted":3,"folder":"INBOX","account":"poly","verification":{"method":"UID SEARCH INBOX","checked":["deleted"],"expected":{"deleted":"41,42,43"},"actual":{"deleted":"41,42,43"},"ok":true}}
        - Delete from another folder:
            `mail-proxy do message-delete '{"uids":[7],"folder":"Spam"}'`
            → {"deleted":1,"folder":"Spam","account":"poly","verification":{"method":"UID SEARCH Spam","checked":["deleted"],"expected":{"deleted":"7"},"actual":{"deleted":"7"},"ok":true}}
    """
    imap = client.imap()
    imap.delete_messages(p.uids, p.folder)

    def read() -> list[int]:
        return remaining_uids(client, p.uids, p.folder)

    verification = verify_absence(
        read,
        ",".join(str(u) for u in p.uids),
        f"UID SEARCH {p.folder}",
        timeout_seconds=10.0,
        interval_seconds=0.25,
    )
    data = {"deleted": len(p.uids), "folder": p.folder, "account": client.account.id}
    return data, verification


ACTIONS = [
    ActionDef("message-list", MessageListPayload, message_list, group="Messages"),
    ActionDef("message-info", MessageInfoPayload, message_info, group="Messages"),
    ActionDef("message-search", MessageSearchPayload, message_search, group="Messages"),
    ActionDef("message-thread", MessageThreadPayload, message_thread, group="Messages"),
    ActionDef("message-mark", MessageMarkPayload, message_mark, group="Messages"),
    action_def("message-move", MessageMovePayload, message_move, group="Messages"),
    action_def(
        "message-archive", MessageMoveSimplePayload, message_archive, group="Messages"
    ),
    action_def(
        "message-trash", MessageMoveSimplePayload, message_trash, group="Messages"
    ),
    action_def(
        "message-spam", MessageMoveSimplePayload, message_spam, group="Messages"
    ),
    action_def(
        "message-delete", MessageDeletePayload, message_delete, group="Messages"
    ),
]
