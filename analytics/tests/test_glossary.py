"""Glossary gates: evaluator flags, removal, validator parsing, the builder's ban list."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from flows import glossary
from flows.chat_context_builder import build_extraction_prompt, merge_profiles

GLOSSARY = {
    "כדורסל": {"translation": "кадурсаль", "note": "basketball"},
    "גבעולים": {"translation": "Геваулим", "note": "school"},
}


def _eval(original, translated, issues, score=2):
    return {
        "original_text": original,
        "translated_text": translated,
        "quality_score": score,
        "issues": [{"type": t} for t in issues],
    }


def test_hits_implicate_only_entries_present_in_both_source_and_bad_translation():
    evaluations = [
        _eval("עופר יצא לכדורסל", "Офер ушёл на кадурсаль", ["mistranslation"]),
        _eval("ברוכים הבאים לגבעולים", "Добро пожаловать в Геваулим", ["omission"]),  # not a glossary issue
        _eval("מחר כדורסל", "Завтра баскетбол", ["grammar"]),  # rendering not used
    ]
    hits = glossary.find_hits(evaluations, GLOSSARY)
    assert list(hits) == ["כדורסל"]
    assert hits["כדורסל"][0]["issues"] == ["mistranslation"]


def test_flags_accumulate_across_nights_and_remove_at_threshold():
    profile = {"glossary": dict(GLOSSARY)}
    hit = {"כדורסל": [{"score": 2, "original": "o", "translated": "t", "issues": ["mistranslation"]}]}

    profile, removed = glossary.apply_flags(profile, hit, date(2026, 10, 6), threshold=3)
    assert removed == [] and profile["glossary_flags"]["כדורסל"]["count"] == 1
    profile, removed = glossary.apply_flags(profile, hit, date(2026, 10, 7), threshold=3)
    assert removed == []
    profile, removed = glossary.apply_flags(profile, hit, date(2026, 10, 8), threshold=3)

    assert removed == ["כדורסל"]
    assert "כדורסל" not in profile["glossary"]
    assert "כדורסל" not in profile["glossary_flags"]
    assert profile["glossary_removed"]["כדורסל"]["rendering"] == "кадурсаль"
    assert profile["glossary_removed"]["כדורסל"]["removed_at"] == "2026-10-08"
    assert "גבעולים" in profile["glossary"]  # the innocent entry stays


def test_apply_flags_does_not_mutate_the_input():
    profile = {"glossary": dict(GLOSSARY)}
    glossary.apply_flags(profile, {"כדורסל": [{}] * 5}, threshold=3)
    assert "כדורסל" in profile["glossary"]
    assert "glossary_flags" not in profile


def test_removed_entries_cannot_come_back_through_merge():
    existing = {"glossary": {"גבעולים": "Геваулим"}, "glossary_removed": {"כדורסל": {"reason": "evaluator"}}}
    merged = merge_profiles(existing, {"glossary": {"כדורסל": {"translation": "кадурсаль"}, "אופק": {"translation": "Офек"}}})
    assert set(merged["glossary"]) == {"גבעולים", "אופק"}
    assert "כדורסל" in merged["glossary_removed"]


def test_extraction_prompt_bans_removed_keys_and_forbids_everyday_words():
    prompt = build_extraction_prompt("Russian", {"glossary_removed": {"כדורסל": {}, "חוג": {}}})
    assert "NEVER propose these glossary keys again" in prompt
    assert "כדורסל" in prompt and "חוג" in prompt
    assert "NEVER an everyday word" in prompt
    assert "should be transliterated" not in prompt


def test_validator_parses_fenced_json_and_ignores_unknown_keys():
    content = '```json\n{"drop": {"כדורסל": "everyday word", "לא קיים": "x"}}\n```'
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    )))
    kept, dropped = glossary.validate_entries(GLOSSARY, "Russian", client)
    assert set(kept) == {"גבעולים"}
    assert dropped == {"כדורסל": "everyday word"}


def test_validator_failure_keeps_everything():
    def boom(**kw):
        raise RuntimeError("openai down")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=boom)))
    kept, dropped = glossary.validate_entries(GLOSSARY, "Russian", client)
    assert kept == GLOSSARY and dropped == {}


def test_record_dropped_removes_and_remembers():
    profile = {"glossary": dict(GLOSSARY), "glossary_flags": {"כדורסל": {"count": 1}}}
    out = glossary.record_dropped(profile, {"כדורסל": "everyday word"}, date(2026, 10, 6))
    assert "כדורסל" not in out["glossary"]
    assert out["glossary_removed"]["כדורסל"]["reason"] == "validator: everyday word"
    assert "כדורסל" not in out["glossary_flags"]
    assert json.loads(json.dumps(out, ensure_ascii=False)) == out  # JSON-serialisable for jsonb
