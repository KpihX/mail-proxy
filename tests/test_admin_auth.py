"""Admin — auth login/status/logout + reset/purge with faked HITL and probes."""

import os

import pytest
from typer.testing import CliRunner

from mail_proxy import admin, config
from mail_proxy.cli import app


# No local isolated_env fixture — conftest.py provides it for all tests.


def _approve(payload, comment="", edited=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        status="approved", payload=payload, comment=comment, edited=edited
    )


def _reject(comment="no"):
    from types import SimpleNamespace

    return SimpleNamespace(status="rejected", payload=None, comment=comment, edited=False)


def test_mask():
    assert admin._mask("user.name@mail.com") == "user…l.com"
    assert admin._mask("") == ""
    assert admin._mask("short") == "…"


# ── auth login ────────────────────────────────────────────────────────────────


def test_auth_login_writes_json_and_env(monkeypatch):
    monkeypatch.setattr(
        admin, "request_approval",
        lambda action, payload: _approve({
            "id": "testaccount",
            "email": "test@gmail.com",
            "aliases": ["test"],
            "display_name": "Test User",
            "password": "secret123",
        }),
    )
    monkeypatch.setattr(
        admin, "_probe_account_imap",
        lambda account: {"reachable": True, "auth_ok": True, "error": ""},
    )
    monkeypatch.setattr(
        admin, "_probe_account_smtp",
        lambda account: {"reachable": True, "error": ""},
    )
    data, status, *_ = admin.auth_login()
    assert status == "approved"
    assert data["account"] == "testaccount"
    assert data["configured"] is True
    assert data["imap"]["auth_ok"] is True
    # Both JSON and .env should be written
    assert config.ACCOUNTS_JSON_PATH.exists()
    assert config.ENV_PATH.exists()
    loaded_env = config.load_env()
    assert loaded_env.get("MAIL_TESTACCOUNT_PASS") == "secret123"
    # Verify JSON was updated
    config._accounts_cache = []
    accounts = config.load_accounts(force=True)
    assert any(a.id == "testaccount" for a in accounts)


def test_auth_login_rejected(monkeypatch):
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _reject("nope"))
    data, status, _, comment = admin.auth_login()
    assert data is None
    assert status == "rejected"
    assert comment == "nope"
    assert not config.ENV_PATH.exists()


def test_auth_login_missing_fields(monkeypatch):
    monkeypatch.setattr(
        admin, "request_approval",
        lambda action, payload: _approve({"id": "", "email": "", "password": ""}),
    )
    with pytest.raises(Exception):
        admin.auth_login()


# ── auth status ───────────────────────────────────────────────────────────────


def test_auth_status_shape(monkeypatch):
    config.write_env({"MAIL_POLY_PASS": "pw"})
    monkeypatch.setattr(
        admin, "_probe_imap",
        lambda account_id: {"reachable": True, "auth_ok": True, "error": ""},
    )
    monkeypatch.setattr(
        admin, "_probe_smtp",
        lambda account_id: {"reachable": True, "error": ""},
    )
    state = admin.status()
    assert "accounts_json" in state
    assert state["default_account"] == "poly"
    poly = next(a for a in state["accounts"] if a["id"] == "poly")
    assert poly["configured"] is True
    assert poly["imap"]["auth_ok"] is True
    assert poly["smtp"]["reachable"] is True


def test_auth_status_unconfigured(monkeypatch):
    monkeypatch.setattr(
        admin, "_probe_imap",
        lambda account_id: {"reachable": False, "auth_ok": False, "error": "missing"},
    )
    monkeypatch.setattr(
        admin, "_probe_smtp",
        lambda account_id: {"reachable": False, "error": "missing"},
    )
    state = admin.status()
    assert "accounts_json" in state
    poly = next(a for a in state["accounts"] if a["id"] == "poly")
    assert poly["configured"] is False


# ── auth logout ───────────────────────────────────────────────────────────────


def test_auth_logout_removes_password(monkeypatch):
    config.write_env({"MAIL_POLY_PASS": "pw"})
    monkeypatch.setattr(
        admin, "request_approval",
        lambda action, payload: _approve({"account_id": "poly"}),
    )
    data, status, _, _ = admin.auth_logout()
    assert status == "approved"
    assert data["account"] == "poly"
    assert data["configured"] is False
    loaded_env = config.load_env()
    assert "MAIL_POLY_PASS" not in loaded_env


def test_auth_logout_rejected(monkeypatch):
    config.write_env({"MAIL_POLY_PASS": "pw"})
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _reject())
    data, status, *_ = admin.auth_logout()
    assert data is None
    assert status == "rejected"
    # Password should still be there
    loaded_env = config.load_env()
    assert "MAIL_POLY_PASS" in loaded_env


# ── auth default ──────────────────────────────────────────────────────────────


def test_auth_default_sets_explicit_account_and_ignores_review_edits(monkeypatch):
    monkeypatch.setattr(
        admin,
        "request_approval",
        lambda action, payload: _approve({"account": "gmail"}, edited=True),
    )

    data, status, edited, _ = admin.auth_default("work")

    assert status == "approved"
    assert edited is True
    assert data == {"account": "outlook", "default": True}
    accounts = config.load_accounts(force=True)
    assert [account.id for account in accounts if account.default] == ["outlook"]


def test_auth_default_requires_account_option():
    result = CliRunner().invoke(app, ["admin", "auth", "default"])

    assert result.exit_code == 2
    assert "Missing option '--account' / '-a'" in result.output
    assert "Usage:" in result.output


def test_auth_default_accepts_account_option(monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(
        admin,
        "auth_default",
        lambda account: (called.append(account) or ({"account": account}, "approved", False, "")),
    )

    result = CliRunner().invoke(app, ["admin", "auth", "default", "-a", "work"])

    assert result.exit_code == 0
    assert called == ["work"]
    assert '"account": "work"' in result.output


# ── reset / purge ─────────────────────────────────────────────────────────────


def test_reset_clears(monkeypatch):
    config.write_env({"MAIL_POLY_PASS": "p"})
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _approve(payload))
    data, status, _, _ = admin.reset()
    assert status == "approved"
    assert data["status"] == "cleared"
    assert config.load_env() == {}


def test_reset_rejected(monkeypatch):
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _reject())
    data, status, *_ = admin.reset()
    assert data is None
    assert status == "rejected"


def test_purge_deletes_config_dir(monkeypatch):
    config.write_env({"MAIL_POLY_PASS": "u"})
    assert config.CONFIG_DIR.exists()
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _approve(payload))
    data, status, _, _ = admin.purge()
    assert status == "approved"
    assert data["config_dir_deleted"] is True
    assert not config.CONFIG_DIR.exists()
    assert "uv tool uninstall mail-proxy" in data["note"]


def test_purge_rejected(monkeypatch):
    config.write_env({"MAIL_POLY_PASS": "u"})
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _reject())
    data, status, _, _ = admin.purge()
    assert data is None
    assert status == "rejected"
    assert config.CONFIG_DIR.exists()
