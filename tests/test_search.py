"""Search — the SearchCriteria → IMAP criteria translation (the filter engine)."""

from datetime import UTC, datetime

from mail_proxy.api.imap import IMAPClient
from mail_proxy.api.models import SearchCriteria


class _FakeClient:
    """Minimal stand-in carrying only what build_imap_criteria needs."""

    def __init__(self):
        self._client = None


def _criteria(**kwargs):
    client = _FakeClient()
    return IMAPClient(client).build_imap_criteria(SearchCriteria(**kwargs))  # type: ignore[arg-type]


def test_empty_is_all():
    assert _criteria() == ["ALL"]


def test_unseen_and_flagged():
    assert _criteria(unseen_only=True, flagged_only=True) == ["UNSEEN", "FLAGGED"]


def test_sender_and_subject():
    assert _criteria(sender="@polytechnique.edu", subject_filter="TP") == [
        "FROM", "@polytechnique.edu",
        "SUBJECT", "TP",
    ]


def test_to_cc():
    assert _criteria(to_filter="a@b.fr", cc_filter="c@d.fr") == ["TO", "a@b.fr", "CC", "c@d.fr"]


def test_date_window():
    since = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    before = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    assert _criteria(since=since, before=before) == ["SINCE", since.date(), "BEFORE", before.date()]


def test_query_or_subject_body():
    assert _criteria(query="invoice") == ["OR", ["SUBJECT", "invoice"], ["BODY", "invoice"]]


def test_attachment_and_sizes():
    assert _criteria(has_attachment=True, min_size=100, max_size=1000) == [
        "HEADER", "Content-Type", "multipart",
        "LARGER", 100,
        "SMALLER", 1000,
    ]


def test_keyword():
    assert _criteria(keyword="todo") == ["KEYWORD", "todo"]


def test_full_combo():
    criteria = _criteria(
        unseen_only=True,
        sender="x@y.fr",
        query="hello",
        has_attachment=True,
        limit=5,
    )
    assert criteria == [
        "UNSEEN",
        "FROM", "x@y.fr",
        "OR", ["SUBJECT", "hello"], ["BODY", "hello"],
        "HEADER", "Content-Type", "multipart",
    ]
