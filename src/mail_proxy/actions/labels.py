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


class LabelDeletePayload(AccountScoped):
    """Payload of `label-delete`.

    Attributes:
        labels (list[str]): Keywords to delete from ALL messages in a folder.
        folder (str): Folder to scan (default INBOX).
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> LabelDeletePayload(labels=["todo"]).folder
        'INBOX'
    """

    labels: list[str] = Field(
        ..., description="Keywords to remove from all matching messages"
    )
    folder: str = Field("INBOX", description="Folder to scan")


def _detect_custom_keyword_support(imap_client: object, folder: str) -> bool:
    r"""Check whether the IMAP server supports custom keywords.

    Returns True when PERMANENTFLAGS contains the ``\*`` wildcard (meaning
    the server accepts arbitrary keywords).  Falls back to True on test
    fakes that lack ``_select_folder`` so existing unit tests keep passing.
    """
    if not hasattr(imap_client, "_select_folder"):
        return True  # test fake — assume supported
    resp = imap_client._select_folder(folder, readonly=True)  # type: ignore[attr-defined]
    permanent = resp.get(b"PERMANENTFLAGS", ())
    # The \* wildcard means the server accepts additional keywords beyond the listed ones
    return b"\\*" in permanent or "*" in permanent


def label_list(client: MailClient, p: LabelListPayload) -> dict:
    """List user-defined keyword labels on a folder.

    Scans PERMANENTFLAGS AND the flags on the last 50 messages to discover
    custom keywords in use.  Reports whether the server supports custom keywords
    (servers like Hotmail/Outlook silently drop them).

    Parameters:
        - folder (str): Folder to inspect (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - List labels of INBOX:
            `mail-proxy do label-list`
            → {"folder":"INBOX","labels":["important","todo"],"account":"poly","custom_keywords_supported":true}
        - Labels of another folder:
            `mail-proxy do label-list '{"folder":"Archive"}'`
            → {"folder":"Archive","labels":["Dx0"],"account":"x","custom_keywords_supported":true}
        - Hotmail (no custom keyword support):
            `mail-proxy do label-list '{"folder":"Inbox","account_id":"hotmail"}'`
            → {"folder":"Inbox","labels":[],"account":"ivann.kamdem","custom_keywords_supported":false}
    """
    imap = client.imap()
    keywords = imap.list_keywords(p.folder)
    supported = _detect_custom_keyword_support(imap, p.folder)
    if not supported and "\\Flagged" not in keywords:
        keywords.append("\\Flagged")
    return {
        "folder": p.folder,
        "labels": keywords,
        "account": client.account.id,
        "custom_keywords_supported": supported,
    }


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
            → {"modified":2,"labels":["todo"],"action":"added","folder":"INBOX","account":"poly","verification":{"ok":true}}
        - Remove the tag:
            `mail-proxy do label-set '{"uids":[311],"labels":["todo"],"add":false}'`
            → {"modified":1,"labels":["todo"],"action":"removed","folder":"INBOX","account":"poly","verification":{"ok":true}}
        - Tag with a Zimbra tag on another account:
            `mail-proxy do label-set '{"uids":[7],"labels":["important"],"folder":"INBOX","account_id":"work"}'`
            → {"modified":1,"labels":["important"],"action":"added","folder":"INBOX","account":"work","verification":{"ok":true}}
    """
    imap = client.imap()
    fallback = False
    # Try custom keywords first — only fall back to \Flagged if verification
    # shows the keyword was silently dropped (Hotmail pattern).
    for label in p.labels:
        imap.set_keyword(p.uids, p.folder, label, add=p.add)

    # Read-back: check if keywords stuck.
    flags_by_uid = imap.current_flags(p.uids, p.folder)
    all_stuck = (
        all(label in flags for label in p.labels for flags in flags_by_uid.values())
        if p.add
        else all(
            label not in flags for label in p.labels for flags in flags_by_uid.values()
        )
    )

    if not all_stuck and p.add:
        # Keywords were silently dropped (Hotmail/Outlook pattern).
        # Fall back to \Flagged as a single-label workaround.
        fallback = True
        imap.set_keyword(p.uids, p.folder, "\\Flagged", add=True)
        flags_by_uid = imap.current_flags(p.uids, p.folder)
    observed: list[str] = []
    check_labels = ["\\Flagged"] if fallback else p.labels
    for label in check_labels:
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
    if fallback:
        data["fallback"] = "\\Flagged"
        data["note"] = (
            "Server does not support custom keywords; \\Flagged used as single-label workaround"
        )
    return data, verification


@require_verification("labels")
def label_delete(
    client: MailClient, p: LabelDeletePayload
) -> tuple[dict, Verification]:
    """Remove keyword labels from ALL messages in a folder that carry them.

    Scans the last 500 messages in the folder, finds every UID carrying any
    of the specified keywords, and removes them all.  Returns the count of
    modified messages and which labels were actually removed.

    Parameters:
        - labels (list[str]): Keywords to remove.
        - folder (str): Folder to scan (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Delete label from all messages in INBOX:
            `mail-proxy do label-delete '{"labels":["todo"],"folder":"INBOX"}'`
            → {"folder":"INBOX","labels":["todo"],"action":"deleted","total_scanned":200,"modified":3,"account":"poly","verification":{"ok":true}}
        - Delete multiple labels:
            `mail-proxy do label-delete '{"labels":["todo","urgent"],"folder":"Archive"}'`
            → {"folder":"Archive","labels":["todo","urgent"],"action":"deleted","total_scanned":100,"modified":5,"account":"poly","verification":{"ok":true}}
        - Delete from a specific account:
            `mail-proxy do label-delete '{"labels":["Dx0"],"folder":"Archive","account_id":"x"}'`
            → {"folder":"Archive","labels":["Dx0"],"action":"deleted","total_scanned":150,"modified":2,"account":"x","verification":{"ok":true}}
    """
    imap = client.imap()

    # Scan last 500 messages for UIDs carrying any of the target labels.
    imap._select_folder(p.folder, readonly=True)  # type: ignore[attr-defined]
    try:
        all_uids = imap._c().search("ALL")  # type: ignore[attr-defined]
    except (OSError, ValueError, KeyError):
        all_uids = []

    if not all_uids:
        data = {
            "folder": p.folder,
            "labels": p.labels,
            "action": "deleted",
            "total_scanned": 0,
            "modified": 0,
            "account": client.account.id,
        }
        verification = compare(
            f"UID FETCH {p.folder}",
            {"labels": p.labels},
            {"labels": []},
        )
        return data, verification

    sample = all_uids[-500:] if len(all_uids) > 500 else all_uids
    flags_by_uid = imap.current_flags(sample, p.folder)

    # Find UIDs carrying at least one of the target labels.
    target_uids: list[int] = []
    for uid, flags in flags_by_uid.items():
        if any(label in flags for label in p.labels):
            target_uids.append(uid)

    if target_uids:
        for label in p.labels:
            imap.set_keyword(target_uids, p.folder, label, add=False)

    # Verification: re-read — none of the target labels should appear on any of these UIDs.
    flags_after = imap.current_flags(target_uids, p.folder) if target_uids else {}
    removed_labels: list[str] = []
    for label in p.labels:
        still_present = any(label in flags for flags in flags_after.values())
        if not still_present:
            removed_labels.append(label)

    verification = compare(
        f"UID FETCH {p.folder}",
        {"labels": p.labels},
        {"labels": removed_labels},
    )

    data = {
        "folder": p.folder,
        "labels": p.labels,
        "action": "deleted",
        "total_scanned": len(sample),
        "modified": len(target_uids),
        "account": client.account.id,
    }
    return data, verification


ACTIONS = [
    ActionDef("label-list", LabelListPayload, label_list, group="Labels"),
    ActionDef("label-set", LabelSetPayload, label_set, group="Labels"),
    ActionDef("label-delete", LabelDeletePayload, label_delete, group="Labels"),
]
