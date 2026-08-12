"""
mail-proxy: mail administrative proxy — RPC CLI for IMAP/SMTP accounts, messages,
folders and labels.

Config: ~/.config/mail-proxy/.env (MAIL_<ACCOUNT>_LOGIN, MAIL_<ACCOUNT>_PASS).
Credentials live only in that chmod-600 file — see `admin setup`.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mail-proxy")
except PackageNotFoundError:  # pragma: no cover - only when running from source tree
    __version__ = "0.0.0"
