"""SMTP transport for mail-proxy — sending, replying, forwarding, drafts.

Ported from `mail_mcp/core/smtp_client.py` (the mail-mcp content source).

Signature handling:
- Plain text: appended after body with a "--" separator
- HTML: multipart/related wrapping multipart/alternative, logo embedded as CID

MIME structure when signature logo is present:
  multipart/related
    ├── multipart/alternative
    │     ├── text/plain  (body + text signature)
    │     └── text/html   (body + HTML signature with <img src="cid:sig_logo">)
    └── image/png         (logo, Content-ID: sig_logo)

Signature parameter convention (send/reply/forward/build_draft_bytes):
  "default"   → account's configured default signature (text + image)
  ""          → no signature at all
  "sig-xxx"   → specific signature by id
  "any text"  → custom plain-text signature, appended as "--\\n<text>" (no logo)
"""

from __future__ import annotations

import mimetypes
import smtplib
import socket
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

from ..config import SIGNATURES_DIR, AccountDef, SignatureDef, api_timeout
from ..exceptions import MailAPIError
from .models import Message

_PKG_DIR = Path(__file__).parent.parent  # src/mail_proxy/
_SIG_CID = "sig_logo"


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------


def _sig_text(sig: SignatureDef) -> str:
    """Plain-text signature block.

    Args:
        sig (SignatureDef): Signature definition.

    Returns:
        str: Text block prefixed with a `--` separator ("" when empty).

    Examples:
        >>> _sig_text(SignatureDef(before_logo="John Doe", after_logo="ACME"))
        '\\n\\n--\\nJohn Doe\\nACME'
        >>> _sig_text(SignatureDef())
        ''
    """
    if not sig.before_logo and not sig.after_logo:
        return ""
    parts = []
    if sig.before_logo:
        parts.append(sig.before_logo.strip())
    if sig.after_logo:
        parts.append(sig.after_logo.strip())
    return "\n\n--\n" + "\n".join(parts)


def _sig_html(sig: SignatureDef) -> str:
    """HTML signature block (uses a CID reference for the logo).

    Args:
        sig (SignatureDef): Signature definition.

    Returns:
        str: HTML fragment.

    Examples:
        >>> "<img" in _sig_html(SignatureDef(id="s1", image="assets/logo.png"))
        True
        >>> "John" in _sig_html(SignatureDef(id="s1", before_logo="John"))
        True
    """
    before = sig.before_logo.strip().replace("\n", "<br>") if sig.before_logo else ""
    after = sig.after_logo.strip().replace("\n", "<br>") if sig.after_logo else ""

    logo_html = ""
    if sig.image:
        alt_text = sig.name or "Signature logo"
        logo_html = (
            f'<img src="cid:{_SIG_CID}" alt="{alt_text}"'
            f' style="max-width:280px; display:block; margin:6px 0;">'
        )

    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#1a1a2e;line-height:1.5;">'
        f"<p style='margin:0 0 4px 0;'><strong>{before}</strong></p>"
        f"{logo_html}"
        f"<p style='margin:4px 0 0 0;'>{after}</p>"
        "</div>"
    )


def _load_logo(sig: SignatureDef) -> bytes | None:
    """Load the logo image bytes from the signatures directory.

    Args:
        sig (SignatureDef): Signature definition.

    Returns:
        bytes | None: Logo payload, or None when unset/missing.

    Examples:
        >>> _load_logo(SignatureDef()) is None
        True
    """
    if not sig.image:
        return None
    logo_path = SIGNATURES_DIR / sig.image
    if not logo_path.exists():
        return None
    return logo_path.read_bytes()


# ---------------------------------------------------------------------------
# SMTP client
# ---------------------------------------------------------------------------


class SMTPClient:
    """Stateless SMTP sender — connects, sends, disconnects per call.

    Examples:
        >>> SMTPClient(account).send(["a@b.fr"], "Hi", "Hello")
        '<msgid@host>'
    """

    def __init__(self, account: AccountDef) -> None:
        self.account = account

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> smtplib.SMTP:
        """Open an authenticated SMTP connection.

        Supports both password auth (``login()``) and OAuth2 (``AUTH XOAUTH2``
        via raw ``docmd``). For OAuth2, the access token is obtained from the
        stored token file with automatic refresh.

        Returns:
            smtplib.SMTP: Authenticated connection (context-managed).

        Raises:
            MailAPIError: On network failure or auth rejection.

        Examples:
            >>> SMTPClient(account)._connect()
            <smtplib.SMTP …>
        """

        cfg = self.account.smtp
        try:
            if cfg.starttls:
                server = smtplib.SMTP(cfg.host, cfg.port, timeout=api_timeout())
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            else:
                server = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=api_timeout())
        except (TimeoutError, socket.gaierror, ConnectionRefusedError, OSError) as exc:
            raise MailAPIError(
                0,
                f"Cannot reach SMTP server {cfg.host}:{cfg.port} ({exc}).",
            ) from exc
        try:
            if self.account.auth_method == "oauth2":
                from ..oauth2 import build_xoauth2_string, get_valid_access_token

                access_token = get_valid_access_token(self.account.id)
                xoauth2_str = build_xoauth2_string(self.account.username, access_token)
                # SMTP AUTH XOAUTH2 requires a raw command — server.login()
                # doesn't support XOAUTH2 natively.
                code, msg = server.docmd("AUTH", "XOAUTH2 " + xoauth2_str)
                if code not in (235, 334):
                    raise smtplib.SMTPAuthenticationError(code, msg)
            else:
                server.login(self.account.username, self.account.password)
        except smtplib.SMTPAuthenticationError as exc:
            server.close()
            raise MailAPIError(
                0,
                f"SMTP login rejected for account {self.account.id!r} — check "
                f"MAIL_{self.account.id.upper()}_LOGIN / _PASS or run "
                "'mail-proxy admin setup'.",
            ) from exc
        except smtplib.SMTPException as exc:
            server.close()
            raise MailAPIError(
                0, f"SMTP login failed for {self.account.id!r}: {exc}."
            ) from exc
        return server

    # ------------------------------------------------------------------
    # Message builder
    # ------------------------------------------------------------------

    def _resolve_signature(self, signature: str) -> tuple[str, str, bytes | None]:
        """Return (plain_sig_block, html_sig_block, logo_bytes).

        Args:
            signature (str): "default" | "" | "sig-xxx" | custom text.

        Returns:
            tuple[str, str, bytes | None]: The three signature ingredients.

        Examples:
            >>> SMTPClient(account)._resolve_signature("")[0]
            ''
            >>> "custom" in SMTPClient(account)._resolve_signature("custom")[0]
            True
        """
        if signature == "":
            return "", "", None

        if signature == "default":
            sig = self.account.get_default_signature()
            if sig is None:
                return "", "", None
            return _sig_text(sig), _sig_html(sig), _load_logo(sig)

        # Signature ID lookup (e.g. "sig-a1b2c3d4")
        if signature.startswith("sig-"):
            sig = self.account.get_signature_by_id(signature)
            if sig is None:
                return "", "", None
            return _sig_text(sig), _sig_html(sig), _load_logo(sig)

        # Custom text signature (ephemeral — not stored)
        plain = f"\n\n--\n{signature.strip()}"
        html = (
            '<hr style="border:none;border-top:1px solid #ccc;margin:16px 0;">'
            f'<div style="font-family:Arial,sans-serif;font-size:12px;color:#333;">'
            f"{signature.strip().replace(chr(10), '<br>')}"
            f"</div>"
        )
        return plain, html, None

    def _build_message(
        self,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: str = "",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        in_reply_to: str = "",
        references: list[str] | None = None,
        message_id: str = "",
        signature: str = "default",
        attachments: list[str] | None = None,
    ) -> MIMEMultipart:
        """Build the full MIME message (headers + body + signature + files).

        Args:
            to (list[str]): Recipients.
            subject (str): Subject line.
            body_text (str): Plain-text body.
            body_html (str): Optional HTML body.
            cc (list[str] | None): Visible carbon copies.
            bcc (list[str] | None): Blind copies — SMTP envelope only, never headers.
            in_reply_to (str): In-Reply-To header value.
            references (list[str] | None): References header tokens.
            message_id (str): Explicit Message-ID ("" → generated).
            signature (str): "default" | "" | custom text.
            attachments (list[str] | None): Absolute file paths to attach.

        Returns:
            MIMEMultipart: The complete message.

        Examples:
            >>> SMTPClient(account)._build_message(["a@b.fr"], "Hi", "Hello")["To"]
            'a@b.fr'
        """
        plain_sig, html_sig, logo_bytes = self._resolve_signature(signature)

        full_text = body_text + plain_sig

        if body_html or html_sig:
            body_html_content = (
                body_html or f"<p>{body_text.replace(chr(10), '<br>')}</p>"
            )
            full_html = body_html_content + html_sig
        else:
            full_html = ""

        if logo_bytes:
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(full_text, "plain", "utf-8"))
            alt.attach(MIMEText(full_html, "html", "utf-8"))
            content_part: MIMEMultipart = MIMEMultipart("related")
            content_part.attach(alt)
            img = MIMEImage(logo_bytes)
            img.add_header("Content-ID", f"<{_SIG_CID}>")
            img.add_header("Content-Disposition", "inline", filename="logo.png")
            content_part.attach(img)
        elif full_html:
            content_part = MIMEMultipart("alternative")
            content_part.attach(MIMEText(full_text, "plain", "utf-8"))
            content_part.attach(MIMEText(full_html, "html", "utf-8"))
        else:
            content_part = MIMEMultipart("alternative")
            content_part.attach(MIMEText(full_text, "plain", "utf-8"))

        if attachments or logo_bytes:
            root = MIMEMultipart("mixed")
            root.attach(content_part)
        else:
            root = content_part

        if attachments:
            for filepath in attachments:
                p = Path(filepath)
                mime_type, _ = mimetypes.guess_type(str(p))
                maintype, subtype = (mime_type or "application/octet-stream").split(
                    "/", 1
                )
                part = MIMEBase(maintype, subtype)
                part.set_payload(p.read_bytes())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", "attachment", filename=p.name)
                root.attach(part)

        root["From"] = formataddr(
            (self.account.display_name, self.account.from_address)
        )
        root["To"] = ", ".join(to)
        if cc:
            root["Cc"] = ", ".join(cc)
        root["Subject"] = subject
        root["Date"] = formatdate(localtime=True)
        root["Message-ID"] = message_id or make_msgid()
        if in_reply_to:
            root["In-Reply-To"] = in_reply_to
        if references:
            root["References"] = " ".join(references)

        return root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_crlf(raw: bytes) -> bytes:
        """Normalize line endings to SMTP-mandated CRLF (RFC 5321 §4.1.1).

        Bare line feeds (``\\n`` without ``\\r``) are illegal in the SMTP
        ``DATA`` command body.  Gmail silently accepts them; strict servers
        like Zimbra reject them with ``SMTPSEND.BareLinefeedsAreIllegal``
        when they do not support ``BDAT`` (chunked transfer).

        ``MIMEMultipart.as_bytes()`` *should* emit CRLF, but HTML strings
        constructed with Python ``\\n`` can leak bare LFs into the payload
        when nested MIME parts are assembled.

        The normalisation is idempotent: ``\\r\\n`` → ``\\n`` → ``\\r\\n``.
        """
        return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")

    def send(
        self,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: str = "",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        signature: str = "default",
        attachments: list[str] | None = None,
    ) -> str:
        """Send a new message.

        Args:
            to (list[str]): Recipients.
            subject (str): Subject line.
            body_text (str): Plain-text body.
            body_html (str): Optional HTML body.
            cc (list[str] | None): Visible carbon copies.
            bcc (list[str] | None): Blind copies — SMTP envelope only.
            signature (str): "default" | "" | custom text.
            attachments (list[str] | None): Absolute file paths.

        Returns:
            str: The Message-ID of the submitted message.

        Raises:
            MailAPIError: On network/auth/SMTP failure.

        Examples:
            >>> SMTPClient(account).send(["a@b.fr"], "Hi", "Hello")
            '<a1b2c3d4@webmail.polytechnique.fr>'
        """
        msg = self._build_message(
            to,
            subject,
            body_text,
            body_html,
            cc,
            bcc,
            signature=signature,
            attachments=attachments,
        )
        mid = msg["Message-ID"]
        all_rcpt = to + (cc or []) + (bcc or [])
        try:
            with self._connect() as server:
                server.sendmail(
                    self.account.from_address,
                    all_rcpt,
                    self._normalize_crlf(msg.as_bytes()),
                )
        except smtplib.SMTPException as exc:
            raise MailAPIError(0, f"SMTP submission failed: {exc}.") from exc
        return mid

    def reply(
        self,
        original: Message,
        body_text: str,
        body_html: str = "",
        reply_all: bool = False,
        bcc: list[str] | None = None,
        signature: str = "default",
        subject_override: str | None = None,
    ) -> str:
        """Reply to an existing message (proper Re:/In-Reply-To/References).

        Args:
            original (Message): The message being answered.
            body_text (str): Reply body.
            body_html (str): Optional HTML body.
            reply_all (bool): Include all original recipients in CC.
            bcc (list[str] | None): Blind copies — SMTP envelope only.
            signature (str): "default" | "" | custom text.
            subject_override (str | None): Exact subject to use instead of
                the auto-computed "Re: <original>".

        Returns:
            str: The Message-ID of the reply.

        Raises:
            MailAPIError: On network/auth/SMTP failure.

        Examples:
            >>> SMTPClient(account).reply(original, "Thanks")
            '<a1b2c3d4@webmail.polytechnique.fr>'
        """
        if subject_override:
            subject = subject_override
        else:
            subject = original.subject
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"

        to_list = [original.sender.email] if original.sender else []
        cc_list: list[str] = []
        if reply_all:
            me = self.account.from_address
            to_list = [
                e for e in {a.email for a in original.recipients} if e != me
            ] or to_list
            cc_list = [e for e in {a.email for a in original.cc} if e != me]

        refs = original.references + (
            [original.message_id] if original.message_id else []
        )

        msg = self._build_message(
            to=to_list,
            subject=subject,
            body_text=body_text,
            body_html=body_html,
            cc=cc_list or None,
            bcc=bcc,
            in_reply_to=original.message_id,
            references=refs,
            signature=signature,
        )
        mid = msg["Message-ID"]
        all_rcpt = to_list + cc_list + (bcc or [])
        try:
            with self._connect() as server:
                server.sendmail(
                    self.account.from_address,
                    all_rcpt,
                    self._normalize_crlf(msg.as_bytes()),
                )
        except smtplib.SMTPException as exc:
            raise MailAPIError(0, f"SMTP submission failed: {exc}.") from exc
        return mid

    def forward(
        self,
        original: Message,
        to: list[str],
        body_text: str = "",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        signature: str = "default",
        subject_override: str | None = None,
    ) -> str:
        """Forward a message (Fwd: prefix + quoted original).

        Args:
            original (Message): The message being forwarded.
            to (list[str]): New recipients.
            body_text (str): Text prepended above the forwarded content.
            cc (list[str] | None): Visible carbon copies.
            bcc (list[str] | None): Blind copies — SMTP envelope only.
            signature (str): "default" | "" | custom text.
            subject_override (str | None): Exact subject to use instead of
                the auto-computed "Fwd: <original>".

        Returns:
            str: The Message-ID of the forward.

        Raises:
            MailAPIError: On network/auth/SMTP failure.

        Examples:
            >>> SMTPClient(account).forward(original, ["c@d.fr"], "FYI")
            '<a1b2c3d4@webmail.polytechnique.fr>'
        """
        if subject_override:
            subject = subject_override
        else:
            subject = original.subject
            if not subject.lower().startswith(
                "fwd:"
            ) and not subject.lower().startswith("fw:"):
                subject = f"Fwd: {subject}"

        sender_str = original.sender.email if original.sender else "unknown"
        date_str = original.date.strftime("%Y-%m-%d %H:%M") if original.date else ""
        prefix = (
            f"\n\n---------- Forwarded message ----------\n"
            f"From: {sender_str}\n"
            f"Date: {date_str}\n"
            f"Subject: {original.subject}\n\n"
        )
        full_body = (body_text + prefix + original.body_text).strip()

        msg = self._build_message(
            to=to,
            subject=subject,
            body_text=full_body,
            cc=cc,
            bcc=bcc,
            signature=signature,
        )
        mid = msg["Message-ID"]
        all_rcpt = to + (cc or []) + (bcc or [])
        try:
            with self._connect() as server:
                server.sendmail(
                    self.account.from_address,
                    all_rcpt,
                    self._normalize_crlf(msg.as_bytes()),
                )
        except smtplib.SMTPException as exc:
            raise MailAPIError(0, f"SMTP submission failed: {exc}.") from exc
        return mid

    def build_draft_bytes(
        self,
        to: list[str],
        subject: str,
        body_text: str,
        body_html: str = "",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        signature: str = "default",
        attachments: list[str] | None = None,
    ) -> tuple[bytes, str]:
        """Build a draft as raw bytes for IMAP APPEND.

        Args:
            to (list[str]): Recipients.
            subject (str): Subject line.
            body_text (str): Plain-text body.
            body_html (str): Optional HTML body.
            cc (list[str] | None): Visible carbon copies.
            bcc (list[str] | None): Blind copies — SMTP envelope only.
            signature (str): "default" | "" | custom text.
            attachments (list[str] | None): Absolute file paths.

        Returns:
            tuple[bytes, str]: (raw RFC822 bytes, Message-ID).

        Examples:
            >>> raw, mid = SMTPClient(account).build_draft_bytes(["a@b.fr"], "Hi", "Hello")
            >>> b"Subject: Hi" in raw
            True
        """
        msg = self._build_message(
            to,
            subject,
            body_text,
            body_html,
            cc,
            bcc,
            signature=signature,
            attachments=attachments,
        )
        return msg.as_bytes(), msg["Message-ID"]
