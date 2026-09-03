"""Registry integrity — the anti-drift gate for the 37 actions."""

from mail_proxy.actions.registry import REGISTRY, by_group

EXPECTED_ACTIONS = 37
REQUIRE_VERIFICATION = {
    "label-set",
    "label-delete",
    "label-delete",
    "message-mark",
    "message-move",
    "message-archive",
    "message-trash",
    "message-spam",
    "message-delete",
    "folder-delete",
    "zimbra-tag-create",
    "zimbra-tag-delete",
    "zimbra-tag-apply",
    "zimbra-tag-remove",
}
HITL = {
    "message-send",
    "message-reply",
    "message-forward",
    "message-draft",
    "message-move",
    "message-archive",
    "message-trash",
    "message-spam",
    "message-delete",
    "folder-delete",
    "raw",
    "zimbra-tag-delete",
    "zimbra-tag-apply",
    "zimbra-tag-remove",
}


def test_action_count():
    assert len(REGISTRY) == EXPECTED_ACTIONS


def test_no_duplicate_names():
    assert len(REGISTRY) == len(set(REGISTRY))


def test_names_are_domain_first_kebab():
    for name in REGISTRY:
        assert name == name.lower()
        assert " " not in name and "_" not in name


def test_every_action_has_a_docstring_with_examples():
    for name, action in REGISTRY.items():
        doc = action.handler.__doc__ or ""
        assert doc.strip(), f"{name} has no docstring"
        assert "Parameters:" in doc, f"{name} docstring lacks Parameters:"
        assert "Examples:" in doc, f"{name} docstring lacks Examples:"


def test_every_action_has_at_least_three_examples():
    """KπX rule (2026-08-12): ≥3 real examples per action docstring."""
    for name, action in REGISTRY.items():
        doc = action.handler.__doc__ or ""
        examples = doc.split("Examples:")[-1]
        arrow_count = examples.count("→")
        assert arrow_count >= 3, (
            f"{name} has only {arrow_count} example(s) — at least 3 required"
        )


def test_raw_has_more_examples():
    """The escape hatch is arbitrary — it needs extra examples (≥5)."""
    doc = REGISTRY["raw"].handler.__doc__ or ""
    examples = doc.split("Examples:")[-1]
    assert examples.count("→") >= 5, "raw must carry at least 5 examples"


def test_required_verifications_carry_the_decorator():
    declared = {
        name
        for name, action in REGISTRY.items()
        if getattr(action.handler, "__require_verification__", False)
    }
    assert declared == REQUIRE_VERIFICATION
    for name in declared:
        handler = REGISTRY[name].handler
        assert getattr(handler, "__verification_checks__", ()), (
            f"{name} requires verification but declares no compared fields"
        )


def test_hitl_actions_match_the_explicit_review_policy():
    assert {n for n, a in REGISTRY.items() if a.hitl} == HITL
    for name in HITL:
        assert getattr(REGISTRY[name].handler, "__require_approval__", False), (
            f"{name} is HITL but does not declare @require_approval"
        )


def test_all_irreversible_actions_declare_preflight_and_locked_target():
    """Ensure every destructive review validates and locks its target first."""
    expected = {
        "message-delete": ("uids", "folder"),
        "folder-delete": ("names",),
    }
    for name, identity_fields in expected.items():
        handler = REGISTRY[name].handler
        assert callable(getattr(handler, "__preflight_check__", None)), name
        assert getattr(handler, "__preflight_identity_fields__", ()) == identity_fields


def test_reversible_marks_and_labels_never_require_hitl():
    """Only message moves are reviewed; flags and labels remain direct writes."""
    for name in ("message-mark", "label-set", "label-delete"):
        assert not REGISTRY[name].hitl, f"{name} must not require HITL"
        assert getattr(REGISTRY[name].handler, "__require_verification__", False), (
            f"{name} must declare read-back verification"
        )


def test_groups_cover_every_action():
    assert sum(len(v) for v in by_group().values()) == EXPECTED_ACTIONS


def test_v2_flag_is_never_used_for_mail():
    """IMAP/SMTP have a single credential tier — the v2 flag stays off."""
    for name, action in REGISTRY.items():
        assert action.v2 is False, f"{name} must not require V2 auth"
