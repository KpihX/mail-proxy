"""Compose — MIME building: headers, signatures, attachments, reply/forward logic."""

from email import message_from_bytes

from mail_proxy.api.models import Address, Message
from mail_proxy.api.smtp import SMTPClient
from mail_proxy.config import AccountDef, ImapEndpoint, SignatureDef, SmtpEndpoint

ACCOUNT = AccountDef(
    id="test",
    imap=ImapEndpoint(host="imap.test.fr"),
    smtp=SmtpEndpoint(host="smtp.test.fr"),
    email="me@test.fr",
    display_name="Me Test",
    signatures=[
        SignatureDef(
            id="sig-test01",
            name="Test",
            before_logo="Me Test",
            image="",
            after_logo="TEST",
        ),
    ],
    default_signature_id="sig-test01",
)


def _parse(**kwargs):
    client = SMTPClient(ACCOUNT)
    raw, mid = client.build_draft_bytes(**kwargs)
    return message_from_bytes(raw), mid


def test_basic_message_headers():
    msg, mid = _parse(to=["a@b.fr"], subject="Hello", body_text="Hi there")
    assert msg["To"] == "a@b.fr"
    assert msg["Subject"] == "Hello"
    assert msg["From"] == "Me Test <me@test.fr>"
    assert msg["Message-ID"] == mid
    assert msg["Bcc"] is None  # never in headers


def test_bcc_only_in_envelope_never_headers():
    msg, _ = _parse(to=["a@b.fr"], subject="S", body_text="b", bcc=["z@w.fr"])
    assert "z@w.fr" not in (msg["Bcc"] or "")


def test_cc_header_present():
    msg, _ = _parse(to=["a@b.fr"], subject="S", body_text="b", cc=["c@d.fr"])
    assert msg["Cc"] == "c@d.fr"


def _plain_text(msg) -> str:
    """Return the decoded text/plain payload of a built message."""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            return payload.decode("utf-8") if payload else ""
    return ""


def test_no_signature_mode():
    msg, _ = _parse(to=["a@b.fr"], subject="S", body_text="b", signature="")
    assert "--" not in _plain_text(msg)


def test_default_signature_text_appended():
    msg, _ = _parse(to=["a@b.fr"], subject="S", body_text="b", signature="default")
    plain = _plain_text(msg)
    assert "--" in plain
    assert "Me Test" in plain
    assert "TEST" in plain


def test_custom_signature_appended():
    msg, _ = _parse(to=["a@b.fr"], subject="S", body_text="b", signature="best regards")
    plain = _plain_text(msg)
    assert "--" in plain
    assert "best regards" in plain


def test_attachment_included():
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"content")
        path = Path(f.name)
    try:
        msg, _ = _parse(
            to=["a@b.fr"], subject="S", body_text="b", attachments=[str(path)]
        )
        parts = msg.walk()
        next(parts)  # root
        names = [p.get_filename() for p in parts if p.get_filename()]
        assert path.name in names
    finally:
        path.unlink()


class _FakeSMTP:
    """Stand-in for smtplib.SMTP — records every submission."""

    def __init__(self):
        self.sent: list[tuple[str, list[str], bytes]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_: object):
        return False

    def sendmail(self, from_addr: str, rcpts: list[str], msg: bytes) -> None:
        self.sent.append((from_addr, rcpts, msg))


def _fake_connect(monkeypatch, fake: _FakeSMTP) -> _FakeSMTP:
    """Replace SMTPClient._connect with a fake, returning the recorder."""
    monkeypatch.setattr(SMTPClient, "_connect", lambda self: fake)
    return fake


def test_reply_subject_and_headers(monkeypatch):
    original = Message(
        uid=1,
        message_id="<orig@host>",
        subject="TP",
        sender=Address(email="x@y.fr"),
        recipients=[Address(email="me@test.fr")],
        references=["<ref0@host>"],
    )
    fake = _fake_connect(monkeypatch, _FakeSMTP())
    client = SMTPClient(ACCOUNT)
    mid = client.reply(original=original, body_text="ok")
    assert mid
    assert len(fake.sent) == 1
    from_addr, rcpts, raw = fake.sent[0]
    assert from_addr == "me@test.fr"
    assert rcpts == ["x@y.fr"]
    sent_msg = message_from_bytes(raw)
    assert sent_msg["Subject"] == "Re: TP"
    assert sent_msg["In-Reply-To"] == "<orig@host>"
    assert sent_msg["References"] == "<ref0@host> <orig@host>"


def test_reply_subject_override_replaces_auto_re_prefix(monkeypatch):
    """A reviewer-adjusted subject in the HITL fully replaces the auto 'Re: '
    prefix — this is the fix for the reply/forward HITL form previously
    showing an empty, non-functional subject field."""
    original = Message(
        uid=1,
        message_id="<orig@host>",
        subject="TP",
        sender=Address(email="x@y.fr"),
        recipients=[Address(email="me@test.fr")],
    )
    fake = _fake_connect(monkeypatch, _FakeSMTP())
    client = SMTPClient(ACCOUNT)
    client.reply(original=original, body_text="ok", subject_override="Custom subject")
    _, _, raw = fake.sent[0]
    sent_msg = message_from_bytes(raw)
    assert sent_msg["Subject"] == "Custom subject"


def test_reply_without_subject_override_keeps_auto_prefix(monkeypatch):
    """No explicit override falls back to the original 'Re: ' computation —
    subject_override=None must never change existing behavior."""
    original = Message(
        uid=1,
        message_id="<orig@host>",
        subject="TP",
        sender=Address(email="x@y.fr"),
        recipients=[Address(email="me@test.fr")],
    )
    fake = _fake_connect(monkeypatch, _FakeSMTP())
    client = SMTPClient(ACCOUNT)
    client.reply(original=original, body_text="ok", subject_override=None)
    _, _, raw = fake.sent[0]
    sent_msg = message_from_bytes(raw)
    assert sent_msg["Subject"] == "Re: TP"


def test_reply_all_recipient_logic(monkeypatch):
    original = Message(
        uid=1,
        message_id="<orig@host>",
        subject="TP",
        sender=Address(email="x@y.fr"),
        recipients=[Address(email="me@test.fr"), Address(email="other@y.fr")],
        cc=[Address(email="cc@y.fr")],
    )
    fake = _fake_connect(monkeypatch, _FakeSMTP())
    client = SMTPClient(ACCOUNT)
    client.reply(original=original, body_text="ok", reply_all=True)
    _, _, raw = fake.sent[0]
    sent_msg = message_from_bytes(raw)
    # Account's own address is excluded from To; others move to To/CC.
    assert "me@test.fr" not in sent_msg["To"]
    assert "other@y.fr" in sent_msg["To"]
    assert "cc@y.fr" in sent_msg["Cc"]


def test_forward_subject_prefix(monkeypatch):
    original = Message(
        uid=1, message_id="<o@h>", subject="Hello", body_text="body",
        sender=Address(email="x@y.fr"), date=None,
    )
    fake = _fake_connect(monkeypatch, _FakeSMTP())
    client = SMTPClient(ACCOUNT)
    mid = client.forward(original=original, to=["z@w.fr"])
    assert mid
    _, rcpts, raw = fake.sent[0]
    assert rcpts == ["z@w.fr"]
    sent_msg = message_from_bytes(raw)
    assert sent_msg["Subject"] == "Fwd: Hello"
    assert "---------- Forwarded message ----------" in sent_msg.get_payload()[0].get_payload(decode=True).decode("utf-8")


def test_forward_subject_override_replaces_auto_fwd_prefix(monkeypatch):
    """A reviewer-adjusted forward subject fully replaces the auto 'Fwd: '."""
    original = Message(
        uid=1, message_id="<o@h>", subject="Hello", body_text="body",
        sender=Address(email="x@y.fr"), date=None,
    )
    fake = _fake_connect(monkeypatch, _FakeSMTP())
    client = SMTPClient(ACCOUNT)
    client.forward(original=original, to=["z@w.fr"], subject_override="My forward")
    _, _, raw = fake.sent[0]
    sent_msg = message_from_bytes(raw)
    assert sent_msg["Subject"] == "My forward"


def test_draft_bytes_are_rfc822():
    raw, mid = SMTPClient(ACCOUNT).build_draft_bytes(
        to=["a@b.fr"], subject="Draft", body_text="draft body"
    )
    assert b"Subject: Draft" in raw
    assert mid
