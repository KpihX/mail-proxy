"""
Ultra-simple docstring helper for the mail-proxy CLI.

Returns the docstring of an action handler with `→` output examples
auto-wrapped in the meta+data envelope, for human/agent clarity.
Single source of truth: the docstrings themselves — there is no second
documentation surface (this is what replaces `mail-mcp`'s `mail_guide`).
"""

import inspect
import json
import re
from collections.abc import Callable


def get_compact_help(func: Callable) -> str:
    """Return the docstring without the Examples section — for the group overview.

    Args:
        func (Callable): An action handler carrying a structured docstring.

    Returns:
        str: Everything before the `Examples:` marker, stripped.

    Examples:
        >>> def f():
        ...     '''Do a thing.\\n\\nExamples:\\n    - x'''
        >>> get_compact_help(f)
        'Do a thing.'
        >>> def g():
        ...     '''No examples here.'''
        >>> get_compact_help(g)
        'No examples here.'
    """
    doc = inspect.getdoc(func) or ""
    parts = re.split(r"(?i)^\s*Examples:\s*$", doc, flags=re.MULTILINE, maxsplit=1)
    return parts[0].strip()


def _wrap_output(line: str) -> str:
    """Wrap a `→ {json}` example line in the meta+data envelope.

    Non-JSON example lines are returned untouched.

    Args:
        line (str): A single docstring line.

    Returns:
        str: The same line, or the JSON re-rendered inside the envelope.

    Examples:
        >>> _wrap_output('    → {"uid": 42}')
        '    → {\\n  "meta": {...},\\n  "data": {\\n    "uid": 42\\n  }\\n}'
        >>> _wrap_output('    - just prose')
        '    - just prose'
    """
    m = re.match(r"^( *→\s*)(.*)", line)
    if not m:
        return line
    arrow = m.group(1)
    content = m.group(2).strip()
    try:
        data = json.loads(content)
        wrapped = json.dumps(
            {
                "meta": {
                    "status": "ok",
                    "comment": "",
                    "edited": False,
                },
                "data": data,
            },
            indent=2,
            default=str,
        )
        return f"{arrow}{wrapped}"
    except (json.JSONDecodeError, ValueError, TypeError):
        return line


def get_full_help(func: Callable) -> str:
    """Return the full docstring with `→` examples wrapped in meta+data.

    Args:
        func (Callable): An action handler carrying a structured docstring.

    Returns:
        str: The docstring, with every JSON example expanded to the real
        envelope the CLI prints.

    Examples:
        >>> def f():
        ...     '''T.\\n\\nExamples:\\n    → {"ok": true}'''
        >>> "meta" in get_full_help(f)
        True
        >>> def g():
        ...     '''No arrow lines.'''
        >>> get_full_help(g)
        'No arrow lines.'
    """
    doc = inspect.getdoc(func) or ""
    lines = doc.split("\n")
    new_lines = [_wrap_output(line) for line in lines]
    return "\n".join(new_lines)
