"""Envelope helpers — ok / rejected / Verification."""

from mail_proxy.models import Output, Verification, ok, rejected


def test_ok_envelope():
    envelope = ok({"uid": 42})
    assert envelope["meta"]["status"] == "ok"
    assert envelope["meta"]["comment"] == ""
    assert envelope["meta"]["edited"] is False
    assert envelope["data"] == {"uid": 42}


def test_ok_with_hitl_metadata():
    envelope = ok([], edited=True, comment="reviewed", status="approved")
    assert envelope["meta"]["status"] == "approved"
    assert envelope["meta"]["comment"] == "reviewed"
    assert envelope["meta"]["edited"] is True


def test_rejected_envelope():
    envelope = rejected("wrong recipient")
    assert envelope["meta"]["status"] == "rejected"
    assert envelope["data"] is None
    assert envelope["meta"]["comment"] == "wrong recipient"


def test_output_model_defaults():
    out = Output(data={"uid": 1})
    assert out.meta.status == "ok"
    assert out.meta.edited is False


def test_verification_model():
    v = Verification(
        method="UID SEARCH INBOX",
        checked=["uids"],
        expected={"uids": []},
        actual={"uids": []},
        ok=True,
    )
    assert v.ok is True
    assert v.checked == ["uids"]


def test_verification_mismatch():
    v = Verification(
        method="UID SEARCH INBOX",
        checked=["uids"],
        expected={"uids": []},
        actual={"uids": [42]},
        ok=False,
    )
    assert v.ok is False
