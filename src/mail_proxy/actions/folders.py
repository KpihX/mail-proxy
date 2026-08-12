"""Folder actions — list, create, rename, delete (delete = HITL + preflight)."""

from typing import Any

from pydantic import Field

from ..client import MailClient
from ..exceptions import MailProxyError
from ..models import Verification
from .base import (
    AccountScoped,
    ActionDef,
    action_def,
    require_approval,
    require_preflight,
    require_verification,
    verify_absence,
)


class FolderListPayload(AccountScoped):
    """Payload of `folder-list`.

    Attributes:
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> FolderListPayload().account_id is None
        True
    """


class FolderCreatePayload(AccountScoped):
    """Payload of `folder-create`.

    Attributes:
        name (str): Folder name — '/' separates sub-folders.
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> FolderCreatePayload(name="Work/Project-X").name
        'Work/Project-X'
    """

    name: str = Field(..., description="Folder name ('/' = sub-folder)")


class FolderRenamePayload(AccountScoped):
    """Payload of `folder-rename`.

    Attributes:
        old_name (str): Current folder name.
        new_name (str): New folder name.
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> FolderRenamePayload(old_name="Old", new_name="New").new_name
        'New'
    """

    old_name: str = Field(..., description="Current folder name")
    new_name: str = Field(..., description="New folder name")


class FolderDeletePayload(AccountScoped):
    """Payload of `folder-delete`.

    Attributes:
        name (str): Folder to delete.
        account_id (str | None): Account id (omit → default).

    Examples:
        >>> FolderDeletePayload(name="Work/Project-X").name
        'Work/Project-X'
    """

    name: str = Field(..., description="Folder to delete")


def folder_list(client: MailClient, p: FolderListPayload) -> list[dict]:
    """List every IMAP folder of the account.

    Returns folder name, delimiter and attributes — discover folder names
    before any move/archive/trash operation.

    Parameters:
        - account_id (str | None): Account id (omit → default).

    Examples:
        - All folders:
            `mail-proxy do folder-list`
            → [{"name":"INBOX","delimiter":"/","attributes":["\\HasNoChildren"],"selectable":true},{"name":"Sent","delimiter":"/","attributes":[],"selectable":true}]
        - Table view:
            `mail-proxy do folder-list -f table`
            → [{"name":"INBOX","delimiter":"/","selectable":true}]
        - Another account:
            `mail-proxy do folder-list '{"account_id":"work"}'`
            → [{"name":"INBOX","delimiter":"/","attributes":["\\HasNoChildren"],"selectable":true},{"name":"Projects","delimiter":"/","attributes":[],"selectable":true}]
    """
    return [
        {
            "name": f.name,
            "delimiter": f.delimiter,
            "attributes": f.attributes,
            "selectable": f.is_selectable,
        }
        for f in client.imap().list_folders()
    ]


def folder_create(client: MailClient, p: FolderCreatePayload) -> dict:
    """Create a new IMAP folder.

    Parameters:
        - name (str): Folder name — '/' separates sub-folders.
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Simple folder:
            `mail-proxy do folder-create '{"name":"Projects"}'`
            → {"created":true,"name":"Projects","account":"poly"}
        - Sub-folder:
            `mail-proxy do folder-create '{"name":"Work/Project-X"}'`
            → {"created":true,"name":"Work/Project-X","account":"poly"}
        - Create on another account:
            `mail-proxy do folder-create '{"name":"2026","account_id":"work"}'`
            → {"created":true,"name":"2026","account":"work"}
    """
    client.imap().create_folder(p.name)
    return {"created": True, "name": p.name, "account": client.account.id}


def folder_rename(client: MailClient, p: FolderRenamePayload) -> dict:
    """Rename an IMAP folder.

    Parameters:
        - old_name (str): Current folder name.
        - new_name (str): New folder name.
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Simple rename:
            `mail-proxy do folder-rename '{"old_name":"Old","new_name":"New"}'`
            → {"renamed":true,"from":"Old","to":"New","account":"poly"}
        - Move a folder into a hierarchy:
            `mail-proxy do folder-rename '{"old_name":"Projects","new_name":"Work/Projects"}'`
            → {"renamed":true,"from":"Projects","to":"Work/Projects","account":"poly"}
        - Rename on another account:
            `mail-proxy do folder-rename '{"old_name":"2026","new_name":"2027","account_id":"work"}'`
            → {"renamed":true,"from":"2026","to":"2027","account":"work"}
    """
    client.imap().rename_folder(p.old_name, p.new_name)
    return {
        "renamed": True,
        "from": p.old_name,
        "to": p.new_name,
        "account": client.account.id,
    }


def _folder_delete_preflight(client: MailClient, p: FolderDeletePayload) -> None:
    """Fail before HITL when the target folder does not exist.

    Args:
        client (MailClient): The mail client.
        p (FolderDeletePayload): The validated payload.

    Returns:
        None

    Raises:
        MailProxyError: When the folder is absent from the server.

    Examples:
        >>> _folder_delete_preflight(client, FolderDeletePayload(name="INBOX"))
        >>> _folder_delete_preflight(client, FolderDeletePayload(name="Nope"))
        Traceback (most recent call last):
        ...
        mail_proxy.exceptions.MailProxyError: Folder 'Nope' does not exist.
    """
    if not client.imap().folder_exists(p.name):
        raise MailProxyError(f"Folder {p.name!r} does not exist — nothing to delete.")


@require_approval()
@require_preflight(check=_folder_delete_preflight, identity_fields=("name",))
@require_verification("deleted")
def folder_delete(
    client: MailClient, p: FolderDeletePayload
) -> tuple[dict, Verification]:
    """Delete an IMAP folder (HITL required, preflighted and verified).

    WARNING: some servers require the folder to be empty first — clear it with
    `message-move` or `message-delete` beforehand. The folder must exist, the
    review locks its name, and the deletion is confirmed by re-reading the
    folder list until the name is absent.

    Parameters:
        - name (str): Folder to delete.
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Delete an empty folder:
            `mail-proxy do folder-delete '{"name":"Work/Project-X"}'`
            → {"deleted":true,"name":"Work/Project-X","account":"poly","verification":{"method":"LIST Work/Project-X","checked":["deleted"],"expected":{"deleted":"Work/Project-X"},"actual":{"deleted":"Work/Project-X"},"ok":true}}
        - Delete on another account:
            `mail-proxy do folder-delete '{"name":"2026","account_id":"work"}'`
            → {"deleted":true,"name":"2026","account":"work","verification":{"method":"LIST 2026","checked":["deleted"],"expected":{"deleted":"2026"},"actual":{"deleted":"2026"},"ok":true}}
        - Delete a non-existent folder fails before HITL:
            `mail-proxy do folder-delete '{"name":"Nope"}'`
            → (error envelope, exit 1 — no review page opens)
    """
    imap = client.imap()
    imap.delete_folder(p.name)

    def read() -> Any:
        return [f.name for f in imap.list_folders() if f.name == p.name]

    verification = verify_absence(
        read, p.name, f"LIST {p.name}", timeout_seconds=10.0, interval_seconds=0.25
    )
    data = {"deleted": True, "name": p.name, "account": client.account.id}
    return data, verification


ACTIONS = [
    ActionDef("folder-list", FolderListPayload, folder_list, group="Folders"),
    ActionDef("folder-create", FolderCreatePayload, folder_create, group="Folders"),
    ActionDef("folder-rename", FolderRenamePayload, folder_rename, group="Folders"),
    action_def("folder-delete", FolderDeletePayload, folder_delete, group="Folders"),
]
