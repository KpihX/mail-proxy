"""Shared fixtures — provides a test accounts.json for every test."""

import json

import pytest

from mail_proxy import config
from mail_proxy.actions import signatures as sig_actions
from mail_proxy.api import smtp

TEST_ACCOUNTS = [
    {
        "id": "poly",
        "email": "user.name@polytechnique.edu",
        "display_name": "User NAME",
        "aliases": ["x", "polytechnique"],
        "default": True,
        "signatures": [
            {
                "id": "sig-poly001",
                "name": "Work",
                "before_logo": "User NAME",
                "image": "",
                "after_logo": "SCHOOL NAME",
            },
        ],
        "default_signature_id": "sig-poly001",
    },
    {
        "id": "outlook",
        "email": "user.name@outlook.com",
        "display_name": "User NAME",
        "aliases": ["work", "m365"],
    },
    {
        "id": "gmail",
        "email": "user.name@gmail.com",
        "display_name": "User NAME",
        "aliases": ["google"],
    },
]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point config at a temporary directory and seed a test accounts.json."""
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(config, "ACCOUNTS_JSON_PATH", tmp_path / "accounts.json")
    (tmp_path / "accounts.json").write_text(json.dumps(TEST_ACCOUNTS, indent=2))
    monkeypatch.setattr(config, "_accounts_cache", [])
    # SIGNATURES_DIR is computed at import time in config.py (and re-bound
    # at import in api/smtp.py and actions/signatures.py), so patching
    # CONFIG_DIR alone does NOT redirect signature image writes. Patch all
    # three bindings so tests write to tmp, never to the real install.
    sig_dir = tmp_path / "assets" / "signatures"
    monkeypatch.setattr(config, "SIGNATURES_DIR", sig_dir)
    monkeypatch.setattr(smtp, "SIGNATURES_DIR", sig_dir)
    monkeypatch.setattr(sig_actions, "SIGNATURES_DIR", sig_dir)
    for key in list(__import__("os").environ):
        if key.startswith("MAIL_"):
            monkeypatch.delenv(key, raising=False)
    yield
