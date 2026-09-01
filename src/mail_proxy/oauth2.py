"""OAuth2 support for mail-proxy — token lifecycle, browser flow, XOAUTH2.

Provides OAuth2 authentication for IMAP (via XOAUTH2) and SMTP (via AUTH
XOAUTH2) with automatic token refresh. Two providers are supported:

  - **Microsoft** (Outlook/Hotmail/Live): public client (Thunderbird's
    well-known client_id, no secret needed). IMAP Basic Auth is disabled
    by Microsoft — OAuth2 is mandatory for IMAP.
  - **Google** (Gmail): requires a custom OAuth2 client_id + secret from
    Google Cloud Console. App passwords still work but OAuth2 is the
    modern standard.

Token storage:
  Tokens are persisted as JSON files at
  ``~/.config/mail-proxy/tokens/<account_id>.json`` with chmod 600. Each
  file contains ``access_token``, ``refresh_token``, ``expires_at``
  (unix timestamp), and ``provider``.

Authorization flow:
  ``start_oauth2_flow()`` starts a local HTTP server on a random port,
  opens the browser to the provider's authorization URL, and waits for
  the callback with the authorization code. The code is exchanged for
  tokens and stored to disk.

XOAUTH2:
  ``build_xoauth2_string()`` builds the RFC-compatible base64-encoded
  XOAUTH2 string used by both IMAP ``AUTHENTICATE XOAUTH2`` and SMTP
  ``AUTH XOAUTH2``.

Examples:
    >>> build_xoauth2_string("user@example.com", "tok123")
    'dXNlcj11c2VyQGV4YW1wbGUuY29tAWF1dGg9QmVhcmVyIHRvazEyMwEB'
    >>> load_token("nonexistent") is None
    True
    >>> save_token("test", {"access_token": "a", "refresh_token": "r", "expires_at": 0, "provider": "microsoft"})
    >>> load_token("test")["access_token"]
    'a'
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import CONFIG_DIR, FILE_PERMISSIONS
from .exceptions import MailProxyError

logger = logging.getLogger(__name__)

# ── Provider configurations ──────────────────────────────────────────────────

OAUTH2_PROVIDERS: dict[str, dict[str, str]] = {
    "microsoft": {
        "auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": (
            "https://outlook.office.com/IMAP.AccessAsUser.All "
            "https://outlook.office.com/SMTP.Send offline_access"
        ),
        "client_id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
        "client_secret": "",
    },
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": "https://mail.google.com/",
    },
}

# ── Provider mapping from email domains ──────────────────────────────────────

OAUTH2_PROVIDER_MAP: dict[str, str] = {
    "outlook.com": "microsoft",
    "hotmail.com": "microsoft",
    "live.com": "microsoft",
    "gmail.com": "google",
}

# ── Token storage ────────────────────────────────────────────────────────────

TOKENS_DIR = CONFIG_DIR / "tokens"


def _provider_config(provider: str) -> dict[str, str]:
    """Return the OAuth2 provider config, resolving client_id/secret from env for Google.

    Args:
        provider (str): Provider name — ``"microsoft"`` or ``"google"``.

    Returns:
        dict[str, str]: Complete provider config with auth_url, token_url,
        scopes, client_id, client_secret.

    Raises:
        MailProxyError: When the provider is unknown or Google credentials
        are missing from .env.

    Examples:
        >>> _provider_config("microsoft")["client_id"]
        '9e5f94bc-e8a4-4e73-b8be-63364c29d753'
        >>> _provider_config("google")  # with env set
        {'auth_url': '...', 'token_url': '...', ...}
        >>> _provider_config("unknown")
        Traceback (most recent call last):
        ...
        mail_proxy.exceptions.MailProxyError: Unknown OAuth2 provider: unknown
    """
    if provider not in OAUTH2_PROVIDERS:
        raise MailProxyError(
            f"Unknown OAuth2 provider: {provider!r}. "
            f"Supported: {', '.join(sorted(OAUTH2_PROVIDERS))}."
        )
    cfg = dict(OAUTH2_PROVIDERS[provider])
    if provider == "google":
        from .config import load_env

        load_env()
        client_id = os.environ.get("MAIL_OAUTH2_GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("MAIL_OAUTH2_GOOGLE_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise MailProxyError(
                "Google OAuth2 requires MAIL_OAUTH2_GOOGLE_CLIENT_ID and "
                "MAIL_OAUTH2_GOOGLE_CLIENT_SECRET in ~/.config/mail-proxy/.env. "
                "Create them at https://console.cloud.google.com/apis/credentials."
            )
        cfg["client_id"] = client_id
        cfg["client_secret"] = client_secret
    return cfg


# ── Token persistence ────────────────────────────────────────────────────────


def _token_path(account_id: str) -> Path:
    """Return the file path for an account's OAuth2 token.

    Args:
        account_id (str): The account identifier.

    Returns:
        Path: ``~/.config/mail-proxy/tokens/<account_id>.json``.

    Examples:
        >>> str(_token_path("poly")).endswith("tokens/poly.json")
        True
        >>> str(_token_path("work")).endswith("tokens/work.json")
        True
    """
    return TOKENS_DIR / f"{account_id}.json"


def load_token(account_id: str) -> dict[str, Any] | None:
    """Load a stored OAuth2 token for an account.

    Args:
        account_id (str): The account identifier.

    Returns:
        dict[str, Any] | None: Token dict with access_token, refresh_token,
        expires_at, provider — or None when no token exists.

    Examples:
        >>> save_token("t1", {"access_token": "a", "refresh_token": "r", "expires_at": 100, "provider": "microsoft"})
        >>> load_token("t1")["access_token"]
        'a'
        >>> load_token("nonexistent") is None
        True
    """
    path = _token_path(account_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "access_token" in data:
            return data
        return None
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load OAuth2 token for %s: %s", account_id, exc)
        return None


def save_token(account_id: str, token_data: dict[str, Any]) -> None:
    """Persist an OAuth2 token to disk (chmod 600).

    Args:
        account_id (str): The account identifier.
        token_data (dict[str, Any]): Must contain at least ``access_token``,
        ``refresh_token``, ``expires_at`` (unix timestamp), and ``provider``.

    Returns:
        None: Writes the token file.

    Examples:
        >>> save_token("x", {"access_token": "a", "refresh_token": "r", "expires_at": 0, "provider": "microsoft"})
        >>> _token_path("x").exists()
        True
        >>> _token_path("x").stat().st_mode & 0o777
        384
    """
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_DIR.chmod(0o700)
    path = _token_path(account_id)
    path.write_text(
        json.dumps(token_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    path.chmod(FILE_PERMISSIONS)
    logger.info("OAuth2 token saved for account %s", account_id)


def delete_token(account_id: str) -> bool:
    """Remove a stored OAuth2 token.

    Args:
        account_id (str): The account identifier.

    Returns:
        bool: True if a file was deleted, False if it didn't exist.

    Examples:
        >>> save_token("del", {"access_token": "a", "refresh_token": "r", "expires_at": 0, "provider": "microsoft"})
        >>> delete_token("del")
        True
        >>> delete_token("del")
        False
    """
    path = _token_path(account_id)
    if path.exists():
        path.unlink()
        return True
    return False


# ── Token refresh ────────────────────────────────────────────────────────────


def refresh_access_token(
    provider: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> dict[str, Any]:
    """Exchange a refresh_token for a new access_token.

    Args:
        provider (str): Provider name — ``"microsoft"`` or ``"google"``.
        refresh_token (str): The stored refresh token.
        client_id (str): OAuth2 client ID.
        client_secret (str): OAuth2 client secret ("" for public clients).

    Returns:
        dict[str, Any]: New token dict with ``access_token``,
        ``refresh_token``, ``expires_at``, ``provider``.

    Raises:
        MailProxyError: When the token endpoint rejects the refresh.

    Examples:
        >>> refresh_access_token("microsoft", "rt", "cid", "")
        Traceback (most recent call last):
        ...
        mail_proxy.exceptions.MailProxyError: Token refresh failed
        >>> # In real usage, a valid refresh_token returns:
        >>> # {"access_token": "...", "refresh_token": "...", "expires_at": 123456, "provider": "microsoft"}
    """
    cfg = _provider_config(provider)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if provider == "google":
        data["scope"] = cfg["scopes"]

    body = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in data.items())
    req = urllib.request.Request(
        cfg["token_url"],
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best-effort error body read
            logger.debug("Failed to read error body from %s", exc)
        raise MailProxyError(
            f"Token refresh failed for {provider}: HTTP {exc.code} — {error_body}"
        ) from exc
    except (OSError, TimeoutError) as exc:
        raise MailProxyError(
            f"Token refresh network error for {provider}: {exc}"
        ) from exc

    new_access = result.get("access_token", "")
    new_refresh = result.get("refresh_token", refresh_token)
    if "expires_at" in result:
        expires_at = result["expires_at"]
    elif "expires_in" in result:
        expires_at = int(time.time()) + result["expires_in"]
    else:
        expires_at = int(time.time()) + 3600

    if not new_access:
        raise MailProxyError(
            f"Token refresh returned no access_token for {provider}. "
            "Try re-authorizing with 'mail-proxy admin auth login'."
        )

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expires_at": expires_at,
        "provider": provider,
    }


# ── Auto-refresh ─────────────────────────────────────────────────────────────


def get_valid_access_token(account_id: str) -> str:
    """Return a valid (possibly refreshed) access_token for an account.

    Loads the stored token, checks expiry (with 60s buffer), refreshes if
    needed, saves the updated token, and returns the access_token.

    Args:
        account_id (str): The account identifier.

    Returns:
        str: A valid access token string.

    Raises:
        MailProxyError: When no token is stored, the provider is unknown,
        or the refresh fails.

    Examples:
        >>> # With a valid token on disk:
        >>> # get_valid_access_token("poly") → "ya29..." (refreshed if needed)
        >>> # Without a token:
        >>> get_valid_access_token("no-token-account")
        Traceback (most recent call last):
        ...
        mail_proxy.exceptions.MailProxyError: No OAuth2 token stored for 'no-token-account'
    """
    token = load_token(account_id)
    if token is None:
        raise MailProxyError(
            f"No OAuth2 token stored for {account_id!r}. "
            "Run 'mail-proxy admin auth login' to authorize."
        )

    expires_at = token.get("expires_at", 0)
    buffer_seconds = 60
    now = int(time.time())

    if now < (expires_at - buffer_seconds):
        return str(token["access_token"])

    # Token expired or about to expire — refresh
    provider = token.get("provider", "")
    refresh_token = token.get("refresh_token", "")
    if not provider:
        raise MailProxyError(
            f"OAuth2 token for {account_id!r} is missing 'provider'. "
            "Re-authorize with 'mail-proxy admin auth login'."
        )
    if not refresh_token:
        raise MailProxyError(
            f"OAuth2 token for {account_id!r} has no refresh_token. "
            "Re-authorize with 'mail-proxy admin auth login'."
        )

    cfg = _provider_config(provider)
    new_token = refresh_access_token(
        provider=provider,
        refresh_token=refresh_token,
        client_id=cfg["client_id"],
        client_secret=cfg.get("client_secret", ""),
    )
    save_token(account_id, new_token)
    logger.info("OAuth2 token refreshed for account %s", account_id)
    return str(new_token["access_token"])


# ── XOAUTH2 string builder ──────────────────────────────────────────────────


def build_xoauth2_string(username: str, access_token: str) -> str:
    """Build the RFC-compliant base64-encoded XOAUTH2 authentication string.

    The XOAUTH2 format (RFC draft) is:
    ``user=<email>\\x01auth=Bearer <token>\\x01\\x01``

    Args:
        username (str): The email address / login.
        access_token (str): A valid OAuth2 access token.

    Returns:
        str: Base64-encoded XOAUTH2 string for use with IMAP AUTHENTICATE
        XOAUTH2 or SMTP AUTH XOAUTH2.

    Examples:
        >>> build_xoauth2_string("user@example.com", "tok123")
        'dXNlcj11c2VyQGV4YW1wbGUuY29tAWF1dGg9QmVhcmVyIHRvazEyMwEB'
        >>> import base64
        >>> decoded = base64.b64decode(build_xoauth2_string("a@b.com", "xyz"))
        >>> b"user=a@b.com" in decoded
        True
        >>> b"auth=Bearer xyz" in decoded
        True
    """
    raw = f"user={username}\x01auth=Bearer {access_token}\x01\x01"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


# ── Browser-based authorization flow ─────────────────────────────────────────


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth2 authorization code from the callback."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:
        """Handle the OAuth2 callback with ?code= or ?error= parameters.

        Examples:
            >>> # GET /callback?code=abc123 → captures code
            >>> # GET /callback?error=access_denied → captures error
        """
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "error" in params:
            _OAuthCallbackHandler.error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authorization failed</h2>"
                b"<p>You can close this window.</p></body></html>"
            )
            return

        if "code" in params:
            _OAuthCallbackHandler.code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authorization successful!</h2>"
                b"<p>You can close this window and return to the terminal.</p>"
                b"</body></html>"
            )
            return

        self.send_error(400, "Missing authorization code.")

    def log_message(self, format: str, *args: Any) -> None:
        """Silence access logs."""
        return


def start_oauth2_flow(provider: str, account_id: str) -> dict[str, Any]:
    """Run the OAuth2 authorization flow and return tokens.

    For Microsoft: Device Code Flow (no redirect URI — user goes to
    microsoft.com/devicelogin and enters a code).
    For Google: Authorization Code Flow with local HTTP redirect.

    Args:
        provider (str): Provider name — ``"microsoft"`` or ``"google"``.
        account_id (str): Account identifier (used for display).

    Returns:
        dict[str, Any]: Token data with access_token, refresh_token,
        expires_at, provider — ready for ``save_token()``.

    Raises:
        MailProxyError: When the flow fails.

    Examples:
        >>> # In real usage, this opens the browser and waits:
        >>> # token = start_oauth2_flow("microsoft", "work")
        >>> # save_token("work", token)
        >>> # Returns: {"access_token": "ya29...", "refresh_token": "1//0g...", ...}
    """
    cfg = _provider_config(provider)

    if provider == "microsoft":
        return _device_code_flow(cfg, account_id)
    return _auth_code_flow(cfg, account_id)


def _device_code_flow(cfg: dict[str, str], account_id: str) -> dict[str, Any]:
    """Microsoft Device Code Flow — no redirect URI needed.

    Args:
        cfg (dict[str, str]): Provider config.
        account_id (str): Account identifier.

    Returns:
        dict[str, Any]: Token data.

    Raises:
        MailProxyError: On failure.

    Examples:
        >>> # Opens browser to microsoft.com/devicelogin, polls for consent
    """
    device_code_url = cfg["token_url"].replace("/token", "/devicecode")
    data = urllib.parse.urlencode(
        {"client_id": cfg["client_id"], "scope": cfg["scopes"]}
    ).encode()

    try:
        req = urllib.request.Request(
            device_code_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:
        raise MailProxyError(f"Failed to start device code flow: {exc}") from exc

    user_code = result.get("user_code", "")
    verification_uri = result.get("verification_uri", "")
    interval = int(result.get("interval", 5))
    expires_in = int(result.get("expires_in", 900))

    print(f"\n🔑 [OAuth2] Authorizing {account_id} with Microsoft", file=sys.stderr)
    print(f"🔗 Go to: {verification_uri}", file=sys.stderr)
    print(f"📝 Enter code: {user_code}", file=sys.stderr)
    print("⏳ Waiting for you to authorize in the browser...", file=sys.stderr)

    try:
        webbrowser.open(verification_uri)
    except OSError:
        logger.warning("Failed to open browser: %s", verification_uri)

    # Poll token endpoint
    token_body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": cfg["client_id"],
            "device_code": result["device_code"],
        }
    ).encode()

    start_time = time.time()
    while time.time() - start_time < expires_in:
        time.sleep(interval)
        try:
            req = urllib.request.Request(
                cfg["token_url"],
                data=token_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                token = json.loads(resp.read().decode())
                if "access_token" in token:
                    return {
                        "access_token": token["access_token"],
                        "refresh_token": token.get("refresh_token", ""),
                        "expires_at": int(time.time()) + token.get("expires_in", 3600),
                        "provider": "microsoft",
                    }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                err = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                err = {"error": body}
            error = err.get("error", "")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error == "authorization_declined":
                raise MailProxyError("Authorization was declined by the user.")
            if error == "expired_token":
                raise MailProxyError("Device code expired. Please try again.")
            raise MailProxyError(
                f"OAuth2 error: {error} — {err.get('error_description', '')}"
            )
        except Exception as exc:
            raise MailProxyError(f"OAuth2 token poll failed: {exc}") from exc

    raise MailProxyError("OAuth2 device code flow timed out.")


def _auth_code_flow(cfg: dict[str, str], account_id: str) -> dict[str, Any]:
    """Google Authorization Code Flow with local HTTP redirect.

    Args:
        cfg (dict[str, str]): Provider config.
        account_id (str): Account identifier.

    Returns:
        dict[str, Any]: Token data.

    Raises:
        MailProxyError: On failure.

    Examples:
        >>> # Opens browser, waits for redirect, exchanges code for tokens
    """
    server = None
    try:
        server = HTTPServer(("127.0.0.1", 0), _OAuthCallbackHandler)
        port = server.server_address[1]
        redirect_uri = f"http://localhost:{port}/callback"

        _OAuthCallbackHandler.code = None
        _OAuthCallbackHandler.error = None

        auth_url = (
            f"{cfg['auth_url']}"
            f"?client_id={cfg['client_id']}"
            f"&response_type=code"
            f"&redirect_uri={redirect_uri}"
            f"&scope={cfg['scopes'].replace(' ', '%20')}"
            f"&response_mode=query"
            f"&access_type=offline"
            f"&prompt=consent"
        )

        event = threading.Event()

        def _wait_for_code() -> None:
            server.handle_request()
            event.set()

        thread = threading.Thread(target=_wait_for_code, daemon=True)
        thread.start()

        print(f"\n🔑 [OAuth2] Authorizing {account_id} with Google", file=sys.stderr)
        print("🔗 Open this URL if the browser doesn't open:", file=sys.stderr)
        print(f"   {auth_url}", file=sys.stderr)

        try:
            webbrowser.open(auth_url)
        except OSError:
            logger.warning("Failed to open browser for OAuth2: %s", auth_url)

        if not event.wait(timeout=300):
            raise MailProxyError(
                "OAuth2 flow timed out (300s). Try again or use an app password."
            )

        if _OAuthCallbackHandler.error:
            raise MailProxyError(
                f"OAuth2 authorization denied: {_OAuthCallbackHandler.error}. "
                "Try again or use an app password as fallback."
            )

        code = _OAuthCallbackHandler.code
        if not code:
            raise MailProxyError(
                "OAuth2 flow did not receive an authorization code. "
                "Try again or use an app password as fallback."
            )

        # Exchange code for tokens
        token_data = urllib.parse.urlencode(
            {
                "code": code,
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        ).encode()

        req = urllib.request.Request(
            cfg["token_url"],
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())

        if "error" in result:
            raise MailProxyError(
                f"Token exchange failed: {result['error']} — {result.get('error_description', '')}"
            )

        return {
            "access_token": result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "expires_at": int(time.time()) + result.get("expires_in", 3600),
            "provider": "google",
        }

    except MailProxyError:
        raise
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            error_body = ""
        raise MailProxyError(
            f"OAuth2 token exchange failed: HTTP {exc.code} — {error_body}"
        ) from exc
    except (OSError, TimeoutError) as exc:
        raise MailProxyError(f"OAuth2 flow network error: {exc}") from exc
    finally:
        if server:
            server.server_close()
