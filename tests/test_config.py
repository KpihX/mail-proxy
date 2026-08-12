"""Config — env loading, account resolution, prefix derivation, ensure_env."""

import os

import pytest

from mail_proxy import config
from mail_proxy.exceptions import MailProxyError


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point the config at a temporary directory and clear MAIL_* env vars."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    for key in list(os.environ):
        if key.startswith("MAIL_"):
            monkeypatch.delenv(key, raising=False)
    yield


def test_account_env_prefix():
    assert config.account_env_prefix("poly") == "MAIL_POLY_"
    assert config.account_env_prefix("work") == "MAIL_WORK_"


def test_load_env_absent_returns_empty():
    assert config.load_env() == {}


def test_write_then_load_env(tmp_path):
    config.write_env({"MAIL_POLY_LOGIN": "ivann.kamdem-pouokam", "MAIL_POLY_PASS": "secret"})
    assert config.ENV_PATH.exists()
    assert config.ENV_PATH.stat().st_mode & 0o777 == 0o600
    loaded = config.load_env()
    assert loaded["MAIL_POLY_LOGIN"] == "ivann.kamdem-pouokam"
    assert loaded["MAIL_POLY_PASS"] == "secret"


def test_write_env_skips_empty_values(tmp_path):
    config.write_env({"MAIL_POLY_LOGIN": "ivann", "MAIL_POLY_PASS": ""})
    loaded = config.load_env()
    assert "MAIL_POLY_PASS" not in loaded
    assert loaded["MAIL_POLY_LOGIN"] == "ivann"


def test_process_env_wins_over_file(tmp_path, monkeypatch):
    config.write_env({"MAIL_POLY_LOGIN": "from-file"})
    monkeypatch.setenv("MAIL_POLY_LOGIN", "from-shell")
    assert config.load_env()["MAIL_POLY_LOGIN"] == "from-shell"


def test_resolve_account_with_secrets():
    config.write_env({
        "MAIL_POLY_LOGIN": "ivann.kamdem-pouokam",
        "MAIL_POLY_PASS": "secret",
    })
    config.load_env()
    account = config.get_account("poly")
    assert account.username == "ivann.kamdem-pouokam"
    assert account.password == "secret"
    assert account.id == "poly"
    assert account.from_address == "ivann.kamdem-pouokam@polytechnique.edu"


def test_get_default_account_is_poly():
    account = config.get_account(None)
    assert account.id == "poly"
    assert account.default is True


def test_get_unknown_account_raises():
    with pytest.raises(MailProxyError, match="Unknown account"):
        config.get_account("nope")


def test_endpoint_overrides(tmp_path, monkeypatch):
    config.write_env({
        "MAIL_POLY_LOGIN": "u",
        "MAIL_POLY_PASS": "p",
        "MAIL_POLY_IMAP_HOST": "imap.example.com",
        "MAIL_POLY_IMAP_PORT": "143",
        "MAIL_POLY_IMAP_TLS": "false",
        "MAIL_POLY_SMTP_HOST": "smtp.example.com",
        "MAIL_POLY_SMTP_STARTTLS": "false",
    })
    config.load_env()
    account = config.get_account("poly")
    assert account.imap.host == "imap.example.com"
    assert account.imap.port == 143
    assert account.imap.tls is False
    assert account.smtp.host == "smtp.example.com"
    assert account.smtp.starttls is False


def test_ensure_env_missing_file_raises():
    with pytest.raises(MailProxyError, match="admin setup"):
        config.ensure_env()


def test_ensure_env_missing_credentials_raises(tmp_path):
    config.write_env({"MAIL_POLY_LOGIN": "u"})
    with pytest.raises(MailProxyError, match="MAIL_POLY_PASS"):
        config.ensure_env()


def test_ensure_env_ok(tmp_path):
    config.write_env({"MAIL_POLY_LOGIN": "u", "MAIL_POLY_PASS": "p"})
    assert config.ensure_env() is None


def test_api_timeout_default_and_override(monkeypatch):
    assert config.api_timeout() == 15.0
    monkeypatch.setenv("MAIL_TIMEOUT", "30")
    assert config.api_timeout() == 30.0
    monkeypatch.setenv("MAIL_TIMEOUT", "garbage")
    assert config.api_timeout() == 15.0


def test_account_from_address_falls_back_to_username():
    account = config.ACCOUNTS[0].model_copy()
    account.email = ""
    account.username = "ivann"
    assert account.from_address == "ivann"
