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


def test_render_onboarding_step2():
    url = "https://wa.example.com/qr/page/123"
    text = render("onboarding_step2_wait", qr_url=url)
    assert url in text


def test_render_onboarding_complete():
    text = render("onboarding_complete", wa_name="Work Group")
    assert "Work Group" in text
