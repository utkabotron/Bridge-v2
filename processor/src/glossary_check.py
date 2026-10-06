"""Translate fixed phrases with a chosen glossary scope — run before switching statuses on.

    docker compose exec processor python -m src.glossary_check [--statuses verified,locked]

The same prompt, chat profile and models production uses (both A/B variants in a chat, the
DM model with no chat), but straight to the model: nothing is written to the translation
cache, so a bad answer here cannot be served later. Each case says what the translation
must and must not contain. Names spelled like everyday words are checked both ways, in a
chat they are scoped to: "עמוס אמר…" must keep Амос, "אני עמוס היום" must not.

A case that fails is retried on the BASELINE statuses (what production runs now); if it fails
there too it is reported as SAME — the chat's own glossary already did that, so it is not a
regression of the change being checked (אופק in a school chat where Офек is the platform).
Exit code 1 when any case fails that does not fail on the baseline.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from bridge_shared.chat_context import format_chat_context, with_glossary

from .config import DIRECT_MODEL, OPENAI_MODEL
from .llm import chat as llm_chat
from .pipeline import glossary
from .pipeline.prompts import VARIANTS, get_translate_prompt

LANG = "Russian"
BASELINE = ("locked",)

# (text, must contain any of, must contain none of) — DM, no chat: service-wide names only.
DM_CASES = [
    ("אני עמוס היום, אחזור אליך מחר", [], ["Амос"]),
    ("יש לנו אופק חדש", [], ["Офек"]),
    ("זה היה ממש קסם", [], ["Кесем"]),
    ("יש קשת בשמיים", [], ["Кешет"]),
    ("אנחנו גרים בישראל כבר עשר שנים", ["Израил"], ["Исраэль"]),
    ("קיבלתי משוב מהמורה על העבודה", [], ["Машов"]),
    ("מחר בגבעולים יום ספורט", ["Гиволим"], []),
    ("נסענו לרמת גן לבקר את סבתא", ["Рамат-Ган", "Рамат Ган"], []),
    ("היא גרה בפתח תקווה", ["Петах-Тикв", "Петах Тикв"], []),
]

# word: (sentence where it is the name, sentence where it is an ordinary word,
#        must not appear in the ordinary one)
AMBIGUOUS = {
    "עמוס": ("עמוס אמר שהוא יגיע בשש", "אני עמוס היום, אחזור אליך מחר", "Амос"),
    "אופק": ("תבדקו באופק את שיעורי הבית", "יש לנו אופק חדש", "Офек"),
    "ישראל": ("ישראל ודנה מגיעים למסיבה", "אנחנו גרים בישראל כבר עשר שנים", "Исраэль"),
    "קשת": ("קשת הביאה עוגה לכיתה", "יש קשת בשמיים", "Кешет"),
    "קסם": ("נרשמתם כבר לקסם?", "זה היה ממש קסם", "Кесем"),
}


def _has(text: str, needle: str) -> bool:
    norm = lambda s: s.casefold().replace("ё", "е")  # noqa: E731
    return norm(needle) in norm(text)


async def _translate(text: str, chat_pair_id: int | None, variant: str) -> tuple[str, str]:
    profile: dict = {}
    if chat_pair_id:
        from .db import fetch_chat_profile
        profile = await fetch_chat_profile(chat_pair_id) or {}
    context = format_chat_context(with_glossary(profile, await glossary.lookup(LANG, text, chat_pair_id)))
    model = DIRECT_MODEL if chat_pair_id is None else (VARIANTS[variant]["model"] or OPENAI_MODEL)
    messages = [{"role": "system", "content": get_translate_prompt(LANG, context, variant)},
                {"role": "user", "content": text}]
    return (await llm_chat(messages, model=model, purpose="glossary_check")).text, context


async def _scoped_pairs() -> dict[str, int]:
    """A chat each ambiguous test word is scoped to, if it is in the loaded glossary."""
    from .db import get_pool
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT source, chat_pairs FROM glossary
        WHERE source = ANY($1::text[]) AND status = ANY($2::text[])
          AND also_word IS NOT FALSE AND status <> 'locked' AND cardinality(chat_pairs) > 0
    """, list(AMBIGUOUS), list(glossary.USED_STATUSES))
    return {r["source"]: r["chat_pairs"][0] for r in rows}


def _use(statuses: tuple[str, ...]) -> None:
    glossary.USED_STATUSES = statuses
    glossary.reload()


async def run(statuses: tuple[str, ...]) -> int:
    _use(statuses)
    failures = 0

    def passes(out: str, text: str, need: list, banned: list) -> bool:
        return ((not need or any(_has(out, n) for n in need))
                and not any(_has(out, b) for b in banned)
                and not ("[" in out and "[" not in text))  # a [note] copied into the translation

    async def check(label: str, text: str, pair: int | None, variant: str, need: list, banned: list):
        nonlocal failures
        out, _ = await _translate(text, pair, variant)
        verdict = "OK  "
        if not passes(out, text, need, banned):
            _use(BASELINE)
            before, _ = await _translate(text, pair, variant)
            _use(statuses)
            if passes(before, text, need, banned):
                verdict = "FAIL"
                failures += 1
            else:
                verdict = "SAME"
                out += f"\n       (baseline {','.join(BASELINE)} too: {before})"
        print(f"{verdict} {label:<14} {text}\n       → {out}")

    print(f"statuses: {','.join(statuses)}\n── DM (service-wide names only)")
    for text, need, banned in DM_CASES:
        await check("dm", text, None, "A", need, banned)

    pairs = await _scoped_pairs()
    print("── chats the ambiguous names are scoped to")
    for word, (as_name, as_word, rendering) in AMBIGUOUS.items():
        pair = pairs.get(word)
        if pair is None:
            print(f"skip {word}: not scoped to any chat under these statuses")
            continue
        for variant in VARIANTS:
            await check(f"pair {pair} {variant} name", as_name, pair, variant, [rendering], [])
            await check(f"pair {pair} {variant} word", as_word, pair, variant, [], [rendering])

    print(f"\n{failures} failed")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(prog="src.glossary_check")
    parser.add_argument("--statuses", default="verified,locked")
    args = parser.parse_args()
    statuses = tuple(s.strip() for s in args.statuses.split(",") if s.strip())
    sys.exit(1 if asyncio.run(run(statuses)) else 0)


if __name__ == "__main__":
    main()
