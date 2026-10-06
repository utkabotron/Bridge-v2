"""The chat context block the translator receives, and the A/B variant choice."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from processor.src.pipeline.prompts import (
    PROMPT_VERSION,
    PROMPT_VERSION_B,
    choose_variant,
    format_chat_context,
    get_translate_prompt,
    register_prompt,
)


def test_glossary_is_framed_as_names_not_transliterations():
    """'Use these transliterations' made the translator write кадурсаль for basketball."""
    ctx = format_chat_context({"glossary": {"אופק": {"translation": "Офек", "note": "school platform"}}})

    assert "אופק → Офек (school platform)" in ctx
    assert "transliteration" not in ctx.lower()
    assert "translate everything else normally" in ctx


def test_removal_log_and_flags_never_reach_the_translator():
    ctx = format_chat_context({
        "glossary": {"גבעולים": "Геваулим"},
        "glossary_removed": {"כדורסל": {"rendering": "кадурсаль"}},
        "glossary_flags": {"חוג": {"count": 1}},
    })
    assert "кадурсаль" not in ctx and "חוג" not in ctx
    assert "גבעולים → Геваулим" in ctx


def test_empty_profile_adds_nothing():
    assert format_chat_context({}) == ""
    assert format_chat_context({"glossary_removed": {"x": {}}}) == ""


def test_context_is_appended_to_the_system_prompt():
    prompt = get_translate_prompt("Russian", format_chat_context({"tone": "warm"}))
    assert prompt.rstrip().endswith("- Tone: warm")
    assert "into Russian" in prompt


# ── A/B ──

def test_variant_b_only_for_odd_pairs_while_the_flag_is_on():
    assert choose_variant(29, True) == "B"
    assert choose_variant(12, True) == "A"
    assert choose_variant(29, False) == "A"
    assert choose_variant(None, True) == "A"   # DM / unpaired: always the production prompt
    assert choose_variant(0, True) == "A"


def test_variants_are_different_prompts_with_different_versions():
    a = get_translate_prompt("Russian", variant="A")
    b = get_translate_prompt("Russian", variant="B")
    assert a != b
    assert "Compound nouns" in b and "Compound nouns" not in a
    assert PROMPT_VERSION != PROMPT_VERSION_B
    assert get_translate_prompt("Russian", variant="nonsense") == a  # unknown → A


@pytest.mark.asyncio
async def test_register_prompt_logs_a_version_change_for_the_weekly_report():
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={"version": "v2.9"})

    await register_prompt(pool)

    keys = [call.args[1] for call in pool.execute.await_args_list if "prompt_registry" in call.args[0]]
    assert keys == ["translate", "translate_b"]
    changelog = [call for call in pool.execute.await_args_list if "analytics_changelog" in call.args[0]]
    assert len(changelog) == 1
    assert changelog[0].args[1] == f"translate prompt v2.9 → {PROMPT_VERSION}"


@pytest.mark.asyncio
async def test_register_prompt_is_silent_when_the_version_is_unchanged():
    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value={"version": PROMPT_VERSION})
    await register_prompt(pool)
    assert not [c for c in pool.execute.await_args_list if "analytics_changelog" in c.args[0]]
