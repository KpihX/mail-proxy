"""Label actions — IMAP custom keywords (user-defined labels / Zimbra tags)."""

from pydantic import Field

from ..client import MailClient
from ..models import Verification
from .base import AccountScoped, ActionDef, compare, require_verification


class LabelListPayload(AccountScoped):
    """Payload of `label-list`.

    Attributes:
        folder (str): Folder to inspect (default INBOX).
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> LabelListPayload().folder
        'INBOX'
    """

    folder: str = Field("INBOX", description="Folder to inspect")


class LabelSetPayload(AccountScoped):
    """Payload of `label-set`.

    Attributes:
        uids (list[int]): Target UIDs.
        labels (list[str]): Keywords to add/remove, e.g. `["todo"]`.
        add (bool): True = add the labels, False = remove them.
        folder (str): Folder of the messages (default INBOX).
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> LabelSetPayload(uids=[1], labels=["todo"]).add
        True
    """

    uids: list[int] = Field(..., description="Target UIDs")
    labels: list[str] = Field(..., description="Keywords to add or remove")
    add: bool = Field(True, description="True=add, False=remove")
    folder: str = Field("INBOX", description="Folder of the messages")


def label_list(client: MailClient, p: LabelListPayload) -> dict:
    """List user-defined keyword labels available on a folder (PERMANENTFLAGS).

    Standard system flags (\\Seen, \\Flagged, \\Answered, \\Draft, \\Deleted)
    are excluded — only custom/user labels are returned. On Zimbra, tags appear
    here as IMAP keywords.

    Parameters:
        - folder (str): Folder to inspect (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - List labels of INBOX:
            `mail-proxy do label-list`
            → {"folder":"INBOX","labels":["important","todo"],"account":"poly"}
        - Labels of another folder:
            `mail-proxy do label-list '{"folder":"Archive"}'`
            → {"folder":"Archive","labels":[],"account":"poly"}
        - Table view:
            `mail-proxy do label-list -f table`
            → {"folder":"INBOX","labels":["important","todo"],"account":"poly"}
    """
    keywords = client.imap().list_keywords(p.folder)
    return {"folder": p.folder, "labels": keywords, "account": client.account.id}


@require_verification("uids", "labels")
def label_set(client: MailClient, p: LabelSetPayload) -> tuple[dict, Verification]:
    """Add or remove custom keyword labels on messages.

    `labels` is a list of IMAP keyword strings — user-defined strings like
    `todo`, or Zimbra tags as returned by `label-list`. Set `add:false` to
    remove the labels instead of adding them.

    Parameters:
        - uids (list[int]): Target UIDs.
        - labels (list[str]): Keywords to add or remove.
        - add (bool): True=add, False=remove (default True).
        - folder (str): Folder of the messages (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Tag two messages:
            `mail-proxy do label-set '{"uids":[311,312],"labels":["todo"]}'`
            → {"modified":2,"labels":["todo"],"action":"added","folder":"INBOX","account":"poly","verification":{"method":"UID FETCH INBOX","checked":["uids","labels"],"expected":{"uids":[311,312],"labels":["todo"]},"actual":{"uids":[311,312],"labels":["todo"]},"ok":true}}
        - Remove the tag:
            `mail-proxy do label-set '{"uids":[311],"labels":["todo"],"add":false}'`
            → {"modified":1,"labels":["todo"],"action":"removed","folder":"INBOX","account":"poly","verification":{"method":"UID FETCH INBOX","checked":["uids","labels"],"expected":{"uids":[311],"labels":["todo"]},"actual":{"uids":[311],"labels":[]},"ok":true}}
        - Tag with a Zimbra tag on another account:
            `mail-proxy do label-set '{"uids":[7],"labels":["important"],"folder":"INBOX","account_id":"work"}'`
            → {"modified":1,"labels":["important"],"action":"added","folder":"INBOX","account":"work","verification":{"method":"UID FETCH INBOX","checked":["uids","labels"],"expected":{"uids":[7],"labels":["important"]},"actual":{"uids":[7],"labels":["important"]},"ok":true}}
    """
    imap = client.imap()
    for label in p.labels:
        imap.set_keyword(p.uids, p.folder, label, add=p.add)

    # Read-back: every target UID must carry (or lack) every label — a silent
    # partial failure (one UID missing the keyword) fails the verification.
    flags_by_uid = imap.current_flags(p.uids, p.folder)
    observed: list[str] = []
    for label in p.labels:
        states = [label in flags for flags in flags_by_uid.values()]
        fully_applied = all(states) if p.add else not any(states)
        if fully_applied:
            observed.append(label)
    actual_labels = sorted(observed)
    verification = compare(
        f"UID FETCH {p.folder}",
        {"uids": p.uids, "labels": p.labels},
        {"uids": sorted(flags_by_uid), "labels": actual_labels},
    )
    data = {
        "modified": len(p.uids),
        "labels": p.labels,
        "action": "added" if p.add else "removed",
        "folder": p.folder,
        "account": client.account.id,
    }
    return data, verification


ACTIONS = [
    ActionDef("label-list", LabelListPayload, label_list, group="Labels"),
    ActionDef("label-set", LabelSetPayload, label_set, group="Labels"),
]
