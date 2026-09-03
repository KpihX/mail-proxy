"""Config — env loading, account resolution, prefix derivation, ensure_env."""

import os

import pytest

from mail_proxy import config
from mail_proxy.exceptions import MailProxyError


# No local isolated_env fixture — conftest.py provides it for all tests.


def test_account_env_prefix():
    assert config.account_env_prefix("poly") == "MAIL_POLY_"
    assert config.account_env_prefix("work") == "MAIL_WORK_"


def test_load_env_absent_returns_empty():
    assert config.load_env() == {}


def test_write_then_load_env():
    config.write_env({"MAIL_POLY_PASS": "secret"})
    assert config.ENV_PATH.exists()
    assert config.ENV_PATH.stat().st_mode & 0o777 == 0o600
    loaded = config.load_env()
    assert loaded["MAIL_POLY_PASS"] == "secret"


def test_write_env_skips_empty_values():
    config.write_env({"MAIL_POLY_PASS": ""})
    loaded = config.load_env()
    assert "MAIL_POLY_PASS" not in loaded


def test_process_env_wins_over_file(monkeypatch):
    config.write_env({"MAIL_POLY_PASS": "from-file"})
    monkeypatch.setenv("MAIL_POLY_PASS", "from-shell")
    assert config.load_env()["MAIL_POLY_PASS"] == "from-shell"


def test_resolve_account_with_secrets():
    config.write_env({"MAIL_POLY_PASS": "secret"})
    config.load_env()
    account = config.get_account("poly")
    # Login = email from JSON (no separate LOGIN field in .env anymore)
    assert account.username == "user.name@polytechnique.edu"
    assert account.password == "secret"
    assert account.id == "poly"
    assert account.from_address == "user.name@polytechnique.edu"


def test_get_account_none_raises():
    with pytest.raises(MailProxyError, match="account_id is required"):
        config.get_account(None)


def test_get_unknown_account_raises():
    with pytest.raises(MailProxyError, match="Unknown account"):
        config.get_account("nope")


def test_endpoint_auto_detection_from_email():
    """IMAP/SMTP endpoints are auto-detected from the email domain."""
    account = config.get_account("poly")
    assert account.imap.host == "webmail.polytechnique.fr"
    assert account.smtp.host == "webmail.polytechnique.fr"
    account_gmail = config.get_account("gmail")
    assert account_gmail.imap.host == "imap.gmail.com"
    assert account_gmail.smtp.host == "smtp.gmail.com"


def test_ensure_env_missing_accounts_json_raises():
    """When accounts.json doesn't exist, error points to it."""
    config.ACCOUNTS_JSON_PATH.unlink(missing_ok=True)
    config._accounts_cache = []
    with pytest.raises(MailProxyError, match="accounts.json"):
        config.ensure_env()


def test_ensure_env_missing_file_raises():
    with pytest.raises(MailProxyError, match="admin setup"):
        config.ensure_env()


def test_ensure_env_missing_credentials_raises():
    config.write_env({"MAIL_POLY_PASS": ""})
    config.load_env()
    with pytest.raises(MailProxyError, match="MAIL_POLY_PASS"):
        config.ensure_env("poly")


def test_ensure_env_ok():
    config.write_env({"MAIL_POLY_PASS": "p"})
    config.load_env()
    assert config.ensure_env("poly") is None


def test_api_timeout_default_and_override(monkeypatch):
    assert config.api_timeout() == 15.0
    monkeypatch.setenv("MAIL_TIMEOUT", "30")
    assert config.api_timeout() == 30.0
    monkeypatch.setenv("MAIL_TIMEOUT", "garbage")
    assert config.api_timeout() == 15.0


def test_account_from_address_falls_back_to_login():
    """from_address returns email, or login if email is empty."""
    from mail_proxy.config import AccountDef, ImapEndpoint, SmtpEndpoint

    account = AccountDef(
        id="test", email="", login="user",
        imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y"),
    )
    assert account.from_address == "user"


def test_account_aliases_loaded_from_json():
    """Aliases are loaded from accounts.json, not hardcoded."""
    poly = config.get_account("poly")
    assert "x" in poly.aliases
    assert "polytechnique" in poly.aliases
    outlook = config.get_account("outlook")
    assert "work" in outlook.aliases


def test_account_resolution_by_alias():
    """-a x → poly, -a work → outlook, -a google → gmail."""
    assert config.get_account("x").id == "poly"
    assert config.get_account("work").id == "outlook"
    assert config.get_account("google").id == "gmail"


def test_account_resolution_by_email_prefix():
    """-a user.name → poly (email prefix match)."""
    assert config.get_account("user.name").id == "poly"


def test_list_accounts():
    accounts = config.list_accounts()
    assert len(accounts) == 3
    ids = [a["id"] for a in accounts]
    assert "poly" in ids
    assert "outlook" in ids
    assert "gmail" in ids
