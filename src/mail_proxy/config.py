"""
Minimal .env config loader for mail-proxy.

Single source of truth: ~/.config/mail-proxy/.env — no config.yaml, no in-repo
.env, no cache. Account definitions (hosts, ports, e-mail, signature) live here
as documented constants and every one of their fields is overridable from that
same .env file.

Password policy (KπX directive, same as tick-proxy): credentials are NEVER
committed. The .env holds at most the per-account MAIL_<ACCOUNT>_LOGIN and
MAIL_<ACCOUNT>_PASS pairs plus optional endpoint overrides, all chmod 600.
"""

import os
from pathlib import Path

from pydantic import BaseModel, Field

from .exceptions import MailProxyError

CONFIG_DIR = Path.home() / ".config" / "mail-proxy"
ENV_PATH = CONFIG_DIR / ".env"

# ── Permissions (single source of truth) ──────────────────────────────────────
DIR_PERMISSIONS = 0o700
FILE_PERMISSIONS = 0o600

# ── Endpoint defaults (documented constants, all overridable via .env) ─────────
DEFAULT_TIMEOUT = 15.0
ENV_TIMEOUT = "MAIL_TIMEOUT"

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
    port: int = 993
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
    port: int = 587
    starttls: bool = True


class SignatureDef(BaseModel):
    """E-mail signature shown below the body of composed messages.

    Attributes:
        before_logo (str): Text lines above the logo image.
        logo_path (str): Logo file relative to the package dir ("" = none).
        after_logo (str): Text lines below the logo image.

    Examples:
        >>> SignatureDef(before_logo="Ivann KAMDEM", after_logo="ÉCOLE POLYTECHNIQUE").before_logo
        'Ivann KAMDEM'
        >>> SignatureDef().logo_path
        ''
    """

    before_logo: str = ""
    logo_path: str = ""
    after_logo: str = ""


class AccountDef(BaseModel):
    """One mail account — non-sensitive definition (secrets live in `.env`).

    The credential env-var names are derived from the id: `MAIL_<ID_UPPER>_LOGIN`
    and `MAIL_<ID_UPPER>_PASS` — see `account_env_prefix()`.

    Attributes:
        id (str): Stable account id, used as `account_id` in every action
            payload and as the env prefix, e.g. `poly`.
        label (str): Human-readable label, e.g. `Polytechnique (X)`.
        imap (ImapEndpoint): IMAP endpoint.
        smtp (SmtpEndpoint): SMTP endpoint.
        email (str): Full address for the From header and SMTP envelope.
        display_name (str): Human name shown in the From header.
        signature (SignatureDef): Signature block of this account.
        default (bool): True = used when `account_id` is omitted.

    Examples:
        >>> AccountDef(id="poly", imap=ImapEndpoint(host="imap.x.fr"),
        ...             smtp=SmtpEndpoint(host="smtp.x.fr"), default=True).default
        True
        >>> AccountDef(id="poly", imap=ImapEndpoint(host="imap.x.fr"),
        ...             smtp=SmtpEndpoint(host="smtp.x.fr")).id
        'poly'
    """

    id: str
    label: str = ""
    imap: ImapEndpoint
    smtp: SmtpEndpoint
    email: str = ""
    display_name: str = ""
    signature: SignatureDef = Field(default_factory=SignatureDef)
    default: bool = False
    # Resolved at runtime by `resolve_account()` — populated from .env, excluded
    # from serialization (never dumped anywhere).
    username: str = Field(default="", exclude=True)
    password: str = Field(default="", exclude=True)

    @property
    def from_address(self) -> str:
        """Return the SMTP envelope address: `email` if set, else the login.

        Returns:
            str: The full sender address.

        Examples:
            >>> AccountDef(id="poly", imap=ImapEndpoint(host="imap.x.fr"),
            ...             smtp=SmtpEndpoint(host="smtp.x.fr"),
            ...             email="ivann@polytechnique.edu").from_address
            'ivann@polytechnique.edu'
            >>> AccountDef(id="poly", imap=ImapEndpoint(host="imap.x.fr"),
            ...             smtp=SmtpEndpoint(host="smtp.x.fr"),
            ...             username="ivann").from_address
            'ivann'
        """
        return self.email or self.username


# ── Account catalog (single source of truth — add an account here) ─────────────
#
# Adding an account = one AccountDef below + the matching MAIL_<ID>_LOGIN and
# MAIL_<ID>_PASS keys in ~/.config/mail-proxy/.env (see .env.example). Hosts,
# ports, e-mail and display name can each be overridden per account with
# MAIL_<ID>_{IMAP_HOST,IMAP_PORT,IMAP_TLS,SMTP_HOST,SMTP_PORT,SMTP_STARTTLS,
# EMAIL,DISPLAY_NAME} from the same .env.
ACCOUNTS: list[AccountDef] = [
    AccountDef(
        id="poly",
        label="Polytechnique (X)",
        imap=ImapEndpoint(host="webmail.polytechnique.fr", port=993, tls=True),
        smtp=SmtpEndpoint(host="webmail.polytechnique.fr", port=587, starttls=True),
        email="ivann.kamdem-pouokam@polytechnique.edu",
        display_name="Ivann KAMDEM POUOKAM",
        default=True,
        signature=SignatureDef(
            before_logo="Ivann KAMDEM\nEIX X2024",
            logo_path="assets/signature_logo.png",
            after_logo=(
                "ÉCOLE POLYTECHNIQUE\n91128 PALAISEAU CEDEX\nT. +33(0)605957785\n"
                "ivann.kamdem-pouokam@polytechnique.edu"
            ),
        ),
    ),
]


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
        >>> load_env()
        {'MAIL_POLY_LOGIN': 'ivann.kamdem-pouokam', 'MAIL_POLY_PASS': '…'}
        >>> load_env()          # when ~/.config/mail-proxy/.env is absent
        {}
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
            ``{"MAIL_POLY_LOGIN": "ivann.kamdem-pouokam", "MAIL_POLY_PASS": "…"}``.

    Returns:
        None: Writes the file and sets 0600 permissions.

    Examples:
        >>> write_env({"MAIL_POLY_LOGIN": "ivann.kamdem-pouokam"})  # 1 key written
        >>> write_env({})                                            # file now empty
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
        >>> _get_int("MAIL_POLY_IMAP_PORT", 993)
        993
        >>> _get_int("MAIL_TIMEOUT", 15)
        15
    """
    raw = os.environ.get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def resolve_account(account: AccountDef) -> AccountDef:
    """Apply .env overrides (secrets + endpoints) to an account definition.

    Reads MAIL_<ID>_LOGIN / MAIL_<ID>_PASS plus the optional EMAIL,
    DISPLAY_NAME and IMAP/SMTP endpoint overrides from the environment.

    Args:
        account (AccountDef): The definition to resolve.

    Returns:
        AccountDef: A copy with username/password and any override applied.

    Examples:
        >>> resolve_account(ACCOUNTS[0]).username
        'ivann.kamdem-pouokam'
        >>> resolve_account(ACCOUNTS[0]).password == ""
        True
    """
    prefix = account_env_prefix(account.id)
    resolved = account.model_copy()
    resolved.username = os.environ.get(f"{prefix}LOGIN", "")
    resolved.password = os.environ.get(f"{prefix}PASS", "")
    resolved.email = os.environ.get(f"{prefix}EMAIL", account.email)
    resolved.display_name = os.environ.get(
        f"{prefix}DISPLAY_NAME", account.display_name
    )
    resolved.imap = ImapEndpoint(
        host=os.environ.get(f"{prefix}IMAP_HOST", account.imap.host),
        port=_get_int(f"{prefix}IMAP_PORT", account.imap.port),
        tls=_get_bool(f"{prefix}IMAP_TLS", account.imap.tls),
    )
    resolved.smtp = SmtpEndpoint(
        host=os.environ.get(f"{prefix}SMTP_HOST", account.smtp.host),
        port=_get_int(f"{prefix}SMTP_PORT", account.smtp.port),
        starttls=_get_bool(f"{prefix}SMTP_STARTTLS", account.smtp.starttls),
    )
    return resolved


def get_account(account_id: str | None = None) -> AccountDef:
    """Return the resolved account for an id (or the default account).

    Args:
        account_id (str | None): Account id; None → the default account.

    Returns:
        AccountDef: The resolved account (credentials + endpoint overrides).

    Raises:
        MailProxyError: When the id is unknown or no default is declared.

    Examples:
        >>> get_account("poly").id
        'poly'
        >>> get_account().id      # default account
        'poly'
    """
    if account_id:
        for account in ACCOUNTS:
            if account.id == account_id:
                return resolve_account(account)
        raise MailProxyError(
            f"Unknown account {account_id!r}. Known accounts: "
            f"{', '.join(a.id for a in ACCOUNTS)}."
        )
    for account in ACCOUNTS:
        if account.default:
            return resolve_account(account)
    raise MailProxyError("No default account declared in config.py.")


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

    Args:
        account_id (str | None): Account to validate; None → default account.

    Returns:
        None: Returns silently when the configuration is usable.

    Raises:
        MailProxyError: With the exact command to run as a fix.

    Examples:
        >>> ensure_env()                  # default account credentials present
        >>> ensure_env("poly")
        MailProxyError: Config file not found at …/.env. Run 'mail-proxy admin setup'.
    """
    if not ENV_PATH.exists():
        raise MailProxyError(
            f"Config file not found at {ENV_PATH}. Run 'mail-proxy admin setup' first."
        )
    load_env()
    account = get_account(account_id)
    prefix = account_env_prefix(account.id)
    if not account.username or not account.password:
        raise MailProxyError(
            f"Account {account.id!r} is missing {prefix}LOGIN or {prefix}PASS. "
            "Run 'mail-proxy admin setup' to configure."
        )
