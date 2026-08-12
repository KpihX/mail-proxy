"""
Custom exceptions for mail-proxy. Prevents stack traces and hides secrets.
"""


class MailProxyError(Exception):
    """Base exception for all mail-proxy errors.

    Raised for configuration problems, payload problems and any IMAP/SMTP
    failure that must reach the user as a clean one-line message instead of a
    Python traceback (secrets must never leak through a stack trace).

    Args:
        message (str): Human-readable, actionable error text. Should tell the
            user what to run next when a fix exists.

    Examples:
        >>> raise MailProxyError("Config not found. Run 'mail-proxy admin setup'.")
        MailProxyError: Config not found. Run 'mail-proxy admin setup'.
        >>> str(MailProxyError("IMAP login failed"))
        'IMAP login failed'
    """

    def __init__(self, message: str):
        super().__init__(message)


class MailAPIError(MailProxyError):
    """An IMAP/SMTP exchange failed with a non-recoverable status.

    Args:
        status (int): HTTP-like status code (0 when the failure happened before
            any exchange, e.g. DNS/socket error or login rejection).
        message (str): Explanation plus the recommended fix.

    Examples:
        >>> MailAPIError(0, "IMAP login rejected").status
        0
        >>> str(MailAPIError(429, "Rate limited by the mail server"))
        '[429] Rate limited by the mail server'
    """

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"[{status}] {message}")
