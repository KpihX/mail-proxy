"""IMAP connection error translation must never leak raw transport exceptions."""

import socket
import ssl

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError, ProtocolError

from mail_proxy import config
from mail_proxy.api import imap as imap_api
from mail_proxy.exceptions import MailAPIError


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (TimeoutError("slow server"), "connection to .* timed out"),
        (socket.gaierror(-2, "unknown host"), "DNS resolution failed"),
        (ssl.SSLError("bad certificate"), "TLS negotiation failed"),
        (ConnectionRefusedError("refused"), "connection refused"),
        (OSError("network unavailable"), "network I/O failed"),
    ],
)
def test_connect_translates_socket_open_failures(monkeypatch, error, expected_message):
    """Each socket-open failure has a distinct actionable MailAPIError."""
    account = config.get_account("poly")

    def raise_during_open(**_kwargs):
        raise error

    monkeypatch.setattr(imap_api.imapclient, "IMAPClient", raise_during_open)

    with pytest.raises(MailAPIError, match=expected_message):
        imap_api.IMAPClient(account).connect()


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (LoginError("invalid credentials"), "login rejected"),
        (TimeoutError("slow server"), "login to .* timed out"),
        (socket.gaierror(-2, "unknown host"), "DNS resolution failed"),
        (ssl.SSLError("bad certificate"), "TLS negotiation failed"),
        (ConnectionRefusedError("refused"), "connection refused"),
        (OSError("network unavailable"), "network I/O failed"),
        (IMAPClientAbortError("server closed"), "connection aborted"),
        (ProtocolError("invalid response"), "protocol error"),
        (IMAPClientError("unexpected response"), "login failed"),
    ],
)
def test_connect_translates_login_failures(monkeypatch, error, expected_message):
    """Each authentication-stage transport/protocol failure stays inside the API boundary."""
    account = config.get_account("poly")

    class FailingLoginClient:
        def login(self, _username, _password):
            raise error

    monkeypatch.setattr(
        imap_api.imapclient, "IMAPClient", lambda **_kwargs: FailingLoginClient()
    )

    with pytest.raises(MailAPIError, match=expected_message):
        imap_api.IMAPClient(account).connect()
