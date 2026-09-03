"""Keyring cache TTL defaults."""

from mail_proxy.secrets import DEFAULT_TTL, cache_ttl


def test_cache_ttl_default_is_twenty_minutes(monkeypatch):
    monkeypatch.delenv("MAIL_CACHE_TTL", raising=False)
    assert DEFAULT_TTL == 1200
    assert cache_ttl() == 1200.0
