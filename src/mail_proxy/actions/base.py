"""
Action framework — declarative approval, preflight, and verification policies.

Same contract as `tick-proxy`'s actions/base.py: an `ActionDef` carries the
action name, its colocated Pydantic payload model and the handler; the three
decorators (`@require_approval`, `@require_preflight`, `@require_verification`)
derive the HITL / safety policy from the handler itself — `cli.py` has no
separate policy table and no bypass path.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from time import monotonic, sleep
from typing import Any

from pydantic import BaseModel, Field

from ..exceptions import MailProxyError
from ..models import Verification

Preflight = Callable[[Any, Any], None]
DELETE_CONFIRM_TIMEOUT_SECONDS = 10.0
DELETE_CONFIRM_INTERVAL_SECONDS = 0.25


class AccountScoped(BaseModel):
    """Shared base of every action payload — optional account selection.

    Attributes:
        account_id (str | None): Account id; omit → the default account.

    Examples:
        >>> AccountScoped().account_id is None
        True
        >>> AccountScoped(account_id="poly").account_id
        'poly'
    """

    account_id: str | None = Field(
        None, description="Account id (omit → default account)"
    )


@dataclass(frozen=True)
class ActionDef:
    """One `do` action: name, payload model, handler and its policies.

    Attributes:
        name (str): The flat kebab-case action name, e.g. `message-send`.
        payload (type[BaseModel] | None): Pydantic model validating the payload
            (None when the action takes no payload).
        handler (Callable): `handler(client, payload) -> dict` or a
            `(dict, Verification)` tuple for required post-write checks.
        hitl (bool): Derived from the handler's `@require_approval` declaration.
        v2 (bool): Kept for ADN parity with tick-proxy; always False for mail
            (IMAP/SMTP have a single credential tier — no dual API).
        review_mode (ReviewMode): Always `"default"` for mail — every HITL page
            is the standard editable full-JSON form.
        group (str): Catalog group used by `do --help`, e.g. "Compose".
        aliases (tuple[str, ...]): Optional command aliases.

    Examples:
        >>> ActionDef("message-send", None, lambda c, p: {}, group="Compose").name
        'message-send'
        >>> ActionDef("message-info", None, lambda c, p: {}).hitl
        False
    """

    name: str
    payload: type[BaseModel] | None
    handler: Callable[..., Any]
    hitl: bool = False
    v2: bool = False
    review_mode: str = "default"
    group: str = "Misc"
    aliases: tuple[str, ...] = field(default_factory=tuple)


def require_verification(*checks: str) -> Callable:
    """Declare fields a write must read back and compare before returning.

    The wrapped handler must return `(data, verification)` where `verification`
    is a `Verification` model built by the handler itself (it knows what to
    re-read). The decorator only guarantees the contract: the attribute
    `__require_verification__` is set so registry tests can prove the policy.

    Args:
        *checks (str): Field names the handler must compare, e.g.
            `"uids", "destination_folder"`.

    Returns:
        Callable: The decorator.

    Examples:
        >>> @require_verification("uids")
        ... def h(client, payload): return {}, None
        >>> h.__require_verification__
        True
        >>> h.__verification_checks__
        ('uids',)
        >>> @require_verification("uids", "folder")
        ... def delete(client, payload): return {}
        >>> delete.__verification_checks__
        ('uids', 'folder')
        >>> callable(delete)
        True
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__require_verification__ = True  # type: ignore[attr-defined]
        wrapper.__verification_checks__ = checks  # type: ignore[attr-defined]
        return wrapper

    return decorator


def require_approval() -> Callable:
    """Declare a handler's mandatory centralized HITL review policy.

    Returns:
        Callable: A decorator carrying auditable review metadata.

    Examples:
        >>> @require_approval()
        ... def send(client, payload): return {}
        >>> send.__require_approval__
        True
        >>> send.__review_mode__
        'default'
        >>> callable(send)
        True
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__require_approval__ = True  # type: ignore[attr-defined]
        wrapper.__review_mode__ = "default"  # type: ignore[attr-defined]
        return wrapper

    return decorator


def require_preflight(
    *, check: Preflight, identity_fields: tuple[str, ...]
) -> Callable:
    """Require a resource-safety read before HITL and lock its identity in review.

    Args:
        check (Preflight): Read-only guard receiving the API client and validated
            payload. It raises `MailProxyError` when the requested resource is
            not safe to act on.
        identity_fields (tuple[str, ...]): Payload fields that identify the
            reviewed target and cannot change between preflight and approval.

    Returns:
        Callable: Decorator that declares the preflight and identity policy.

    Examples:
        >>> def exists(client, payload): return None
        >>> @require_preflight(check=exists, identity_fields=("uids",))
        ... def delete(client, payload): return {}
        >>> delete.__preflight_identity_fields__
        ('uids',)
        >>> delete.__preflight_check__ is exists
        True
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        wrapper.__preflight_check__ = check  # type: ignore[attr-defined]
        wrapper.__preflight_identity_fields__ = identity_fields  # type: ignore[attr-defined]
        return wrapper

    return decorator


def action_def(
    name: str,
    payload: type[BaseModel] | None,
    handler: Callable[..., Any],
    *,
    v2: bool = False,
    group: str = "Misc",
    aliases: tuple[str, ...] = (),
) -> ActionDef:
    """Build an action definition from visible handler decorators.

    Args:
        name (str): Flat registered action name.
        payload (type[BaseModel] | None): Pydantic payload model.
        handler (Callable[..., Any]): Decorated implementation.
        v2 (bool): Always False for mail — kept for ADN parity only.
        group (str): Help catalog group.
        aliases (tuple[str, ...]): Optional command aliases.

    Returns:
        ActionDef: HITL policy derived from the handler.

    Examples:
        >>> @require_approval()
        ... def delete(client, payload): return {}
        >>> action_def("message-delete", None, delete).hitl
        True
        >>> action_def("message-info", None, lambda c, p: {}).hitl
        False
        >>> action_def("raw", None, lambda c, p: {}, group="Escape hatch").group
        'Escape hatch'
    """
    return ActionDef(
        name=name,
        payload=payload,
        handler=handler,
        hitl=bool(getattr(handler, "__require_approval__", False)),
        v2=v2,
        review_mode="default",
        group=group,
        aliases=aliases,
    )


def compare(
    method: str, expected: dict[str, Any], actual: dict[str, Any]
) -> Verification:
    """Build a `Verification` by comparing the intended and observed states.

    Args:
        method (str): The read-back performed, for the audit trail.
        expected (dict[str, Any]): What the caller asked for.
        actual (dict[str, Any]): What the server really holds.

    Returns:
        Verification: `ok=True` only when every expected key matches.

    Examples:
        >>> compare("UID SEARCH INBOX", {"uids": [42]}, {"uids": [42]}).ok
        True
        >>> compare("UID SEARCH INBOX", {"uids": []}, {"uids": [42]}).ok
        False
    """
    ok = all(actual.get(k) == v for k, v in expected.items())
    return Verification(
        method=method,
        checked=sorted(expected),
        expected=expected,
        actual={k: actual.get(k) for k in expected},
        ok=ok,
    )


def verify_absence(
    read: Callable[[], Any],
    resource_id: str,
    method: str,
    *,
    timeout_seconds: float = DELETE_CONFIRM_TIMEOUT_SECONDS,
    interval_seconds: float = DELETE_CONFIRM_INTERVAL_SECONDS,
) -> Verification:
    """Poll a post-delete read until the mail server confirms absence.

    The read must return the still-present resources: `[]`, `{}` or None all
    mean "gone". A raise is only tolerated when it signals absence
    (MailAPIError with status 0 carrying "not found" semantics is NOT enough —
    any raised error aborts, matching tick-proxy's 404-only tolerance).

    Args:
        read (Callable[[], Any]): Fresh read returning the remaining resources
            (empty = absent).
        resource_id (str): Deleted resource identifier.
        method (str): Human-readable read endpoint recorded in the proof.
        timeout_seconds (float): Maximum eventual-consistency confirmation wait.
        interval_seconds (float): Delay between reads while the stale resource
            remains visible.

    Returns:
        Verification: `ok=True` after the read returns an empty result.

    Raises:
        MailProxyError: When the resource still exists after the deadline.

    Examples:
        >>> calls = iter([[], [42]])
        >>> verify_absence(lambda: next(calls), "42", "UID SEARCH INBOX",
        ...                timeout_seconds=1, interval_seconds=0).ok
        True
        >>> verify_absence(lambda: [42], "42", "UID SEARCH INBOX",
        ...                timeout_seconds=0, interval_seconds=0)
        Traceback (most recent call last):
        ...
        mail_proxy.exceptions.MailProxyError: Delete was accepted but 42 still exists after 0.0 seconds.
    """
    deadline = monotonic() + timeout_seconds
    while True:
        observed = read()
        if observed in ({}, None, []):
            return compare(
                method,
                {"deleted": resource_id},
                {"deleted": resource_id},
            )
        if monotonic() >= deadline:
            raise MailProxyError(
                f"Delete was accepted but {resource_id} still exists after {timeout_seconds} seconds."
            )
        sleep(interval_seconds)


def remaining_uids(client: Any, uids: list[int], folder: str) -> list[int]:
    """Return the UIDs that still exist in a folder (delete/move verification).

    Args:
        client (Any): The MailClient (or a fake in tests).
        uids (list[int]): UIDs that should be gone.
        folder (str): Folder to re-check.

    Returns:
        list[int]: The subset still present.

    Examples:
        >>> remaining_uids(client, [42], "INBOX")
        []
    """
    present = []
    for uid in uids:
        if client.imap().message_exists(uid, folder):
            present.append(uid)
    return present
