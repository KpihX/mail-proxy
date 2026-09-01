"""
mail-proxy configuration — JSON accounts + email domain detection + secrets-only .env.

Architecture (KπX directive 2026-09-01):
  - ~/.config/mail-proxy/accounts.json  = account definitions (NOT secrets)
  - ~/.config/mail-proxy/.env           = secrets ONLY (passwords, chmod 600)
  - ~/.config/mail-proxy/tokens/<id>.json = OAuth2 tokens (chmod 600)
  - ~/.config/mail-proxy/assets/signatures/ = signature images (flat dir)
  - src/mail_proxy/config.py            = EMAIL_PROVIDER_DEFAULTS + load logic

An account = email + aliases + display_name + optional IMAP/SMTP overrides + signatures.
The IMAP/SMTP endpoints are AUTO-DETECTED from the email domain using
EMAIL_PROVIDER_DEFAULTS. Custom or private servers override with explicit
imap_host/smtp_host in the JSON.

Resolution by `-a` flag: id → alias → email prefix → error.
Password policy (KπX directive, same as tick-proxy): credentials are NEVER
committed. The .env holds only MAIL_<ID>_PASS per account, all chmod 600.
OAuth2 tokens live in separate files under tokens/ — never in .env or JSON.

Signature system (KπX directive 2026-09-01):
  - Multiple signatures per account, one marked as default via default_signature_id.
  - Images stored in ~/.config/mail-proxy/assets/signatures/ (deduped by SHA256).
  - Auto-generated IDs: sig-{uuid4().hex[:8]} — never manual.
  - Migration: old `signature: {}` → auto-converted to `signatures: []` + `default_signature_id`.
"""

import hashlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .exceptions import MailProxyError

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "mail-proxy"
ENV_PATH = CONFIG_DIR / ".env"
ACCOUNTS_JSON_PATH = CONFIG_DIR / "accounts.json"
SIGNATURES_DIR = CONFIG_DIR / "assets" / "signatures"

# ── Permissions (single source of truth) ──────────────────────────────────────
DIR_PERMISSIONS = 0o700
FILE_PERMISSIONS = 0o600

# ── Endpoint defaults (documented constants) ───────────────────────────────────
DEFAULT_TIMEOUT = 15.0
ENV_TIMEOUT = "MAIL_TIMEOUT"
DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 587

# ── Email domain → IMAP/SMTP endpoint auto-detection ───────────────────────────
#
# When a JSON account does NOT specify imap_host/smtp_host, the code looks up
# the email domain here. If not found, the account MUST provide explicit hosts.
#
# Each entry: domain → (imap_host, imap_port, imap_tls, smtp_host, smtp_port, smtp_starttls)
EMAIL_PROVIDER_DEFAULTS: dict[str, tuple[str, int, bool, str, int, bool]] = {
    "polytechnique.edu": (
        "webmail.polytechnique.fr",
        993,
        True,
        "webmail.polytechnique.fr",
        587,
        True,
    ),
    "outlook.com": (
        "outlook.office365.com",
        993,
        True,
        "smtp.office365.com",
        587,
        True,
    ),
    "hotmail.com": (
        "outlook.office365.com",
        993,
        True,
        "smtp.office365.com",
        587,
        True,
    ),
    "live.com": (
        "outlook.office365.com",
        993,
        True,
        "smtp.office365.com",
        587,
        True,
    ),
    "gmail.com": (
        "imap.gmail.com",
        993,
        True,
        "smtp.gmail.com",
        587,
        True,
    ),
}

# ── Provider type → endpoint presets (from HITL form type selector) ───────────
#
# The provider type SELECTED BY THE USER is the source of truth for endpoints,
# never the email domain. A Google Workspace account with a custom domain
# (e.g. user@polytechnique.org) still uses Google's IMAP/SMTP servers.
#
# "google"    → imap.gmail.com / smtp.gmail.com   (gmail.com AND Google Workspace custom domains)
# "microsoft" → outlook.office365.com             (outlook.com AND Microsoft 365 custom domains)
# "custom"    → user specifies imap_host/smtp_host in the form
PROVIDER_TYPE_DEFAULTS: dict[str, tuple[str, int, bool, str, int, bool]] = {
    "google": (
        "imap.gmail.com",
        993,
        True,
        "smtp.gmail.com",
        587,
        True,
    ),
    "microsoft": (
        "outlook.office365.com",
        993,
        True,
        "smtp.office365.com",
        587,
        True,
    ),
}

# ── OAuth2 provider mapping from email domains ───────────────────────────────
#
# When an account uses auth_method="oauth2" and no explicit oauth2_provider is
# set, the provider is auto-detected from the email domain using this map.
# Known domains that support OAuth2 for IMAP/SMTP.
OAUTH2_PROVIDER_MAP: dict[str, str] = {
    "outlook.com": "microsoft",
    "hotmail.com": "microsoft",
    "live.com": "microsoft",
    "gmail.com": "google",
}


# ── Account definition models ─────────────────────────────────────────────────


class ImapEndpoint(BaseModel):
    """IMAP server endpoint of an account.

    Attributes:
        host (str): IMAP server hostname, e.g. `webmail.polytechnique.fr`.
        port (int): IMAP port — 993 for direct TLS (IMAPS).
        tls (bool): True = direct TLS (imapclient ssl=True); False = plain.

    Examples:
        >>> ImapEndpoint(host="webmail.polytechnique.fr").port
        993
        >>> ImapEndpoint(host="imap.example.com", tls=False).tls
        False
    """

    host: str
    port: int = DEFAULT_IMAP_PORT
    tls: bool = True


class SmtpEndpoint(BaseModel):
    """SMTP server endpoint of an account.

    Attributes:
        host (str): SMTP server hostname, e.g. `webmail.polytechnique.fr`.
        port (int): SMTP port — 587 with STARTTLS by default.
        starttls (bool): True = STARTTLS on the plain port; False = direct
            SMTP_SSL.

    Examples:
        >>> SmtpEndpoint(host="webmail.polytechnique.fr").port
        587
        >>> SmtpEndpoint(host="smtp.example.com", starttls=False).starttls
        False
    """

    host: str
    port: int = DEFAULT_SMTP_PORT
    starttls: bool = True


class SignatureDef(BaseModel):
    """E-mail signature shown below the body of composed messages.

    Attributes:
        id (str): Auto-generated unique id, e.g. `sig-a1b2c3d4`.
        name (str): Human-readable label, e.g. "Work signature".
        before_logo (str): Text lines above the logo image.
        image (str): Filename in ~/.config/mail-proxy/assets/signatures/ ("" = none).
        after_logo (str): Text lines below the logo image.

    Examples:
        >>> SignatureDef(id="sig-1", name="Work", before_logo="John Doe").name
        'Work'
        >>> SignatureDef().image
        ''
    """

    id: str = ""
    name: str = ""
    before_logo: str = ""
    image: str = ""
    after_logo: str = ""


class AccountDef(BaseModel):
    """One mail account — non-sensitive definition loaded from `accounts.json`.

    The credential env-var name is derived from the id: `MAIL_<ID_UPPER>_PASS`
    (secrets only — the login IS the email address from this definition).

    Accounts can be resolved by id, alias, or email prefix via `get_account()`.
    IMAP/SMTP endpoints are auto-detected from the email domain when not
    explicitly overridden in the JSON.

    Attributes:
        id (str): Stable account id, used as `account_id` in every action
            payload and as the env prefix, e.g. `poly`.
        email (str): Full e-mail address — the login for IMAP/SMTP and the
            From header address.
        display_name (str): Human name shown in the From header.
        aliases (tuple[str, ...]): Alternative names for `-a` flag resolution,
            e.g. `("x", "polytechnique")` for the `poly` account.
        login (str): IMAP/SMTP login; defaults to `email` when absent.
            Override only when the server expects a different login
            (e.g. Exchange `DOMAIN\\user`).
        auth_method (str): Authentication method — ``"password"`` (default, app
            password) or ``"oauth2"`` (OAuth2 bearer token via XOAUTH2).
        oauth2_provider (str): OAuth2 provider name — ``"microsoft"`` or
            ``"google"``. Auto-detected from email domain when empty.
        imap_host (str | None): IMAP hostname override; None → auto-detect.
        imap_port (int | None): IMAP port override; None → default (993).
        imap_tls (bool | None): IMAP TLS override; None → default (True).
        smtp_host (str | None): SMTP hostname override; None → auto-detect.
        smtp_port (int | None): SMTP port override; None → default (587).
        smtp_starttls (bool | None): SMTP STARTTLS override; None → default.
        signatures (list[SignatureDef]): All signatures for this account.
        default_signature_id (str): ID of the default signature ("" → first found).
        default (bool): True = used when `account_id` is omitted.
        imap (ImapEndpoint): Resolved IMAP endpoint (filled by load_accounts).
        smtp (SmtpEndpoint): Resolved SMTP endpoint (filled by load_accounts).
        username (str): Resolved login (filled by resolve_account).
        password (str): Resolved secret (filled by resolve_account).

    Examples:
        >>> AccountDef(id="poly", email="a@b.com", imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y")).email
        'a@b.com'
        >>> AccountDef(id="poly", email="a@b.com", imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y"), aliases=("x",)).aliases
        ('x',)
        >>> AccountDef(id="w", email="a@b.com", imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y"), auth_method="oauth2").auth_method
        'oauth2'
    """

    id: str
    email: str
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    login: str = ""
    provider_type: str = (
        ""  # "gmail", "outlook", "zimbra", "" — persisted, used for endpoint resolution
    )
    auth_method: str = "password"  # "password" or "oauth2"
    oauth2_provider: str = ""  # "microsoft" or "google" (auto-detected when empty)
    imap_host: str | None = None
    imap_port: int | None = None
    imap_tls: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_starttls: bool | None = None
    signatures: list[SignatureDef] = Field(default_factory=list)
    default_signature_id: str = ""
    default: bool = False
    # Resolved at runtime — filled by load_accounts() and resolve_account().
    imap: ImapEndpoint = Field(default_factory=lambda: ImapEndpoint(host="localhost"))
    smtp: SmtpEndpoint = Field(default_factory=lambda: SmtpEndpoint(host="localhost"))
    username: str = Field(default="", exclude=True)
    password: str = Field(default="", exclude=True)

    def get_default_signature(self) -> SignatureDef | None:
        """Return the default signature, or first found, or None.

        Resolution order:
          1. Match by default_signature_id
          2. First signature in the list
          3. None (no signatures)

        Returns:
            SignatureDef | None: The resolved signature, or None.

        Examples:
            >>> a = AccountDef(id="x", email="a@b.com", imap=ImapEndpoint(host="i"), smtp=SmtpEndpoint(host="s"), signatures=[SignatureDef(id="s1", name="Work")])
            >>> a.get_default_signature().id
            's1'
            >>> a = AccountDef(id="x", email="a@b.com", imap=ImapEndpoint(host="i"), smtp=SmtpEndpoint(host="s"), default_signature_id="s2", signatures=[SignatureDef(id="s1"), SignatureDef(id="s2")])
            >>> a.get_default_signature().id
            's2'
            >>> AccountDef(id="x", email="a@b.com", imap=ImapEndpoint(host="i"), smtp=SmtpEndpoint(host="s")).get_default_signature() is None
            True
        """
        if self.default_signature_id:
            for sig in self.signatures:
                if sig.id == self.default_signature_id:
                    return sig
        return self.signatures[0] if self.signatures else None

    def get_signature_by_id(self, sig_id: str) -> SignatureDef | None:
        """Return a signature by its id, or None.

        Args:
            sig_id (str): The signature id to look up.

        Returns:
            SignatureDef | None: The matching signature, or None.

        Examples:
            >>> a = AccountDef(id="x", email="a@b.com", imap=ImapEndpoint(host="i"), smtp=SmtpEndpoint(host="s"), signatures=[SignatureDef(id="s1", name="Work")])
            >>> a.get_signature_by_id("s1").name
            'Work'
            >>> a.get_signature_by_id("nope") is None
            True
        """
        for sig in self.signatures:
            if sig.id == sig_id:
                return sig
        return None

    @property
    def from_address(self) -> str:
        """Return the SMTP envelope address: `email` if set, else the login.

        Returns:
            str: The full sender address.

        Examples:
            >>> AccountDef(id="poly", email="a@poly.edu", imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y")).from_address
            'a@poly.edu'
            >>> AccountDef(id="poly", email="", login="user", imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y")).from_address
            'user'
        """
        return self.email or self.username or self.login


# ── Endpoint resolution helpers ───────────────────────────────────────────────


def _detect_endpoints(email: str) -> tuple[ImapEndpoint, SmtpEndpoint] | None:
    """Auto-detect IMAP/SMTP endpoints from the email domain.

    Args:
        email (str): Full e-mail address, e.g. `user@gmail.com`.

    Returns:
        tuple[ImapEndpoint, SmtpEndpoint] | None: Resolved endpoints, or None
        if the domain is unknown.

    Examples:
        >>> _detect_endpoints("user@gmail.com")[0].host
        'imap.gmail.com'
        >>> _detect_endpoints("user@polytechnique.edu")[1].port
        587
        >>> _detect_endpoints("user@unknown-domain.xyz") is None
        True
    """
    domain = email.split("@")[-1].lower() if "@" in email else ""
    defaults = EMAIL_PROVIDER_DEFAULTS.get(domain)
    if defaults is None:
        return None
    ih, ip, it, sh, sp, st = defaults
    return ImapEndpoint(host=ih, port=ip, tls=it), SmtpEndpoint(
        host=sh, port=sp, starttls=st
    )


def _resolve_endpoints(account_data: dict) -> tuple[ImapEndpoint, SmtpEndpoint]:
    """Resolve IMAP/SMTP endpoints: explicit overrides → provider type → email domain.

    Resolution order:
      1. Explicit ``imap_host`` + ``smtp_host`` in the account data (custom server)
      2. ``provider_type`` from the HITL form (e.g. ``"gmail"`` → ``imap.gmail.com``)
         — handles custom domains like ``user@company.com`` on Gmail/Outlook servers
      3. Email domain lookup in ``EMAIL_PROVIDER_DEFAULTS`` (``user@gmail.com``)
      4. Error if nothing matches

    Args:
        account_data (dict): Raw JSON account dict, optionally containing
            ``email``, ``imap_host``, ``smtp_host``, ``provider_type``.

    Returns:
        tuple[ImapEndpoint, SmtpEndpoint]: Resolved endpoints.

    Raises:
        MailProxyError: When hosts cannot be determined.

    Examples:
        >>> d = {"email": "u@gmail.com"}
        >>> _resolve_endpoints(d)[0].host
        'imap.gmail.com'
        >>> d2 = {"email": "u@x.com", "imap_host": "custom.imap.com", "smtp_host": "custom.smtp.com"}
        >>> _resolve_endpoints(d2)[0].host
        'custom.imap.com'
        >>> d3 = {"email": "u@company.com", "provider_type": "gmail"}
        >>> _resolve_endpoints(d3)[0].host
        'imap.gmail.com'
    """
    email = account_data.get("email", "")
    imap_host = account_data.get("imap_host")
    smtp_host = account_data.get("smtp_host")
    provider_type = account_data.get("provider_type", "")

    # 1. Explicit overrides
    if imap_host and smtp_host:
        return (
            ImapEndpoint(
                host=imap_host,
                port=account_data.get("imap_port", DEFAULT_IMAP_PORT),
                tls=account_data.get("imap_tls", True),
            ),
            SmtpEndpoint(
                host=smtp_host,
                port=account_data.get("smtp_port", DEFAULT_SMTP_PORT),
                starttls=account_data.get("smtp_starttls", True),
            ),
        )

    # 2. Provider type from HITL form (handles custom domains)
    if provider_type:
        type_defaults = PROVIDER_TYPE_DEFAULTS.get(provider_type)
        if type_defaults:
            ih, ip, it, sh, sp, st = type_defaults
            return (
                ImapEndpoint(
                    host=imap_host or ih,
                    port=account_data.get("imap_port", ip),
                    tls=account_data.get("imap_tls", it),
                ),
                SmtpEndpoint(
                    host=smtp_host or sh,
                    port=account_data.get("smtp_port", sp),
                    starttls=account_data.get("smtp_starttls", st),
                ),
            )

    # 3. Email domain detection
    detected = _detect_endpoints(email)

    if detected is None:
        raise MailProxyError(
            f"Cannot determine IMAP/SMTP endpoints for {email!r}: "
            f"unknown domain and no imap_host/smtp_host/provider_type provided. "
            f"Known domains: {', '.join(sorted(EMAIL_PROVIDER_DEFAULTS))}."
        )

    imap_default, smtp_default = detected
    return (
        ImapEndpoint(
            host=imap_host or imap_default.host,
            port=account_data.get("imap_port", imap_default.port),
            tls=account_data.get("imap_tls", imap_default.tls),
        ),
        SmtpEndpoint(
            host=smtp_host or smtp_default.host,
            port=account_data.get("smtp_port", smtp_default.port),
            starttls=account_data.get("smtp_starttls", smtp_default.starttls),
        ),
    )


# ── JSON accounts loader ──────────────────────────────────────────────────────

_accounts_cache: list[AccountDef] = []


def load_accounts(force: bool = False) -> list[AccountDef]:
    """Load and resolve all accounts from ~/.config/mail-proxy/accounts.json.

    Each account's IMAP/SMTP endpoints are resolved: explicit JSON fields
    override the auto-detected defaults from the email domain.

    Args:
        force (bool): True = reload from disk even if already cached.

    Returns:
        list[AccountDef]: All resolved account definitions (empty when missing).

    Raises:
        MailProxyError: When the JSON file exists but is malformed.

    Examples:
        >>> load_accounts(force=True)[0].id
        'poly'
        >>> len(load_accounts()) >= 1
        True
    """
    global _accounts_cache
    if _accounts_cache and not force:
        return _accounts_cache

    if not ACCOUNTS_JSON_PATH.exists():
        _accounts_cache = []
        return []

    try:
        raw_list = json.loads(ACCOUNTS_JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise MailProxyError(f"Cannot parse {ACCOUNTS_JSON_PATH}: {exc}") from exc

    if not isinstance(raw_list, list):
        raise MailProxyError(f"{ACCOUNTS_JSON_PATH} must contain a JSON array.")

    accounts: list[AccountDef] = []
    for i, entry in enumerate(raw_list):
        if not isinstance(entry, dict) or "id" not in entry or "email" not in entry:
            logger.warning(
                "Skipping account #%d in %s: missing 'id' or 'email'.",
                i,
                ACCOUNTS_JSON_PATH,
            )
            continue
        try:
            imap, smtp = _resolve_endpoints(entry)
        except MailProxyError as exc:
            logger.warning(
                "Skipping account %r in %s: %s",
                entry.get("id"),
                ACCOUNTS_JSON_PATH,
                exc,
            )
            continue

        # Migration: old `signature: {}` → new `signatures: []` + `default_signature_id`
        signatures: list[SignatureDef] = []
        default_signature_id = entry.get("default_signature_id", "")
        if "signatures" in entry and isinstance(entry["signatures"], list):
            for sig_data in entry["signatures"]:
                if isinstance(sig_data, dict):
                    signatures.append(SignatureDef(**sig_data))
        elif "signature" in entry and isinstance(entry["signature"], dict):
            old_sig = entry["signature"]
            if (
                old_sig.get("before_logo")
                or old_sig.get("after_logo")
                or old_sig.get("logo_path")
            ):
                new_id = f"sig-{uuid.uuid4().hex[:8]}"
                signatures.append(
                    SignatureDef(
                        id=new_id,
                        name="Default",
                        before_logo=old_sig.get("before_logo", ""),
                        image=old_sig.get("logo_path", ""),
                        after_logo=old_sig.get("after_logo", ""),
                    )
                )
                default_signature_id = new_id
                logger.info(
                    "Migrated old signature for account %r → sig %s",
                    entry.get("id"),
                    new_id,
                )

        account = AccountDef(
            id=entry["id"],
            email=entry["email"],
            display_name=entry.get("display_name", ""),
            aliases=tuple(entry.get("aliases", [])),
            login=entry.get("login", ""),
            provider_type=entry.get("provider_type", ""),
            auth_method=entry.get("auth_method", "password"),
            oauth2_provider=entry.get("oauth2_provider", ""),
            imap=imap,
            smtp=smtp,
            signatures=signatures,
            default_signature_id=default_signature_id,
            default=entry.get("default", False),
        )
        accounts.append(account)

    _accounts_cache = accounts
    return accounts


def write_accounts_json(accounts: list[AccountDef]) -> None:
    """Write the full accounts list to ~/.config/mail-proxy/accounts.json (chmod 600).

    Serializes every AccountDef (minus runtime-resolved fields) as a clean JSON
    array. This is the ONLY writer — prevents partial writes and desync.

    Args:
        accounts (list[AccountDef]): All account definitions to persist.

    Returns:
        None: Writes the file atomically and clears the cache.

    Examples:
        >>> write_accounts_json([AccountDef(id="x", email="a@b.com", imap=ImapEndpoint(host="i"), smtp=SmtpEndpoint(host="s"))])
        >>> ACCOUNTS_JSON_PATH.exists()
        True
    """
    global _accounts_cache
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(DIR_PERMISSIONS)

    data = []
    for a in accounts:
        entry: dict[str, Any] = {
            "id": a.id,
            "email": a.email,
        }
        if a.display_name:
            entry["display_name"] = a.display_name
        if a.aliases:
            entry["aliases"] = list(a.aliases)
        if a.login:
            entry["login"] = a.login
        if a.provider_type:
            entry["provider_type"] = a.provider_type
        if a.auth_method != "password":
            entry["auth_method"] = a.auth_method
        if a.oauth2_provider:
            entry["oauth2_provider"] = a.oauth2_provider
        if a.default:
            entry["default"] = True
        if a.imap_host:
            entry["imap_host"] = a.imap_host
        if a.smtp_host:
            entry["smtp_host"] = a.smtp_host
        if a.signatures:
            entry["signatures"] = []
            for sig in a.signatures:
                sig_entry: dict[str, str] = {"id": sig.id}
                if sig.name:
                    sig_entry["name"] = sig.name
                if sig.before_logo:
                    sig_entry["before_logo"] = sig.before_logo
                if sig.image:
                    sig_entry["image"] = sig.image
                if sig.after_logo:
                    sig_entry["after_logo"] = sig.after_logo
                entry["signatures"].append(sig_entry)
        if a.default_signature_id:
            entry["default_signature_id"] = a.default_signature_id
        data.append(entry)

    ACCOUNTS_JSON_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    ACCOUNTS_JSON_PATH.chmod(FILE_PERMISSIONS)
    _accounts_cache = []


# ── Account resolution ────────────────────────────────────────────────────────


def account_env_prefix(account_id: str) -> str:
    """Return the env prefix of an account — `MAIL_<ID_UPPER>_`.

    Args:
        account_id (str): Account id, e.g. `poly`.

    Returns:
        str: The prefix, e.g. `MAIL_POLY_`.

    Examples:
        >>> account_env_prefix("poly")
        'MAIL_POLY_'
        >>> account_env_prefix("work")
        'MAIL_WORK_'
    """
    return f"MAIL_{account_id.upper()}_"


def load_env() -> dict[str, str]:
    """Load the .env file into os.environ and return it as a dict.

    Existing environment variables win (``setdefault``), so an operator can
    override any key for a single run without touching the file.

    Returns:
        dict[str, str]: The key/value pairs found in the file (empty when the
        file does not exist).

    Examples:
        >>> load_env()          # when ~/.config/mail-proxy/.env is absent
        {}
        >>> load_env()          # when .env has MAIL_POLY_PASS=secret
        {'MAIL_POLY_PASS': 'secret'}
    """
    if not ENV_PATH.exists():
        return {}
    result: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not val:
            continue
        os.environ.setdefault(key, val)
        result[key] = os.environ[key]
    return result


def write_env(values: dict[str, str]) -> None:
    """Rewrite ~/.config/mail-proxy/.env with the given values (chmod 600).

    Keys whose value is an empty string are omitted, which is how `admin setup`
    clears a credential.

    Args:
        values (dict[str, str]): Full desired content, e.g.
            ``{"MAIL_POLY_PASS": "secret", "MAIL_WORK_PASS": "app-password"}``.

    Returns:
        None: Writes the file and sets 0600 permissions.

    Examples:
        >>> write_env({"MAIL_POLY_PASS": "s3cret"})  # 1 key written
        >>> write_env({})                              # file now empty
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# mail-proxy configuration — managed by `mail-proxy admin setup`.",
        "# Credentials are NEVER committed; this file is chmod 600.",
        "",
    ]
    lines.extend(f"{k}={v}" for k, v in values.items() if v)
    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(FILE_PERMISSIONS)
    CONFIG_DIR.chmod(DIR_PERMISSIONS)


def _get_bool(name: str, default: bool) -> bool:
    """Parse a boolean env override, falling back to the default.

    Args:
        name (str): Env var name.
        default (bool): Value when the var is absent or unparsable.

    Returns:
        bool: The parsed value.

    Examples:
        >>> _get_bool("MAIL_POLY_IMAP_TLS", True)
        True
        >>> _get_bool("MAIL_NOPE_IMAP_TLS", True)
        True
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    """Parse an integer env override, falling back to the default.

    Args:
        name (str): Env var name.
        default (int): Value when the var is absent or unparsable.

    Returns:
        int: The parsed value.

    Examples:
        >>> _get_int("MAIL_TIMEOUT", 15)
        15
        >>> _get_int("MAIL_NOPE_PORT", 993)
        993
    """
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def resolve_account(account: AccountDef) -> AccountDef:
    """Apply .env secret (password) to an account definition.

    The login is the email from the JSON — only the password comes from .env.
    For OAuth2 accounts, the password field is left empty (the caller uses
    XOAUTH2 via the oauth2 module instead).

    Args:
        account (AccountDef): The definition to resolve.

    Returns:
        AccountDef: A copy with username/password populated.

    Examples:
        >>> a = AccountDef(id="poly", email="a@b.com", imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y"))
        >>> resolve_account(a).username
        'a@b.com'
        >>> resolve_account(a).password == ""
        True
    """
    prefix = account_env_prefix(account.id)
    resolved = account.model_copy()
    resolved.username = account.login or account.email
    if account.auth_method == "oauth2":
        resolved.password = ""
    elif account.provider_type == "custom":
        from .secrets import get_cached_password

        resolved.password = get_cached_password(account.id) or ""
    else:
        resolved.password = os.environ.get(f"{prefix}PASS", "")
    return resolved


def get_account(account_id: str | None = None) -> AccountDef:
    """Return the resolved account for an id, alias, or email prefix.

    Resolution order: exact id match → alias match → email prefix match → error.
    This lets users pass `-a poly`, `-a x`, or `-a user.name` and all resolve
    to the same account.

    Args:
        account_id (str | None): Account id, alias, or email prefix; None → default.

    Returns:
        AccountDef: The resolved account (credentials + endpoint overrides).

    Raises:
        MailProxyError: When the id is unknown or no default is declared.

    Examples:
        >>> get_account("poly").id
        'poly'
        >>> get_account("x").id       # alias resolution
        'poly'
        >>> get_account().id          # default account
        'poly'
    """
    accounts = load_accounts()
    if not accounts:
        raise MailProxyError(
            f"No accounts found. Create {ACCOUNTS_JSON_PATH} (see accounts.json.example) "
            "or run 'mail-proxy admin setup'."
        )
    if account_id:
        # 1. Exact id match
        for account in accounts:
            if account.id == account_id:
                return resolve_account(account)
        # 2. Alias match
        for account in accounts:
            if account_id in account.aliases:
                return resolve_account(account)
        # 3. Email prefix match (e.g. "user.name" matches "user.name@example.com")
        lower_id = account_id.lower()
        for account in accounts:
            if account.email and account.email.lower().startswith(lower_id):
                return resolve_account(account)
        raise MailProxyError(
            f"Unknown account {account_id!r}. Known accounts: "
            f"{', '.join(a.id + (' (' + '|'.join(a.aliases) + ')' if a.aliases else '') for a in accounts)}."
        )
    for account in accounts:
        if account.default:
            return resolve_account(account)
    raise MailProxyError('No default account ("default": true) in accounts.json.')


def api_timeout() -> float:
    """Connection timeout, in seconds, for every IMAP/SMTP call.

    Returns:
        float: The timeout (overridable via MAIL_TIMEOUT).

    Examples:
        >>> api_timeout()
        15.0
        >>> api_timeout()   # with MAIL_TIMEOUT=30
        30.0
    """
    raw = os.environ.get(ENV_TIMEOUT)
    try:
        return float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def ensure_env(account_id: str | None = None) -> None:
    """Check that the config exists and exposes usable account credentials.

    Verifies: accounts.json exists, .env exists, and the target account has
    MAIL_<ID>_PASS (for password auth) or a valid OAuth2 token (for oauth2 auth).

    Args:
        account_id (str | None): Account to validate; None → default account.

    Returns:
        None: Returns silently when the configuration is usable.

    Raises:
        MailProxyError: With the exact command to run as a fix.

    Examples:
        >>> ensure_env()                  # default account credentials present
        >>> ensure_env("poly")
        MailProxyError: Accounts file not found at …/accounts.json.
    """
    if not ACCOUNTS_JSON_PATH.exists():
        raise MailProxyError(
            f"Accounts file not found at {ACCOUNTS_JSON_PATH}. "
            "Copy accounts.json.example to that path and edit it."
        )
    if not ENV_PATH.exists():
        raise MailProxyError(
            f"Config file not found at {ENV_PATH}. Run 'mail-proxy admin setup' first."
        )
    load_env()
    account = get_account(account_id)
    # Custom accounts: password lives in system keyring (not .env), prompted on first do
    if account.provider_type == "custom":
        return
    prefix = account_env_prefix(account.id)
    if account.auth_method == "oauth2":
        from .oauth2 import load_token

        token = load_token(account.id)
        if token is None:
            raise MailProxyError(
                f"Account {account.id!r} uses OAuth2 but has no stored token. "
                "Run 'mail-proxy admin auth login' to authorize."
            )
    elif not account.password:
        raise MailProxyError(
            f"Account {account.id!r} is missing {prefix}PASS. "
            "Run 'mail-proxy admin auth login' to configure."
        )


def list_accounts() -> list[dict[str, str | bool | list[str]]]:
    """Return all declared accounts with their env prefix and labels.

    Used by `admin setup` and `admin status` to iterate every account.

    Returns:
        list[dict[str, str | bool | list[str]]]: One dict per account with
        keys: id, label, prefix, email, aliases, default.

    Examples:
        >>> accounts = list_accounts()
        >>> accounts[0]["id"]
        'poly'
        >>> accounts[0]["prefix"]
        'MAIL_POLY_'
        >>> len(accounts) >= 1
        True
    """
    accounts = load_accounts()
    return [
        {
            "id": a.id,
            "label": a.display_name or a.id,
            "prefix": account_env_prefix(a.id),
            "email": a.email,
            "aliases": list(a.aliases),
            "default": a.default,
        }
        for a in accounts
    ]


# ── Signature image helpers ──────────────────────────────────────────────────


def get_signatures_dir() -> Path:
    """Return ~/.config/mail-proxy/assets/signatures/, creating if needed.

    Returns:
        Path: The signatures directory (created lazily).

    Examples:
        >>> get_signatures_dir().name
        'signatures'
        >>> get_signatures_dir().exists()
        True
    """
    SIGNATURES_DIR.mkdir(parents=True, exist_ok=True)
    return SIGNATURES_DIR


def copy_signature_image(source_path: str) -> str:
    """Copy an image to the signatures dir, deduplicated by SHA256.

    If an image with the same content already exists, the existing filename
    is returned without creating a duplicate.

    Args:
        source_path (str): Absolute path to the source image file.

    Returns:
        str: The stored filename (basename in the signatures dir).

    Raises:
        MailProxyError: When the source file does not exist.

    Examples:
        >>> copy_signature_image("/tmp/nonexistent.png")
        Traceback (most recent call last):
            ...
        mail_proxy.exceptions.MailProxyError: Source image not found: /tmp/nonexistent.png
    """
    source = Path(source_path)
    if not source.exists():
        raise MailProxyError(f"Source image not found: {source_path}")

    content = source.read_bytes()
    sha256 = hashlib.sha256(content).hexdigest()
    suffix = source.suffix or ".png"
    target_name = f"{sha256}{suffix}"
    target_dir = get_signatures_dir()
    target_path = target_dir / target_name

    if not target_path.exists():
        shutil.copy2(source, target_path)

    return target_name
