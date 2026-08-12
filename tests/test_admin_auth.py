"""Admin — setup/status/reset/purge with a faked HITL and probes."""

import os

import pytest

from mail_proxy import admin, config


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point the config at a temporary directory and clear MAIL_* env vars."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    for key in list(os.environ):
        if key.startswith("MAIL_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _approve(payload, comment="", edited=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        status="approved", payload=payload, comment=comment, edited=edited
    )


def _reject(comment="no"):
    from types import SimpleNamespace

    return SimpleNamespace(status="rejected", payload=None, comment=comment, edited=False)


def test_mask():
    assert admin._mask("ivann.kamdem-pouokam") == "ivan…uokam"
    assert admin._mask("") == ""
    assert admin._mask("short") == "…"


def test_setup_writes_fields(monkeypatch):
    monkeypatch.setattr(
        admin, "request_approval",
        lambda action, payload: _approve({"MAIL_POLY_LOGIN": "ivann", "MAIL_POLY_PASS": "pw"}),
    )
    data, status, *_ = admin.setup()
    assert status == "approved"
    assert data["config"] == str(config.ENV_PATH)
    assert "MAIL_POLY_LOGIN" in data["fields"]
    assert config.ENV_PATH.exists()
    assert config.ENV_PATH.stat().st_mode & 0o777 == 0o600
    assert config.load_env()["MAIL_POLY_LOGIN"] == "ivann"


def test_setup_keeps_untouched_and_clears(monkeypatch):
    config.write_env({"MAIL_POLY_LOGIN": "old", "MAIL_POLY_PASS": "oldpw"})
    # Reviewer changes the login, clears the pass (empty string), keeps nothing else.
    monkeypatch.setattr(
        admin, "request_approval",
        lambda action, payload: _approve({"MAIL_POLY_LOGIN": "new", "MAIL_POLY_PASS": ""}),
    )
    admin.setup()
    # The FILE is the source of truth — the in-process env may keep stale values
    # (shell env intentionally wins over the file, see config.load_env).
    content = config.ENV_PATH.read_text()
    assert "MAIL_POLY_LOGIN=new" in content
    assert "MAIL_POLY_PASS" not in content


def test_setup_rejected(monkeypatch):
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _reject("nope"))
    data, status, _, comment = admin.setup()
    assert data is None
    assert status == "rejected"
    assert comment == "nope"


def test_status_shape(monkeypatch):
    config.write_env({"MAIL_POLY_LOGIN": "ivann.kamdem-pouokam", "MAIL_POLY_PASS": "pw"})
    monkeypatch.setattr(
        admin, "_probe_imap",
        lambda account_id: {"reachable": True, "auth_ok": True, "error": ""},
    )
    monkeypatch.setattr(
        admin, "_probe_smtp",
        lambda account_id: {"reachable": True, "error": ""},
    )
    state = admin.status()
    assert state["config_exists"] is True
    assert state["default_account"] == "poly"
    assert state["accounts"][0]["id"] == "poly"
    assert state["accounts"][0]["configured"] is True
    assert state["accounts"][0]["login"] == "ivan…uokam"  # masked
    assert state["imap"]["auth_ok"] is True
    assert state["smtp"]["reachable"] is True
    assert state["permissions"]["config_file"]["status"] == "ok"


def test_status_unconfigured(monkeypatch):
    monkeypatch.setattr(
        admin, "_probe_imap",
        lambda account_id: {"reachable": False, "auth_ok": False, "error": "missing"},
    )
    monkeypatch.setattr(
        admin, "_probe_smtp",
        lambda account_id: {"reachable": False, "error": "missing"},
    )
    state = admin.status()
    assert state["config_exists"] is False
    assert state["accounts"][0]["configured"] is False
    assert state["accounts"][0]["login"] == ""


def test_reset_clears(monkeypatch):
    config.write_env({"MAIL_POLY_LOGIN": "u", "MAIL_POLY_PASS": "p"})
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
    config.write_env({"MAIL_POLY_LOGIN": "u"})
    assert config.CONFIG_DIR.exists()
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _approve(payload))
    data, status, _, _ = admin.purge()
    assert status == "approved"
    assert data["config_dir_deleted"] is True
    assert not config.CONFIG_DIR.exists()
    assert "uv tool uninstall mail-proxy" in data["note"]


def test_purge_rejected(monkeypatch):
    config.write_env({"MAIL_POLY_LOGIN": "u"})
    monkeypatch.setattr(admin, "request_approval", lambda action, payload: _reject())
    data, status, _, _ = admin.purge()
    assert data is None
    assert status == "rejected"
    assert config.CONFIG_DIR.exists()  # untouched
