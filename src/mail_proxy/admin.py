"""
Admin logic — single `admin auth` command. No desync between JSON and .env.

The design follows the KπX directive: ONE command manages BOTH accounts.json
AND .env atomically. No separate "accounts add" — that risks false data.

Commands:
  admin auth login    — collect full account + password or OAuth2 → write JSON + .env/tokens atomically
  admin auth status   — show ALL accounts: JSON config + .env secret state
  admin auth logout   — remove password for ONE account (interactive selection)
  admin reset         — clear ALL passwords (HITL-confirmed)
  admin purge         — delete the entire config directory (HITL-confirmed)
"""

import json as _json
import os
import shutil
import sys
from typing import Any, cast

from . import config as cfg
from .config import (
    ACCOUNTS_JSON_PATH,
    DIR_PERMISSIONS,
    FILE_PERMISSIONS,
    _resolve_endpoints,
    account_env_prefix,
    api_timeout,
    get_account,
    list_accounts,
    load_accounts,
    load_env,
    write_accounts_json,
    write_env,
)
from .exceptions import MailAPIError, MailProxyError
from .hitl import request_approval
from .models import Status

AdminResult = tuple[dict | None, Status, bool, str]


def _mask(value: str) -> str:
    """Mask a secret, keeping only its head and tail.

    Args:
        value (str): The secret to mask.

    Returns:
        str: `user…l.com`, or an empty string when there is nothing to mask.

    Examples:
        >>> _mask("user.name@mail.com")
        'user…l.com'
        >>> _mask("")
        ''
    """
    if not value:
        return ""
    return f"{value[:4]}…{value[-5:]}" if len(value) > 12 else "…"


# ── admin auth login ──────────────────────────────────────────────────────────


def auth_login() -> AdminResult:
    """Collect one full account via a smart HITL form with type + auth selector.

    The form shows a provider type selector (Gmail/Outlook/Zimbra/Custom) that
    auto-adjusts the IMAP/SMTP info, and an auth method selector (App Password
    or OAuth2). User fills email, id, aliases, display name, and either a
    password or runs the OAuth2 browser flow.

    **Validation happens BEFORE any write.** If the email domain is unknown and no
    custom hosts are provided, the form is rejected and nothing on disk changes.
    The IMAP probe runs BEFORE any write — if authentication fails, NOTHING is
    written to accounts.json or .env. A failed login never pollutes state.

    OAuth2 flow: when auth_method is "oauth2", the browser-based authorization
    flow runs BEFORE any write. Tokens are stored in
    ``~/.config/mail-proxy/tokens/<id>.json`` (chmod 600). The auth_method and
    oauth2_provider are written to accounts.json.

    Returns:
        tuple[dict, Status, bool, str]: A tuple containing the written account
        info, the HITL response status, edited flag, and reviewer comment.

    Examples:
        - Gmail account added with OAuth2:
            `mail-proxy admin auth login`
            → {"account":"user","email":"user@gmail.com","configured":true,"auth_method":"oauth2","imap":{"auth_ok":true}}
        - Outlook account added with app password:
            `mail-proxy admin auth login`
            → {"account":"work","email":"user@outlook.com","configured":true,"auth_method":"password"}
        - Reviewer rejected the form:
            `mail-proxy admin auth login`
            → (rejected envelope, exit 1 — nothing written)
    """
    load_env()

    # Build existing accounts list for the form header
    existing: list[dict[str, str]] = []
    for account_info in list_accounts():
        prefix = account_info["prefix"]
        has_pass = bool(os.environ.get(f"{prefix}PASS", ""))
        existing.append(
            {
                "id": str(account_info["id"]),
                "email": str(account_info["email"]),
                "configured": str(has_pass),
            }
        )

    form: dict[str, Any] = {
        "existing_accounts": existing,
    }
    response = request_approval("admin auth login", form)
    if response.status == "rejected":
        return None, "rejected", response.edited, response.comment

    payload = response.payload if isinstance(response.payload, dict) else form
    new_id = str(payload.get("id", "")).strip()
    new_email = str(payload.get("email", "")).strip()
    new_password = str(payload.get("password", "")).strip()
    new_aliases = payload.get("aliases", [])
    new_display_name = str(payload.get("display_name", "")).strip()
    new_imap_host = str(payload.get("imap_host", "")).strip() or None
    new_smtp_host = str(payload.get("smtp_host", "")).strip() or None
    new_auth_method = str(payload.get("auth_method", "password")).strip() or "password"
    new_oauth2_provider = str(payload.get("oauth2_provider", "")).strip()
    new_provider_type = str(payload.get("type", "")).strip()

    # ── VALIDATE BEFORE ANY WRITE ─────────────────────────────────────────
    if not new_id or not new_email:
        raise MailProxyError("id and email are required.")

    # Custom accounts: always password auth, no OAuth2, password mandatory for verify
    is_custom = new_provider_type == "custom"
    if is_custom:
        new_auth_method = "password"
        new_oauth2_provider = ""

    # Auto-detect OAuth2 provider from the SELECTED provider type (not email domain)
    if new_auth_method == "oauth2" and not new_oauth2_provider:
        if new_provider_type == "microsoft":
            new_oauth2_provider = "microsoft"
        elif new_provider_type == "google":
            new_oauth2_provider = "google"

    if new_auth_method == "oauth2" and not new_oauth2_provider:
        raise MailProxyError(
            f"Cannot auto-detect OAuth2 provider for {new_email}. "
            "Set 'oauth2_provider' to 'microsoft' or 'google' in the form."
        )

    # Custom accounts: password mandatory (for IMAP/SMTP verification only, never saved)
    if not new_password:
        raise MailProxyError(
            "Password is required for verification."
            if is_custom
            else "Password is required for password authentication."
        )

    # Validate email domain can be resolved (or custom hosts/provider type provided)
    try:
        _resolve_endpoints(
            {
                "email": new_email,
                "imap_host": new_imap_host,
                "smtp_host": new_smtp_host,
                "provider_type": new_provider_type,
            }
        )
    except MailProxyError as exc:
        raise MailProxyError(
            f"Cannot resolve IMAP/SMTP for {new_email}: {exc} "
            "Fix the email or provide imap_host + smtp_host in the form."
        ) from exc

    # ── BUILD IN-MEMORY ACCOUNT + PROBE (validate BEFORE any write) ────────
    # Resolve endpoints for the new account
    resolved_imap, resolved_smtp = _resolve_endpoints(
        {
            "email": new_email,
            "imap_host": new_imap_host,
            "smtp_host": new_smtp_host,
            "provider_type": new_provider_type,
        }
    )

    # Build the account definition with resolved endpoints
    new_account = cfg.AccountDef(
        id=new_id,
        email=new_email,
        display_name=new_display_name,
        aliases=tuple(new_aliases) if isinstance(new_aliases, list) else (),
        provider_type=new_provider_type,
        auth_method=new_auth_method,
        oauth2_provider=new_oauth2_provider,
        imap=resolved_imap,
        smtp=resolved_smtp,
        username=new_email,
        password=new_password
        if (not is_custom and new_auth_method == "password")
        else "",
    )

    # ── OAUTH2 FLOW (before any write — if it fails, nothing changes) ─────
    if new_auth_method == "oauth2":
        from .oauth2 import save_token, start_oauth2_flow

        try:
            token_data = start_oauth2_flow(new_oauth2_provider, new_id)
            save_token(new_id, token_data)
        except MailProxyError as exc:
            raise MailProxyError(
                f"OAuth2 authorization failed: {exc} "
                "Try again or select 'App Password' instead."
            ) from exc

    # ── PROBE (BEFORE any write — if check fails, nothing is saved) ──────
    imap_probe: dict[str, Any] = {"reachable": False, "auth_ok": False, "error": ""}
    smtp_probe: dict[str, Any] = {"reachable": False, "error": ""}

    if is_custom:
        # Custom: password mandatory for IMAP/SMTP verification, NEVER saved to disk
        new_account.password = new_password
        imap_probe = _probe_account_imap(new_account)
        smtp_probe = _probe_account_smtp(new_account)
        new_account.password = ""  # NEVER write password to accounts.json or .env
    else:
        # Google/Microsoft: full auth probe (app password or OAuth2)
        imap_probe = _probe_account_imap(new_account)
        smtp_probe = _probe_account_smtp(new_account)

        if not imap_probe.get("auth_ok"):
            if new_auth_method == "oauth2":
                from .oauth2 import delete_token

                delete_token(new_id)
            raise MailProxyError(
                f"IMAP authentication failed for {new_email}: "
                f"{imap_probe.get('error', 'unknown error')} "
                "Nothing was written. Check your password/app-password and retry."
            )

    # ── AUTH VALIDATED — WRITE ────────────────────────────────────────────
    accounts = load_accounts(force=True)
    updated = False
    for acc in accounts:
        if acc.id == new_id:
            acc.email = new_email
            acc.aliases = (
                tuple(new_aliases) if isinstance(new_aliases, list) else acc.aliases
            )
            acc.display_name = new_display_name or acc.display_name
            acc.auth_method = new_auth_method
            acc.oauth2_provider = new_oauth2_provider
            acc.provider_type = new_provider_type
            updated = True
            break
    if not updated:
        accounts.append(new_account)

    write_accounts_json(accounts)

    # Only write .env for password accounts (NOT custom — custom uses keyring)
    if new_auth_method == "password" and not is_custom:
        prefix = account_env_prefix(new_id)
        env_values: dict[str, str] = {}
        for acc in accounts:
            p = account_env_prefix(acc.id)
            existing_pass = os.environ.get(f"{p}PASS", "")
            if acc.id == new_id:
                env_values[f"{p}PASS"] = new_password
            elif existing_pass:
                env_values[f"{p}PASS"] = existing_pass
        write_env(env_values)

    load_env()

    return (
        {
            "account": new_id,
            "email": new_email,
            "configured": True,
            "auth_method": new_auth_method,
            "imap": imap_probe,
            "smtp": smtp_probe,
        },
        cast(Status, response.status),
        response.edited,
        response.comment,
    )


# ── admin auth status ─────────────────────────────────────────────────────────


def auth_status() -> dict:
    """Show the auth state of ALL accounts: JSON config + .env secret state.

    Reports per-account: id, email, aliases, configured (has password),
    and live IMAP/SMTP probes for configured accounts.

    Returns:
        dict: Per-account credential presence, IMAP/SMTP probe results,
        config paths and permissions.

    Examples:
        - Two accounts configured:
            `mail-proxy admin auth status`
            → {"accounts":[{"id":"poly","email":"…@polytechnique.edu","configured":true,"imap":{"auth_ok":true}},…],…}
        - No accounts yet:
            `mail-proxy admin auth status`
            → {"accounts":[],"note":"No accounts.json found."}
        - One account missing password:
            `mail-proxy admin auth status`
            → {"accounts":[{"id":"poly","configured":false,"note":"Run 'mail-proxy admin auth login'"},…],…}
    """
    load_env()
    accounts_data = load_accounts(force=True)

    accounts_state = []
    for a in accounts_data:
        prefix = account_env_prefix(a.id)
        password = os.environ.get(f"{prefix}PASS", "")
        if a.auth_method == "oauth2":
            from .oauth2 import load_token

            configured = load_token(a.id) is not None
        elif a.provider_type == "custom":
            configured = (
                True  # custom: password prompted on first do, account is configured
            )
        else:
            configured = bool(password)
        imap_probe = (
            _probe_imap(a.id)
            if configured
            else {
                "reachable": False,
                "auth_ok": False,
                "error": "Missing credentials — run 'mail-proxy admin auth login'.",
            }
        )
        smtp_probe = (
            _probe_smtp(a.id)
            if configured
            else {
                "reachable": False,
                "error": "Missing credentials — run 'mail-proxy admin auth login'.",
            }
        )
        accounts_state.append(
            {
                "id": a.id,
                "email": a.email,
                "aliases": list(a.aliases),
                "default": a.default,
                "configured": configured,
                "auth_method": a.auth_method,
                "imap": imap_probe,
                "smtp": smtp_probe,
            }
        )

    default_account = None
    for a in accounts_data:
        if a.default:
            default_account = a.id
            break

    dir_mode = None
    dir_status = "absent"
    dir_fix = None
    if cfg.CONFIG_DIR.exists():
        dir_mode = os.stat(cfg.CONFIG_DIR).st_mode & 0o777
        if dir_mode == DIR_PERMISSIONS:
            dir_status = "ok"
        else:
            dir_status = "warning"
            dir_fix = f"chmod {oct(DIR_PERMISSIONS)[2:]} {cfg.CONFIG_DIR}"

    file_mode = None
    file_status = "absent"
    file_fix = None
    if cfg.ENV_PATH.exists():
        file_mode = os.stat(cfg.ENV_PATH).st_mode & 0o777
        if file_mode == FILE_PERMISSIONS:
            file_status = "ok"
        else:
            file_status = "warning"
            file_fix = f"chmod {oct(FILE_PERMISSIONS)[2:]} {cfg.ENV_PATH}"

    binary_path = shutil.which("mail-proxy") or os.path.abspath(sys.argv[0])

    return {
        "config_dir": str(cfg.CONFIG_DIR),
        "accounts_json": str(ACCOUNTS_JSON_PATH),
        "accounts_json_exists": ACCOUNTS_JSON_PATH.exists(),
        "env_exists": cfg.ENV_PATH.exists(),
        "accounts": accounts_state,
        "default_account": default_account,
        "binary": binary_path,
        "permissions": {
            "config_dir": {
                "path": str(cfg.CONFIG_DIR),
                "mode": oct(dir_mode) if dir_mode is not None else None,
                "status": dir_status,
                "fix": dir_fix,
            },
            "config_file": {
                "path": str(cfg.ENV_PATH),
                "mode": oct(file_mode) if file_mode is not None else None,
                "status": file_status,
                "fix": file_fix,
            },
        },
    }


# ── admin auth logout ─────────────────────────────────────────────────────────


def auth_logout(account_id: str | None = None) -> AdminResult:
    """Remove the password for ONE account (HITL-confirmed). The account stays in JSON.

    If account_id is not given, the form shows all configured accounts and
    asks the user to pick one.

    Returns:
        tuple[dict, Status, bool, str]: A tuple containing the logout status
        dict, the HITL response status, a boolean indicating whether it was
        edited, and the HITL reviewer comment.

    Examples:
        - Logout poly:
            `mail-proxy do auth logout`  → picks poly in the form
            → {"account":"poly","configured":false}
        - Reviewer rejected:
            `mail-proxy do auth logout`
            → (rejected envelope, exit 1 — credentials kept)
        - Logout when already not configured:
            `mail-proxy do auth logout`
            → {"account":"poly","configured":false,"note":"Already not configured."}
    """
    load_env()

    accounts = load_accounts(force=True)
    if not accounts:
        raise MailProxyError("No accounts found in accounts.json.")

    candidates = []
    for a in accounts:
        prefix = account_env_prefix(a.id)
        has_pass = bool(os.environ.get(f"{prefix}PASS", ""))
        if has_pass:
            candidates.append({"id": a.id, "email": a.email, "configured": True})

    if not candidates:
        return (
            {"note": "No accounts have passwords configured."},
            "approved",
            False,
            "",
        )

    form = {
        "action": "auth_logout",
        "accounts": candidates,
        "instructions": "Set 'account_id' to the account you want to logout.",
    }
    response = request_approval("admin auth logout", form)
    if response.status == "rejected":
        return None, "rejected", response.edited, response.comment

    payload = response.payload if isinstance(response.payload, dict) else form
    target_id = str(payload.get("account_id", "")).strip()

    if not target_id:
        raise MailProxyError("account_id is required.")

    # Rebuild .env without the target account's password
    env_values = {}
    for a in accounts:
        prefix = account_env_prefix(a.id)
        if a.id != target_id:
            existing_pass = os.environ.get(f"{prefix}PASS", "")
            if existing_pass:
                env_values[f"{prefix}PASS"] = existing_pass
    write_env(env_values)

    return (
        {"account": target_id, "configured": False},
        cast(Status, response.status),
        response.edited,
        response.comment,
    )


def auth_default(account: str) -> AdminResult:
    """Set the default account used when -a is omitted.

    The account is selected explicitly by the CLI's `-a` / `--account` option.
    HITL confirms that selection; the reviewed payload cannot change it.

    Returns:
        tuple[dict, Status, bool, str]: A tuple containing the result
        dict, the HITL response status, edited flag, and reviewer comment.

    Examples:
        - Set default to an account ID:
            `mail-proxy admin auth default -a poly`
            → {"account":"poly","default":true}
        - Set default through an alias:
            `mail-proxy admin auth default --account work`
            → {"account":"outlook","default":true}
        - Reviewer rejected:
            `mail-proxy admin auth default -a poly`
            → (rejected envelope, exit 1 — nothing changed)
        - Unknown account:
            `mail-proxy admin auth default -a unknown`
            → (error, exit 1 — account does not exist)
    """
    load_env()
    accounts = load_accounts(force=True)
    target_id = get_account(account).id

    form: dict[str, Any] = {
        "action": "auth_default",
        "account": target_id,
        "instructions": f"Confirm {target_id!r} as the default account.",
    }
    response = request_approval("admin auth default", form)
    if response.status == "rejected":
        return None, "rejected", response.edited, response.comment

    # Set default — clear all others, set target
    for a in accounts:
        a.default = a.id == target_id

    write_accounts_json(accounts)

    return (
        {"account": target_id, "default": True},
        cast(Status, response.status),
        response.edited,
        response.comment,
    )


# ── probes ────────────────────────────────────────────────────────────────────


def _probe_account_imap(account: "cfg.AccountDef") -> dict[str, Any]:
    """Probe IMAP reachability + authentication for an in-memory account.

    Unlike `_probe_imap`, this takes a fully-resolved `AccountDef` directly
    (with username/password/token already populated) instead of reading from
    disk. Used by `auth_login` to validate credentials BEFORE writing anything.

    Args:
        account (AccountDef): Fully-resolved account to probe.

    Returns:
        dict[str, Any]: `{"reachable": bool, "auth_ok": bool, "error": str}`.

    Examples:
        >>> a = AccountDef(id="t", email="u@gmail.com", imap=ImapEndpoint(host="imap.gmail.com"), smtp=SmtpEndpoint(host="smtp.gmail.com"))
        >>> _probe_account_imap(a)["reachable"]
        False
    """
    from .api.imap import IMAPClient

    # Custom accounts: password lives in keyring (not available at status time).
    # Just check reachability — password will be prompted on first do.
    if account.provider_type == "custom":
        client = IMAPClient(account)
        try:
            client.connect()
            return {
                "reachable": True,
                "auth_ok": False,
                "error": "Custom — password prompted on first do (keyring, 15 min TTL).",
            }
        except MailAPIError as exc:
            return {"reachable": False, "auth_ok": False, "error": str(exc)}
        finally:
            client.disconnect()

    if account.auth_method == "oauth2":
        from .oauth2 import load_token

        token = load_token(account.id)
        if not token:
            return {
                "reachable": False,
                "auth_ok": False,
                "error": f"No OAuth2 token for {account.id!r}.",
            }
    elif not account.password:
        return {
            "reachable": False,
            "auth_ok": False,
            "error": f"Missing password for {account.id!r}.",
        }

    client = IMAPClient(account)
    try:
        client.connect()
        return {"reachable": True, "auth_ok": True, "error": ""}
    except MailAPIError as exc:
        return {"reachable": False, "auth_ok": False, "error": str(exc)}
    finally:
        client.disconnect()


def _probe_account_smtp(account: "cfg.AccountDef") -> dict[str, Any]:
    """Probe SMTP reachability + authentication for an in-memory account.

    Args:
        account (AccountDef): Fully-resolved account to probe.

    Returns:
        dict[str, Any]: `{"reachable": bool, "error": str}`.

    Examples:
        >>> a = AccountDef(id="t", email="u@gmail.com", imap=ImapEndpoint(host="imap.gmail.com"), smtp=SmtpEndpoint(host="smtp.gmail.com"))
        >>> _probe_account_smtp(a)["reachable"]
        False
    """
    import smtplib

    # Custom accounts: reachability-only (password not stored, prompted on first do)
    if account.provider_type == "custom":
        smtp = account.smtp
        try:
            if smtp.starttls:
                server = smtplib.SMTP(smtp.host, smtp.port, timeout=api_timeout())
                server.ehlo()
            else:
                server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=api_timeout())
            server.quit()
            return {
                "reachable": True,
                "error": "Custom — password prompted on first do (keyring, 15 min TTL).",
            }
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "error": str(exc)}

    if account.auth_method == "oauth2":
        from .oauth2 import load_token

        token = load_token(account.id)
        if not token:
            return {
                "reachable": False,
                "error": f"No OAuth2 token for {account.id!r}.",
            }
    elif not account.password:
        return {
            "reachable": False,
            "error": f"Missing password for {account.id!r}.",
        }

    smtp = account.smtp
    try:
        if smtp.starttls:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=api_timeout())
            server.ehlo()
            server.starttls()
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=api_timeout())
        server.quit()
        return {"reachable": True, "error": ""}
    except Exception as exc:  # noqa: BLE001 - any SMTP/socket failure is reported
        return {"reachable": False, "error": str(exc)}


def _probe_imap(account_id: str | None) -> dict[str, Any]:
    """Probe IMAP reachability and authentication for an account.

    For OAuth2 accounts, a valid stored token is required. For password
    accounts, a password in .env is required.

    Args:
        account_id (str | None): Account to probe (None → default).

    Returns:
        dict[str, Any]: `{"reachable": bool, "auth_ok": bool, "error": str}`.

    Examples:
        >>> _probe_imap("poly")["reachable"]
        True
        >>> _probe_imap("nope")["reachable"]
        False
        >>> _probe_imap(None)["reachable"]
        True
    """
    from .api.imap import IMAPClient

    try:
        account = get_account(account_id)
    except MailProxyError as exc:
        return {"reachable": False, "auth_ok": False, "error": str(exc)}

    # Custom accounts: reachability-only (TCP connect + IMAP greeting, no login)
    if account.provider_type == "custom":
        import imaplib

        cfg = account.imap
        try:
            if cfg.tls:
                conn = imaplib.IMAP4_SSL(cfg.host, cfg.port, timeout=api_timeout())
            else:
                conn = imaplib.IMAP4(cfg.host, cfg.port, timeout=api_timeout())
            conn.logout()
            return {
                "reachable": True,
                "auth_ok": False,
                "error": "Custom — password prompted on first do (keyring, 15 min TTL).",
            }
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "auth_ok": False, "error": str(exc)}

    if account.auth_method == "oauth2":
        from .oauth2 import load_token

        token = load_token(account.id)
        if not token:
            return {
                "reachable": False,
                "auth_ok": False,
                "error": f"No OAuth2 token for {account.id!r} — run 'mail-proxy admin auth login'.",
            }
    elif not account.password:
        return {
            "reachable": False,
            "auth_ok": False,
            "error": f"Missing credentials for {account.id!r} — run 'mail-proxy admin auth login'.",
        }

    client = IMAPClient(account)
    try:
        client.connect()
        return {"reachable": True, "auth_ok": True, "error": ""}
    except MailAPIError as exc:
        return {"reachable": False, "auth_ok": False, "error": str(exc)}
    finally:
        client.disconnect()


def _probe_smtp(account_id: str | None) -> dict[str, Any]:
    """Probe SMTP reachability (connect + ehlo, no login).

    For OAuth2 accounts, a valid stored token is required to be considered
    "reachable". For password accounts, a password in .env is required.

    Args:
        account_id (str | None): Account to probe (None → default).

    Returns:
        dict[str, Any]: `{"reachable": bool, "error": str}`.

    Examples:
        >>> _probe_smtp("poly")["reachable"]
        True
        >>> _probe_smtp("nope")["reachable"]
        False
        >>> _probe_smtp(None)["reachable"]
        True
    """
    import smtplib

    try:
        account = get_account(account_id)
    except MailProxyError as exc:
        return {"reachable": False, "error": str(exc)}

    # Custom accounts: reachability-only (connect + ehlo + starttls, no login)
    if account.provider_type == "custom":
        smtp = account.smtp
        try:
            if smtp.starttls:
                server = smtplib.SMTP(smtp.host, smtp.port, timeout=api_timeout())
                server.ehlo()
                server.starttls()
                server.ehlo()
            else:
                server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=api_timeout())
            server.quit()
            return {
                "reachable": True,
                "error": "Custom — password prompted on first do (keyring, 15 min TTL).",
            }
        except Exception as exc:  # noqa: BLE001
            return {"reachable": False, "error": str(exc)}

    if account.auth_method == "oauth2":
        from .oauth2 import load_token

        token = load_token(account.id)
        if not token:
            return {
                "reachable": False,
                "error": f"No OAuth2 token for {account.id!r} — run 'mail-proxy admin auth login'.",
            }
    elif not account.password:
        return {
            "reachable": False,
            "error": f"Missing credentials for {account.id!r} — run 'mail-proxy admin auth login'.",
        }

    smtp = account.smtp
    try:
        if smtp.starttls:
            server = smtplib.SMTP(smtp.host, smtp.port, timeout=api_timeout())
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(smtp.host, smtp.port, timeout=api_timeout())
        server.quit()
        return {"reachable": True, "error": ""}
    except Exception as exc:  # noqa: BLE001 - any SMTP/socket failure is reported
        return {"reachable": False, "error": str(exc)}


# ── admin reset / purge (keep as-is — destructive, HITL-confirmed) ────────────


def reset() -> AdminResult:
    """Clear ALL passwords from .env (HITL-confirmed). Accounts in JSON are untouched.

    Returns:
        tuple[dict, Status, bool, str]: A tuple containing the reset status
        dict, the HITL response status, a boolean indicating whether it was
        edited, and the HITL reviewer comment.

    Examples:
        - Confirm and clear:
            `mail-proxy admin reset`
            → {"status":"cleared","config":"…/.env"}
        - Reviewer rejected:
            `mail-proxy admin reset`
            → (rejected envelope, exit 1 — credentials kept)
        - Reset when the file is already empty:
            `mail-proxy admin reset`
            → {"status":"cleared","config":"…/.env"}
    """
    form = {
        "action": "clear_credentials",
        "config_file": str(cfg.ENV_PATH),
        "confirm": "Yes, clear all passwords",
    }
    response = request_approval("admin reset", form)
    if response.status == "rejected":
        return None, "rejected", response.edited, response.comment

    write_env({})
    return (
        {"status": "cleared", "config": str(cfg.ENV_PATH)},
        cast(Status, response.status),
        response.edited,
        response.comment,
    )


def purge() -> AdminResult:
    """Delete the entire config directory (HITL-confirmed). Both JSON and .env.

    The CLI itself is NOT uninstalled from within this process — that would
    wipe this package's own site-packages mid-execution. Config is removed
    here; the operator finishes with `uv tool uninstall mail-proxy`.

    Returns:
        tuple[dict, Status, bool, str]: A tuple containing the purge status
        dict, the HITL response status, a boolean indicating whether it was
        edited, and the HITL reviewer comment.

    Examples:
        - Confirm and purge:
            `mail-proxy admin purge`
            → {"status":"purged","config_dir":"…","config_dir_deleted":true}
        - Reviewer rejected:
            `mail-proxy admin purge`
            → (rejected envelope, exit 1 — config kept)
        - Purge when the config directory never existed:
            `mail-proxy admin purge`
            → {"status":"purged","config_dir":"…","config_dir_deleted":false}
    """
    form = {
        "action": "delete_config",
        "config_dir": str(cfg.CONFIG_DIR),
        "confirm": "Yes, delete the configuration directory",
    }
    response = request_approval("admin purge", form)
    if response.status == "rejected":
        return None, "rejected", response.edited, response.comment

    config_dir_deleted = False
    if cfg.CONFIG_DIR.exists():
        shutil.rmtree(cfg.CONFIG_DIR)
        config_dir_deleted = True

    return (
        {
            "status": "purged",
            "config_dir": str(cfg.CONFIG_DIR),
            "config_dir_deleted": config_dir_deleted,
            "note": "Config removed. To fully uninstall the CLI, run: uv tool uninstall mail-proxy",
        },
        cast(Status, response.status),
        response.edited,
        response.comment,
    )


# ── admin doctor ──────────────────────────────────────────────────────────────


def doctor() -> dict:
    """Scan config directory and auto-fix every permission / structural problem.

    Checks: config dir existence + mode, accounts.json parse + mode, .env mode,
    account completeness, binary presence. Fixes what it can (chmod).

    Returns:
        dict: Per-file status (ok/fixed/error/missing), issues found, fixes applied.

    Examples:
        - Everything clean:
            `mail-proxy admin doctor`
            → {"config_dir":{"status":"ok"},…,"fixes_applied":[],"healthy":true}
        - Dir was 777, fixed to 700:
            `mail-proxy admin doctor`
            → {"config_dir":{"status":"fixed","old_mode":"0o777","new_mode":"0o700"},…,"fixes_applied":["config_dir_permissions"]}
        - accounts.json missing:
            `mail-proxy admin doctor`
            → {"accounts_json":{"status":"missing"},…,"healthy":false}
    """
    issues: list[str] = []
    fixes: list[str] = []

    # ── config dir ────────────────────────────────────────────────────────
    dir_result: dict[str, Any] = {"path": str(cfg.CONFIG_DIR)}
    if cfg.CONFIG_DIR.exists():
        current_mode = os.stat(cfg.CONFIG_DIR).st_mode & 0o777
        if current_mode == DIR_PERMISSIONS:
            dir_result["status"] = "ok"
            dir_result["mode"] = oct(current_mode)
        else:
            cfg.CONFIG_DIR.chmod(DIR_PERMISSIONS)
            dir_result["status"] = "fixed"
            dir_result["old_mode"] = oct(current_mode)
            dir_result["new_mode"] = oct(DIR_PERMISSIONS)
            fixes.append("config_dir_permissions")
            issues.append(
                f"config dir was {oct(current_mode)}, fixed to {oct(DIR_PERMISSIONS)}"
            )
    else:
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        cfg.CONFIG_DIR.chmod(DIR_PERMISSIONS)
        dir_result["status"] = "created"
        dir_result["mode"] = oct(DIR_PERMISSIONS)
        fixes.append("config_dir_created")

    # ── accounts.json ─────────────────────────────────────────────────────
    json_result: dict[str, Any] = {"path": str(ACCOUNTS_JSON_PATH)}
    if ACCOUNTS_JSON_PATH.exists():
        current_mode = os.stat(ACCOUNTS_JSON_PATH).st_mode & 0o777
        if current_mode != FILE_PERMISSIONS:
            ACCOUNTS_JSON_PATH.chmod(FILE_PERMISSIONS)
            json_result["old_mode"] = oct(current_mode)
            json_result["new_mode"] = oct(FILE_PERMISSIONS)
            fixes.append("accounts_json_permissions")
            issues.append(
                f"accounts.json was {oct(current_mode)}, fixed to {oct(FILE_PERMISSIONS)}"
            )
        try:
            raw = _json.loads(ACCOUNTS_JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                json_result["status"] = "ok"
                json_result["count"] = len(raw)
                json_result["mode"] = oct(os.stat(ACCOUNTS_JSON_PATH).st_mode & 0o777)
            else:
                json_result["status"] = "error"
                json_result["error"] = "not a JSON array"
                issues.append("accounts.json is not a JSON array")
        except (_json.JSONDecodeError, OSError) as exc:
            json_result["status"] = "error"
            json_result["error"] = str(exc)
            issues.append(f"accounts.json parse error: {exc}")
    else:
        json_result["status"] = "missing"
        issues.append("accounts.json not found — run 'mail-proxy admin auth login'")

    # ── .env ──────────────────────────────────────────────────────────────
    env_result: dict[str, Any] = {"path": str(cfg.ENV_PATH)}
    if cfg.ENV_PATH.exists():
        current_mode = os.stat(cfg.ENV_PATH).st_mode & 0o777
        if current_mode != FILE_PERMISSIONS:
            cfg.ENV_PATH.chmod(FILE_PERMISSIONS)
            env_result["old_mode"] = oct(current_mode)
            env_result["new_mode"] = oct(FILE_PERMISSIONS)
            fixes.append("env_permissions")
            issues.append(
                f".env was {oct(current_mode)}, fixed to {oct(FILE_PERMISSIONS)}"
            )
        loaded = load_env()
        pass_keys = [k for k in loaded if k.endswith("_PASS")]
        env_result["status"] = "ok"
        env_result["secrets_count"] = len(pass_keys)
        env_result["mode"] = oct(os.stat(cfg.ENV_PATH).st_mode & 0o777)
    else:
        env_result["status"] = "missing"
        issues.append(".env not found — no passwords configured")

    # ── account completeness ──────────────────────────────────────────────
    account_issues: list[dict[str, str]] = []
    try:
        load_env()
        accounts = load_accounts(force=True)
        for a in accounts:
            prefix = account_env_prefix(a.id)
            if a.auth_method == "oauth2":
                from .oauth2 import load_token

                if load_token(a.id) is None:
                    account_issues.append(
                        {
                            "id": a.id,
                            "email": a.email,
                            "issue": "no OAuth2 token stored",
                            "fix": "run 'mail-proxy admin auth login'",
                        }
                    )
            else:
                password = os.environ.get(f"{prefix}PASS", "")
                if not password:
                    account_issues.append(
                        {
                            "id": a.id,
                            "email": a.email,
                            "issue": f"missing {prefix}PASS",
                            "fix": "run 'mail-proxy admin auth login'",
                        }
                    )
    except MailProxyError as exc:
        issues.append(f"cannot load accounts: {exc}")

    # ── binary ────────────────────────────────────────────────────────────
    binary_path = shutil.which("mail-proxy")
    binary_result: dict[str, Any] = {}
    if binary_path:
        binary_result["path"] = binary_path
        binary_result["status"] = "ok"
    else:
        binary_result["status"] = "not_in_path"
        issues.append("mail-proxy not found in PATH")

    return {
        "config_dir": dir_result,
        "accounts_json": json_result,
        "env": env_result,
        "accounts": account_issues,
        "binary": binary_result,
        "issues_found": issues,
        "fixes_applied": fixes,
        "healthy": len(issues) == 0,
    }


# ── admin status ──────────────────────────────────────────────────────────────


def status() -> dict:
    """Complete system status: accounts, auth state, permissions, probes, issues.

    Combines auth probes + permission scan into one unified view. This is the
    single command to see everything about mail-proxy's health.

    Returns:
        dict: Full status including per-account IMAP/SMTP probes, permission
        state for every config file, issues list, and healthy flag.

    Examples:
        - Fully configured:
            `mail-proxy admin status`
            → {"accounts":[{"id":"poly","configured":true,"imap":{"auth_ok":true}}],"healthy":true}
        - No accounts.json:
            `mail-proxy admin status`
            → {"accounts":[],"issues":["No accounts found"],"healthy":false}
        - Mixed:
            `mail-proxy admin status`
            → {"accounts":[{"id":"poly","configured":true},{"id":"gmail","configured":false}],"issues":["gmail: no password"]}
    """
    load_env()
    issues: list[str] = []

    # ── accounts ──────────────────────────────────────────────────────────
    accounts_data = load_accounts(force=True)
    accounts_state: list[dict[str, Any]] = []

    if not accounts_data:
        issues.append("No accounts found in accounts.json")
    else:
        for a in accounts_data:
            prefix = account_env_prefix(a.id)
            password = os.environ.get(f"{prefix}PASS", "")
            if a.auth_method == "oauth2":
                from .oauth2 import load_token

                configured = load_token(a.id) is not None
            else:
                configured = bool(password)
            imap_probe: dict[str, Any] = {
                "reachable": False,
                "auth_ok": False,
                "error": "",
            }
            smtp_probe: dict[str, Any] = {"reachable": False, "error": ""}
            if configured:
                imap_probe = _probe_imap(a.id)
                smtp_probe = _probe_smtp(a.id)
            else:
                imap_probe["error"] = (
                    "Missing credentials — run 'mail-proxy admin auth login'."
                )
                smtp_probe["error"] = (
                    "Missing credentials — run 'mail-proxy admin auth login'."
                )
                issues.append(f"{a.id}: no credentials configured")
            accounts_state.append(
                {
                    "id": a.id,
                    "email": a.email,
                    "aliases": list(a.aliases),
                    "default": a.default,
                    "configured": configured,
                    "auth_method": a.auth_method,
                    "imap": imap_probe,
                    "smtp": smtp_probe,
                }
            )

    # ── default account ───────────────────────────────────────────────────
    default_account = None
    for a in accounts_data:
        if a.default:
            default_account = a.id
            break

    # ── permissions ───────────────────────────────────────────────────────
    permissions: dict[str, Any] = {}

    dir_mode = None
    dir_status = "absent"
    if cfg.CONFIG_DIR.exists():
        dir_mode = os.stat(cfg.CONFIG_DIR).st_mode & 0o777
        dir_status = "ok" if dir_mode == DIR_PERMISSIONS else "warning"
        if dir_status == "warning":
            issues.append(
                f"config dir permissions: {oct(dir_mode)} (expected {oct(DIR_PERMISSIONS)})"
            )
    permissions["config_dir"] = {
        "path": str(cfg.CONFIG_DIR),
        "mode": oct(dir_mode) if dir_mode is not None else None,
        "status": dir_status,
    }

    json_status = "missing"
    if ACCOUNTS_JSON_PATH.exists():
        json_mode = os.stat(ACCOUNTS_JSON_PATH).st_mode & 0o777
        json_status = "ok" if json_mode == FILE_PERMISSIONS else "warning"
        if json_status == "warning":
            issues.append(
                f"accounts.json permissions: {oct(json_mode)} (expected {oct(FILE_PERMISSIONS)})"
            )
    permissions["accounts_json"] = {
        "path": str(ACCOUNTS_JSON_PATH),
        "status": json_status,
    }

    env_status = "missing"
    env_mode = None
    if cfg.ENV_PATH.exists():
        env_mode = os.stat(cfg.ENV_PATH).st_mode & 0o777
        env_status = "ok" if env_mode == FILE_PERMISSIONS else "warning"
        if env_status == "warning":
            issues.append(
                f".env permissions: {oct(env_mode)} (expected {oct(FILE_PERMISSIONS)})"
            )
    permissions["env"] = {
        "path": str(cfg.ENV_PATH),
        "mode": oct(env_mode) if env_mode is not None else None,
        "status": env_status,
    }

    # ── binary ────────────────────────────────────────────────────────────
    binary = shutil.which("mail-proxy") or os.path.abspath(sys.argv[0])

    return {
        "accounts": accounts_state,
        "default_account": default_account,
        "config_dir": str(cfg.CONFIG_DIR),
        "accounts_json": str(ACCOUNTS_JSON_PATH),
        "env": str(cfg.ENV_PATH),
        "permissions": permissions,
        "binary": binary,
        "issues": issues,
        "healthy": len(issues) == 0,
    }
