"""CLI helpers — payload parsing and envelope output."""

import pytest

from mail_proxy.cli import _autosave, parse_payload
from mail_proxy.exceptions import MailProxyError


def test_parse_payload_empty():
    assert parse_payload(None) == {}
    assert parse_payload("") == {}


def test_parse_payload_inline_json():
    assert parse_payload('{"uid": 42}') == {"uid": 42}


def test_parse_payload_file(tmp_path):
    path = tmp_path / "payload.json"
    path.write_text('{"uids": [1, 2], "folder": "INBOX"}')
    assert parse_payload(str(path)) == {"uids": [1, 2], "folder": "INBOX"}


def test_parse_payload_invalid_raises():
    with pytest.raises(MailProxyError, match="Invalid JSON or file not found"):
        parse_payload("not-json-and-not-a-file")


def test_autosave_writes_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr("mail_proxy.cli.AUTOSAVE_DIR", tmp_path)
    path = _autosave("inbox-check", {"meta": {"status": "ok"}, "data": []})
    assert path.exists()
    assert path.name.startswith("inbox-check_")
    assert path.suffix == ".json"
    assert '"status": "ok"' in path.read_text()


def test_autosave_creates_dir(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "dir"
    monkeypatch.setattr("mail_proxy.cli.AUTOSAVE_DIR", target)
    path = _autosave("raw", {"meta": {}, "data": {}})
    assert path.exists()
