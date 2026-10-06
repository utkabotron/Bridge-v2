"""Glossary resolver: one checked reading per name for the whole service (docs/glossary-plan.md).

Hebrew is written without vowels, and every chat profile guessed the vowels of a name on its
own (גבעולים: Гивъолим / Гевалим / Геваулим). Here a name is read once, from a source:

  places, organisations, apps …  web search for the official Latin spelling (the school's
                                 site, maps, Wikipedia), and the rendering follows it;
  people                         the established spelling of the first name or surname
                                 among Russian-speaking Israelis, in batches, no search.

The resolver never sees what the chats guessed — its answer is an independent reading, and
`decide` compares the two: when the chats were unanimous and the resolver agrees with
confidence, the entry is `verified` straight away; anything contested, unsourced or unsure
is `proposed` for the admin's approval in the digest. A confident "not a name" is
`rejected` (סבב "round" was pinned as «савев»).

  python -m flows.glossary_resolver --import                     profiles → candidates
  python -m flows.glossary_resolver --resolve --limit 20 --contested --dry-run
  python -m flows.glossary_resolver --resolve [--limit N] [--kind person|other]
  python -m flows.glossary_resolver --classify [--dry-run]       also_word + short hint
  python -m flows.glossary_resolver --arbitrate [--limit N] [--dry-run]

Contested names are settled by --arbitrate, not by hand: one web check per proposed entry;
a confident single reading is verified, "several real readings" (אורי: Ори / Ури), "not a
name" and "unsure" are rejected for the service — left to each chat's own profile.

A name spelled like an everyday word (עמוס Amos / "busy") is `also_word`: the processor
applies it only in the chats it was seen in (chat_pairs). In every prompt it turned
"I'm busy today" into "I'm Amos today".

People are stored word by word and without who they are: a relation ("child of …") would
cross from one user's chats into another's prompts. Relations stay in the chat profile.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import defaultdict

from bridge_shared.glossary_match import MAX_WORDS, key_of, words

from . import llm

logger = logging.getLogger("glossary_resolver")

RESOLVER_MODEL = os.getenv("RESOLVER_MODEL", "gpt-6.1-sol")
AUTO_ACCEPT_CONFIDENCE = float(os.getenv("GLOSSARY_AUTO_ACCEPT_CONFIDENCE", 0.8))
PERSON_BATCH = 40
KINDS = ("person", "place", "org", "other")


# ── Collecting candidates from chat profiles ──────────────

def _is_name_key(key: str) -> bool:
    ws = words(key)
    return (1 <= len(ws) <= MAX_WORDS
            and all(any(c.isalpha() for c in w) for w in ws)
            and len("".join(ws)) >= 2)


def _clean_rendering(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" .,;:!?«»\"'")


def norm_rendering(text) -> str:
    """For comparing renderings: case, ё/е, hyphens and spacing do not count."""
    t = _clean_rendering(text).casefold().replace("ё", "е")
    return re.sub(r"[\s\-‐–]+", " ", t)


def collect_names(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """{(source, language): {"kind", "renderings": {rendering: {pair ids}}, "notes": [..]}}
    from chat profiles. rows: [{"chat_pair_id", "target_language", "profile_data"}].

    Glossary entries go in whole (a school can be two words). People go in word by word
    when the Hebrew and the rendering have the same number of words (דנה דרחי → Дана Драхи
    gives דנה → Дана and דרחי → Драхи); otherwise the pair says nothing reliable and is skipped.
    """
    names: dict[tuple[str, str], dict] = {}

    def add(source: str, lang: str, kind: str, rendering, pair_id: int, note: str | None = None):
        key, rendering = key_of(source), _clean_rendering(rendering)
        if not key or not rendering or not _is_name_key(key):
            return
        item = names.setdefault((key, lang), {"kind": kind, "renderings": defaultdict(set), "notes": []})
        if kind != "person":
            item["kind"] = kind  # an entry in a glossary outranks the same word as a person
        item["renderings"][rendering].add(pair_id)
        if note and note not in item["notes"] and len(item["notes"]) < 3:
            item["notes"].append(note)

    def add_person(source: str, lang: str, rendering, pair_id: int):
        src_words, ren_words = words(source), _clean_rendering(rendering).split()
        if len(src_words) != len(ren_words):
            return
        for s, r in zip(src_words, ren_words):
            add(s, lang, "person", r, pair_id)

    for row in rows:
        profile = row.get("profile_data") or {}
        if isinstance(profile, str):
            profile = json.loads(profile)
        lang, pair_id = row.get("target_language") or "Russian", row["chat_pair_id"]

        for key, info in (profile.get("glossary") or {}).items():
            if isinstance(info, dict):
                add(key, lang, "other", info.get("translation"), pair_id, (info.get("note") or "")[:120] or None)
            else:
                add(key, lang, "other", info, pair_id)
        for key, rendering in (profile.get("members") or {}).items():
            add_person(key, lang, rendering if isinstance(rendering, str) else "", pair_id)
        for key, info in (profile.get("mentioned_people") or {}).items():
            # The relation stays behind: it is about a family, not about the name.
            add_person(key, lang, (info or {}).get("transliteration") if isinstance(info, dict) else info, pair_id)

    return names


def import_candidates(conn) -> int:
    """Upsert every name from chat profiles as a candidate; decided rows keep their status
    and only get fresh chat_renderings / chats_seen."""
    cur = conn.cursor()
    cur.execute("""
        SELECT prof.chat_pair_id, prof.profile_data,
               coalesce(cp.target_language, u.target_language, 'Russian') AS target_language
        FROM chat_profiles prof
        JOIN chat_pairs cp ON cp.id = prof.chat_pair_id
        JOIN users u ON u.id = cp.user_id
    """)
    names = collect_names([dict(r) for r in cur.fetchall()])
    rows = []
    for (source, lang), item in names.items():
        renderings = {r: len(p) for r, p in item["renderings"].items()}
        pairs = sorted(set().union(*item["renderings"].values()))
        rows.append((source, lang, item["kind"], "; ".join(item["notes"]) or None,
                     json.dumps(renderings, ensure_ascii=False), len(pairs), pairs))
    cur.executemany("""
        INSERT INTO glossary (source, target_language, kind, note, chat_renderings, chats_seen, chat_pairs)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (source, target_language) DO UPDATE
            SET chat_renderings = EXCLUDED.chat_renderings,
                chats_seen = EXCLUDED.chats_seen,
                chat_pairs = EXCLUDED.chat_pairs,
                kind = CASE WHEN glossary.status = 'candidate' THEN EXCLUDED.kind ELSE glossary.kind END,
                note = CASE WHEN glossary.status = 'candidate' THEN EXCLUDED.note ELSE glossary.note END,
                updated_at = now()
            WHERE glossary.chat_renderings IS DISTINCT FROM EXCLUDED.chat_renderings
               OR glossary.chats_seen IS DISTINCT FROM EXCLUDED.chats_seen
               OR glossary.chat_pairs IS DISTINCT FROM EXCLUDED.chat_pairs
    """, rows)
    return len(rows)


# ── Asking the model ──────────────────────────────────────

def _parse_json(content: str):
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(content) if content else {}


def person_prompt(target_lang: str) -> str:
    return f"""You write Israeli personal names in {target_lang}.
Each input is one word from how a person is named in an Israeli WhatsApp group — a first
name or a surname, in Hebrew (no vowels) or in Latin letters.
Give the established {target_lang} spelling used by {target_lang}-speaking Israelis for the
usual Israeli pronunciation of that name (e.g. for Russian: יעל → Яэль, נועה → Ноа,
איתי → Итай, Yael → Яэль). ALL renderings in {target_lang} script.
If the word is not a personal name at all (an everyday word, a title, a role, an emoji
label), set is_name to false.
confidence: 0..1 — how sure you are this is THE usual reading; lower it when the
Hebrew spelling allows several common names.
Return ONLY JSON: {{"items": [{{"source": "<as given>", "is_name": true, "translation": "...",
"confidence": 0.9}}]}}"""


def entity_prompt(target_lang: str) -> str:
    return f"""You find out how a name from an Israeli parents' WhatsApp group is pronounced and
write it in {target_lang}. Hebrew is written without vowels, so do not guess: search the web
for the official Latin spelling — the organisation's own site, Google Maps, Wikipedia,
municipal or Ministry of Education lists — and follow it.
A name is a school, kindergarten, street, neighbourhood, city, park, venue, organisation,
company, brand, app or programme. An everyday word (class, round, after-school, club,
homework, a holiday greeting) is NOT a name: set is_name to false.
translation: the NAME as it sounds, in {target_lang} script, following the official
pronunciation (for Russian, Israeli-Russian conventions: ח/כ → х, צ → ц; the article ה →
ха-; e.g. Givolim → Гиволим, Ofek → Офек). Transliterate, never translate the meaning of
its words: שער הדר → Шаар Хадар, not «Ворота Хадар»; גינת השקדיה → Гинат ха-Шкедия.
Return ONLY JSON: {{"is_name": true, "kind": "place|org|other", "latin": "official Latin
spelling or null", "url": "source URL or null", "translation": "...", "note": "what it is,
a few words in Russian", "confidence": 0.9}}
confidence: 0..1; below 0.7 when you found no source for the reading."""


def resolve_people(client, sources: list[str], target_lang: str) -> tuple[dict[str, dict], float]:
    """{source: answer}, cost. One call per PERSON_BATCH names."""
    out: dict[str, dict] = {}
    cost = 0.0
    for i in range(0, len(sources), PERSON_BATCH):
        batch = sources[i:i + PERSON_BATCH]
        try:
            response = llm.complete(client, llm.build_request(
                RESOLVER_MODEL,
                [{"role": "system", "content": person_prompt(target_lang)},
                 {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
                max_tokens=4000, json_mode=True,
            ), log=logger)
            cost += llm.usage_cost(RESOLVER_MODEL, response.usage)
            items = _parse_json(response.choices[0].message.content).get("items") or []
        except Exception as exc:
            logger.warning("Person batch failed (%s) — %d names stay candidates", exc, len(batch))
            continue
        wanted = {key_of(s): s for s in batch}
        for item in items:
            if isinstance(item, dict) and key_of(item.get("source") or "") in wanted:
                out[wanted[key_of(item["source"])]] = item
    return out, cost


def resolve_entity(client, source: str, note: str | None, target_lang: str) -> tuple[dict | None, float, int]:
    """(answer, cost, web searches) for one place / organisation / other name."""
    request = _web_request(entity_prompt(target_lang), {"name": source, "context_from_chat": note or ""})
    try:
        response = llm.respond(client, request, log=logger)
        searches = sum(1 for o in (response.output or []) if getattr(o, "type", "") == "web_search_call")
        return (_parse_json(response.output_text), llm.usage_cost(RESOLVER_MODEL, response.usage), searches)
    except Exception as exc:
        logger.warning("Resolving %s failed: %s", source, exc)
        return None, 0.0, 0


# ── Names that are also everyday words ────────────────────

CLASSIFY_BATCH = 50


def classify_prompt() -> str:
    return """You check entries of a glossary of names used to translate Hebrew WhatsApp messages.
Many Hebrew names are spelled exactly like an everyday word: עמוס (Amos / "busy"),
אופק (Ofek / "horizon"), קשת (Keshet / "rainbow"), ישראל (Israel the person / the country),
קסם (an app / "magic"), גבעולים (a school / "stalks"), טל (Tal / "dew").
For each entry say whether its spelling (with or without a one-letter prefix) is ALSO a common
Hebrew word, phrase or well-known place with another meaning that people write in ordinary
messages. Latin-letter brand names that are also English words count too (Smart School no,
Apple yes).
Also give a hint of at most 3 Russian words saying what the name is (школа, приложение,
парк в Рамат-Гане); for people just "имя".
Return ONLY JSON: {"items": [{"source": "<as given>", "also_word": true, "hint": "..."}]}"""


def classify(conn, client, *, dry_run: bool = False) -> dict:
    """Mark also_word and a short hint on every resolved entry that has none yet."""
    cur = conn.cursor()
    cur.execute("""
        SELECT id, source, translation, kind, note, status FROM glossary
        WHERE also_word IS NULL AND translation IS NOT NULL
          AND status IN ('verified', 'proposed', 'locked')
        ORDER BY id
    """)
    rows = [dict(r) for r in cur.fetchall()]
    cost, results = 0.0, {}
    for i in range(0, len(rows), CLASSIFY_BATCH):
        batch = rows[i:i + CLASSIFY_BATCH]
        payload = [{"source": r["source"], "rendering": r["translation"], "kind": r["kind"],
                    "about": (r["note"] or "")[:80]} for r in batch]
        try:
            response = llm.complete(client, llm.build_request(
                RESOLVER_MODEL,
                [{"role": "system", "content": classify_prompt()},
                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                max_tokens=6000, json_mode=True,
            ), log=logger)
            cost += llm.usage_cost(RESOLVER_MODEL, response.usage)
            items = _parse_json(response.choices[0].message.content).get("items") or []
        except Exception as exc:
            logger.warning("Classify batch failed (%s) — %d entries stay unclassified", exc, len(batch))
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("also_word"), bool):
                results[key_of(item.get("source") or "")] = item

    updates = []
    for r in rows:
        item = results.get(key_of(r["source"]))
        if item is None:
            continue
        hint = _clean_rendering(item.get("hint"))[:40] or None
        # A hand-written note on a locked entry is the admin's; people carry no note at all.
        keep_note = r["status"] == "locked" or r["kind"] == "person"
        updates.append((item["also_word"], None if keep_note else hint, r["id"]))
    if not dry_run:
        cur.executemany("""
            UPDATE glossary SET also_word = %s, note = coalesce(%s, note), updated_at = now()
            WHERE id = %s
        """, updates)
    return {"classified": len(updates), "also_word": sum(1 for u in updates if u[0]),
            "cost": round(cost, 4), "items": {r["source"]: results.get(key_of(r["source"])) for r in rows}}


# ── Deciding ──────────────────────────────────────────────

def decide(answer: dict | None, chat_renderings: dict, threshold: float = AUTO_ACCEPT_CONFIDENCE) -> str:
    """verified | proposed | rejected | candidate (no answer — try again next run)."""
    if not answer:
        return "candidate"
    try:
        confidence = float(answer.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if answer.get("is_name") is False:
        return "rejected" if confidence >= threshold else "proposed"
    if not _clean_rendering(answer.get("translation")):
        return "proposed"
    chats = {norm_rendering(r) for r in (chat_renderings or {})}
    if confidence >= threshold and len(chats) == 1 and norm_rendering(answer["translation"]) in chats:
        return "verified"
    return "proposed"


def evidence_of(answer: dict | None) -> str | None:
    if not answer:
        return None
    parts = [str(p) for p in (answer.get("latin"), answer.get("url")) if p]
    return " · ".join(parts)[:500] or None


def resolve(conn, client, *, limit: int | None = None, contested: bool = False,
            kind: str | None = None, dry_run: bool = False) -> dict:
    """Resolve candidates; returns {"decisions": [...], "cost": $, "searches": n}."""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT id, source, target_language, kind, note, chat_renderings, chats_seen
        FROM glossary
        WHERE status = 'candidate'
          {"AND (SELECT count(*) FROM jsonb_object_keys(chat_renderings)) > 1" if contested else ""}
          {"AND kind = %(kind)s" if kind == "person" else "AND kind <> 'person'" if kind else ""}
        ORDER BY chats_seen DESC, id
        {"LIMIT %(limit)s" if limit else ""}
    """, {"kind": kind, "limit": limit})
    rows = [dict(r) for r in cur.fetchall()]

    cost, searches = 0.0, 0
    answers: dict[int, dict | None] = {}
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["kind"] == "person":
            by_lang[r["target_language"]].append(r)
        else:
            answer, c, s = resolve_entity(client, r["source"], r["note"], r["target_language"])
            answers[r["id"]], cost, searches = answer, cost + c, searches + s
    for lang, people in by_lang.items():
        found, c = resolve_people(client, [p["source"] for p in people], lang)
        cost += c
        for p in people:
            answers[p["id"]] = found.get(p["source"])

    decisions = []
    for r in rows:
        answer = answers.get(r["id"])
        renderings = r["chat_renderings"] if isinstance(r["chat_renderings"], dict) else json.loads(r["chat_renderings"] or "{}")
        status = decide(answer, renderings)
        decisions.append({"id": r["id"], "source": r["source"], "kind": r["kind"], "chats": renderings,
                          "answer": answer, "status": status})

    if not dry_run:
        updates = []
        for d in decisions:
            a = d["answer"]
            if d["status"] == "candidate":
                continue
            new_kind = a.get("kind") if a.get("kind") in KINDS and d["kind"] != "person" else d["kind"]
            note = (a.get("note") or None) if d["kind"] != "person" else None
            updates.append((
                _clean_rendering(a.get("translation")) or None, new_kind, note, evidence_of(a),
                float(a.get("confidence") or 0), d["status"], RESOLVER_MODEL,
                d["status"] in ("verified", "rejected"), d["status"] in ("verified", "rejected"), d["id"],
            ))
        cur.executemany("""
            UPDATE glossary
            SET translation = %s, kind = %s, note = coalesce(%s, note), evidence = %s,
                confidence = %s, status = %s, resolver_model = %s,
                decided_at = CASE WHEN %s THEN now() ELSE decided_at END,
                decided_by = CASE WHEN %s THEN 'auto' END,
                updated_at = now()
            WHERE id = %s AND status = 'candidate'
        """, updates)

    return {"decisions": decisions, "cost": round(cost, 4), "searches": searches}


# ── Arbiter: settling what the chats disagree on, from the web ──

ARBITER_CONFIDENCE = float(os.getenv("GLOSSARY_ARBITER_CONFIDENCE", 0.75))
NOT_SETTLED_NOTE = "решает чат"


def arbiter_prompt(target_lang: str) -> str:
    return f"""You settle how one name from Israeli WhatsApp groups is written in {target_lang}.
The groups' own guesses disagree, or a first reading was unsure. Hebrew has no vowels, so
check the web, do not guess:
- a place, school, organisation, brand, app: its official Latin spelling (own website,
  maps, Wikipedia, municipal / Ministry of Education pages);
- a person's first name or surname: how the Hebrew name is pronounced (Hebrew name lists,
  Wikipedia, behindthename) and how {target_lang}-speaking Israeli media write it.
Decide:
  "one"       the spelling has ONE usual reading — give it in {target_lang} script
              (for Russian: ח/כ → х, צ → ц, the article ה → ха-; transliterate, never
              translate the meaning: שער הדר → Шаар Хадар).
  "several"   several real readings are common (אורי — Ори and Ури are both names): which
              one is meant depends on the person or place, not on the spelling.
  "not_name"  it is an everyday word or phrase, not a name.
Return ONLY JSON: {{"decision": "one|several|not_name", "translation": "... or null",
"readings": ["..."], "evidence": "official Latin spelling and/or URL", "confidence": 0.9,
"why": "a few words"}}"""


def _web_request(instructions: str, payload: dict) -> dict:
    return {
        "model": RESOLVER_MODEL,
        "instructions": instructions,
        "input": json.dumps(payload, ensure_ascii=False),
        "max_output_tokens": 2000,
        "reasoning": {"effort": "low"},
        "tools": [{
            "type": "web_search",
            "search_context_size": "low",
            "user_location": {"type": "approximate", "country": "IL", "timezone": "Asia/Jerusalem"},
        }],
    }


def arbitrate_entry(client, row: dict) -> tuple[dict | None, float, int]:
    """(answer, cost, web searches) for one proposed entry."""
    payload = {"name": row["source"], "kind": row["kind"], "what_it_is": row.get("note") or "",
               "variants_in_chats": row.get("chat_renderings") or {},
               "first_reading": row.get("translation"), "first_reading_source": row.get("evidence") or ""}
    try:
        response = llm.respond(client, _web_request(arbiter_prompt(row["target_language"]), payload), log=logger)
        searches = sum(1 for o in (response.output or []) if getattr(o, "type", "") == "web_search_call")
        return _parse_json(response.output_text), llm.usage_cost(RESOLVER_MODEL, response.usage), searches
    except Exception as exc:
        logger.warning("Arbitrating %s failed: %s", row["source"], exc)
        return None, 0.0, 0


def arbiter_decision(answer: dict | None, threshold: float = ARBITER_CONFIDENCE) -> tuple[str, str | None, str | None]:
    """(status, translation, evidence). Only a confident single reading enters the service
    glossary; everything else is `rejected` there — which leaves the name to each chat's own
    profile, where it is known who is meant. None answer → stays proposed (retry later)."""
    if not answer:
        return "proposed", None, None
    try:
        confidence = float(answer.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    evidence = _clean_rendering(answer.get("evidence"))[:400] or None
    translation = _clean_rendering(answer.get("translation")) or None
    decision = answer.get("decision")
    if decision == "one" and translation and confidence >= threshold:
        return "verified", translation, evidence
    readings = ", ".join(_clean_rendering(r) for r in (answer.get("readings") or []) if _clean_rendering(r))
    why = {"several": f"несколько прочтений: {readings}" if readings else "несколько прочтений",
           "not_name": "не имя"}.get(decision, f"не уверен ({confidence:.2f})")
    return "rejected", None, f"{NOT_SETTLED_NOTE}: {why}"[:400]


def arbitrate(conn, client, *, limit: int | None = None, dry_run: bool = False) -> dict:
    """Settle `proposed` entries no admin has decided, one web check each. Each decision is
    committed as it comes, so a long run that dies keeps what it did."""
    cur = conn.cursor()
    cur.execute(f"""
        SELECT id, source, target_language, kind, note, translation, evidence, chat_renderings
        FROM glossary WHERE status = 'proposed' AND decided_by IS DISTINCT FROM 'admin'
        ORDER BY chats_seen DESC, id {"LIMIT %(limit)s" if limit else ""}
    """, {"limit": limit})
    rows = [dict(r) for r in cur.fetchall()]
    cost, searches, decisions = 0.0, 0, []
    for i, r in enumerate(rows, 1):
        if isinstance(r.get("chat_renderings"), str):
            r["chat_renderings"] = json.loads(r["chat_renderings"] or "{}")
        answer, c, s = arbitrate_entry(client, r)
        cost, searches = cost + c, searches + s
        status, translation, evidence = arbiter_decision(answer)
        decisions.append({"id": r["id"], "source": r["source"], "chats": r["chat_renderings"],
                          "first": r.get("translation"), "status": status, "translation": translation,
                          "evidence": evidence, "answer": answer})
        if dry_run or status == "proposed":
            continue
        cur.execute("""
            UPDATE glossary
            SET status = %s, translation = coalesce(%s, translation), evidence = coalesce(%s, evidence),
                confidence = %s, decided_by = 'auto', decided_at = now(), updated_at = now()
            WHERE id = %s AND status = 'proposed' AND decided_by IS DISTINCT FROM 'admin'
        """, (status, translation, evidence, float((answer or {}).get("confidence") or 0), r["id"]))
        if i % 10 == 0:
            conn.commit()
    return {"decisions": decisions, "cost": round(cost, 4), "searches": searches}


def _print_decisions(result: dict) -> None:
    for d in result["decisions"]:
        a = d["answer"] or {}
        chats = ", ".join(f"{r}×{n}" for r, n in d["chats"].items())
        print(f"{d['status']:9} {d['source']:<22} resolver: {a.get('translation') or '—':<18} "
              f"conf {a.get('confidence', '—')!s:<5} chats: {chats}"
              + (f"  [{evidence_of(a)}]" if evidence_of(a) else ""))
    counts = defaultdict(int)
    for d in result["decisions"]:
        counts[d["status"]] += 1
    print(f"\n{dict(counts)} · cost ${result['cost']} · web searches {result['searches']}")


def main(argv: list[str] | None = None) -> None:
    from openai import OpenAI

    from .shared import db_conn

    parser = argparse.ArgumentParser(prog="flows.glossary_resolver")
    parser.add_argument("--import", dest="do_import", action="store_true")
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--classify", action="store_true")
    parser.add_argument("--arbitrate", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--contested", action="store_true")
    parser.add_argument("--kind", choices=("person", "other"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.do_import:
        with db_conn() as conn:
            print("candidates upserted:", import_candidates(conn))
    if args.resolve:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        with db_conn() as conn:
            result = resolve(conn, client, limit=args.limit, contested=args.contested,
                             kind=args.kind, dry_run=args.dry_run)
        _print_decisions(result)
    if args.arbitrate:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        with db_conn() as conn:
            result = arbitrate(conn, client, limit=args.limit, dry_run=args.dry_run)
        for d in result["decisions"]:
            chats = ", ".join(f"{r}×{n}" for r, n in d["chats"].items())
            print(f"{d['status']:9} {d['source']:<20} → {d['translation'] or '—':<18} "
                  f"chats: {chats}  [{d['evidence'] or ''}]")
        counts = defaultdict(int)
        for d in result["decisions"]:
            counts[d["status"]] += 1
        print(f"\n{dict(counts)} · cost ${result['cost']} · web searches {result['searches']}")
    if args.classify or (args.resolve and not args.dry_run):
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
        with db_conn() as conn:
            result = classify(conn, client, dry_run=args.dry_run)
        ambiguous = sorted(s for s, i in result["items"].items() if i and i.get("also_word"))
        print(f"classified {result['classified']}, also a word: {result['also_word']} · "
              f"cost ${result['cost']}\n" + ", ".join(ambiguous))


if __name__ == "__main__":
    main()
