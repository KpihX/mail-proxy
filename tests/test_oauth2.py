"""OAuth2 module — token lifecycle, XOAUTH2 string builder, provider configs."""

import base64
import json
import time

import pytest

from mail_proxy import oauth2
from mail_proxy.config import CONFIG_DIR
from mail_proxy.exceptions import MailProxyError


# ── conftest patches TOKENS_DIR at the config level; we also patch oauth2 ──


@pytest.fixture(autouse=True)
def _patch_tokens_dir(tmp_path, monkeypatch):
    """Point oauth2.TOKENS_DIR at the test tmp_path."""
    monkeypatch.setattr(oauth2, "TOKENS_DIR", tmp_path / "tokens")
    monkeypatch.setattr(oauth2, "CONFIG_DIR", tmp_path)
    yield


# ── build_xoauth2_string ────────────────────────────────────────────────────


def test_build_xoauth2_string_format():
    """XOAUTH2 string is base64-encoded and contains user + auth."""
    result = oauth2.build_xoauth2_string("user@example.com", "tok123")
    decoded = base64.b64decode(result)
    assert b"user=user@example.com" in decoded
    assert b"auth=Bearer tok123" in decoded
    assert decoded.endswith(b"\x01\x01")


def test_build_xoauth2_string_empty_token():
    """Empty access token still produces a valid base64 string."""
    result = oauth2.build_xoauth2_string("a@b.com", "")
    decoded = base64.b64decode(result)
    assert b"user=a@b.com" in decoded
    assert b"auth=Bearer " in decoded


def test_build_xoauth2_string_unicode():
    """Unicode email addresses are handled correctly."""
    result = oauth2.build_xoauth2_string("user@exämple.com", "tok")
    decoded = base64.b64decode(result)
    assert b"user=user@ex" in decoded


# ── token persistence ───────────────────────────────────────────────────────


def test_save_and_load_token():
    """Round-trip: save_token → load_token returns the same data."""
    token_data = {
        "access_token": "ya29.a0AfH6SMBx",
        "refresh_token": "1//0gGx",
        "expires_at": 9999999999,
        "provider": "microsoft",
    }
    oauth2.save_token("test-roundtrip", token_data)
    loaded = oauth2.load_token("test-roundtrip")
    assert loaded is not None
    assert loaded["access_token"] == "ya29.a0AfH6SMBx"
    assert loaded["provider"] == "microsoft"
    assert loaded["expires_at"] == 9999999999


def test_save_token_chmod_600():
    """Token file is created with 0600 permissions."""
    oauth2.save_token("test-perms", {
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 0,
        "provider": "microsoft",
    })
    path = oauth2._token_path("test-perms")
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_load_token_nonexistent():
    """Loading a nonexistent token returns None."""
    assert oauth2.load_token("does-not-exist") is None


def test_load_token_corrupt_json(tmp_path, monkeypatch):
    """Corrupt JSON file returns None (doesn't crash)."""
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    path = tokens_dir / "corrupt.json"
    path.write_text("not valid json {{{")
    monkeypatch.setattr(oauth2, "TOKENS_DIR", tokens_dir)
    assert oauth2.load_token("corrupt") is None


def test_load_token_missing_access_token(tmp_path, monkeypatch):
    """JSON without access_token returns None."""
    tokens_dir = tmp_path / "tokens"
    tokens_dir.mkdir()
    path = tokens_dir / "noaccess.json"
    path.write_text(json.dumps({"refresh_token": "r"}))
    monkeypatch.setattr(oauth2, "TOKENS_DIR", tokens_dir)
    assert oauth2.load_token("noaccess") is None


def test_delete_token():
    """delete_token removes the file and returns True."""
    oauth2.save_token("to-delete", {
        "access_token": "a",
        "refresh_token": "r",
        "expires_at": 0,
        "provider": "microsoft",
    })
    assert oauth2.delete_token("to-delete") is True
    assert oauth2.load_token("to-delete") is None


def test_delete_token_nonexistent():
    """Deleting a nonexistent token returns False."""
    assert oauth2.delete_token("never-existed") is False


# ── get_valid_access_token ──────────────────────────────────────────────────


def test_get_valid_access_token_fresh():
    """Fresh token is returned without refresh."""
    future = int(time.time()) + 7200
    oauth2.save_token("fresh", {
        "access_token": "fresh_tok",
        "refresh_token": "r",
        "expires_at": future,
        "provider": "microsoft",
    })
    assert oauth2.get_valid_access_token("fresh") == "fresh_tok"


def test_get_valid_access_token_no_token_raises():
    """Missing token raises MailProxyError."""
    with pytest.raises(MailProxyError, match="No OAuth2 token stored"):
        oauth2.get_valid_access_token("nonexistent")


def test_get_valid_access_token_expired_no_refresh_raises():
    """Expired token without refresh_token raises MailProxyError."""
    past = int(time.time()) - 3600
    oauth2.save_token("expired", {
        "access_token": "old_tok",
        "refresh_token": "",
        "expires_at": past,
        "provider": "microsoft",
    })
    with pytest.raises(MailProxyError, match="no refresh_token"):
        oauth2.get_valid_access_token("expired")


def test_get_valid_access_token_no_provider_raises():
    """Token without provider field raises MailProxyError."""
    oauth2.save_token("noprovider", {
        "access_token": "tok",
        "refresh_token": "r",
        "expires_at": 0,
        "provider": "",
    })
    with pytest.raises(MailProxyError, match="missing 'provider'"):
        oauth2.get_valid_access_token("noprovider")


# ── _provider_config ────────────────────────────────────────────────────────


def test_provider_config_microsoft():
    """Microsoft uses the well-known Thunderbird client_id."""
    cfg = oauth2._provider_config("microsoft")
    assert cfg["client_id"] == "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
    assert cfg["client_secret"] == ""
    assert "login.microsoftonline.com" in cfg["token_url"]


def test_provider_config_unknown_raises():
    """Unknown provider raises MailProxyError."""
    with pytest.raises(MailProxyError, match="Unknown OAuth2 provider"):
        oauth2._provider_config("yahoo")


def test_provider_config_google_requires_env(monkeypatch):
    """Google OAuth2 requires client_id/secret from env."""
    monkeypatch.delenv("MAIL_OAUTH2_GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("MAIL_OAUTH2_GOOGLE_CLIENT_SECRET", raising=False)
    with pytest.raises(MailProxyError, match="MAIL_OAUTH2_GOOGLE_CLIENT_ID"):
        oauth2._provider_config("google")


def test_provider_config_google_with_env(tmp_path, monkeypatch):
    """Google OAuth2 loads credentials from env."""
    monkeypatch.setenv("MAIL_OAUTH2_GOOGLE_CLIENT_ID", "gcid")
    monkeypatch.setenv("MAIL_OAUTH2_GOOGLE_CLIENT_SECRET", "gsec")
    # Patch the config.ENV_PATH to a non-existent file so load_env doesn't read real .env
    monkeypatch.setattr("mail_proxy.config.ENV_PATH", tmp_path / "nonexistent.env")
    cfg = oauth2._provider_config("google")
    assert cfg["client_id"] == "gcid"
    assert cfg["client_secret"] == "gsec"


# ── OAUTH2_PROVIDER_MAP ─────────────────────────────────────────────────────


def test_oauth2_provider_map_coverage():
    """Known OAuth2 domains are mapped."""
    assert oauth2.OAUTH2_PROVIDER_MAP["outlook.com"] == "microsoft"
    assert oauth2.OAUTH2_PROVIDER_MAP["hotmail.com"] == "microsoft"
    assert oauth2.OAUTH2_PROVIDER_MAP["live.com"] == "microsoft"
    assert oauth2.OAUTH2_PROVIDER_MAP["gmail.com"] == "google"


# ── config integration ─────────────────────────────────────────────────────


def test_account_def_auth_method_default():
    """Default auth_method is 'password'."""
    from mail_proxy.config import AccountDef, ImapEndpoint, SmtpEndpoint

    a = AccountDef(
        id="t", email="a@b.com",
        imap=ImapEndpoint(host="i"), smtp=SmtpEndpoint(host="s"),
    )
    assert a.auth_method == "password"
    assert a.oauth2_provider == ""


def test_account_def_auth_method_oauth2():
    """OAuth2 auth_method and provider are stored."""
    from mail_proxy.config import AccountDef, ImapEndpoint, SmtpEndpoint

    a = AccountDef(
        id="t", email="a@b.com",
        imap=ImapEndpoint(host="i"), smtp=SmtpEndpoint(host="s"),
        auth_method="oauth2", oauth2_provider="microsoft",
    )
    assert a.auth_method == "oauth2"
    assert a.oauth2_provider == "microsoft"


def test_config_ensure_env_oauth2_no_token_raises(tmp_path):
    """ensure_env for OAuth2 account without token raises."""
    from mail_proxy import config

    accounts = [{"id": "o", "email": "a@outlook.com", "auth_method": "oauth2", "oauth2_provider": "microsoft"}]
    config.ACCOUNTS_JSON_PATH.write_text(json.dumps(accounts))
    config._accounts_cache = []
    config.write_env({})
    config.load_env()
    with pytest.raises(MailProxyError, match="no stored token"):
        config.ensure_env("o")


def test_config_write_accounts_json_auth_method():
    """auth_method and oauth2_provider are persisted to accounts.json."""
    from mail_proxy import config

    accounts = [
        config.AccountDef(
            id="a", email="a@b.com",
            imap=config.ImapEndpoint(host="i"),
            smtp=config.SmtpEndpoint(host="s"),
            auth_method="oauth2",
            oauth2_provider="google",
        )
    ]
    config.write_accounts_json(accounts)
    raw = json.loads(config.ACCOUNTS_JSON_PATH.read_text())
    assert raw[0]["auth_method"] == "oauth2"
    assert raw[0]["oauth2_provider"] == "google"


def test_config_write_accounts_json_default_password_omitted():
    """Default auth_method 'password' is omitted from JSON (backward compat)."""
    from mail_proxy import config

    accounts = [
        config.AccountDef(
            id="b", email="b@b.com",
            imap=config.ImapEndpoint(host="i"),
            smtp=config.SmtpEndpoint(host="s"),
        )
    ]
    config.write_accounts_json(accounts)
    raw = json.loads(config.ACCOUNTS_JSON_PATH.read_text())
    assert "auth_method" not in raw[0]
    assert "oauth2_provider" not in raw[0]


def test_resolve_account_oauth2_empty_password():
    """OAuth2 accounts have empty password (XOAUTH2 used instead)."""
    from mail_proxy import config

    accounts = [
        config.AccountDef(
            id="o2", email="a@outlook.com",
            imap=config.ImapEndpoint(host="i"),
            smtp=config.SmtpEndpoint(host="s"),
            auth_method="oauth2",
            oauth2_provider="microsoft",
        )
    ]
    config.write_accounts_json(accounts)
    resolved = config.get_account("o2")
    assert resolved.password == ""
    assert resolved.auth_method == "oauth2"
