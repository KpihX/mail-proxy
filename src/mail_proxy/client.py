"""
MailClient — the single object every action handler receives.

It owns the IMAP connection (lazy, cached for the invocation) and hands out
stateless SMTP senders. Handlers never touch the transport directly: they use
``client.imap()`` and ``client.smtp()``, exactly like ``TickClient`` exposes its
verbs to the tick-proxy handlers.

For custom accounts (provider_type="custom"), the password is prompted on first
use (getpass, like sudo) and cached in the system keyring with a TTL.
"""

from .api.imap import IMAPClient
from .api.smtp import SMTPClient
from .config import AccountDef, get_account
from .exceptions import MailProxyError


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

    def _is_custom_password(self) -> bool:
        """Check if this account uses keyring-based custom password auth.

        Returns:
            bool: True when provider_type is "custom" and auth is password-based.

        Examples:
            >>> c = MailClient.__new__(MailClient)
            >>> c.account = AccountDef(id="t", email="a@b.com",
            ...     imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y"),
            ...     provider_type="custom")
            >>> c._is_custom_password()
            True
        """
        return (
            self.account.provider_type == "custom"
            and self.account.auth_method == "password"
        )

    def _prompt_and_cache(self) -> None:
        """Prompt for password (getpass), cache in keyring with TTL.

        Raises:
            MailProxyError: On bad password or user cancel.

        Examples:
            >>> # Interactive: prompts "Password for zimbra (user@host): "
            >>> c = MailClient.__new__(MailClient)
            >>> c.account = AccountDef(id="t", email="a@b.com", imap=ImapEndpoint(host="x"), smtp=SmtpEndpoint(host="y"), provider_type="custom")
            >>> c._prompt_and_cache()
        """
        from .secrets import clear_cached_password, prompt_password, set_cached_password

        password = prompt_password(self.account.id, self.account.email)
        self.account.password = password

        # Verify against IMAP — this IS the check (not stored on disk, only in keyring on success)
        try:
            self._imap = IMAPClient(self.account).connect()
        except MailProxyError:
            clear_cached_password(self.account.id)
            self.account.password = ""
            raise
        else:
            set_cached_password(self.account.id, password)

    def imap(self) -> IMAPClient:
        """Return the shared IMAP connection, connecting on first use.

        For custom accounts, prompts for password on first use (sudo-like),
        verifies via IMAP connect, and caches in the system keyring with TTL.
        Re-prompts after TTL expiry.

        Returns:
            IMAPClient: Connected and authenticated.

        Examples:
            >>> MailClient().imap().list_folders()[0].name
            'INBOX'
        """
        if self._imap is None:
            if self._is_custom_password() and not self.account.password:
                self._prompt_and_cache()
                # _prompt_and_cache may have set self._imap on success
                if self._imap is not None:
                    return self._imap
            self._imap = IMAPClient(self.account).connect()
        return self._imap

    def smtp(self) -> SMTPClient:
        """Return a stateless SMTP sender for the account.

        For custom accounts, reuses the cached password from keyring.

        Returns:
            SMTPClient: Sends connect-per-call.

        Examples:
            >>> MailClient().smtp().account.id
            'poly'
        """
        return SMTPClient(self.account)
