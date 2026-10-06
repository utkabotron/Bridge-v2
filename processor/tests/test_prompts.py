"""The chat context block the translator receives alongside the system prompt."""
from __future__ import annotations

from processor.src.pipeline.prompts import format_chat_context, get_translate_prompt


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
