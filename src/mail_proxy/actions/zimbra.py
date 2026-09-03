"""First-class Zimbra tag actions, backed by Zimbra SOAP."""

from typing import Any

from pydantic import BaseModel, Field

from ..api.zimbra import ZimbraSOAPClient
from ..client import MailClient
from ..exceptions import MailProxyError
from .base import (
    AccountScoped,
    Verification,
    action_def,
    compare,
    require_approval,
    require_preflight,
    require_verification,
)


def _soap(client: MailClient) -> ZimbraSOAPClient:
    client.imap()  # resolve keyring-backed credentials first
    return ZimbraSOAPClient(client.account)


class TagSpec(BaseModel):
    """One tag to create: name (required) and optional Zimbra color index (0–25)."""

    model_config = {"extra": "forbid"}
    name: str = Field(..., min_length=1)
    color: int | None = Field(None, ge=0, le=25)


class ZimbraTagCreatePayload(AccountScoped):
    tags: list[TagSpec] = Field(..., min_length=1)


class ZimbraTagDeletePayload(AccountScoped):
    tag_ids: list[str] = Field(..., min_length=1)


class ZimbraTagItemsPayload(AccountScoped):
    tag_ids: list[str] = Field(..., min_length=1)
    item_ids: list[str] = Field(..., min_length=1)


class ZimbraTagItemsListPayload(AccountScoped):
    tag_ids: list[str] = Field(..., min_length=1)


def zimbra_tag_list(client: MailClient, p: AccountScoped) -> dict[str, Any]:
    """List native Zimbra tags with IDs, names, colors, and counters.

    Parameters:
        - account_id (str | None): Zimbra account (omit → default).
    Examples:
        - `mail-proxy do zimbra-tag-list '{"account_id":"poly"}'`
          → {"tags":[{"id":"12","name":"Important"}],"account":"poly"}
        - `mail-proxy do zimbra-tag-list` → {"tags":[],"account":"poly"}
        - `mail-proxy do zimbra-tag-list -f table` → {"tags":[],"account":"poly"}
    """
    return {"tags": _soap(client).tags(), "account": client.account.id}


def _resolve_tags(
    soap: ZimbraSOAPClient, tag_ids: list[str]
) -> dict[str, dict[str, str]]:
    tags = {tag["id"]: tag for tag in soap.tags() if "id" in tag}
    missing = [tag_id for tag_id in tag_ids if tag_id not in tags]
    if missing:
        raise MailProxyError(f"Zimbra tags do not exist: {', '.join(missing)}")
    return {tag_id: tags[tag_id] for tag_id in tag_ids}


def zimbra_tag_items(
    client: MailClient, p: ZimbraTagItemsListPayload
) -> dict[str, Any]:
    """List messages associated with one or more native Zimbra tags.

    Parameters:
        - tag_ids (list[str]): Native Zimbra tag IDs.
    Examples:
        - `mail-proxy do zimbra-tag-items '{"tag_ids":["3583"]}'` → {"items":[]}
        - `mail-proxy do zimbra-tag-items '{"tag_ids":["3583","7957"]}'` → {"items":[]}
        - `mail-proxy do zimbra-tag-items '{"tag_ids":["3583"],"account_id":"poly"}'` → {"items":[]}
    """
    soap = _soap(client)
    tags = _resolve_tags(soap, p.tag_ids)
    return {
        "tags": list(tags.values()),
        "items": {
            tag_id: soap.tagged_items(tag["name"]) for tag_id, tag in tags.items()
        },
        "account": client.account.id,
    }


@require_verification("names")
def zimbra_tag_create(
    client: MailClient, p: ZimbraTagCreatePayload
) -> tuple[dict, Verification]:
    """Create one or more native Zimbra tags with optional per-tag colour.

    Parameters:
        - tags (list[dict]): Each `{name, color?}` creates one tag.  `color`
          is a Zimbra palette index (0–25), omit for the default colour.
    Examples:
        - `mail-proxy do zimbra-tag-create '{"tags":[{"name":"Course"}]}'`
          → {"created":[{"id":"999","name":"Course"}],"account":"poly"}
        - `mail-proxy do zimbra-tag-create '{"tags":[{"name":"A","color":6},{"name":"B","color":1}]}'`
          → {"created":[{"id":"100","name":"A","color":"6"},{"id":"101","name":"B","color":"1"}],"account":"poly"}
        - `mail-proxy do zimbra-tag-create '{"tags":[{"name":"Research","color":4}],"account_id":"poly"}'`
          → {"created":[{"id":"102","name":"Research","color":"4"}],"account":"poly"}
    """
    soap = _soap(client)
    created = [soap.create_tag(tag.name, tag.color) for tag in p.tags]
    names = [tag.get("name", "") for tag in soap.tags()]
    requested_names = [tag.name for tag in p.tags]
    verification = compare(
        "GetTagRequest",
        {"names": sorted(requested_names)},
        {"names": sorted(name for name in names if name in requested_names)},
    )
    return {"created": created, "account": client.account.id}, verification


def _tag_delete_preflight(client: MailClient, p: ZimbraTagDeletePayload) -> None:
    _resolve_tags(_soap(client), p.tag_ids)


@require_approval()
@require_preflight(check=_tag_delete_preflight, identity_fields=("tag_ids",))
@require_verification("deleted")
def zimbra_tag_delete(
    client: MailClient, p: ZimbraTagDeletePayload
) -> tuple[dict, Verification]:
    """Delete one or more native Zimbra tags (HITL required).

    IMPORTANT: Deleting a tag removes it from the catalogue but leaves orphan
    IMAP keyword flags on messages that carried it. For a clean deletion,
    **always call `zimbra-tag-remove` on every associated item first**, then
    delete the tag. Items still carrying the flag are not automatically cleaned
    up by Zimbra.

    Parameters:
        - tag_ids (list[str]): Native Zimbra tag IDs.
    Examples:
        - `mail-proxy do zimbra-tag-delete '{"tag_ids":["12"]}'` → {"deleted":["12"]}
        - `mail-proxy do zimbra-tag-delete '{"tag_ids":["12","13"]}'` → {"deleted":["12","13"]}
        - `mail-proxy do zimbra-tag-delete '{"tag_ids":["12"],"account_id":"poly"}'` → {"deleted":["12"]}
    """
    soap = _soap(client)
    _resolve_tags(soap, p.tag_ids)
    soap.delete_tags(p.tag_ids)
    remaining = {tag.get("id") for tag in soap.tags()}
    verification = compare(
        "GetTagRequest",
        {"deleted": sorted(p.tag_ids)},
        {"deleted": sorted(tag_id for tag_id in p.tag_ids if tag_id not in remaining)},
    )
    return {"deleted": p.tag_ids, "account": client.account.id}, verification


def _tag_items_preflight(client: MailClient, p: ZimbraTagItemsPayload) -> None:
    soap = _soap(client)
    _resolve_tags(soap, p.tag_ids)
    soap.items(p.item_ids)


def _tag_items(
    client: MailClient, p: ZimbraTagItemsPayload, add: bool
) -> tuple[dict, Verification]:
    soap = _soap(client)
    _resolve_tags(soap, p.tag_ids)
    soap.items(p.item_ids)
    soap.tag_items(p.tag_ids, p.item_ids, add)
    resolved = soap.items(p.item_ids)
    tag_ids_in_items: list[str] = []
    for item in resolved:
        current = [t for t in item.get("t", "").split(",") if t]
        for tag_id in p.tag_ids:
            if add and tag_id in current and tag_id not in tag_ids_in_items:
                tag_ids_in_items.append(tag_id)
            if not add and tag_id not in current and tag_id not in tag_ids_in_items:
                tag_ids_in_items.append(tag_id)
    verification = compare(
        "ItemActionRequest",
        {"tag_ids": sorted(p.tag_ids), "item_ids": sorted(p.item_ids)},
        {"tag_ids": sorted(tag_ids_in_items), "item_ids": sorted(p.item_ids)},
    )
    return {
        "tag_ids": p.tag_ids,
        "item_ids": p.item_ids,
        "action": "applied" if add else "removed",
        "account": client.account.id,
    }, verification


@require_approval()
@require_preflight(check=_tag_items_preflight, identity_fields=("tag_ids", "item_ids"))
@require_verification("tag_ids", "item_ids")
def zimbra_tag_apply(
    client: MailClient, p: ZimbraTagItemsPayload
) -> tuple[dict, Verification]:
    """Apply one or more native Zimbra tags to one or more item IDs (HITL required).

    Parameters:
        - tag_ids (list[str]): Tag IDs.
        - item_ids (list[str]): Message/item IDs.
    Examples:
        - `mail-proxy do zimbra-tag-apply '{"tag_ids":["12"],"item_ids":["101"]}'` → {"action":"applied"}
        - `mail-proxy do zimbra-tag-apply '{"tag_ids":["12","13"],"item_ids":["101","102"]}'` → {"action":"applied"}
        - `mail-proxy do zimbra-tag-apply '{"tag_ids":["12"],"item_ids":["101"],"account_id":"poly"}'` → {"action":"applied"}
    """
    return _tag_items(client, p, True)


@require_approval()
@require_preflight(check=_tag_items_preflight, identity_fields=("tag_ids", "item_ids"))
@require_verification("tag_ids", "item_ids")
def zimbra_tag_remove(
    client: MailClient, p: ZimbraTagItemsPayload
) -> tuple[dict, Verification]:
    """Remove one or more native Zimbra tags from one or more item IDs (HITL required).

    Parameters:
        - tag_ids (list[str]): Tag IDs.
        - item_ids (list[str]): Message/item IDs.
    Examples:
        - `mail-proxy do zimbra-tag-remove '{"tag_ids":["12"],"item_ids":["101"]}'` → {"action":"removed"}
        - `mail-proxy do zimbra-tag-remove '{"tag_ids":["12","13"],"item_ids":["101","102"]}'` → {"action":"removed"}
        - `mail-proxy do zimbra-tag-remove '{"tag_ids":["12"],"item_ids":["101"],"account_id":"poly"}'` → {"action":"removed"}
    """
    return _tag_items(client, p, False)


ACTIONS = [
    action_def("zimbra-tag-list", AccountScoped, zimbra_tag_list, group="Zimbra"),
    action_def(
        "zimbra-tag-items", ZimbraTagItemsListPayload, zimbra_tag_items, group="Zimbra"
    ),
    action_def(
        "zimbra-tag-create", ZimbraTagCreatePayload, zimbra_tag_create, group="Zimbra"
    ),
    action_def(
        "zimbra-tag-delete", ZimbraTagDeletePayload, zimbra_tag_delete, group="Zimbra"
    ),
    action_def(
        "zimbra-tag-apply", ZimbraTagItemsPayload, zimbra_tag_apply, group="Zimbra"
    ),
    action_def(
        "zimbra-tag-remove", ZimbraTagItemsPayload, zimbra_tag_remove, group="Zimbra"
    ),
]
