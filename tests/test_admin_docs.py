"""Admin docstrings — every admin function carries ≥3 real examples (KπX rule)."""

import inspect

from mail_proxy import admin


def test_every_admin_function_has_three_examples():
    for name in ("setup", "status", "reset", "purge"):
        func = getattr(admin, name)
        doc = inspect.getdoc(func) or ""
        assert doc.strip(), f"admin.{name} has no docstring"
        assert "Returns:" in doc, f"admin.{name} docstring lacks Returns:"
        examples = doc.split("Examples:")[-1]
        assert examples.count("→") >= 3, (
            f"admin.{name} has only {examples.count('→')} example(s) — at least 3 required"
        )


def test_admin_commands_have_help_docstrings():
    """The four `mail-proxy admin` commands expose a one-line purpose."""
    from mail_proxy.cli import (
        admin_purge,
        admin_reset,
        admin_setup,
        admin_status,
    )

    for cmd in (admin_setup, admin_status, admin_reset, admin_purge):
        doc = inspect.getdoc(cmd) or ""
        assert doc.strip(), f"{cmd.__name__} has no docstring"
        assert "ALWAYS JSON" in doc
