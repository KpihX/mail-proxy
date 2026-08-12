"""Low-level IMAP/SMTP layer of mail-proxy (ported from mail-mcp core)."""

from .imap import IMAPClient
from .models import (
    Address,
    Attachment,
    Flag,
    Folder,
    Message,
    MessageSummary,
    SearchCriteria,
)
from .smtp import SMTPClient

__all__ = [
    "Address",
    "Attachment",
    "Flag",
    "Folder",
    "IMAPClient",
    "Message",
    "MessageSummary",
    "SMTPClient",
    "SearchCriteria",
]
