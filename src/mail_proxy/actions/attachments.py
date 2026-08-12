"""Attachment actions — download to a file or ingest as Base64."""

import base64
from pathlib import Path

from pydantic import Field

from ..client import MailClient
from .base import AccountScoped, ActionDef


class AttachmentDownloadPayload(AccountScoped):
    """Payload of `attachment-download`.

    Attributes:
        uid (int): Message UID (from `message-info` → attachments list).
        filename (str): Exact attachment filename as listed.
        save_path (str | None): Absolute destination path (default
            /tmp/mail_attachments/<filename>).
        folder (str): Folder the message lives in.
        ingest_base64 (bool): True → return `data_base64` instead of saving.

    Examples:
        >>> AttachmentDownloadPayload(uid=42, filename="report.pdf").folder
        'INBOX'
    """

    uid: int = Field(..., description="Message UID")
    filename: str = Field(..., description="Exact attachment filename")
    save_path: str | None = Field(None, description="Absolute save path")
    folder: str = Field("INBOX", description="Folder of the message")
    ingest_base64: bool = Field(
        False, description="Return data_base64 instead of saving"
    )


def attachment_download(client: MailClient, p: AttachmentDownloadPayload) -> dict:
    """Download an attachment from a message.

    By default it saves to a local file; with `ingest_base64:true` it returns
    the data directly in the envelope (`data_base64`).

    Parameters:
        - uid (int): Message UID.
        - filename (str): Exact filename returned by `message-info`.
        - save_path (str | None): Absolute path (default:
          /tmp/mail_attachments/<filename>).
        - folder (str): Folder of the message (default INBOX).
        - ingest_base64 (bool): True → return `data_base64` instead of saving.
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Save to the default directory:
            `mail-proxy do attachment-download '{"uid":42,"filename":"report.pdf"}'`
            → {"saved_to":"/tmp/mail_attachments/report.pdf","filename":"report.pdf","size_bytes":2048,"account":"poly"}
        - Ingest as Base64:
            `mail-proxy do attachment-download '{"uid":42,"filename":"report.pdf","ingest_base64":true}'`
            → {"filename":"report.pdf","content_type":"application/pdf","size_bytes":2048,"data_base64":"JVBERi0xLjQK…","account":"poly"}
        - Save to an explicit path:
            `mail-proxy do attachment-download '{"uid":42,"filename":"report.pdf","save_path":"/home/kpihx/Work/downloads/report.pdf"}'`
            → {"saved_to":"/home/kpihx/Work/downloads/report.pdf","filename":"report.pdf","size_bytes":2048,"account":"poly"}
    """
    imap = client.imap()
    data, content_type = imap.download_attachment(p.uid, p.filename, p.folder)

    if p.ingest_base64:
        if not data:
            raise ValueError(f"Attachment {p.filename!r} data is empty (Base64 mode).")
        return {
            "filename": p.filename,
            "content_type": content_type,
            "size_bytes": len(data),
            "data_base64": base64.b64encode(data).decode("utf-8"),
            "account": client.account.id,
        }

    if not data:
        raise ValueError(f"Attachment {p.filename!r} data is empty (File save mode).")
    dest = (
        Path(p.save_path) if p.save_path else Path("/tmp/mail_attachments") / p.filename
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {
        "saved_to": str(dest),
        "filename": p.filename,
        "size_bytes": len(data),
        "account": client.account.id,
    }


ACTIONS = [
    ActionDef(
        "attachment-download",
        AttachmentDownloadPayload,
        attachment_download,
        group="Attachments",
    ),
]
