"""
The action registry — `name → ActionDef`, built from every domain module.

Adding an action means adding ONE `ActionDef` to its domain module; `cli.py`
builds its commands from here, so nothing else has to be touched. Duplicate
names raise at import time, which is what makes the registry a real single
source of truth (and what `make smoke` verifies).
"""

from . import (
    attachments,
    compose,
    folders,
    inbox,
    labels,
    messages,
    raw,
    signatures,
    zimbra,
)
from .base import ActionDef

_MODULES = (
    inbox,
    messages,
    compose,
    folders,
    attachments,
    labels,
    signatures,
    zimbra,
    raw,
)

REGISTRY: dict[str, ActionDef] = {}
for _module in _MODULES:
    for _action in _module.ACTIONS:
        if _action.name in REGISTRY:
            raise RuntimeError(f"Duplicate action name in registry: {_action.name}")
        REGISTRY[_action.name] = _action


def get(name: str) -> ActionDef:
    """Return one action definition by name.

    Args:
        name (str): The flat kebab-case action name.

    Returns:
        ActionDef: The matching definition.

    Raises:
        KeyError: When the action does not exist.

    Examples:
        >>> get("message-send").group
        'Compose'
        >>> get("raw").hitl
        True
    """
    return REGISTRY[name]


def by_group() -> dict[str, list[ActionDef]]:
    """Group every action by catalog group, preserving registration order.

    Returns:
        dict[str, list[ActionDef]]: `{group_name: [ActionDef, …]}`.

    Examples:
        >>> sorted(by_group())[:3]
        ['Attachments', 'Compose', 'Escape hatch']
        >>> [a.name for a in by_group()["Inbox"]]
        ['inbox-check', 'inbox-digest']
    """
    groups: dict[str, list[ActionDef]] = {}
    for action in REGISTRY.values():
        groups.setdefault(action.group, []).append(action)
    return groups
