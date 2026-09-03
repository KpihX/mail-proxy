"""Preflight + verification — compare, verify_absence, remaining_uids, policies."""

import pytest

from mail_proxy.actions.base import (
    compare,
    remaining_uids,
    verify_absence,
)
from mail_proxy.actions.folders import FolderDeletePayload, _folder_delete_preflight
from mail_proxy.actions.messages import MessageDeletePayload, _message_delete_preflight
from mail_proxy.exceptions import MailProxyError


class _FakeImap:
    def __init__(self, present: list[int], folders: list[str] | None = None):
        self.present = set(present)
        self.folders = folders or ["INBOX"]

    def message_exists(self, uid: int, folder: str) -> bool:
        return uid in self.present

    def folder_exists(self, name: str) -> bool:
        return name in self.folders


class _FakeClient:
    def __init__(self, imap: _FakeImap):
        self._imap = imap
        self.account = type("A", (), {"id": "poly"})()

    def imap(self):
        return self._imap


def test_compare_match():
    v = compare("UID SEARCH INBOX", {"uids": [42]}, {"uids": [42]})
    assert v.ok is True
    assert v.checked == ["uids"]


def test_compare_mismatch():
    v = compare("UID SEARCH INBOX", {"uids": []}, {"uids": [42]})
    assert v.ok is False
    assert v.actual == {"uids": [42]}


def test_verify_absence_empty_read_is_ok():
    v = verify_absence(list, "42", "UID SEARCH INBOX")
    assert v.ok is True
    assert v.expected == {"deleted": "42"}


def test_verify_absence_none_read_is_ok():
    v = verify_absence(lambda: None, "42", "UID SEARCH INBOX")
    assert v.ok is True


def test_verify_absence_polls_until_gone():
    calls = iter([[42], [42], []])
    v = verify_absence(
        lambda: next(calls), "42", "UID SEARCH INBOX",
        timeout_seconds=5, interval_seconds=0,
    )
    assert v.ok is True


def test_verify_absence_timeout_raises():
    with pytest.raises(MailProxyError, match="still exists"):
        verify_absence(
            lambda: [42], "42", "UID SEARCH INBOX",
            timeout_seconds=0, interval_seconds=0,
        )


def test_remaining_uids_filters_present():
    client = _FakeClient(_FakeImap(present=[42, 43]))
    assert remaining_uids(client, [42, 43, 44], "INBOX") == [42, 43]
    assert remaining_uids(client, [44], "INBOX") == []


def test_message_delete_preflight_ok():
    client = _FakeClient(_FakeImap(present=[1, 2]))
    _message_delete_preflight(client, MessageDeletePayload(account_id="poly", uids=[1, 2]))
    assert True


def test_message_delete_preflight_missing_raises():
    client = _FakeClient(_FakeImap(present=[1]))
    with pytest.raises(MailProxyError, match="do not exist"):
        _message_delete_preflight(client, MessageDeletePayload(account_id="poly", uids=[1, 99]))


def test_folder_delete_preflight_ok():
    client = _FakeClient(_FakeImap(present=[], folders=["INBOX", "Work"]))
    _folder_delete_preflight(client, FolderDeletePayload(account_id="poly", names=["INBOX", "Work"]))
    assert True


def test_folder_delete_preflight_missing_raises():
    client = _FakeClient(_FakeImap(present=[], folders=["INBOX"]))
    with pytest.raises(MailProxyError, match="Folders do not exist: 'Nope', 'Also-Nope'"):
        _folder_delete_preflight(
            client, FolderDeletePayload(account_id="poly", names=["INBOX", "Nope", "Also-Nope"])
        )


def test_preflight_identity_fields_locked_in_review():
    """The cli locks identity fields after HITL — verify the declared policy."""
    from mail_proxy.actions.messages import message_delete

    assert message_delete.__preflight_identity_fields__ == ("uids", "folder")
    from mail_proxy.actions.folders import folder_delete

    assert folder_delete.__preflight_identity_fields__ == ("names",)


def test_verification_checks_declared():
    from mail_proxy.actions.messages import (
        message_archive,
        message_delete,
        message_mark,
        message_move,
    )

    assert message_delete.__verification_checks__ == ("deleted",)
    assert message_mark.__verification_checks__ == ("uids", "flags")
    assert message_move.__verification_checks__ == ("uids", "destination_folder")
    assert message_archive.__verification_checks__ == ("uids", "folder")
