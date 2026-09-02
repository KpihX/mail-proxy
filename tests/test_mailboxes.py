"""Mailbox codec negotiation and semantic special-use resolution."""

import pytest

from mail_proxy import config
from mail_proxy.api import imap as imap_api


class _Utf8RawClient:
    """Minimal authenticated imaplib surface accepting UTF8=ACCEPT."""

    capabilities = ("IMAP4REV1", "ENABLE", "UTF8=ACCEPT")

    def __init__(self) -> None:
        self.utf8_enabled = False
        self.enabled: list[str] = []

    def enable(self, capability: str) -> tuple[str, list[bytes]]:
        self.enabled.append(capability)
        self.utf8_enabled = True
        return "OK", [b"UTF8=ACCEPT"]


class _Utf8NegotiatingClient:
    """Minimal imapclient surface used to verify post-login negotiation."""

    def __init__(self) -> None:
        self.folder_encode = True
        self._imap = _Utf8RawClient()

    def login(self, _username: str, _password: str | None) -> None:
        """Authenticate successfully."""

    def capabilities(self) -> tuple[str, ...]:
        """Return post-auth capabilities."""
        return self._imap.capabilities


def test_connect_activates_utf8_mailboxes_after_login(monkeypatch: pytest.MonkeyPatch):
    """Successful UTF8=ACCEPT switches mailbox names to raw UTF-8 only then."""
    raw_client = _Utf8NegotiatingClient()
    monkeypatch.setattr(imap_api.imapclient, "IMAPClient", lambda **_kwargs: raw_client)

    client = imap_api.IMAPClient(config.get_account("poly")).connect()

    assert raw_client._imap.enabled == ["UTF8=ACCEPT"]
    assert raw_client.folder_encode is False
    assert client._c() is raw_client


class _FolderListingClient:
    """Server folder list containing a localized special-use Sent mailbox."""

    def list_folders(self):
        return [
            ((b"\\HasNoChildren",), b"/", "INBOX"),
            ((b"\\HasNoChildren", b"\\Sent"), b"/", "Messages envoyés"),
        ]


def test_sent_folder_resolves_special_use_attribute(monkeypatch: pytest.MonkeyPatch):
    """Localized Sent names resolve through the server's `\\Sent` declaration."""
    client = imap_api.IMAPClient(config.get_account("poly"))
    folder_client = _FolderListingClient()
    monkeypatch.setattr(client, "_c", lambda: folder_client)

    assert client.sent_folder() == "Messages envoyés"
