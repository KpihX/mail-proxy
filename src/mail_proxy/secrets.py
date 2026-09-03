"""
Secret cache for custom accounts — system keyring + TTL timestamp (sudo-like).

Architecture (KπX directive 2026-09-01):

  - Google / Microsoft accounts: app password in `.env` or OAuth2 token in
    `tokens/<id>.json` — handled elsewhere, NOT here.
  - Custom accounts (Zimbra, self-hosted, …): the password is NEVER stored on
    disk in plaintext. It is requested on first use (``getpass``, like `sudo`),
    verified against IMAP, then cached in the SYSTEM KEYRING (Secret Service /
    gnome-keyring) with a TTL timestamp.

Under the hood (sudo-like):
    1. First ``do`` → no cached secret (or TTL expired) → prompt via ``getpass``.
    2. Verify against IMAP.
    3. On success → store the secret in the system keyring + an ``.expiry``
        timestamp key (default TTL 1200s = 20 min).
    4. Subsequent ``do`` within TTL → read the keyring silently, no prompt.
    5. After TTL → re-prompt.

The secret is AES-encrypted by gnome-keyring (we never see the key), and the
keyring auto-locks when the session/ screen locks — exactly like `sudo`
invalidates its cache on lock.
"""

from __future__ import annotations

import getpass
import logging
import time

from .exceptions import MailProxyError

logger = logging.getLogger(__name__)

SERVICE = "mail-proxy"
DEFAULT_TTL = 1200  # seconds — 20 minutes


def _get_keyring() -> object:
    """Return the keyring backend, raising a clear error when unavailable.

    Returns:
        object: The keyring module (imported lazily so a missing dependency
        only breaks the custom flow, never Google/Microsoft).

    Raises:
        MailProxyError: When the keyring dependency is not installed.

    Examples:
        >>> _get_keyring()  # doctest: +SKIP
        <module 'keyring' ...>
    """
    try:
        import keyring  # type: ignore
    except ImportError as exc:
        raise MailProxyError(
            "The system keyring backend is required for custom accounts. "
            "Install it with: uv add keyring"
        ) from exc
    return keyring


def cache_ttl() -> float:
    """Return the cache TTL in seconds (default 1200, overridable via MAIL_CACHE_TTL).

    Returns:
        float: The TTL value.

    Examples:
        >>> cache_ttl()
        1200.0
        >>> cache_ttl()   # with MAIL_CACHE_TTL=1800
        1800.0
    """
    import os

    raw = os.environ.get("MAIL_CACHE_TTL")
    try:
        return float(raw) if raw else DEFAULT_TTL
    except ValueError:
        return DEFAULT_TTL


def get_cached_password(account_id: str) -> str | None:
    """Return the cached password for an account, or None if absent/expired.

    Args:
        account_id (str): Account id, e.g. `zimbra`.

    Returns:
        str | None: The password if present AND still within TTL, else None.

    Examples:
        >>> get_cached_password("zimbra") is None
        True
        >>> get_cached_password("zimbra")  # within TTL after a successful login
        'secret-password'
    """
    keyring = _get_keyring()
    try:
        expiry = keyring.get_password(SERVICE, f"{account_id}.expiry")
        if not expiry:
            return None
        if float(expiry) <= time.time():
            return None
        return keyring.get_password(SERVICE, account_id)
    except Exception:  # noqa: BLE001 - keyring backend can raise many platform errors
        return None


def set_cached_password(account_id: str, password: str) -> None:
    """Store a password in the system keyring with a TTL expiry timestamp.

    Args:
        account_id (str): Account id.
        password (str): The password to cache (never written to disk plaintext).

    Returns:
        None.

    Examples:
        >>> set_cached_password("zimbra", "s3cret")  # stores + sets expiry
    """
    keyring = _get_keyring()
    keyring.set_password(SERVICE, account_id, password)
    keyring.set_password(
        SERVICE, f"{account_id}.expiry", str(time.time() + cache_ttl())
    )


def clear_cached_password(account_id: str) -> None:
    """Remove a cached password (and its expiry) from the keyring.

    Args:
        account_id (str): Account id.

    Returns:
        None.

    Examples:
        >>> clear_cached_password("zimbra")  # idempotent
    """
    keyring = _get_keyring()
    for key in (account_id, f"{account_id}.expiry"):
        try:
            keyring.delete_password(SERVICE, key)
        except Exception:
            logger.debug("Failed to delete keyring key %s: %s", key, exc_info=True)


def prompt_password(account_id: str, email: str) -> str:
    """Prompt for the account password on the terminal (echo disabled, like sudo).

    Args:
        account_id (str): Account id shown in the prompt.
        email (str): Full e-mail address shown as context.

    Returns:
        str: The entered password.

    Raises:
        MailProxyError: When the prompt returns empty.

    Examples:
        >>> # Interactive — prompts "Password for zimbra (user@host): "
        >>> prompt_password("zimbra", "user@host")
        'secret'
    """
    password = getpass.getpass(f"Password for {account_id} ({email}): ")
    if not password:
        raise MailProxyError(f"No password entered for {account_id!r}.")
    return password
