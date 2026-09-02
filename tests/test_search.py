"""Search — the SearchCriteria → IMAP criteria translation (the filter engine)."""

from datetime import UTC, datetime

import pytest
from imapclient.exceptions import IMAPClientError

from mail_proxy.api.imap import IMAPClient
from mail_proxy.api.models import SearchCriteria
from mail_proxy.config import AccountDef
from mail_proxy.exceptions import MailAPIError


def _account() -> AccountDef:
    """Return a non-sensitive account definition for transport-free tests."""
    return AccountDef(id="test", email="test@example.com")


def _criteria(**kwargs):
    return IMAPClient(_account()).build_imap_criteria(SearchCriteria(**kwargs))


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


class _CharsetRejectingClient:
    """Outlook-like search surface rejecting an explicit UTF-8 charset."""

    def __init__(self):
        self.search_calls: list[tuple[list[object], str | None]] = []

    def select_folder(self, _folder: str, *, readonly: bool) -> None:
        assert readonly is True

    def search(self, criteria: list[object], charset: str | None = None) -> list[int]:
        self.search_calls.append((criteria, charset))
        if charset == "UTF-8":
            raise IMAPClientError("SEARCH failed: [BADCHARSET (US-ASCII)]")
        return [3, 7, 5]


def test_search_retries_without_charset_after_badcharset(monkeypatch: pytest.MonkeyPatch):
    """Outlook-compatible fallback preserves newest-first ordering and limits."""
    fake = _CharsetRejectingClient()
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    assert client.search(SearchCriteria(unseen_only=True, limit=2)) == [7, 5]
    assert fake.search_calls == [(["UNSEEN"], "UTF-8"), (["UNSEEN"], None)]


def test_search_does_not_retry_non_charset_errors(monkeypatch: pytest.MonkeyPatch):
    """Only a declared BADCHARSET receives the compatibility fallback."""

    class FailingClient(_CharsetRejectingClient):
        def search(self, criteria: list[object], charset: str | None = None) -> list[int]:
            self.search_calls.append((criteria, charset))
            raise IMAPClientError("SEARCH failed: connection reset")

    fake = FailingClient()
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    with pytest.raises(MailAPIError, match="connection reset"):
        client.search(SearchCriteria(limit=2))
    assert fake.search_calls == [(["ALL"], "UTF-8")]


class _RecordingClient(_CharsetRejectingClient):
    """Always succeeds — records the exact criteria payload it received."""

    def __init__(self, results: list[int] | None = None):
        super().__init__()
        self._results = results if results is not None else [1]

    def search(self, criteria: list[object], charset: str | None = None) -> list[int]:
        self.search_calls.append((criteria, charset))
        return self._results


# ---------------------------------------------------------------------------
# Non-ASCII (Unicode) search terms
#
# imapclient's `_raw_command` sends 8-bit args as RFC 3501 literals, which is
# the only wire form that reliably carries UTF-8 (Gmail rejects non-ASCII
# quoted-strings and advertises neither ENABLE/UTF8=ACCEPT nor LITERAL+).
# Two conditions must therefore hold: non-ASCII terms must be pre-encoded to
# bytes, and they must never sit inside NESTED criteria (imapclient appends
# the closing paren onto the last element, dropping the `_quoted` wrapper and
# burying quotes + paren inside the literal payload).
# ---------------------------------------------------------------------------


def test_ascii_terms_stay_plain_str():
    """ASCII terms are untouched — imapclient encodes them under any charset."""
    assert _criteria(sender="a@b.fr", subject_filter="TP") == [
        "FROM", "a@b.fr",
        "SUBJECT", "TP",
    ]


def test_non_ascii_flat_terms_are_utf8_bytes():
    """Every flat term field pre-encodes non-ASCII to UTF-8 bytes."""
    assert _criteria(
        sender="José",
        subject_filter="fête",
        to_filter="Renée",
        cc_filter="Zoë",
        keyword="tâche",
    ) == [
        "FROM", "José".encode(),
        "SUBJECT", "fête".encode(),
        "TO", "Renée".encode(),
        "CC", "Zoë".encode(),
        "KEYWORD", "tâche".encode(),
    ]


def test_non_ascii_query_nested_form_still_encodes_bytes():
    """Default (nested) build still encodes — used only for ASCII in practice."""
    assert _criteria(query="fête") == [
        "OR",
        ["SUBJECT", "fête".encode()],
        ["BODY", "fête".encode()],
    ]


def test_query_key_produces_flat_criteria():
    """`query_key` emits the FLAT form that keeps literals intact."""
    built = IMAPClient(_account()).build_imap_criteria(
        SearchCriteria(query="fête"), "BODY"
    )
    assert built == ["BODY", "fête".encode()]


def test_ascii_query_uses_single_nested_search(monkeypatch: pytest.MonkeyPatch):
    """ASCII queries keep the efficient single-round-trip nested OR."""
    fake = _RecordingClient([5, 3])
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    assert client.search(SearchCriteria(query="invoice", limit=10)) == [5, 3]
    assert len(fake.search_calls) == 1
    assert fake.search_calls[0][0] == [
        "OR", ["SUBJECT", "invoice"], ["BODY", "invoice"]
    ]


def test_non_ascii_query_splits_into_two_flat_searches(
    monkeypatch: pytest.MonkeyPatch,
):
    """Reproduces the live Gmail/Zimbra bug: a non-ASCII query must run as
    two FLAT searches (never one nested OR), and must not raise.
    """
    fake = _RecordingClient()
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    client.search(SearchCriteria(query="fête", limit=10))

    sent = [criteria for criteria, _charset in fake.search_calls]
    assert sent == [
        ["SUBJECT", "fête".encode()],
        ["BODY", "fête".encode()],
    ]
    # No nested list survives anywhere — that is what corrupts literals.
    for criteria in sent:
        assert not any(isinstance(item, list) for item in criteria)


def test_non_ascii_query_unions_and_dedupes_uids(monkeypatch: pytest.MonkeyPatch):
    """SUBJECT and BODY hits are merged, de-duplicated, newest-first, limited."""

    class TwoPhaseClient(_CharsetRejectingClient):
        def search(
            self, criteria: list[object], charset: str | None = None
        ) -> list[int]:
            self.search_calls.append((criteria, charset))
            return [10, 7] if "SUBJECT" in criteria else [7, 4]

    fake = TwoPhaseClient()
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    assert client.search(SearchCriteria(query="fête", limit=10)) == [10, 7, 4]


def test_non_ascii_query_respects_limit(monkeypatch: pytest.MonkeyPatch):
    """The limit applies to the merged result set, not per sub-search."""

    class TwoPhaseClient(_CharsetRejectingClient):
        def search(
            self, criteria: list[object], charset: str | None = None
        ) -> list[int]:
            self.search_calls.append((criteria, charset))
            return [10, 7] if "SUBJECT" in criteria else [7, 4]

    fake = TwoPhaseClient()
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    assert client.search(SearchCriteria(query="fête", limit=2)) == [10, 7]


def test_non_ascii_query_combines_with_other_filters(
    monkeypatch: pytest.MonkeyPatch,
):
    """Other criteria are preserved in BOTH split searches."""
    fake = _RecordingClient()
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    client.search(SearchCriteria(unseen_only=True, query="fête", limit=10))

    for criteria, _charset in fake.search_calls:
        assert criteria[0] == "UNSEEN"
        assert "fête".encode() in criteria


def test_non_ascii_query_survives_badcharset_retry(
    monkeypatch: pytest.MonkeyPatch,
):
    """Outlook path: the no-charset retry must NOT re-encode as us-ascii.

    Pre-encoded bytes are what make this safe — a `str` term would raise
    UnicodeEncodeError here, which is the exact live Hotmail crash.
    """
    fake = _CharsetRejectingClient()  # raises BADCHARSET on charset="UTF-8"
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    assert client.search(SearchCriteria(query="fête", limit=10)) == [7, 5, 3]
    # Each of the 2 split searches tried UTF-8 then retried with no charset.
    assert [charset for _criteria, charset in fake.search_calls] == [
        "UTF-8", None, "UTF-8", None,
    ]
    for criteria, _charset in fake.search_calls:
        assert "fête".encode() in criteria


# ---------------------------------------------------------------------------
# US-ASCII-only servers (Outlook): client-side matching
#
# Verified live: Outlook answers BADCHARSET (US-ASCII) and then silently
# returns ZERO matches for an accented term it actually holds — it finds
# UID 13323 "[Les Crous] Les actus de la rentrée universitaire" on the ASCII
# term "Crous" but misses it on "rentrée". Silent wrong results are worse
# than a slow correct one, so those searches are matched client-side.
# ---------------------------------------------------------------------------


class _AsciiOnlyServer(_CharsetRejectingClient):
    """Outlook-like: rejects UTF-8 charset AND never matches accents."""

    def __init__(self, ascii_hits: list[int]):
        super().__init__()
        self._ascii_hits = ascii_hits

    def search(self, criteria: list[object], charset: str | None = None) -> list[int]:
        self.search_calls.append((criteria, charset))
        if charset == "UTF-8":
            raise IMAPClientError("SEARCH failed: [BADCHARSET (US-ASCII)]")
        # Accented terms silently match nothing on this server.
        if any(isinstance(item, bytes) and _is8bit(item) for item in criteria):
            return []
        return self._ascii_hits


def _is8bit(data: bytes) -> bool:
    return any(b > 127 for b in data)


def test_ascii_only_server_falls_back_to_client_side_match(
    monkeypatch: pytest.MonkeyPatch,
):
    """The accented subject is found client-side instead of returning empty."""
    fake = _AsciiOnlyServer(ascii_hits=[13323, 13206])
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)
    monkeypatch.setattr(
        client,
        "fetch_bodies_for_pattern",
        lambda uids, folder: [
            (13323, "newsletter@lescrous.fr", "[Les Crous] Les actus de la rentrée universitaire", "body"),
            (13206, "izly@izly.fr", "Votre Crous vous informe", "autre corps"),
        ],
    )

    assert client.search(SearchCriteria(subject_filter="rentrée", limit=10)) == [13323]


def test_ascii_only_server_client_side_query_matches_body(
    monkeypatch: pytest.MonkeyPatch,
):
    """A free-text `query` matches subject OR body, like server-side SEARCH."""
    fake = _AsciiOnlyServer(ascii_hits=[1, 2])
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)
    monkeypatch.setattr(
        client,
        "fetch_bodies_for_pattern",
        lambda uids, folder: [
            (1, "a@b.fr", "sujet neutre", "on parle de la fête ici"),
            (2, "c@d.fr", "rien", "rien du tout"),
        ],
    )

    assert client.search(SearchCriteria(query="fête", limit=10)) == [1]


def test_ascii_only_server_client_side_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
):
    """Accented matching folds case, like a normal IMAP substring search."""
    fake = _AsciiOnlyServer(ascii_hits=[1])
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)
    monkeypatch.setattr(
        client,
        "fetch_bodies_for_pattern",
        lambda uids, folder: [(1, "a@b.fr", "LA FÊTE NATIONALE", "x")],
    )

    assert client.search(SearchCriteria(subject_filter="fête", limit=10)) == [1]


def test_ascii_only_server_client_side_respects_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    """Client-side matching stops at the requested limit."""
    fake = _AsciiOnlyServer(ascii_hits=[1, 2, 3])
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)
    monkeypatch.setattr(
        client,
        "fetch_bodies_for_pattern",
        lambda uids, folder: [
            (1, "a@b.fr", "fête 1", "x"),
            (2, "a@b.fr", "fête 2", "x"),
            (3, "a@b.fr", "fête 3", "x"),
        ],
    )

    assert client.search(SearchCriteria(subject_filter="fête", limit=2)) == [1, 2]


def test_ascii_only_fallback_keeps_ascii_criteria_server_side(
    monkeypatch: pytest.MonkeyPatch,
):
    """Structural + ASCII criteria still narrow the candidate set on the server."""
    fake = _AsciiOnlyServer(ascii_hits=[1])
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)
    monkeypatch.setattr(
        client,
        "fetch_bodies_for_pattern",
        lambda uids, folder: [(1, "a@b.fr", "fête", "x")],
    )

    client.search(SearchCriteria(unseen_only=True, subject_filter="fête", limit=10))

    # The last (successful, no-charset) call keeps UNSEEN but drops the
    # accented SUBJECT term — that one is re-applied client-side.
    last_criteria = fake.search_calls[-1][0]
    assert "UNSEEN" in last_criteria
    assert not any(isinstance(item, bytes) and _is8bit(item) for item in last_criteria)


def test_ascii_capable_server_never_uses_client_side_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    """Gmail/Zimbra path is untouched: no body fetching, pure server search."""
    fake = _RecordingClient([42])
    client = IMAPClient(_account())
    monkeypatch.setattr(client, "_c", lambda: fake)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("client-side fallback must not run on a capable server")

    monkeypatch.setattr(client, "fetch_bodies_for_pattern", _forbidden)

    assert client.search(SearchCriteria(subject_filter="fête", limit=10)) == [42]
