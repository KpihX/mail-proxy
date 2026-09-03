"""Inbox actions — quick check and the structured daily digest."""

from datetime import UTC, datetime

from pydantic import Field

from ..api.models import SearchCriteria
from ..client import MailClient
from .base import AccountScoped, ActionDef


class InboxCheckPayload(AccountScoped):
    """Payload of `inbox-check` — unread count + last N summaries.

    Attributes:
        limit (int): Max summaries to return (default 10).
        account_id (str): Account id (required).

    Examples:
        >>> InboxCheckPayload().limit
        10
        >>> InboxCheckPayload(limit=5).limit
        5
    """

    limit: int = Field(10, description="Max summaries to return")


class InboxDigestPayload(AccountScoped):
    """Payload of `inbox-digest` — structured daily overview.

    Attributes:
        account_id (str): Account id (required).

    Examples:
        >>> InboxDigestPayload().account_id is None
        True
    """


def inbox_check(client: MailClient, p: InboxCheckPayload) -> dict:
    """Quick inbox check: unread count + last N message summaries.

    Entry point for any mail-related session. Returns a compact dict with
    `unread_count`, `total_count` and the `messages` list.

    Parameters:
        - limit (int): Max summaries to return (default 10).
        - account_id (str): Account id (required).

    Examples:
        - Default check:
            `mail-proxy do inbox-check`
            → {"account":"poly","unread_count":14,"total_count":312,"messages":[{"uid":312,"subject":"Re: TP","from":"x@y.fr","date":"2026-08-12T09:00:00+00:00","flags":["\\Seen"]}]}
        - Limit to 3 rows:
            `mail-proxy do inbox-check '{"limit":3}'`
            → {"account":"poly","unread_count":14,"total_count":312,"messages":[{"uid":312},{"uid":311},{"uid":310}]}
        - Another account:
            `mail-proxy do inbox-check '{"account_id":"work"}'`
            → {"account":"work","unread_count":2,"total_count":98,"messages":[{"uid":7,"subject":"Meeting","from":"boss@corp.fr","date":"2026-08-12T07:30:00+00:00","flags":[]}]}
    """
    imap = client.imap()
    status = imap.get_folder_status("INBOX")
    criteria = SearchCriteria(folder="INBOX", unseen_only=True, limit=p.limit)
    uids = imap.search(criteria)
    summaries = imap.fetch_summaries(uids, "INBOX")

    return {
        "account": client.account.id,
        "unread_count": status.unseen_count or 0,
        "total_count": status.message_count or 0,
        "messages": [
            {
                "uid": m.uid,
                "subject": m.subject,
                "from": m.sender.email if m.sender else "",
                "date": m.date.isoformat() if m.date else "",
                "flags": m.flags,
            }
            for m in summaries
        ],
    }


def inbox_digest(client: MailClient, p: InboxDigestPayload) -> dict:
    """Structured daily overview: unread, flagged, and today's messages.

    Ideal as the first action at the start of a session — one call covers the
    whole inbox picture for the current day.

    Parameters:
        - account_id (str): Account id (required).

    Examples:
        - Morning digest:
            `mail-proxy do inbox-digest`
            → {"account":"poly","date":"2026-08-12","inbox":{"total":312,"unread":14},"unread_messages":[{"uid":312,"subject":"Re: TP"}],"flagged_messages":[],"received_today":[{"uid":312}]}
        - Table view:
            `mail-proxy do inbox-digest -f table`
            → {"account":"poly","date":"2026-08-12","inbox":{"total":312,"unread":14}}
        - Quiet day, flagged item present:
            `mail-proxy do inbox-digest`
            → {"account":"poly","date":"2026-08-12","inbox":{"total":312,"unread":0},"unread_messages":[],"flagged_messages":[{"uid":300,"subject":"À traiter"}],"received_today":[]}
    """
    imap = client.imap()
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    inbox_status = imap.get_folder_status("INBOX")

    unread_uids = imap.search(
        SearchCriteria(folder="INBOX", unseen_only=True, limit=20)
    )
    unread = imap.fetch_summaries(unread_uids, "INBOX")
    flagged_uids = imap.search(
        SearchCriteria(folder="INBOX", flagged_only=True, limit=10)
    )
    flagged = imap.fetch_summaries(flagged_uids, "INBOX")
    today_uids = imap.search(
        SearchCriteria(folder="INBOX", since=today_start, limit=20)
    )
    today_msgs = imap.fetch_summaries(today_uids, "INBOX")

    def _fmt(summaries: list) -> list[dict]:
        return [
            {
                "uid": m.uid,
                "subject": m.subject,
                "from": m.sender.email if m.sender else "",
                "date": m.date.isoformat() if m.date else "",
            }
            for m in summaries
        ]

    return {
        "account": client.account.id,
        "date": today_start.date().isoformat(),
        "inbox": {
            "total": inbox_status.message_count or 0,
            "unread": inbox_status.unseen_count or 0,
        },
        "unread_messages": _fmt(unread),
        "flagged_messages": _fmt(flagged),
        "received_today": _fmt(today_msgs),
    }


ACTIONS = [
    ActionDef("inbox-check", InboxCheckPayload, inbox_check, group="Inbox"),
    ActionDef("inbox-digest", InboxDigestPayload, inbox_digest, group="Inbox"),
]
