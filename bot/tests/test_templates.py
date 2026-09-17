"""Tests for message templates and auth logic."""
from bot.src.templates.messages import render


def test_render_known_template():
    text = render("not_authorized")
    assert "restricted" in text.lower() or "access" in text.lower()


def test_render_with_params():
    text = render("admin_whitelist_added", username="alice")
    assert "alice" in text


def test_render_missing_template():
    import pytest
    with pytest.raises(KeyError):
        render("nonexistent_key")


def test_render_pair_created():
    """The message a user gets once a bridge exists."""
    text = render("onboarding_done_success", wa_name="Work Group", tg_title="Work RU")
    assert "Work Group" in text
    assert "Work RU" in text


def test_no_dead_wizard_templates():
    """The inline wizard is gone; its templates must not linger as an invitation to
    resurrect a flow that was never reachable."""
    from src.templates.messages import TEMPLATES

    for key in ("onboarding_step1", "onboarding_step2_wait", "onboarding_step3",
                "onboarding_step4", "onboarding_complete", "onboarding_webapp_linked",
                "onboarding_wa_connected", "welcome_new", "welcome_back"):
        assert key not in TEMPLATES, f"{key} should have been removed"


def test_interface_is_english():
    """One language in the bot's own copy. Language *names* in the picker stay in their
    own script (Русский, Українська) — that is how a speaker recognises them."""
    import re

    from src.templates.messages import TEMPLATES

    for key, value in TEMPLATES.items():
        assert not re.search(r"[а-яА-ЯёЁ]", value), f"{key} contains Cyrillic: {value!r}"
