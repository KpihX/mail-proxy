"""
SHARED types for mail-proxy — the output envelope and common enums.

Per-action payload models live next to their handler in `actions/<domain>.py`
(colocation), so this module stays small and has exactly one responsibility:
describe what every command returns. Mail domain models (Message, Folder, …)
live in `api/models.py` — they are transport-level objects.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["ok", "approved", "rejected", "error"]


class Verification(BaseModel):
    """Read-back verification block returned inside `data.verification`.

    Attributes:
        method (str): The read-back performed, e.g.
            ``UID SEARCH INBOX``.
        checked (list[str]): Field names compared after the write.
        expected (dict): What the caller asked for.
        actual (dict): What the server really holds after the write.
        ok (bool): True when expected == actual on every checked field.

    Examples:
        >>> Verification(method="UID SEARCH INBOX", checked=["uids"],
        ...              expected={"uids": []}, actual={"uids": []}, ok=True).ok
        True
        >>> Verification(method="UID SEARCH INBOX", checked=["uids"],
        ...              expected={"uids": []}, actual={"uids": [42]}, ok=False).ok
        False
    """

    method: str = Field(..., description="The read-back call performed")
    checked: list[str] = Field(default_factory=list, description="Compared fields")
    expected: dict[str, Any] = Field(default_factory=dict, description="Intended state")
    actual: dict[str, Any] = Field(default_factory=dict, description="Observed state")
    ok: bool = Field(..., description="True when every checked field matches")


class OutputMeta(BaseModel):
    """The `meta` half of every response envelope.

    Attributes:
        status (Status): ok · approved · rejected · error. `approved`/`rejected`
            only appear when HITL was involved.
        comment (str): The HITL reviewer's comment (empty when none).
        edited (bool): True when the HITL reviewer modified the payload.

    Examples:
        >>> OutputMeta().model_dump()
        {'status': 'ok', 'comment': '', 'edited': False}
        >>> OutputMeta(status="rejected", comment="wrong recipient").status
        'rejected'
    """

    status: Status = Field(default="ok", description="Result status")
    comment: str = Field(default="", description="HITL reviewer comment")
    edited: bool = Field(default=False, description="HITL reviewer edited the payload")


class Output(BaseModel):
    """The full response envelope printed on stdout.

    Attributes:
        meta (OutputMeta): Command metadata (status, HITL).
        data (Any): The pure mail payload — never mixed with metadata.

    Examples:
        >>> Output(data={"uid": 42}).model_dump()["meta"]["status"]
        'ok'
        >>> Output(meta=OutputMeta(status="rejected"), data=None).data is None
        True
    """

    meta: OutputMeta = Field(default_factory=OutputMeta)
    data: Any = Field(default=None)


def ok(
    data: Any,
    edited: bool = False,
    comment: str = "",
    status: Status = "ok",
) -> dict:
    """Build a successful envelope as a plain dict.

    Args:
        data (Any): The business payload to return.
        edited (bool): Whether the payload was edited during HITL review.
        comment (str): The HITL reviewer's comment (empty when none).
        status (Status): Result status (ok, approved, rejected, error).

    Returns:
        dict: ``{"meta": {...}, "data": ...}`` ready to print.

    Examples:
        >>> ok({"uid": 42})["meta"]["status"]
        'ok'
        >>> ok([], None)["data"]
        []
    """
    return Output(
        meta=OutputMeta(status=status, comment=comment, edited=edited),
        data=data,
    ).model_dump()


def rejected(comment: str = "", edited: bool = False) -> dict:
    """Build a HITL-rejected envelope.

    Args:
        comment (str): Reviewer's reason.
        edited (bool): Whether the reviewer had edited the payload.

    Returns:
        dict: Envelope with `status="rejected"` and `data=None`.

    Examples:
        >>> rejected("not now")["meta"]["status"]
        'rejected'
        >>> rejected()["data"] is None
        True
    """
    return Output(
        meta=OutputMeta(status="rejected", comment=comment, edited=edited),
        data=None,
    ).model_dump()
