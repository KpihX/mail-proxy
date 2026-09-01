"""Signatures — CRUD, default, migration, image dedup."""

import json
import os
from pathlib import Path

import pytest

from mail_proxy import config
from mail_proxy.actions.registry import REGISTRY
from mail_proxy.config import (
    SIGNATURES_DIR,
    AccountDef,
    ImapEndpoint,
    SignatureDef,
    SmtpEndpoint,
    copy_signature_image,
    get_signatures_dir,
    load_accounts,
    write_accounts_json,
)
from mail_proxy.exceptions import MailProxyError


def _make_account(**overrides):
    """Build an AccountDef with sensible defaults for testing."""
    defaults = dict(
        id="test",
        email="test@example.com",
        imap=ImapEndpoint(host="imap.test.fr"),
        smtp=SmtpEndpoint(host="smtp.test.fr"),
    )
    defaults.update(overrides)
    return AccountDef(**defaults)


# ── config helpers ───────────────────────────────────────────────────────────


def test_get_signatures_dir_creates():
    """get_signatures_dir() creates the directory if needed."""
    d = get_signatures_dir()
    assert d.exists()
    assert d.is_dir()


def test_copy_signature_image_dedup(tmp_path):
    """Same content → same filename; different content → different filename."""
    img1 = tmp_path / "logo1.png"
    img1.write_bytes(b"png-data-v1")
    img2 = tmp_path / "logo2.png"
    img2.write_bytes(b"png-data-v1")  # same content
    img3 = tmp_path / "logo3.png"
    img3.write_bytes(b"png-data-v2")  # different content

    name1 = copy_signature_image(str(img1))
    name2 = copy_signature_image(str(img2))
    name3 = copy_signature_image(str(img3))

    assert name1 == name2  # deduped
    assert name1 != name3  # different content


def test_copy_signature_image_not_found():
    """copy_signature_image raises when source does not exist."""
    with pytest.raises(MailProxyError, match="Source image not found"):
        copy_signature_image("/tmp/nonexistent_img.png")


# ── AccountDef helpers ───────────────────────────────────────────────────────


def test_get_default_signature():
    """get_default_signature returns the right one or None."""
    a = _make_account(
        signatures=[
            SignatureDef(id="s1", name="A"),
            SignatureDef(id="s2", name="B"),
        ],
        default_signature_id="s2",
    )
    assert a.get_default_signature().id == "s2"


def test_get_default_signature_fallback():
    """Without default_signature_id, returns the first."""
    a = _make_account(
        signatures=[SignatureDef(id="s1"), SignatureDef(id="s2")],
    )
    assert a.get_default_signature().id == "s1"


def test_get_default_signature_empty():
    """Empty signatures list → None."""
    a = _make_account(signatures=[])
    assert a.get_default_signature() is None


def test_get_signature_by_id():
    """get_signature_by_id returns the right one or None."""
    a = _make_account(signatures=[SignatureDef(id="s1", name="Work")])
    assert a.get_signature_by_id("s1").name == "Work"
    assert a.get_signature_by_id("nope") is None


# ── Migration ────────────────────────────────────────────────────────────────


def test_migration_old_format(tmp_path, monkeypatch):
    """Old `signature: {}` → auto-converted to `signatures: []` + `default_signature_id`."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_JSON_PATH", tmp_path / "accounts.json")
    monkeypatch.setattr(config, "_accounts_cache", [])

    old_data = [
        {
            "id": "legacy",
            "email": "old@gmail.com",
            "signature": {
                "before_logo": "Old Name",
                "logo_path": "",
                "after_logo": "Old Corp",
            },
        }
    ]
    (tmp_path / "accounts.json").write_text(json.dumps(old_data))

    accounts = load_accounts(force=True)
    assert len(accounts) == 1
    acc = accounts[0]
    assert len(acc.signatures) == 1
    sig = acc.signatures[0]
    assert sig.before_logo == "Old Name"
    assert sig.after_logo == "Old Corp"
    assert sig.id.startswith("sig-")
    assert acc.default_signature_id == sig.id


def test_migration_new_format_unchanged(tmp_path, monkeypatch):
    """New format `signatures: [...]` is loaded as-is."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_JSON_PATH", tmp_path / "accounts.json")
    monkeypatch.setattr(config, "_accounts_cache", [])

    new_data = [
        {
            "id": "modern",
            "email": "new@outlook.com",
            "signatures": [{"id": "sig-123", "name": "Work", "before_logo": "Hi"}],
            "default_signature_id": "sig-123",
        }
    ]
    (tmp_path / "accounts.json").write_text(json.dumps(new_data))

    accounts = load_accounts(force=True)
    acc = accounts[0]
    assert acc.signatures[0].id == "sig-123"
    assert acc.default_signature_id == "sig-123"


# ── write_accounts_json round-trip ──────────────────────────────────────────


def test_write_accounts_json_serializes_signatures(tmp_path, monkeypatch):
    """write_accounts_json writes signatures and default_signature_id."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_JSON_PATH", tmp_path / "accounts.json")
    monkeypatch.setattr(config, "_accounts_cache", [])

    acc = _make_account(
        signatures=[SignatureDef(id="s1", name="Work", before_logo="Hi")],
        default_signature_id="s1",
    )
    write_accounts_json([acc])

    raw = json.loads((tmp_path / "accounts.json").read_text())
    assert raw[0]["signatures"][0]["id"] == "s1"
    assert raw[0]["default_signature_id"] == "s1"


# ── Signature commands (via registry) ────────────────────────────────────────


def test_signature_actions_exist():
    """All 6 signature actions are in the registry."""
    for name in (
        "signature-list",
        "signature-create",
        "signature-update",
        "signature-delete",
        "signature-default",
        "signature-get",
    ):
        assert name in REGISTRY, f"{name} missing from registry"


def test_signature_actions_are_not_hitl():
    """Signature management commands are NOT HITL-protected."""
    for name in (
        "signature-list",
        "signature-create",
        "signature-update",
        "signature-delete",
        "signature-default",
        "signature-get",
    ):
        assert not REGISTRY[name].hitl, f"{name} must not require HITL"


def test_signature_list_group():
    """All signature actions are in the 'Signatures' group."""
    for name in (
        "signature-list",
        "signature-create",
        "signature-update",
        "signature-delete",
        "signature-default",
        "signature-get",
    ):
        assert REGISTRY[name].group == "Signatures"


# ── Handler integration tests ────────────────────────────────────────────────


def test_signature_list_handler():
    """signature-list returns all signatures with default flag."""
    from mail_proxy.actions.signatures import signature_list, SignatureListPayload

    p = SignatureListPayload()
    result = signature_list(None, p)
    assert result["account"] == "poly"
    assert len(result["signatures"]) == 1
    assert result["signatures"][0]["default"] is True
    assert result["signatures"][0]["id"] == "sig-poly001"


def test_signature_create_handler():
    """signature-create adds a signature and returns the new id."""
    from mail_proxy.actions.signatures import signature_create, SignatureCreatePayload

    p = SignatureCreatePayload(name="Personal", before_logo="Jane", after_logo="Corp")
    result = signature_create(None, p)
    assert result["name"] == "Personal"
    assert result["id"].startswith("sig-")
    assert result["account"] == "poly"

    # Verify it's persisted
    accounts = load_accounts(force=True)
    poly = [a for a in accounts if a.id == "poly"][0]
    assert len(poly.signatures) == 2


def test_signature_update_handler():
    """signature-update modifies a field."""
    from mail_proxy.actions.signatures import (
        signature_create,
        signature_update,
        SignatureCreatePayload,
        SignatureUpdatePayload,
    )

    # Create first
    create_p = SignatureCreatePayload(name="Temp")
    created = signature_create(None, create_p)

    # Update it
    update_p = SignatureUpdatePayload(
        signature_id=created["id"], name="Updated Name"
    )
    result = signature_update(None, update_p)
    assert result["name"] == "Updated Name"


def test_signature_delete_handler():
    """signature-delete removes a signature (not the only one)."""
    from mail_proxy.actions.signatures import (
        signature_create,
        signature_delete,
        SignatureCreatePayload,
        SignatureDeletePayload,
    )

    # Create an extra so we can delete one
    create_p = SignatureCreatePayload(name="Deleteme")
    created = signature_create(None, create_p)

    delete_p = SignatureDeletePayload(signature_id=created["id"])
    result = signature_delete(None, delete_p)
    assert result["deleted"] == created["id"]
    assert result["image_deleted"] is False


def test_signature_delete_only_one_raises():
    """signature-delete raises when it's the only signature."""
    from mail_proxy.actions.signatures import (
        signature_delete,
        SignatureDeletePayload,
    )

    # Outlook has no signatures, so this won't work — use poly which has 1
    p = SignatureDeletePayload(signature_id="sig-poly001")
    with pytest.raises(MailProxyError, match="Cannot delete the only signature"):
        signature_delete(None, p)


def test_signature_default_handler():
    """signature-default sets the default_signature_id."""
    from mail_proxy.actions.signatures import (
        signature_create,
        signature_default,
        SignatureCreatePayload,
        SignatureDefaultPayload,
    )

    # Create a second signature
    create_p = SignatureCreatePayload(name="NewDefault")
    created = signature_create(None, create_p)

    # Set it as default
    default_p = SignatureDefaultPayload(signature_id=created["id"])
    result = signature_default(None, default_p)
    assert result["default_signature_id"] == created["id"]


def test_signature_get_handler():
    """signature-get returns full details."""
    from mail_proxy.actions.signatures import signature_get, SignatureGetPayload

    p = SignatureGetPayload(signature_id="sig-poly001")
    result = signature_get(None, p)
    assert result["id"] == "sig-poly001"
    assert result["name"] == "Work"
    assert result["image"] is None


def test_signature_list_for_account_with_no_signatures():
    """signature-list on account with no signatures returns empty list."""
    from mail_proxy.actions.signatures import signature_list, SignatureListPayload

    p = SignatureListPayload(account_id="outlook")
    result = signature_list(None, p)
    assert result["account"] == "outlook"
    assert result["signatures"] == []


def test_signature_create_with_image(tmp_path):
    """signature-create with an image copies it to the signatures dir."""
    from mail_proxy.actions.signatures import signature_create, SignatureCreatePayload

    img = tmp_path / "test_logo.png"
    img.write_bytes(b"fake-png-data")

    p = SignatureCreatePayload(
        name="With Image", image=str(img)
    )
    result = signature_create(None, p)
    assert result["has_image"] is True

    # Verify the image was copied
    accounts = load_accounts(force=True)
    poly = [a for a in accounts if a.id == "poly"][0]
    sig = poly.get_signature_by_id(result["id"])
    assert sig is not None
    assert sig.image != ""
    img_path = get_signatures_dir() / sig.image
    assert img_path.exists()
