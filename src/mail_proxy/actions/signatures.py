"""Signature actions — manage email signatures per account.

Each account holds a list of named signatures (text + optional image). One
signature is marked as the default. Images are stored in
``~/.config/mail-proxy/assets/signatures/`` and deduplicated by SHA256.
"""

import base64
import uuid
from pathlib import Path

from pydantic import Field

from ..client import MailClient
from ..config import (
    SIGNATURES_DIR,
    SignatureDef,
    copy_signature_image,
    get_signatures_dir,
    load_accounts,
    write_accounts_json,
)
from ..exceptions import MailProxyError
from .base import AccountScoped, ActionDef


def _match_account(account_id: str, acc: object) -> bool:
    """Check if *account_id* matches an account entry by id or alias.

    The JSON account list stores a flat ``id`` and an optional ``aliases``
    list.  Direct ``acc.id == account_id`` comparison ignores aliases and
    causes inconsistencies with the ``MailClient`` / ``get_account()``
    resolution path used by all other actions.

    ``acc`` may be an ``AccountDef`` model or a plain dict depending on the
    call site.

    Examples:
        >>> _match_account("x", {"id": "ivann.kamdem-pouokam", "aliases": ["x"]})
        True
        >>> _match_account("z", {"id": "poly", "aliases": ["poly"]})
        False
    """
    if getattr(acc, "id", None) == account_id:
        return True
    return account_id in getattr(acc, "aliases", [])


# ── Payloads ─────────────────────────────────────────────────────────────────


class SignatureListPayload(AccountScoped):
    """Payload of `signature-list`.

    Attributes:
        account_id (str): Account id (required).

    Examples:
        >>> SignatureListPayload().account_id is None
        True
        >>> SignatureListPayload(account_id="poly").account_id
        'poly'
    """


class SignatureCreatePayload(AccountScoped):
    """Payload of `signature-create`.

    Attributes:
        name (str): Human-readable label, e.g. "Work".
        before_logo (str): Text above the logo image.
        after_logo (str): Text below the logo image.
        image (str): Absolute path to an image file to copy.
        account_id (str): Account id (required).

    Examples:
        >>> SignatureCreatePayload(name="Work").name
        'Work'
        >>> SignatureCreatePayload(name="Personal", image="/tmp/logo.png").image
        '/tmp/logo.png'
    """

    name: str = Field("", description="Human-readable label for the signature")
    before_logo: str = Field("", description="Text lines above the logo image")
    after_logo: str = Field("", description="Text lines below the logo image")
    image: str = Field("", description="Absolute path to an image file to copy")


class SignatureUpdatePayload(AccountScoped):
    """Payload of `signature-update`.

    Attributes:
        signature_id (str): ID of the signature to update.
        name (str | None): New name (None → keep current).
        before_logo (str | None): New text above logo (None → keep current).
        after_logo (str | None): New text below logo (None → keep current).
        image (str | None): New image path to copy (None → keep, "" → clear).
        account_id (str): Account id (required).

    Examples:
        >>> SignatureUpdatePayload(signature_id="sig-1").signature_id
        'sig-1'
        >>> SignatureUpdatePayload(signature_id="sig-1", name="New Name").name
        'New Name'
    """

    signature_id: str = Field(..., description="ID of the signature to update")
    name: str | None = Field(None, description="New name (None → keep)")
    before_logo: str | None = Field(
        None, description="New text above logo (None → keep)"
    )
    after_logo: str | None = Field(
        None, description="New text below logo (None → keep)"
    )
    image: str | None = Field(
        None, description="New image path (None → keep, '' → clear)"
    )


class SignatureDeletePayload(AccountScoped):
    """Payload of `signature-delete`.

    Attributes:
        signature_id (str): ID of the signature to delete.
        account_id (str): Account id (required).

    Examples:
        >>> SignatureDeletePayload(signature_id="sig-1").signature_id
        'sig-1'
    """

    signature_id: str = Field(..., description="ID of the signature to delete")


class SignatureDefaultPayload(AccountScoped):
    """Payload of `signature-default`.

    Attributes:
        signature_id (str): ID of the signature to set as default.
        account_id (str): Account id (required).

    Examples:
        >>> SignatureDefaultPayload(signature_id="sig-1").signature_id
        'sig-1'
    """

    signature_id: str = Field(..., description="ID of the signature to set as default")


class SignatureGetPayload(AccountScoped):
    """Payload of `signature-get`.

    Attributes:
        signature_id (str): ID of the signature to retrieve.
        account_id (str): Account id (required).

    Examples:
        >>> SignatureGetPayload(signature_id="sig-1").signature_id
        'sig-1'
    """

    signature_id: str = Field(..., description="ID of the signature to retrieve")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _load_account_from_json(account_id: str) -> tuple[list, int]:
    """Load all accounts from JSON and return (raw_list, target_index).

    Args:
        account_id (str): Account id to find (required).

    Returns:
        tuple[list, int]: The raw accounts list and the index of the target.

    Raises:
        MailProxyError: When the account is not found.

    Examples:
        >>> _load_account_from_json("poly")[1]
        0
    """
    if not Path(SIGNATURES_DIR).parent.parent / "accounts.json":
        pass  # will be caught by load_accounts
    accounts = load_accounts(force=True)
    if not accounts:
        raise MailProxyError("No accounts found in accounts.json.")
    target_idx: int | None = None
    for i, acc in enumerate(accounts):
        if _match_account(account_id, acc):
            target_idx = i
            break
    if target_idx is None:
        raise MailProxyError(f"Account {account_id!r} not found.")
    return accounts, target_idx


def _account_summary(sig: SignatureDef, is_default: bool) -> dict:
    """Build the summary dict for one signature.

    Args:
        sig (SignatureDef): The signature to summarize.
        is_default (bool): Whether this is the account's default.

    Returns:
        dict: Summary with id, name, default, has_image, before_logo, after_logo.

    Examples:
        >>> _account_summary(SignatureDef(id="s1", name="Work", image="a.png"), True)["default"]
        True
        >>> _account_summary(SignatureDef(id="s1"), False)["has_image"]
        False
    """
    return {
        "id": sig.id,
        "name": sig.name,
        "default": is_default,
        "has_image": bool(sig.image),
        "before_logo": sig.before_logo,
        "after_logo": sig.after_logo,
    }


def _delete_orphan_image(
    image_name: str, remaining_signatures: list[SignatureDef]
) -> bool:
    """Delete an image file if no other signature uses it.

    Args:
        image_name (str): The filename in the signatures dir.
        remaining_signatures (list[SignatureDef]): Signatures still referencing images.

    Returns:
        bool: True if the image was deleted.

    Examples:
        >>> _delete_orphan_image("abc.png", [])
        True
        >>> _delete_orphan_image("abc.png", [SignatureDef(image="abc.png")])
        False
    """
    if not image_name:
        return False
    still_used = any(sig.image == image_name for sig in remaining_signatures)
    if still_used:
        return False
    img_path = get_signatures_dir() / image_name
    if img_path.exists():
        img_path.unlink()
        return True
    return False


# ── Handlers ─────────────────────────────────────────────────────────────────


def signature_list(client: MailClient, p: SignatureListPayload) -> dict:
    """List all signatures for an account.

    Returns every signature with its id, name, default status, and whether
    it has an image. The list is ordered as stored — first signature is
    the implicit default when no default_signature_id is set.

    Parameters:
        - account_id (str): Account id (required).

    Examples:
        - List signatures:
            `mail-proxy do signature-list`
            → {"account":"poly","signatures":[{"id":"sig-abc","name":"Work","default":true,"has_image":true}]}
        - List for a specific account:
            `mail-proxy do signature-list '{"account_id":"outlook"}'`
            → {"account":"outlook","signatures":[]}
        - Empty account:
            `mail-proxy do signature-list '{"account_id":"gmail"}'`
            → {"account":"gmail","signatures":[]}
    """
    accounts = load_accounts(force=True)
    target = None
    for acc in accounts:
        if _match_account(p.account_id, acc):
            target = acc
            break
    if target is None:
        raise MailProxyError(f"Account {p.account_id!r} not found.")

    summaries = [
        _account_summary(sig, sig.id == target.default_signature_id)
        for sig in target.signatures
    ]
    return {"account": target.id, "signatures": summaries}


def signature_create(client: MailClient, p: SignatureCreatePayload) -> dict:
    """Create a new signature for an account.

    Auto-generates a unique id (`sig-{uuid4().hex[:8]}`). If an image path
    is provided, it is copied to the signatures dir (deduplicated by SHA256).
    The new signature is NOT automatically set as default.

    Parameters:
        - name (str): Human-readable label.
        - before_logo (str): Text above the logo (default "").
        - after_logo (str): Text below the logo (default "").
        - image (str): Absolute path to an image file to copy (default "").
        - account_id (str): Account id (required).

    Examples:
        - Create a text-only signature:
            `mail-proxy do signature-create '{"name":"Work","before_logo":"John Doe","after_logo":"ACME Corp"}'`
            → {"id":"sig-a1b2c3d4","name":"Work","account":"poly","has_image":false}
        - Create with an image:
            `mail-proxy do signature-create '{"name":"Logo","before_logo":"John","image":"/tmp/logo.png"}'`
            → {"id":"sig-e5f6a7b8","name":"Logo","account":"poly","has_image":true}
        - Create for a specific account:
            `mail-proxy do signature-create '{"name":"Personal","account_id":"outlook","before_logo":"Jane"}'`
            → {"id":"sig-c9d0e1f2","name":"Personal","account":"outlook","has_image":false}
    """
    accounts, target_idx = _load_account_from_json(p.account_id)
    target = accounts[target_idx]

    new_id = f"sig-{uuid.uuid4().hex[:8]}"
    image_name = ""
    if p.image:
        image_name = copy_signature_image(p.image)

    sig = SignatureDef(
        id=new_id,
        name=p.name,
        before_logo=p.before_logo,
        image=image_name,
        after_logo=p.after_logo,
    )
    target.signatures.append(sig)
    write_accounts_json(accounts)

    return {
        "id": new_id,
        "name": sig.name,
        "account": target.id,
        "has_image": bool(image_name),
    }


def signature_update(client: MailClient, p: SignatureUpdatePayload) -> dict:
    """Update an existing signature by id.

    Any field can be changed. Pass `image: ""` to clear the image.
    Pass `image: "/path/to/new.png"` to replace it.

    Parameters:
        - signature_id (str): ID of the signature to update.
        - name (str | None): New name (None → keep current).
        - before_logo (str | None): New text above logo (None → keep current).
        - after_logo (str | None): New text below logo (None → keep current).
        - image (str | None): New image path (None → keep, "" → clear).
        - account_id (str): Account id (required).

    Examples:
        - Rename a signature:
            `mail-proxy do signature-update '{"signature_id":"sig-abc","name":"Updated"}'`
            → {"id":"sig-abc","name":"Updated","account":"poly","has_image":false}
        - Replace the image:
            `mail-proxy do signature-update '{"signature_id":"sig-abc","image":"/tmp/new-logo.png"}'`
            → {"id":"sig-abc","name":"Work","account":"poly","has_image":true}
        - Clear the image:
            `mail-proxy do signature-update '{"signature_id":"sig-abc","image":""}'`
            → {"id":"sig-abc","name":"Work","account":"poly","has_image":false}
    """
    accounts, target_idx = _load_account_from_json(p.account_id)
    target = accounts[target_idx]

    sig = target.get_signature_by_id(p.signature_id)
    if sig is None:
        raise MailProxyError(
            f"Signature {p.signature_id!r} not found on account {target.id!r}."
        )

    old_image = sig.image

    if p.name is not None:
        sig.name = p.name
    if p.before_logo is not None:
        sig.before_logo = p.before_logo
    if p.after_logo is not None:
        sig.after_logo = p.after_logo
    if p.image is not None:
        if p.image == "":
            sig.image = ""
        else:
            sig.image = copy_signature_image(p.image)
        # Delete old image if orphaned
        if old_image and sig.image != old_image:
            _delete_orphan_image(old_image, target.signatures)

    write_accounts_json(accounts)
    return {
        "id": sig.id,
        "name": sig.name,
        "account": target.id,
        "has_image": bool(sig.image),
    }


def signature_delete(client: MailClient, p: SignatureDeletePayload) -> dict:
    """Delete a signature and its orphan image.

    Cannot delete the only signature on an account. If the deleted signature's
    image is not used by any other signature, the image file is also removed.

    Parameters:
        - signature_id (str): ID of the signature to delete.
        - account_id (str): Account id (required).

    Examples:
        - Delete a signature:
            `mail-proxy do signature-delete '{"signature_id":"sig-abc"}'`
            → {"deleted":"sig-abc","account":"poly","image_deleted":false}
        - Cannot delete the only signature:
            `mail-proxy do signature-delete '{"signature_id":"sig-only"}'`
            → (error: cannot delete the only signature, exit 1)
        - Delete with orphan image cleanup:
            `mail-proxy do signature-delete '{"signature_id":"sig-def"}'`
            → {"deleted":"sig-def","account":"poly","image_deleted":true}
    """
    accounts, target_idx = _load_account_from_json(p.account_id)
    target = accounts[target_idx]

    if len(target.signatures) <= 1:
        raise MailProxyError(
            f"Cannot delete the only signature on account {target.id!r}. "
            "Create a new signature before deleting this one."
        )

    sig = target.get_signature_by_id(p.signature_id)
    if sig is None:
        raise MailProxyError(
            f"Signature {p.signature_id!r} not found on account {target.id!r}."
        )

    image_deleted = False
    if sig.image:
        remaining = [s for s in target.signatures if s.id != p.signature_id]
        image_deleted = _delete_orphan_image(sig.image, remaining)

    target.signatures = [s for s in target.signatures if s.id != p.signature_id]

    # Clear default if it pointed to the deleted signature
    if target.default_signature_id == p.signature_id:
        target.default_signature_id = (
            target.signatures[0].id if target.signatures else ""
        )

    write_accounts_json(accounts)
    return {
        "deleted": p.signature_id,
        "account": target.id,
        "image_deleted": image_deleted,
    }


def signature_default(client: MailClient, p: SignatureDefaultPayload) -> dict:
    """Set the default signature for an account.

    The default signature is used when compose actions pass `signature:"default"`.

    Parameters:
        - signature_id (str): ID of the signature to set as default.
        - account_id (str): Account id (required).

    Examples:
        - Set default:
            `mail-proxy do signature-default '{"signature_id":"sig-abc"}'`
            → {"account":"poly","default_signature_id":"sig-abc"}
        - Change to another:
            `mail-proxy do signature-default '{"signature_id":"sig-def"}'`
            → {"account":"poly","default_signature_id":"sig-def"}
        - Set for specific account:
            `mail-proxy do signature-default '{"signature_id":"sig-abc","account_id":"outlook"}'`
            → {"account":"outlook","default_signature_id":"sig-abc"}
    """
    accounts, target_idx = _load_account_from_json(p.account_id)
    target = accounts[target_idx]

    sig = target.get_signature_by_id(p.signature_id)
    if sig is None:
        raise MailProxyError(
            f"Signature {p.signature_id!r} not found on account {target.id!r}."
        )

    target.default_signature_id = p.signature_id
    write_accounts_json(accounts)
    return {"account": target.id, "default_signature_id": p.signature_id}


def signature_get(client: MailClient, p: SignatureGetPayload) -> dict:
    """Return full details of one signature, including image as base64.

    Parameters:
        - signature_id (str): ID of the signature to retrieve.
        - account_id (str): Account id (required).

    Examples:
        - Get a signature:
            `mail-proxy do signature-get '{"signature_id":"sig-abc"}'`
            → {"id":"sig-abc","name":"Work","before_logo":"John","after_logo":"Corp","image":null,"account":"poly"}
        - Get with image:
            `mail-proxy do signature-get '{"signature_id":"sig-abc"}'`
            → {"id":"sig-abc","name":"Logo","before_logo":"John","after_logo":"","image":{"filename":"abc.png","base64":"..."},"account":"poly"}
        - Get for a specific account:
            `mail-proxy do signature-get '{"signature_id":"sig-abc","account_id":"outlook"}'`
            → {"id":"sig-abc","name":"Work","before_logo":"Jane","after_logo":"","image":null,"account":"outlook"}
    """
    accounts = load_accounts(force=True)
    target = None
    for acc in accounts:
        if _match_account(p.account_id, acc):
            target = acc
            break
    if target is None:
        raise MailProxyError(f"Account {p.account_id!r} not found.")
    sig = target.get_signature_by_id(p.signature_id)
    if sig is None:
        raise MailProxyError(
            f"Signature {p.signature_id!r} not found on account {target.id!r}."
        )

    image_data = None
    if sig.image:
        img_path = get_signatures_dir() / sig.image
        if img_path.exists():
            image_data = {
                "filename": sig.image,
                "base64": base64.b64encode(img_path.read_bytes()).decode("ascii"),
            }

    return {
        "id": sig.id,
        "name": sig.name,
        "before_logo": sig.before_logo,
        "after_logo": sig.after_logo,
        "image": image_data,
        "account": target.id,
    }


# ── Registry ─────────────────────────────────────────────────────────────────

ACTIONS = [
    ActionDef(
        "signature-list", SignatureListPayload, signature_list, group="Signatures"
    ),
    ActionDef(
        "signature-create", SignatureCreatePayload, signature_create, group="Signatures"
    ),
    ActionDef(
        "signature-update", SignatureUpdatePayload, signature_update, group="Signatures"
    ),
    ActionDef(
        "signature-delete", SignatureDeletePayload, signature_delete, group="Signatures"
    ),
    ActionDef(
        "signature-default",
        SignatureDefaultPayload,
        signature_default,
        group="Signatures",
    ),
    ActionDef("signature-get", SignatureGetPayload, signature_get, group="Signatures"),
]
