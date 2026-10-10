"""bridge_shared (shared/bridge_shared): what processor, analytics and bot share.

Tested here because this suite runs everywhere — locally, before every deploy, in CI.
The guard at the bottom fails when a shared definition is copied back into a service:
that is how three price tables and four sets of script regexes drifted apart.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from bridge_shared import chat_context, env, llm, scripts, telegram_html

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ── llm ───────────────────────────────────────────────────

def test_token_cost_and_flex_discount():
    assert llm.token_cost("gpt-6-luna", 1_000_000, 1_000_000) == 0.60
    assert llm.token_cost("gpt-6.1-sol", 1_000_000, 100_000, flex=True) == 1.5
    # gpt-4.1 has no Flex tier: the request went out on standard, so no discount
    assert llm.token_cost("gpt-4.1-mini", 1_000_000, 100_000, flex=True) == 0.56
    assert llm.token_cost("no-such-model", 10, 10, flex=True) == 0.0
    assert llm.transcribe_cost("gpt-transcribe", 120) == pytest.approx(0.009)
    assert llm.transcribe_cost("gpt-transcribe", None) == 0.0


def test_reasoning_and_flex_prefixes():
    assert llm.is_reasoning("o1") and not llm.supports_flex("o1")
    assert llm.is_reasoning("gpt-6-luna") and llm.supports_flex("gpt-6-luna")
    assert not llm.is_reasoning("gpt-4.1-mini") and not llm.supports_flex("gpt-4.1-mini")
    # the bake-off's old bare "o" prefix called these reasoning models
    assert not llm.is_reasoning("omni-moderation-latest")


def test_chat_request_defaults_are_the_translators_request():
    msgs = [{"role": "user", "content": "hi"}]
    assert llm.chat_request("gpt-6-luna", msgs) == {
        "model": "gpt-6-luna", "messages": msgs, "reasoning_effort": "none"}
    assert llm.chat_request("gpt-4.1-mini", msgs) == {
        "model": "gpt-4.1-mini", "messages": msgs, "temperature": 0}
    judged = llm.chat_request("gpt-6.1-sol", msgs, max_tokens=50, reasoning="low", json_mode=True)
    assert judged["max_completion_tokens"] == 50 and judged["reasoning_effort"] == "low"
    assert judged["response_format"] == {"type": "json_object"} and "temperature" not in judged


def test_the_processors_models_are_priced():
    """An unpriced model costs $0 in llm_usage and on the dashboard without a word."""
    from processor.src import config
    from processor.src.pipeline.prompts import VARIANTS

    chat_models = {"gpt-4.1-mini", config.OPENAI_MODEL, config.DIRECT_MODEL}
    chat_models |= {v["model"] for v in VARIANTS.values() if v["model"]}
    assert chat_models <= set(llm.MODEL_PRICES)
    assert config.TRANSCRIBE_MODEL in llm.TRANSCRIBE_PRICES


# ── scripts, chat context, esc, env ───────────────────────

def test_target_script_lookup():
    assert scripts.target_script_re(" Russian ") is scripts.CYRILLIC_RE
    assert scripts.target_script_re("ENGLISH") is scripts.LATIN_RE
    assert scripts.target_script_re("Hebrew") is scripts.HEBREW_RE
    assert scripts.target_script_re("Thai") is None
    assert scripts.target_script_re(None) is None
    assert scripts.SOURCE_SCRIPT_RE.search("مرحبا") and scripts.HEBREW_RE.search("שלום")
    assert not scripts.HEBREW_RE.search("مرحبا")


def test_chat_context_tolerates_missing_parts():
    assert chat_context.format_chat_context(None) == ""
    assert chat_context.format_chat_context({"glossary": None, "members": None}) == ""
    ctx = chat_context.format_chat_context({"members": {"Dana": "Дана"}})
    assert ctx == "\nChat context:\n- Member names:\n  Dana → Дана"


def test_glossary_hits_override_the_chats_guess():
    profile = {"tone": "warm",
               "glossary": {"גבעולים": {"translation": "Геваулим"},
                            "ביה״ס גבעולים": {"translation": "Бейт-сефер Гевалим"},
                            "אופק": {"translation": "Офек"}},
               "members": {"אורי": "Ури", "דנה": "Дана"}}
    hits = {"גבעולים": {"translation": "Гиволим", "note": "школа"},
            "אורי": {"translation": "Ори"}}

    merged = chat_context.with_glossary(profile, hits)
    assert merged["glossary"] == {"אופק": {"translation": "Офек"}, **hits}
    assert merged["members"] == {"דנה": "Дана"}
    assert merged["tone"] == "warm"
    assert profile["glossary"]["גבעולים"] == {"translation": "Геваулим"}  # caller's untouched

    # Nothing mentioned: the profile goes through as is, so the cache key does not move.
    assert chat_context.with_glossary(profile, {}) is profile
    alone = chat_context.with_glossary(None, hits)
    assert "Гиволим" in chat_context.format_chat_context(alone)
    assert chat_context.with_glossary(None, {}) == {}


def test_covered_by_global_matches_whole_words_either_way():
    assert chat_context.covered_by_global("ביה״ס גבעולים", {"גבעולים"})
    assert chat_context.covered_by_global("גבעולים", {"ביה״ס גבעולים"})
    assert not chat_context.covered_by_global("אופק", {"גבעולים"})
    assert not chat_context.covered_by_global("רגיל", {"גיל"})  # substring is not a name
    assert not chat_context.covered_by_global("אופק", {""})


def test_esc_escapes_quotes_and_accepts_non_strings():
    assert telegram_html.esc('<a href="x">Tom\'s & co</a>') == (
        "&lt;a href=&quot;x&quot;&gt;Tom&#x27;s &amp; co&lt;/a&gt;")
    assert telegram_html.esc(None) == "None"


def test_parse_ids(monkeypatch):
    assert env.parse_ids(" 1, 2,,3 ,") == [1, 2, 3]
    assert env.parse_ids(None) == [] and env.parse_ids("") == []
    with pytest.raises(ValueError):
        env.parse_ids("1,two")
    monkeypatch.setenv("ADMIN_TG_IDS", "7,8")
    assert env.admin_tg_ids() == [7, 8]


# ── one definition only ──────────────────────────────────

# Service source only — tests are left out, this file spells out the very patterns.
_SERVICE_SOURCES = ("processor/src", "analytics/flows", "analytics/serve_flows.py", "bot/src")
_SHARED = ROOT / "shared" / "bridge_shared"

# Script-range boundaries, as escapes or as the characters themselves. Built from code
# points so this file carries neither form.
_BOUNDARIES = (0x0400, 0x04FF, 0x0590, 0x05FF, 0x0600, 0x06FF, 0x0700, 0x074F)
_SCRIPT_MARKERS = [re.escape(chr(92) + "u%04x" % cp) for cp in _BOUNDARIES]
_SCRIPT_MARKERS += [re.escape(chr(cp)) for cp in _BOUNDARIES]
_SCRIPT_MARKERS.append(re.escape("[A-Za-z]"))

GUARDS = {
    # a price table, or a model → price entry anywhere
    "model prices": re.compile(
        r"""\b\w*PRICES\s*(:[^=\n]*)?=[^=]|["'](gpt-|o\d|whisper)[\w.-]*["']\s*:\s*[(\d]"""),
    # a tuple of model-family prefixes: ("gpt-5", "gpt-6", ...)
    "reasoning/Flex prefixes": re.compile(r"""\(\s*["'](gpt-\d|o\d?)["']\s*,"""),
    "script regexes": re.compile("|".join(_SCRIPT_MARKERS), re.IGNORECASE),
    "format_chat_context": re.compile(r"^\s*def format_chat_context\b", re.MULTILINE),
    "esc": re.compile(r"^\s*def esc\b", re.MULTILINE),
    "ADMIN_TG_IDS parsing": re.compile(r"""["']ADMIN_TG_IDS["']"""),
}


def _files(paths):
    for p in paths:
        yield from ([p] if p.is_file() else sorted(p.rglob("*.py")))


@pytest.mark.parametrize("what", sorted(GUARDS))
def test_defined_only_in_bridge_shared(what):
    pattern = GUARDS[what]
    copies = [
        f"{path.relative_to(ROOT)}:{text[:m.start()].count(chr(10)) + 1}"
        for path in _files(ROOT / s for s in _SERVICE_SOURCES)
        for text in [path.read_text(encoding="utf-8")]
        for m in pattern.finditer(text)
    ]
    assert not copies, f"{what} belongs in shared/bridge_shared, found a copy at {copies}"
    # and the guard is not vacuous: the shared package does match it
    assert any(pattern.search(p.read_text(encoding="utf-8")) for p in _files([_SHARED]))
