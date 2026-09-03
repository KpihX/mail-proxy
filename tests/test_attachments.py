"""Attachment download destinations and account-scoped defaults."""

from mail_proxy.actions import attachments
from mail_proxy.actions.attachments import AttachmentDownloadPayload, attachment_download


class _FakeImap:
    """Return one deterministic attachment without a network connection."""

    def download_attachment(self, uid, filename, folder):
        assert (uid, filename, folder) == (42, "diagram.png", "INBOX")
        return b"image-bytes", "image/png"


class _FakeClient:
    """Minimal attachment-download client with an account identity."""

    account = type("Account", (), {"id": "account-one"})()

    def imap(self):
        return _FakeImap()


def test_attachment_download_defaults_to_account_scoped_mail_proxy_dir(tmp_path, monkeypatch):
    """The omitted destination creates ~/Downloads/Mail-Proxy/<account-id> on demand."""
    monkeypatch.setattr(attachments.Path, "home", lambda: tmp_path)

    result = attachment_download(
        _FakeClient(), AttachmentDownloadPayload(account_id="poly", uid=42, filename="diagram.png")
    )

    expected = tmp_path / "Downloads" / "Mail-Proxy" / "account-one" / "diagram.png"
    assert result["saved_to"] == str(expected)
    assert expected.read_bytes() == b"image-bytes"


def test_attachment_download_accepts_an_explicit_directory(tmp_path):
    """A trailing-slash save_path keeps the attachment filename under that directory."""
    destination = tmp_path / "Downloads" / "Mail-Proxy"

    result = attachment_download(
        _FakeClient(),
        AttachmentDownloadPayload(
            account_id="poly",
            uid=42,
            filename="diagram.png",
            save_path=f"{destination}/",
        ),
    )

    expected = destination / "diagram.png"
    assert result["saved_to"] == str(expected)
    assert expected.read_bytes() == b"image-bytes"


def test_attachment_download_preserves_an_explicit_file_path(tmp_path):
    """A file-shaped save_path remains available for deliberate renaming."""
    destination = tmp_path / "custom" / "renamed-image.png"

    result = attachment_download(
        _FakeClient(),
        AttachmentDownloadPayload(
            account_id="poly",
            uid=42,
            filename="diagram.png",
            save_path=str(destination),
        ),
    )

    assert result["saved_to"] == str(destination)
    assert destination.read_bytes() == b"image-bytes"
