"""Handler-level verification flows — message_mark / label_set read-back logic.

Locks the ALL-UIDs semantics of the read-back verification with fake IMAP
clients (guards against comprehension bugs and silent partial failures).
"""

import pytest

from mail_proxy.actions.base import compare
from mail_proxy.actions.labels import LabelSetPayload, label_set
from mail_proxy.actions.messages import MessageMarkPayload, message_mark


class _FakeImap:
    """Fake IMAP — flags dict per uid; current_flags returns it as-is."""

    def __init__(self, flags_by_uid: dict[int, list[str]]):
        self.flags_by_uid = flags_by_uid

    def set_flags(self, uids, folder, flags, add=True):
        for uid in uids:
            current = set(self.flags_by_uid.setdefault(uid, []))
            if add:
                current.update(flags)
            else:
                current.difference_update(flags)
            self.flags_by_uid[uid] = sorted(current)

    def set_keyword(self, uids, folder, keyword, add=True):
        for uid in uids:
            current = set(self.flags_by_uid.setdefault(uid, []))
            if add:
                current.add(keyword)
            else:
                current.discard(keyword)
            self.flags_by_uid[uid] = sorted(current)

    def current_flags(self, uids, folder):
        return {uid: self.flags_by_uid.get(uid, []) for uid in uids}


class _FakeClient:
    def __init__(self, imap):
        self._imap = imap
        self.account = type("A", (), {"id": "poly"})()

    def imap(self):
        return self._imap


def test_message_mark_verification_ok():
    imap = _FakeImap({1: [], 2: []})
    client = _FakeClient(imap)
    data, verification = message_mark(
        client, MessageMarkPayload(uids=[1, 2], seen=True)
    )
    assert data["modified"] == 2
    assert verification.ok is True
    assert verification.checked == ["flags", "uids"]
    assert verification.expected == {"uids": [1, 2], "flags": ["\\Seen"]}


def test_message_mark_partial_failure_fails_verification():
    """Simulates a silent partial failure: uid 2 never got the flag."""
    class _FlakyImap(_FakeImap):
        def set_flags(self, uids, folder, flags, add=True):
            # only the first uid receives the flag — silent partial failure
            super().set_flags([uids[0]], folder, flags, add=add)

    imap = _FlakyImap({1: [], 2: []})
    client = _FakeClient(imap)
    _, verification = message_mark(client, MessageMarkPayload(uids=[1, 2], seen=True))
    assert verification.ok is False
    assert verification.actual["flags"] == []


def test_message_mark_remove_flag_verification():
    imap = _FakeImap({1: ["\\Seen"], 2: ["\\Seen"]})
    client = _FakeClient(imap)
    _, verification = message_mark(client, MessageMarkPayload(uids=[1, 2], seen=False))
    assert verification.ok is True
    # actual lists the flags whose requested state is fully applied: \Seen is
    # gone from every uid → it counts as applied.
    assert verification.actual["flags"] == ["\\Seen"]


def test_label_set_verification_ok():
    imap = _FakeImap({1: [], 2: []})
    client = _FakeClient(imap)
    data, verification = label_set(
        client, LabelSetPayload(uids=[1, 2], labels=["todo"])
    )
    assert data["action"] == "added"
    assert verification.ok is True
    assert verification.expected == {"uids": [1, 2], "labels": ["todo"]}


def test_label_set_partial_failure_fails_verification():
    class _FlakyImap(_FakeImap):
        def set_keyword(self, uids, folder, keyword, add=True):
            super().set_keyword([uids[0]], folder, keyword, add=add)

    imap = _FlakyImap({1: [], 2: []})
    client = _FakeClient(imap)
    _, verification = label_set(client, LabelSetPayload(uids=[1, 2], labels=["todo"]))
    assert verification.ok is False
    assert verification.actual["labels"] == []


def test_label_set_remove_verification():
    imap = _FakeImap({1: ["todo"], 2: ["todo"]})
    client = _FakeClient(imap)
    _, verification = label_set(
        client, LabelSetPayload(uids=[1, 2], labels=["todo"], add=False)
    )
    assert verification.ok is True
    # actual lists the labels whose requested state is fully applied: todo is
    # gone from every uid → it counts as applied.
    assert verification.actual["labels"] == ["todo"]


def test_compare_used_by_handlers():
    v = compare("X", {"a": 1}, {"a": 1})
    assert v.ok is True
    v = compare("X", {"a": 1}, {"a": 2})
    assert v.ok is False
