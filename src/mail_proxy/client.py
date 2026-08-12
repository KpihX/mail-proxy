"""
MailClient — the single object every action handler receives.

It owns the IMAP connection (lazy, cached for the invocation) and hands out
stateless SMTP senders. Handlers never touch the transport directly: they use
`client.imap()` and `client.smtp()`, exactly like `TickClient` exposes its
verbs to the tick-proxy handlers.
"""

from typing import Any

from .api.imap import IMAPClient
from .api.smtp import SMTPClient
from .config import AccountDef, get_account


class MailClient:
    """Facade over IMAP + SMTP used by all 24 actions.

    Examples:
        >>> MailClient().imap().get_folder_status("INBOX").message_count
        128
        >>> MailClient("poly").account.id
        'poly'
    """

    def __init__(self, account_id: str | None = None) -> None:
        self.account: AccountDef = get_account(account_id)
        self._imap: IMAPClient | None = None

    def close(self) -> None:
        """Release the IMAP connection (idempotent).

        Returns:
            None

        Examples:
            >>> c = MailClient(); c.close()
            >>> c.close()      # idempotent
        """
        if self._imap is not None:
            self._imap.disconnect()
            self._imap = None

    def imap(self) -> IMAPClient:
        """Return the shared IMAP connection, connecting on first use.

        Returns:
            IMAPClient: Connected and authenticated.

        Examples:
            >>> MailClient().imap().list_folders()[0].name
            'INBOX'
        """
        if self._imap is None:
            self._imap = IMAPClient(self.account).connect()
        return self._imap

    def smtp(self) -> SMTPClient:
        """Return a stateless SMTP sender for the account.

        Returns:
            SMTPClient: Sends connect-per-call.

        Examples:
            >>> MailClient().smtp().account.id
            'poly'
        """
        return SMTPClient(self.account)

    def raw_imaplib(self) -> Any:
        """Return the raw imaplib connection for expert escape-hatch commands.

        The `raw` action runs on a DEDICATED connection (see actions/raw.py),
        so this returns the low-level object of the shared connection.

        Returns:
            Any: The imaplib.IMAP4 instance owned by the shared IMAPClient.

        Examples:
            >>> MailClient().raw_imaplib() is not None
            True
        """
        return self.imap()._c()._imap
