"""HITL — REAL local HTTP round-trips (the review server is exercised live).

Same approach as tick-proxy's test_task_review: start `request_approval` in a
worker thread, drive the actual HTTP endpoints with urllib, and assert the
decisions. No mocking of the server itself — only the browser is replaced by
HTTP calls. The module-level `print` is patched (monkeypatch restores it) so
the review URL is captured instead of printed.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import mail_proxy.hitl as hitl


@pytest.fixture(autouse=True)
def no_browser_launch(monkeypatch):
    """Never let request_approval open a real browser during tests."""
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    yield


def _start_approval(monkeypatch, action: str, payload: dict) -> tuple[threading.Thread, dict]:
    """Run request_approval in a worker; publish the review URL via patched print."""
    result_box: dict = {}

    def fake_print(*args, **kwargs):
        for arg in args:
            if isinstance(arg, str) and "http://127.0.0.1" in arg:
                result_box["url"] = arg.strip().split(" ")[-1]

    monkeypatch.setattr(hitl, "print", fake_print, raising=False)

    def worker():
        result_box["response"] = hitl.request_approval(action, payload)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while "url" not in result_box and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "url" in result_box, "review URL was never published"
    return thread, result_box


def _base(url: str) -> str:
    return f"http://127.0.0.1:{url.split(':')[2].split('/')[0]}"


def _submit(url: str, status: str, payload=None, comment="", edited=False) -> int:
    """POST a decision to the real /submit endpoint."""
    review_id = url.split("id=")[-1]
    body = json.dumps(
        {
            "id": review_id,
            "status": status,
            "payload": payload,
            "comment": comment,
            "edited": edited,
        }
    ).encode()
    req = urllib.request.Request(
        f"{_base(url)}/submit",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


def test_hitl_approve_round_trip(monkeypatch):
    worker, box = _start_approval(monkeypatch, "message-send", {"to": ["a@b.fr"], "subject": "Hi"})
    assert urllib.request.urlopen(box["url"]).status == 200

    edited_payload = {"to": ["a@b.fr"], "subject": "Hi (edited)", "body_text": "x"}
    assert _submit(box["url"], "approved", payload=edited_payload, comment="ok", edited=True) == 200
    worker.join(timeout=5)
    assert not worker.is_alive()

    response = box["response"]
    assert response.status == "approved"
    assert response.payload == edited_payload
    assert response.comment == "ok"
    assert response.edited is True


def test_hitl_reject_round_trip(monkeypatch):
    worker, box = _start_approval(monkeypatch, "message-delete", {"uids": [42]})
    assert urllib.request.urlopen(box["url"]).status == 200

    assert _submit(box["url"], "rejected", comment="wrong target") == 200
    worker.join(timeout=5)
    assert not worker.is_alive()

    response = box["response"]
    assert response.status == "rejected"
    assert response.comment == "wrong target"
    # On reject the CLI emits data=null regardless of the echoed payload.
    assert response.payload is not None


def test_hitl_unknown_review_id_404(monkeypatch):
    _, box = _start_approval(monkeypatch, "raw", {"command": "STATUS"})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{_base(box['url'])}/review?id=does-not-exist")


def test_hitl_unknown_submit_id_404(monkeypatch):
    _, box = _start_approval(monkeypatch, "raw", {"command": "STATUS"})
    body = json.dumps({"id": "unknown", "status": "approved"}).encode()
    req = urllib.request.Request(
        f"{_base(box['url'])}/submit",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(req)


def test_hitl_timeout_returns_rejected(monkeypatch):
    monkeypatch.setattr(hitl, "HITL_TIMEOUT", 0.1)
    response = hitl.request_approval("raw", {"command": "STATUS"})
    assert response.status == "rejected"
    assert "timeout" in response.comment.lower()


# ---------------------------------------------------------------------------
# Signature rendering in HITL review pages
#
# Regression tests for the 4 signature cases:
#   1. absent (key missing) → default signature rendered (logo + text)
#   2. "default"            → default signature rendered (logo + text)
#   3. ""                   → empty signature section (no text, no logo)
#   4. custom text          → that exact text rendered
#
# Previously the HITL showed "Using default signature" as raw text instead of
# the actual rendered logo, and showed "No signature" for the empty case.
# ---------------------------------------------------------------------------


def _fetch_review_html(url: str) -> str:
    """Fetch the HTML content of a HITL review page."""
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")


def test_hitl_signature_absent_renders_default_logo(monkeypatch):
    """When signature key is missing, HITL resolves default and renders HTML (not the keyword 'default')."""
    worker, box = _start_approval(monkeypatch, "message-send", {
        "to": ["a@b.fr"], "subject": "Test sig absent",
        "body_text": "body",
    })
    html = _fetch_review_html(box["url"])
    assert "hitl-resolved-sig" in html
    assert "Using default" not in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


def test_hitl_signature_explicit_default_renders_logo(monkeypatch):
    """When signature="default", HITL resolves to HTML (not the keyword 'default')."""
    worker, box = _start_approval(monkeypatch, "message-send", {
        "to": ["a@b.fr"], "subject": "Test sig default",
        "body_text": "body", "signature": "default",
    })
    html = _fetch_review_html(box["url"])
    assert "Using default" not in html
    assert "hitl-resolved-sig" in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


def test_hitl_signature_empty_shows_empty_section(monkeypatch):
    """When signature="", HITL renders an empty signature section (no logo, no text)."""
    worker, box = _start_approval(monkeypatch, "message-send", {
        "to": ["a@b.fr"], "subject": "Test sig empty",
        "body_text": "body", "account_id": "poly",
        "signature": "",
    })
    html = _fetch_review_html(box["url"])
    assert "No signature" not in html
    assert "Using default" not in html
    assert 'id="sig-rendered"' in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


def test_hitl_signature_custom_text_renders_as_is(monkeypatch):
    """When signature=custom text, HITL renders that exact text."""
    custom = "My Custom Signature\nLine 2"
    worker, box = _start_approval(monkeypatch, "message-send", {
        "to": ["a@b.fr"], "subject": "Test sig custom",
        "body_text": "body", "account_id": "poly",
        "signature": custom,
    })
    html = _fetch_review_html(box["url"])
    assert "My Custom Signature" in html
    assert "Line 2" in html
    assert "Using default" not in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


# ---------------------------------------------------------------------------
# Reply/forward: original-message visibility + adjustable default subject
#
# message-reply / message-forward payloads never carry `subject` (it is
# computed AFTER approval) or `to` (for reply). Previously the HITL showed
# an empty, non-functional subject field and NO information about the
# message being answered — the reviewer had no way to know what they were
# approving. This reuses the SAME `_uid_resolution` dict that `cli.py`'s
# `_inject_uid_resolution` already injects for move/archive/trash/spam
# (that injection happens in `_execute`, upstream of `request_approval`,
# so these tests build the resolution dict inline exactly as it would land
# in the payload).
# ---------------------------------------------------------------------------


def test_hitl_reply_shows_original_message_and_default_subject(monkeypatch):
    worker, box = _start_approval(monkeypatch, "message-reply", {
        "uid": 42, "body_text": "Merci !",
        "_uid_resolution": {
            "42": {
                "subject": "Question sur le TP",
                "from": "prof@school.fr",
                "date": "2026-01-01T10:00:00",
                "folder": "INBOX",
            }
        },
    })
    html = _fetch_review_html(box["url"])
    assert "prof@school.fr" in html
    assert "Question sur le TP" in html
    assert "Replying to" in html
    assert "Re: Question sur le TP" in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


def test_hitl_reply_default_subject_avoids_double_re_prefix(monkeypatch):
    worker, box = _start_approval(monkeypatch, "message-reply", {
        "uid": 42, "body_text": "Merci !",
        "_uid_resolution": {
            "42": {"subject": "Re: Already prefixed", "from": "a@b.fr",
                   "date": "2026-01-01T10:00:00", "folder": "INBOX"}
        },
    })
    html = _fetch_review_html(box["url"])
    assert "Re: Re: Already prefixed" not in html
    assert "Re: Already prefixed" in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


def test_hitl_reply_escapes_original_message_metadata(monkeypatch):
    """Original-message metadata cannot break out of the review page's JSON data."""
    attacker_input = '</script><img src=x onerror="alert(1)">'
    worker, box = _start_approval(monkeypatch, "message-reply", {
        "uid": 42,
        "body_text": "Thanks",
        "_uid_resolution": {
            "42": {
                "subject": attacker_input,
                "from": attacker_input,
                "date": "2026-01-01T10:00:00",
                "folder": "INBOX",
            }
        },
    })
    html = _fetch_review_html(box["url"])
    assert "\\u003c/script\\u003e" in html
    assert attacker_input not in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


def test_hitl_forward_shows_original_message_and_default_subject(monkeypatch):
    worker, box = _start_approval(monkeypatch, "message-forward", {
        "uid": 7, "to": ["c@d.fr"],
        "_uid_resolution": {
            "7": {"subject": "Rapport mensuel", "from": "boss@corp.fr",
                  "date": "2026-02-02T09:00:00", "folder": "Archive"}
        },
    })
    html = _fetch_review_html(box["url"])
    assert "boss@corp.fr" in html
    assert "Rapport mensuel" in html
    assert "Forwarding" in html
    assert "Fwd: Rapport mensuel" in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


def test_hitl_forward_default_subject_avoids_double_fwd_prefix(monkeypatch):
    worker, box = _start_approval(monkeypatch, "message-forward", {
        "uid": 7, "to": ["c@d.fr"],
        "_uid_resolution": {
            "7": {"subject": "Fwd: Already prefixed", "from": "a@b.fr",
                  "date": "2026-02-02T09:00:00", "folder": "INBOX"}
        },
    })
    html = _fetch_review_html(box["url"])
    assert "Fwd: Fwd: Already prefixed" not in html
    assert "Fwd: Already prefixed" in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)


def test_hitl_send_action_shows_no_original_message_card(monkeypatch):
    """message-send has no uid/original message — the reply/forward-only
    card and default-subject logic must not activate for it."""
    worker, box = _start_approval(monkeypatch, "message-send", {
        "to": ["a@b.fr"], "subject": "Hi", "body_text": "body",
    })
    html = _fetch_review_html(box["url"])
    assert "Replying to" not in html
    assert "Forwarding" not in html
    _submit(box["url"], "rejected")
    worker.join(timeout=5)
