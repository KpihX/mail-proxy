"""
Admin logic — setup, status, reset, purge. Single source of truth.

Credentials are NEVER committed: `setup` collects them through the HITL form
and writes them to ~/.config/mail-proxy/.env (chmod 600). `status` probes the
IMAP/SMTP servers with a real connection attempt and reports masked state.
"""

import os
import shutil
import sys
from typing import Any, cast

from . import config as cfg
from .config import (
    ACCOUNTS,
    DIR_PERMISSIONS,
    FILE_PERMISSIONS,
    account_env_prefix,
    api_timeout,
    get_account,
    load_env,
    write_env,
)
from .exceptions import MailProxyError
from .hitl import request_approval
from .models import Status

AdminResult = tuple[dict | None, Status, bool, str]


def _mask(value: str) -> str:
    """Mask a secret, keeping only its head and tail.

    Args:
        value (str): The secret to mask.

    Returns:
        str: `ivann…pouokam`, or an empty string when there is nothing to mask.

    Examples:
        >>> _mask("ivann.kamdem-pouokam")
        'ivan…ouokam'
        >>> _mask("")
        ''
    """
    if not value:
        return ""
    return f"{value[:4]}…{value[-5:]}" if len(value) > 12 else "…"


def _credential_keys() -> list[tuple[str, str]]:
    """Return the (login_key, pass_key) pairs of every declared account.

    Returns:
        list[tuple[str, str]]: One pair per account in `config.ACCOUNTS`.

    Examples:
        >>> _credential_keys()
        [('MAIL_POLY_LOGIN', 'MAIL_POLY_PASS')]
    """
    return [
        (f"{account_env_prefix(a.id)}LOGIN", f"{account_env_prefix(a.id)}PASS")
        for a in ACCOUNTS
    ]


def setup() -> AdminResult:
    """Collect the per-account credentials through the HITL web form.

    The form shows login + password for every declared account, pre-filled from
    the current `.env`. A field left empty is cleared; a field left untouched
    keeps its current value.

    Returns:
        tuple[dict, Status, bool, str]: A tuple containing the written fields
        dict, the HITL response status, a boolean indicating whether it was
        edited, and the HITL reviewer comment.

    Examples:
        - First-time setup:
            `mail-proxy admin setup`
            → {"config":"/home/kpihx/.config/mail-proxy/.env","fields":["MAIL_POLY_LOGIN","MAIL_POLY_PASS"]}
        - Reviewer cleared the password field:
            `mail-proxy admin setup`
            → {"config":"/home/kpihx/.config/mail-proxy/.env","fields":["MAIL_POLY_LOGIN"]}
        - Reviewer rejected the form:
            `mail-proxy admin setup`
            → (rejected envelope, exit 1 — nothing written)
    """
    load_env()
    current: dict[str, str] = {}
    for login_key, pass_key in _credential_keys():
        current[login_key] = os.environ.get(login_key, "")
        current[pass_key] = os.environ.get(pass_key, "")
    response = request_approval("admin setup", current)
    if response.status == "rejected":
        return None, "rejected", response.edited, response.comment

    values = response.payload if isinstance(response.payload, dict) else current
    kept = {k: str(v).strip() for k, v in values.items() if str(v or "").strip()}
    write_env(kept)
    return (
        {"config": str(cfg.ENV_PATH), "fields": sorted(kept)},
        cast(Status, response.status),
        response.edited,
        response.comment,
    )


def _probe_imap(account_id: str | None) -> dict[str, Any]:
    """Probe IMAP reachability and authentication for an account.

    Args:
        account_id (str | None): Account to probe (None → default).

    Returns:
        dict[str, Any]: `{"reachable": bool, "auth_ok": bool, "error": str}`.

    Examples:
        >>> _probe_imap("poly")["reachable"]
        True
    """
    from .api.imap import IMAPClient

    try:
        account = get_account(account_id)
    except MailProxyError as exc:
        return {"reachable": False, "auth_ok": False, "error": str(exc)}
    if not account.username or not account.password:
        return {
            "reachable": False,
            "auth_ok": False,
            "error": f"Missing credentials for {account.id!r} — run 'mail-proxy admin setup'.",
        }
    client = IMAPClient(account)
    try:
        client.connect()
        return {"reachable": True, "auth_ok": True, "error": ""}
    except MailProxyError as exc:
        return {"reachable": False, "auth_ok": False, "error": str(exc)}
    finally:
        client.disconnect()


def _probe_smtp(account_id: str | None) -> dict[str, Any]:
    """Probe SMTP reachability (connect + ehlo, no login).

    Args:
        account_id (str | None): Account to probe (None → default).

    Returns:
        dict[str, Any]: `{"reachable": bool, "error": str}`.

    Examples:
        >>> _probe_smtp("poly")["reachable"]
        True
    """
    import smtplib

    try:
        account = get_account(account_id)
    except MailProxyError as exc:
        return {"reachable": False, "error": str(exc)}
    if not account.username or not account.password:
        return {
            "reachable": False,
            "error": f"Missing credentials for {account.id!r} — run 'mail-proxy admin setup'.",
        }
    cfg = account.smtp
    try:
        if cfg.starttls:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=api_timeout())
            server.ehlo()
        else:
            server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=api_timeout())
        server.quit()
        return {"reachable": True, "error": ""}
    except Exception as exc:  # noqa: BLE001 - any SMTP/socket failure is reported
        return {"reachable": False, "error": str(exc)}


def status() -> dict:
    """Report the auth state: masked credentials, probes and config hygiene.

    Returns:
        dict: Per-account credential presence + masked values, IMAP/SMTP probe
        results for the default account, permissions and binary path.

    Examples:
        - Fully configured account:
            `mail-proxy admin status`
            → {"config":"/home/kpihx/.config/mail-proxy/.env","config_exists":true,"accounts":[{"id":"poly","label":"Polytechnique (X)","default":true,"login":"ivan…uokam","configured":true}],"default_account":"poly","imap":{"reachable":true,"auth_ok":true,"error":""},"smtp":{"reachable":true,"error":""},"binary":"/home/kpihx/.local/bin/mail-proxy","permissions":{"config_dir":{"status":"ok"},"config_file":{"status":"ok"}}}
        - Not configured yet:
            `mail-proxy admin status`
            → {"config":"…/.env","config_exists":false,"accounts":[{"id":"poly","label":"Polytechnique (X)","default":true,"login":"","configured":false}],"default_account":"poly","imap":{"reachable":false,"auth_ok":false,"error":"Missing credentials for 'poly' — run 'mail-proxy admin setup'."},"smtp":{"reachable":false,"error":"Missing credentials for 'poly' — run 'mail-proxy admin setup'."},"binary":"…","permissions":{"config_dir":{"status":"absent"},"config_file":{"status":"absent"}}}
        - IMAP unreachable but SMTP up:
            `mail-proxy admin status`
            → {"config":"…/.env","config_exists":true,"accounts":[{"id":"poly","login":"ivan…uokam","configured":true}],"default_account":"poly","imap":{"reachable":false,"auth_ok":false,"error":"Cannot reach IMAP server webmail.polytechnique.fr:993 (network unreachable)"},"smtp":{"reachable":true,"error":""},"binary":"…"}
    """
    load_env()
    account = get_account(None)

    accounts_state = []
    for a in ACCOUNTS:
        login_key, pass_key = _credential_keys()[ACCOUNTS.index(a)]
        login = os.environ.get(login_key, "")
        password = os.environ.get(pass_key, "")
        accounts_state.append(
            {
                "id": a.id,
                "label": a.label,
                "default": a.default,
                "login": _mask(login),
                "configured": bool(login and password),
            }
        )

    imap_probe = _probe_imap(None)
    smtp_probe = _probe_smtp(None)

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
        "config": str(cfg.ENV_PATH),
        "config_exists": cfg.ENV_PATH.exists(),
        "accounts": accounts_state,
        "default_account": account.id,
        "imap": imap_probe,
        "smtp": smtp_probe,
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


def reset() -> AdminResult:
    """Clear all credentials from the configuration file (HITL-confirmed).

    Returns:
        tuple[dict, Status, bool, str]: A tuple containing the reset status
        dict, the HITL response status, a boolean indicating whether it was
        edited, and the HITL reviewer comment.

    Examples:
        - Confirm and clear:
            `mail-proxy admin reset`
            → {"status":"cleared","config":"/home/kpihx/.config/mail-proxy/.env"}
        - Reviewer rejected:
            `mail-proxy admin reset`
            → (rejected envelope, exit 1 — credentials kept)
        - Reset when the file is already empty:
            `mail-proxy admin reset`
            → {"status":"cleared","config":"/home/kpihx/.config/mail-proxy/.env"}
    """
    form = {
        "action": "clear_credentials",
        "config_file": str(cfg.ENV_PATH),
        "confirm": "Yes, clear all credentials",
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
    """Delete the configuration directory (HITL-confirmed).

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
            → {"status":"purged","config_dir":"/home/kpihx/.config/mail-proxy","config_dir_deleted":true,"uninstalled":false,"note":"Config removed. To fully uninstall the CLI, run: uv tool uninstall mail-proxy"}
        - Reviewer rejected:
            `mail-proxy admin purge`
            → (rejected envelope, exit 1 — config kept)
        - Purge when the config directory never existed:
            `mail-proxy admin purge`
            → {"status":"purged","config_dir":"/home/kpihx/.config/mail-proxy","config_dir_deleted":false,"uninstalled":false,"note":"Config removed. To fully uninstall the CLI, run: uv tool uninstall mail-proxy"}
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
