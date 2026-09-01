"""Shared fixtures — provides a test accounts.json for every test."""

import json

import pytest

from mail_proxy import config

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
    for key in list(__import__("os").environ):
        if key.startswith("MAIL_"):
            monkeypatch.delenv(key, raising=False)
    yield
