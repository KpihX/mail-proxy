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
