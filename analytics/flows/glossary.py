"""Glossary hygiene for chat profiles: what gets in, and what the evaluator throws out.

The profile builder used to ask for "words that should be transliterated" and the
translator was told to "use these transliterations". Everyday nouns crept in — basketball
became "кадурсаль", a club "хуг", English "англит" — and the translator obeyed. Nothing
ever removed an entry, and the builder learned from its own translations, so the worst
chat sat at a third of its messages rated bad.

Two gates now:
  validate_entries   an LLM classifies each NEW entry as a named entity (kept) or an
                     everyday word (dropped) before it reaches the profile;
  hits/apply_flags   nightly, an entry whose rendering shows up in translations the
                     evaluator rated bad gets a flag; at FLAG_THRESHOLD it is removed and
                     remembered in `glossary_removed` so the builder cannot re-add it.

No Prefect, no database: the flow and the one-off prune share this and tests cover it.
"""
from __future__ import annotations

import json
import os
from datetime import date

# Bad-translation signals that implicate a glossary entry. Omissions and formatting are
# never the glossary's fault.
GLOSSARY_ISSUE_TYPES = {"mistranslation", "untranslated", "grammar"}
FLAG_THRESHOLD = int(os.getenv("GLOSSARY_FLAG_THRESHOLD", 3))
MAX_EXAMPLES = 3
VALIDATION_MODEL = os.getenv("GLOSSARY_VALIDATION_MODEL", "gpt-4.1-mini")

GLOSSARY_RULES = """\
A glossary entry is a NAMED ENTITY with one fixed rendering in {target_lang}: a school, \
kindergarten, platform or app, street or neighbourhood, organisation, brand, programme or \
course title, or a holiday in its well-known {target_lang} form.
NEVER an everyday word, even a culturally flavoured one: activities (basketball, judo, \
art), school terms (class, after-school care, club, homework), food, greetings, holidays \
wishes, job titles, family words. Those are translated normally by the translator and \
must not be pinned.
Also reject an entry whose rendering is wrong for the target language: a raw \
transliteration where an established word exists, or a rendering that reads as an \
unrelated or offensive word in {target_lang} (e.g. Russian "сука" for סוכה — it must be \
"сукка")."""


def _rendering(info) -> str:
    return (info.get("translation", "") if isinstance(info, dict) else str(info or "")).strip()


# ── Evaluator feedback ────────────────────────────────────

def find_hits(evaluations: list[dict], glossary: dict) -> dict[str, list[dict]]:
    """Glossary entries implicated by bad translations: {key: [example, ...]}.

    An entry is implicated when the source contains the Hebrew key and the translation
    contains the entry's rendering, in an evaluation carrying a glossary-type issue.
    """
    hits: dict[str, list[dict]] = {}
    for ev in evaluations:
        issues = ev.get("issues") or []
        types = {i.get("type") for i in issues if isinstance(i, dict)}
        if not types & GLOSSARY_ISSUE_TYPES:
            continue
        original = ev.get("original_text") or ""
        translated = (ev.get("translated_text") or "").lower()
        for key, info in glossary.items():
            rendering = _rendering(info)
            if not rendering or key not in original or rendering.lower() not in translated:
                continue
            hits.setdefault(key, []).append({
                "score": ev.get("quality_score"),
                "original": original[:160],
                "translated": (ev.get("translated_text") or "")[:160],
                "issues": sorted(types & GLOSSARY_ISSUE_TYPES),
            })
    return hits


def apply_flags(profile: dict, hits: dict[str, list[dict]], today: date | None = None,
                threshold: int = FLAG_THRESHOLD) -> tuple[dict, list[str]]:
    """Record tonight's hits on the profile; remove entries that reached the threshold.

    Returns the updated profile and the keys removed. Flags accumulate across nights and
    are cleared when the entry goes.
    """
    today = today or date.today()
    profile = json.loads(json.dumps(profile, ensure_ascii=False))  # never mutate the caller's
    glossary = profile.get("glossary") or {}
    flags = profile.get("glossary_flags") or {}
    removed_log = profile.get("glossary_removed") or {}
    removed: list[str] = []

    for key, examples in hits.items():
        if key not in glossary:
            continue
        entry = flags.get(key) or {"count": 0, "examples": []}
        entry["count"] += len(examples)
        entry["last"] = today.isoformat()
        entry["examples"] = (entry["examples"] + examples)[-MAX_EXAMPLES:]
        flags[key] = entry
        if entry["count"] >= threshold:
            removed_log[key] = {
                "rendering": _rendering(glossary[key]),
                "removed_at": today.isoformat(),
                "reason": "evaluator",
                "flags": entry["count"],
            }
            del glossary[key]
            del flags[key]
            removed.append(key)

    profile["glossary"] = glossary
    profile["glossary_flags"] = flags
    profile["glossary_removed"] = removed_log
    return profile, removed


def drop_removed(delta_glossary: dict, profile: dict | None) -> dict:
    """The builder may not re-add what the evaluator or the validator threw out."""
    banned = set((profile or {}).get("glossary_removed") or {})
    return {k: v for k, v in delta_glossary.items() if k not in banned}


# ── LLM validation of entries ─────────────────────────────

def validation_prompt(target_lang: str) -> str:
    return (
        "You review entries proposed for a translation glossary of a Hebrew WhatsApp group.\n"
        + GLOSSARY_RULES.format(target_lang=target_lang)
        + "\n\nFor each entry decide: keep (named entity) or drop (everyday word, or a "
        "rendering that is a raw transliteration where a normal translation exists).\n"
        'Return ONLY a JSON object: {"drop": {"<hebrew key>": "<short reason>", ...}}. '
        "Entries not listed are kept."
    )


def parse_validation(content: str) -> dict[str, str]:
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    data = json.loads(content) if content else {}
    drop = data.get("drop") if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in (drop or {}).items()}


def validate_entries(glossary: dict, target_lang: str, client, model: str = VALIDATION_MODEL,
                     log=None) -> tuple[dict, dict[str, str]]:
    """Split a glossary into (kept, dropped-with-reason) using an LLM classifier.

    Any failure keeps everything: a validator outage must not silently empty a profile.
    """
    if not glossary:
        return {}, {}
    payload = {k: _rendering(v) for k, v in glossary.items()}
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": validation_prompt(target_lang)},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            max_tokens=1500,
            temperature=0,
        )
        dropped = parse_validation(response.choices[0].message.content)
    except Exception as exc:
        if log:
            log.warning("Glossary validation failed (%s) — keeping all %d entries", exc, len(glossary))
        return dict(glossary), {}
    dropped = {k: r for k, r in dropped.items() if k in glossary}
    kept = {k: v for k, v in glossary.items() if k not in dropped}
    return kept, dropped


def record_dropped(profile: dict, dropped: dict[str, str], today: date | None = None) -> dict:
    """Remember validator rejections so the builder stops proposing them."""
    if not dropped:
        return profile
    today = today or date.today()
    profile = json.loads(json.dumps(profile, ensure_ascii=False))
    removed_log = profile.get("glossary_removed") or {}
    glossary = profile.get("glossary") or {}
    for key, reason in dropped.items():
        removed_log[key] = {
            "rendering": _rendering(glossary.get(key)),
            "removed_at": today.isoformat(),
            "reason": f"validator: {reason}"[:200],
        }
        glossary.pop(key, None)
        (profile.get("glossary_flags") or {}).pop(key, None)
    profile["glossary"] = glossary
    profile["glossary_removed"] = removed_log
    return profile
