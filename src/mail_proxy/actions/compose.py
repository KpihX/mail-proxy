"""Compose actions — send, reply, forward, save draft. ALL require HITL.

These are the only actions that reach OTHER people (or create message content
in the mailbox): every one passes through the HITL review form, and the
approved payload is what gets submitted.
"""

import time
from datetime import UTC, datetime, timedelta

from pydantic import Field

from ..api.imap import IMAPClient
from ..api.models import SearchCriteria
from ..client import MailClient
from .base import AccountScoped, action_def, require_approval


class ComposeBase(AccountScoped):
    """Shared fields of every compose payload.

    Attributes:
        signature (str): "default" → configured signature with logo | "" → none
            | any text → custom plain-text signature.
        verify_bounce_window_seconds (int): >0 → wait N seconds after send,
            then scan INBOX for an immediate DSN/bounce.

    Examples:
        >>> ComposeBase().signature
        'default'
        >>> ComposeBase(verify_bounce_window_seconds=30).verify_bounce_window_seconds
        30
    """

    signature: str = Field(
        "default",
        description='"default" | "" (none) | any custom plain-text signature',
    )
    verify_bounce_window_seconds: int = Field(
        0, description=">0: wait N seconds then probe INBOX for a bounce (DSN)"
    )


class MessageSendPayload(ComposeBase):
    """Payload of `message-send`.

    Attributes:
        to (list[str]): Recipient addresses.
        subject (str): Subject line.
        body_text (str): Plain-text body (required).
        body_html (str): Optional HTML body.
        cc (list[str] | None): Visible carbon copies.
        bcc (list[str] | None): Blind copies — SMTP envelope only.
        attachments (list[str] | None): Absolute local file paths.

    Examples:
        >>> MessageSendPayload(to=["a@b.fr"], subject="Hi", body_text="Hello").subject
        'Hi'
    """

    to: list[str] = Field(..., description="Recipient addresses")
    subject: str = Field(..., description="Subject line")
    body_text: str = Field(..., description="Plain-text body")
    body_html: str = Field("", description="Optional HTML body")
    cc: list[str] | None = Field(None, description="Visible carbon copies")
    bcc: list[str] | None = Field(None, description="Blind copies (envelope only)")
    attachments: list[str] | None = Field(
        None, description="Absolute local file paths to attach"
    )


class MessageReplyPayload(ComposeBase):
    """Payload of `message-reply`.

    Attributes:
        uid (int): UID of the message being answered.
        body_text (str): Reply body.
        body_html (str): Optional HTML body.
        reply_all (bool): Include all original recipients in CC.
        bcc (list[str] | None): Blind copies — SMTP envelope only.
        attachments (list[str] | None): Absolute local file paths to attach.
        folder (str): Folder of the original message.

    Examples:
        >>> MessageReplyPayload(uid=42, body_text="Thanks").uid
        42
    """

    uid: int = Field(..., description="UID of the message being answered")
    body_text: str = Field(..., description="Reply body")
    body_html: str = Field("", description="Optional HTML body")
    reply_all: bool = Field(False, description="Reply to all original recipients")
    bcc: list[str] | None = Field(None, description="Blind copies (envelope only)")
    attachments: list[str] | None = Field(
        None, description="Absolute local file paths to attach"
    )
    folder: str = Field("INBOX", description="Folder of the original message")
    subject_override: str | None = Field(
        None,
        description="Replace the auto 'Re: <original>' subject with this exact text",
    )


class MessageForwardPayload(ComposeBase):
    """Payload of `message-forward`.

    Attributes:
        uid (int): UID of the message being forwarded.
        to (list[str]): New recipients.
        body_text (str): Text prepended above the forwarded content.
        cc (list[str] | None): Visible carbon copies.
        bcc (list[str] | None): Blind copies — SMTP envelope only.
        folder (str): Folder of the original message.

    Examples:
        >>> MessageForwardPayload(uid=42, to=["c@d.fr"]).uid
        42
    """

    uid: int = Field(..., description="UID of the message being forwarded")
    to: list[str] = Field(..., description="New recipients")
    body_text: str = Field("", description="Text prepended above the forward")
    cc: list[str] | None = Field(None, description="Visible carbon copies")
    bcc: list[str] | None = Field(None, description="Blind copies (envelope only)")
    folder: str = Field("INBOX", description="Folder of the original message")
    subject_override: str | None = Field(
        None,
        description="Replace the auto 'Fwd: <original>' subject with this exact text",
    )


class MessageDraftPayload(AccountScoped):
    """Payload of `message-draft`.

    Attributes:
        to (list[str]): Recipient addresses.
        subject (str): Subject line.
        body_text (str): Plain-text body.
        body_html (str): Optional HTML body.
        cc (list[str] | None): Visible carbon copies.
        bcc (list[str] | None): Blind copies — SMTP envelope only.
        signature (str): "default" | "" | custom plain-text signature.
        attachments (list[str] | None): Absolute local file paths.
        drafts_folder (str): Drafts folder name (default "Drafts").

    Examples:
        >>> MessageDraftPayload(to=["a@b.fr"], subject="Hi", body_text="Hello").drafts_folder
        'Drafts'
    """

    to: list[str] = Field(..., description="Recipient addresses")
    subject: str = Field(..., description="Subject line")
    body_text: str = Field(..., description="Plain-text body")
    body_html: str = Field("", description="Optional HTML body")
    cc: list[str] | None = Field(None, description="Visible carbon copies")
    bcc: list[str] | None = Field(None, description="Blind copies (envelope only)")
    signature: str = Field(
        "default",
        description='"default" | "" (none) | any custom plain-text signature',
    )
    attachments: list[str] | None = Field(
        None, description="Absolute local file paths to attach"
    )
    drafts_folder: str = Field("Drafts", description="Drafts folder name")


def _resolve_sent_folder(imap: IMAPClient) -> str:
    """Return the account's server-declared Sent folder.

    Args:
        imap (IMAPClient): Connected IMAP client.

    Returns:
        str: The resolved Sent folder name.

    Examples:
        >>> _resolve_sent_folder(imap)
        'Sent'
    """
    return imap.sent_folder()


def _save_copy_to_sent(
    client: MailClient,
    *,
    to: list[str],
    subject: str,
    body_text: str,
    body_html: str = "",
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    signature: str = "default",
    attachments: list[str] | None = None,
) -> tuple[bool, str]:
    """Append a copy of the sent message to the account's Sent folder.

    Args:
        client (MailClient): The mail client (own IMAP connection).
        to (list[str]): Recipients.
        subject (str): Subject line.
        body_text (str): Body text.
        body_html (str): Optional HTML body.
        cc (list[str] | None): Visible carbon copies.
        bcc (list[str] | None): Blind copies.
        signature (str): Signature parameter.
        attachments (list[str] | None): File paths.

    Returns:
        tuple[bool, str]: (saved, sent_folder).

    Examples:
        >>> _save_copy_to_sent(client, to=["a@b.fr"], subject="Hi", body_text="Hello")
        (True, 'Sent')
    """
    smtp = client.smtp()
    raw_bytes, _ = smtp.build_draft_bytes(
        to=to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        cc=cc,
        bcc=bcc,
        signature=signature,
        attachments=attachments,
    )
    imap = client.imap()
    sent_folder = _resolve_sent_folder(imap)
    imap.append_message(sent_folder, raw_bytes, flags=[])
    return True, sent_folder


def _detect_bounce_for_message_id(client: MailClient, message_id: str) -> dict | None:
    """Look for a DSN/bounce in INBOX referencing the outbound Message-ID.

    Args:
        client (MailClient): The mail client.
        message_id (str): The outbound Message-ID.

    Returns:
        dict | None: Bounce metadata, or None when no DSN references it.

    Examples:
        >>> _detect_bounce_for_message_id(client, "<abc@host>")
        {'uid': 7, 'subject': 'Undelivered Mail Returned to Sender', …}
    """
    now_utc = datetime.now(UTC)
    since_dt = now_utc - timedelta(days=1)
    needle_raw = (message_id or "").strip()
    needle_compact = needle_raw.strip("<>")

    imap = client.imap()
    uids = imap.search(
        SearchCriteria(
            folder="INBOX",
            sender="MAILER-DAEMON",
            since=since_dt,
            limit=50,
        )
    )
    if not uids:
        return None
    summaries = imap.fetch_summaries(uids, "INBOX")
    summaries = sorted(
        summaries,
        key=lambda m: m.date or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    for summary in summaries:
        msg = imap.fetch_message(summary.uid, "INBOX")
        if msg is None:
            continue
        haystack = "\n".join(
            [
                msg.subject or "",
                msg.body_text or "",
                msg.in_reply_to or "",
                msg.message_id or "",
                "\n".join(msg.references or []),
            ]
        )
        if (needle_raw and needle_raw in haystack) or (
            needle_compact and needle_compact in haystack
        ):
            return {
                "uid": msg.uid,
                "subject": msg.subject,
                "date": msg.date.isoformat() if msg.date else "",
                "from": msg.sender.email if msg.sender else "",
                "folder": msg.folder,
            }
    return None


def _delivery_probe(
    client: MailClient, message_id: str, verify_bounce_window_seconds: int
) -> tuple[str, dict | None]:
    """Return (delivery_status, bounce_details) after an optional bounded wait.

    Args:
        client (MailClient): The mail client.
        message_id (str): The outbound Message-ID.
        verify_bounce_window_seconds (int): Wait window (0 = skip).

    Returns:
        tuple[str, dict | None]: ("unknown"|"bounced"|"delivered_hint", details).

    Examples:
        >>> _delivery_probe(client, "<abc@host>", 0)
        ('unknown', None)
    """
    if verify_bounce_window_seconds <= 0:
        return "unknown", None
    time.sleep(max(0, verify_bounce_window_seconds))
    bounce = _detect_bounce_for_message_id(client, message_id)
    if bounce:
        return "bounced", bounce
    return "delivered_hint", None


@require_approval()
def message_send(client: MailClient, p: MessageSendPayload) -> dict:
    """Send a new email message (HITL required).

    Submits via SMTP, then appends a copy to the account's Sent folder. With
    `verify_bounce_window_seconds > 0` it waits that window and scans INBOX for
    an immediate DSN/bounce referencing the outbound Message-ID.

    Parameters:
        - to (list[str]): Recipient addresses.
        - subject (str): Subject line.
        - body_text (str): Plain-text body (required).
        - body_html (str): Optional HTML body.
        - cc (list[str] | None): Visible carbon copies.
        - bcc (list[str] | None): Blind copies — added to the SMTP envelope
          only, never visible in headers.
        - signature (str): "default" | "" | custom plain-text.
        - attachments (list[str] | None): Absolute local file paths.
        - verify_bounce_window_seconds (int): >0 → bounded DSN probe.
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Simple send:
            `mail-proxy do message-send '{"to":["x@y.fr"],"subject":"Rendez-vous","body_text":"Dispo demain 15h ?"}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<a1b2c3d4@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"unknown","bounce_details":null}
        - Send with BCC, signature and bounce probe:
            `mail-proxy do message-send '{"to":["x@y.fr"],"bcc":["z@w.fr"],"subject":"Rapport","body_text":"Ci-joint.","attachments":["/tmp/report.pdf"],"verify_bounce_window_seconds":60}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<a1b2c3d4@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"delivered_hint","bounce_details":null}
        - No signature, HTML body:
            `mail-proxy do message-send '{"to":["x@y.fr"],"subject":"Bienvenue","body_text":"Texte","body_html":"<p>Texte</p>","signature":""}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<a1b2c3d4@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"unknown","bounce_details":null}
    """
    smtp = client.smtp()
    mid = smtp.send(
        to=p.to,
        subject=p.subject,
        body_text=p.body_text,
        body_html=p.body_html,
        cc=p.cc,
        bcc=p.bcc,
        signature=p.signature,
        attachments=p.attachments,
    )
    saved_to_sent = False
    sent_folder = ""
    try:
        saved_to_sent, sent_folder = _save_copy_to_sent(
            client,
            to=p.to,
            subject=p.subject,
            body_text=p.body_text,
            body_html=p.body_html,
            cc=p.cc,
            bcc=p.bcc,
            signature=p.signature,
            attachments=p.attachments,
        )
    except Exception:  # noqa: BLE001 - the Sent copy is best-effort; never fail the send
        saved_to_sent = False
    delivery_status, bounce_details = _delivery_probe(
        client, mid, p.verify_bounce_window_seconds
    )
    return {
        "smtp_accepted": True,
        "sent": True,
        "message_id": mid,
        "account": client.account.id,
        "saved_to_sent": saved_to_sent,
        "sent_folder": sent_folder,
        "delivery_status": delivery_status,
        "bounce_details": bounce_details,
    }


@require_approval()
def message_reply(client: MailClient, p: MessageReplyPayload) -> dict:
    """Reply to a message by UID (HITL required).

    Automatically sets Re:, In-Reply-To and References headers. With
    `reply_all:true` the original recipients (minus yourself) move to To/CC.

    Parameters:
        - uid (int): UID of the message being answered.
        - body_text (str): Reply body.
        - body_html (str): Optional HTML body.
        - reply_all (bool): Include all original recipients.
        - bcc (list[str] | None): Blind copies (envelope only).
        - attachments (list[str] | None): Absolute local file paths to attach.
        - signature (str): "default" | "" | custom plain-text.
        - verify_bounce_window_seconds (int): >0 → bounded DSN probe.
        - folder (str): Folder of the original message (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Simple reply:
            `mail-proxy do message-reply '{"uid":312,"body_text":"Merci !"}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<b2c3d4e5@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"unknown","bounce_details":null}
        - Reply-all to a message in Archive:
            `mail-proxy do message-reply '{"uid":99,"body_text":"OK pour vendredi","reply_all":true,"folder":"Archive"}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<b2c3d4e5@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"unknown","bounce_details":null}
        - Reply without signature and with a blind copy:
            `mail-proxy do message-reply '{"uid":312,"body_text":"Voir pièce jointe","bcc":["archive@x.fr"],"signature":""}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<b2c3d4e5@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"unknown","bounce_details":null}
    """
    original = client.imap().fetch_message(p.uid, p.folder)
    if original is None:
        raise ValueError(f"Message UID {p.uid} not found in {p.folder}.")

    smtp = client.smtp()
    mid = smtp.reply(
        original=original,
        body_text=p.body_text,
        body_html=p.body_html,
        reply_all=p.reply_all,
        bcc=p.bcc,
        attachments=p.attachments,
        signature=p.signature,
        subject_override=p.subject_override,
    )
    saved_to_sent = False
    sent_folder = ""
    try:
        if p.subject_override:
            subject = p.subject_override
        else:
            subject = original.subject
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
        to_copy = [original.sender.email] if original.sender else []
        cc_copy: list[str] = []
        if p.reply_all:
            me = client.account.from_address
            to_copy = [
                e for e in {a.email for a in original.recipients} if e != me
            ] or to_copy
            cc_copy = [e for e in {a.email for a in original.cc} if e != me]
        saved_to_sent, sent_folder = _save_copy_to_sent(
            client,
            to=to_copy,
            subject=subject,
            body_text=p.body_text,
            body_html=p.body_html,
            cc=cc_copy or None,
            bcc=p.bcc,
            signature=p.signature,
            attachments=p.attachments,
        )
    except Exception:  # noqa: BLE001 - the Sent copy is best-effort; never fail the send
        saved_to_sent = False
    delivery_status, bounce_details = _delivery_probe(
        client, mid, p.verify_bounce_window_seconds
    )
    return {
        "smtp_accepted": True,
        "sent": True,
        "message_id": mid,
        "account": client.account.id,
        "saved_to_sent": saved_to_sent,
        "sent_folder": sent_folder,
        "delivery_status": delivery_status,
        "bounce_details": bounce_details,
    }


@require_approval()
def message_forward(client: MailClient, p: MessageForwardPayload) -> dict:
    """Forward a message by UID to new recipients (HITL required).

    Prepends a standard forward header and the original body; `body_text` is
    placed above the forwarded content.

    Parameters:
        - uid (int): UID of the message being forwarded.
        - to (list[str]): New recipients.
        - body_text (str): Text prepended above the forward.
        - cc (list[str] | None): Visible carbon copies.
        - bcc (list[str] | None): Blind copies (envelope only).
        - signature (str): "default" | "" | custom plain-text.
        - verify_bounce_window_seconds (int): >0 → bounded DSN probe.
        - folder (str): Folder of the original message (default INBOX).
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Simple forward:
            `mail-proxy do message-forward '{"uid":312,"to":["c@d.fr"],"body_text":"Pour info"}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<c3d4e5f6@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"unknown","bounce_details":null}
        - Forward with CC:
            `mail-proxy do message-forward '{"uid":312,"to":["c@d.fr"],"cc":["e@f.fr"]}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<c3d4e5f6@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"unknown","bounce_details":null}
        - Forward without comment text:
            `mail-proxy do message-forward '{"uid":99,"to":["g@h.fr"],"folder":"Archive"}'`
            → {"smtp_accepted":true,"sent":true,"message_id":"<c3d4e5f6@webmail.polytechnique.fr>","account":"poly","saved_to_sent":true,"sent_folder":"Sent","delivery_status":"unknown","bounce_details":null}
    """
    original = client.imap().fetch_message(p.uid, p.folder)
    if original is None:
        raise ValueError(f"Message UID {p.uid} not found in {p.folder}.")

    smtp = client.smtp()
    mid = smtp.forward(
        original=original,
        to=p.to,
        body_text=p.body_text,
        cc=p.cc,
        bcc=p.bcc,
        signature=p.signature,
        subject_override=p.subject_override,
    )
    saved_to_sent = False
    sent_folder = ""
    try:
        if p.subject_override:
            fwd_subject = p.subject_override
        else:
            fwd_subject = original.subject
            if not fwd_subject.lower().startswith(
                "fwd:"
            ) and not fwd_subject.lower().startswith("fw:"):
                fwd_subject = f"Fwd: {fwd_subject}"
        full_body = (
            p.body_text
            + "\n\n---------- Forwarded message ----------\n"
            + f"From: {original.sender.email if original.sender else 'unknown'}\n"
            + f"Date: {original.date.strftime('%Y-%m-%d %H:%M') if original.date else ''}\n"
            + f"Subject: {original.subject}\n\n"
            + original.body_text
        ).strip()
        saved_to_sent, sent_folder = _save_copy_to_sent(
            client,
            to=p.to,
            subject=fwd_subject,
            body_text=full_body,
            cc=p.cc,
            bcc=p.bcc,
            signature=p.signature,
        )
    except Exception:  # noqa: BLE001 - the Sent copy is best-effort; never fail the send
        saved_to_sent = False
    delivery_status, bounce_details = _delivery_probe(
        client, mid, p.verify_bounce_window_seconds
    )
    return {
        "smtp_accepted": True,
        "sent": True,
        "message_id": mid,
        "account": client.account.id,
        "saved_to_sent": saved_to_sent,
        "sent_folder": sent_folder,
        "delivery_status": delivery_status,
        "bounce_details": bounce_details,
    }


@require_approval()
def message_draft(client: MailClient, p: MessageDraftPayload) -> dict:
    """Save a message as a draft in the IMAP Drafts folder (HITL required).

    The message is built locally and appended via IMAP APPEND — never sent.

    Parameters:
        - to (list[str]): Recipient addresses.
        - subject (str): Subject line.
        - body_text (str): Plain-text body.
        - body_html (str): Optional HTML body.
        - cc (list[str] | None): Visible carbon copies.
        - bcc (list[str] | None): Blind copies (envelope only).
        - signature (str): "default" | "" | custom plain-text.
        - attachments (list[str] | None): Absolute local file paths.
        - drafts_folder (str): Drafts folder name (default "Drafts").
        - account_id (str | None): Account id (omit → default).

    Examples:
        - Draft without attachments:
            `mail-proxy do message-draft '{"to":["x@y.fr"],"subject":"Brouillon","body_text":"Idée à développer"}'`
            → {"saved":true,"message_id":"<d4e5f6a7@webmail.polytechnique.fr>","folder":"Drafts","account":"poly"}
        - Draft with attachment into a custom folder:
            `mail-proxy do message-draft '{"to":["x@y.fr"],"subject":"Brouillon","body_text":"Voir PJ","attachments":["/tmp/notes.md"],"drafts_folder":"Brouillons"}'`
            → {"saved":true,"message_id":"<d4e5f6a7@webmail.polytechnique.fr>","folder":"Brouillons","account":"poly"}
        - Draft without signature:
            `mail-proxy do message-draft '{"to":["x@y.fr"],"subject":"Brouillon","body_text":"…","signature":""}'`
            → {"saved":true,"message_id":"<d4e5f6a7@webmail.polytechnique.fr>","folder":"Drafts","account":"poly"}
    """
    smtp = client.smtp()
    raw_bytes, mid = smtp.build_draft_bytes(
        to=p.to,
        subject=p.subject,
        body_text=p.body_text,
        body_html=p.body_html,
        cc=p.cc,
        bcc=p.bcc,
        signature=p.signature,
        attachments=p.attachments,
    )
    client.imap().append_message(p.drafts_folder, raw_bytes, flags=["\\Draft"])
    return {
        "saved": True,
        "message_id": mid,
        "folder": p.drafts_folder,
        "account": client.account.id,
    }


ACTIONS = [
    action_def("message-send", MessageSendPayload, message_send, group="Compose"),
    action_def("message-reply", MessageReplyPayload, message_reply, group="Compose"),
    action_def(
        "message-forward", MessageForwardPayload, message_forward, group="Compose"
    ),
    action_def("message-draft", MessageDraftPayload, message_draft, group="Compose"),
]
