"""Prefect flow: per-chat context builder (glossaries, members, tone).

Analyzes recent messages per chat_pair, extracts a glossary of named entities, member
names and chat tone using an LLM. Profiles are merged incrementally and injected into the
translation prompt.

The glossary is gated both ways (see flows/glossary.py): new entries pass an LLM
validator before they are merged, and every morning the evaluator's bad ratings from the
translation-quality run flag the entries they implicate — three flags and the entry is
removed for good. Without this the glossary only ever grew, and it grew everyday words
pinned to transliterations.

Deploy:
  cron "0 5 * * *" (after translation-quality — the feedback step reads its run)

One-off: python -m flows.chat_context_builder --prune-glossaries
  Runs the validator over every existing glossary and removes what it rejects.
"""
from __future__ import annotations

import json
import os
import sys

from openai import OpenAI
from prefect import flow, get_run_logger, task

from . import glossary as glossary_rules
from . import llm
from .shared import db_conn, invalidate_profile_cache

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

ANALYSIS_MODEL = os.getenv("CONTEXT_MODEL", "gpt-6.1-sol")

# Minimum messages per chat to trigger analysis. A chat with an existing profile needs a
# real day of talk before another LLM pass is worth it — three messages never add a name.
MIN_MESSAGES = 5
MIN_MESSAGES_EXISTING = int(os.getenv("CONTEXT_MIN_MESSAGES_EXISTING", 15))

PROFILE_FIELDS = ("chat_type", "chat_description", "tone", "glossary", "members", "mentioned_people", "recurring_topics")


@task(retries=2, name="collect-per-chat-data")
def collect_per_chat_data() -> list[dict]:
    """Collect messages grouped by chat_pair_id.

    For chats WITH existing profile: last 24h messages.
    For chats WITHOUT profile: last 90 days (bootstrap).

    Originals only. The builder used to see its own past translations too, and learned
    the transliterations it had itself caused.
    """
    logger = get_run_logger()
    with db_conn() as conn:
        cur = conn.cursor()

        # Get all active chat pairs with their existing profiles
        cur.execute("""
            SELECT cp.id AS chat_pair_id,
                   cp.wa_chat_id,
                   coalesce(cp.target_language, u.target_language) as target_language,
                   prof.profile_data,
                   prof.version
            FROM chat_pairs cp
            JOIN users u ON u.id = cp.user_id
            LEFT JOIN chat_profiles prof ON prof.chat_pair_id = cp.id
            WHERE cp.status = 'active'
        """)
        pairs = [dict(r) for r in cur.fetchall()]

        # Names the service glossary already settled (migration 025) — not the builder's
        # to re-guess. Rejected ones neither: they were judged not names, or misread.
        cur.execute("""
            SELECT source, target_language FROM glossary
            WHERE status IN ('verified', 'locked', 'rejected')
        """)
        pinned: dict[str, list[str]] = {}
        for r in cur.fetchall():
            pinned.setdefault(r["target_language"], []).append(r["source"])

        result = []
        for pair in pairs:
            has_profile = pair["profile_data"] is not None
            interval = "1 day" if has_profile else "90 days"

            cur.execute("""
                SELECT original_text, sender_name
                FROM message_events
                WHERE chat_pair_id = %s
                  AND created_at >= current_date - interval %s
                  AND created_at < current_date
                  AND original_text IS NOT NULL
                  AND original_text != ''
                  AND delivery_status = 'delivered'
                ORDER BY created_at
                LIMIT 500
            """, (pair["chat_pair_id"], interval))

            messages = [dict(r) for r in cur.fetchall()]

            if len(messages) < (MIN_MESSAGES_EXISTING if has_profile else MIN_MESSAGES):
                continue

            target_language = pair.get("target_language") or "Russian"
            result.append({
                "chat_pair_id": pair["chat_pair_id"],
                "wa_chat_id": pair["wa_chat_id"],
                "target_language": target_language,
                "global_keys": pinned.get(target_language, []),
                "existing_profile": pair["profile_data"],
                "existing_version": pair.get("version") or 0,
                "messages": messages,
            })

    logger.info("Collected data for %d chats (of %d active pairs)", len(result), len(pairs))
    return result


def build_extraction_prompt(target_lang: str, existing: dict | None,
                            global_keys: list[str] | None = None) -> str:
    removed = sorted((existing or {}).get("glossary_removed") or {})
    banned_block = ""
    if removed:
        banned_block = (
            "\n- NEVER propose these glossary keys again; they were removed as everyday words "
            "or bad renderings: " + ", ".join(removed[:60])
        )
    if global_keys:
        banned_block += (
            "\n- These names already have a fixed rendering for every chat; do NOT add them "
            "or phrases containing them to the glossary: " + ", ".join(sorted(global_keys)[:60])
        )
    return f"""You analyze WhatsApp group chat messages to build a translation context profile.
The messages are translated from Hebrew to {target_lang}.

Your task: extract ONLY NEW or UPDATED items not already in the existing profile.

Return a JSON object with these fields (include only fields with new data):
- "chat_type": string — type of chat (e.g. "parents_group", "work_team", "family", "neighbors")
- "chat_description": string — brief description of what the group is about
- "tone": string — communication style (e.g. "informal, warm, emoji-heavy")
- "glossary": object — fixed {target_lang} renderings of NAMED ENTITIES only.
  {glossary_rules.GLOSSARY_RULES.format(target_lang=target_lang)}
  Format: {{"hebrew name": {{"translation": "{target_lang} rendering", "note": "what it is"}}}}
  Example for Russian: {{"אופק": {{"translation": "Офек", "note": "school platform"}}}} — NOT "Ofek".
  When in doubt, leave it out: a missing entry costs nothing, a wrong one pins a mistake.
- "members": object — Hebrew names transliterated into {target_lang} script (NOT Latin).
  Format: {{"hebrew_name": "transliterated_name"}}
  Example for Russian: {{"גיל": "Гиль"}} — NOT "Gil".
- "mentioned_people": object — people mentioned in messages who are NOT group members (children, spouses, teachers, doctors, etc.).
  Format: {{"name": {{"transliteration": "{target_lang} transliteration", "relation": "who they are, e.g. child of [member], teacher at [school]"}}}}
  This helps the translator correctly transliterate names and understand context.
- "recurring_topics": list of strings — key recurring themes and topics discussed in the group (e.g. "school events", "holiday planning", "homework", "medical appointments"). Max 10 items.

Rules:
- Return ONLY a delta (new/changed items), not the full profile
- If a glossary entry or member already exists in the current profile with the same value, do NOT include it
- If you see a better rendering than what's in the current profile, include the updated version{banned_block}
- If nothing new to add, return an empty object {{}}
- Return ONLY the JSON object, no markdown fences
- Use web search to verify names of places, schools, organizations, and public figures mentioned in messages. This helps ensure correct renderings in the glossary.
- ALL renderings MUST use {target_lang} script. For Russian → Cyrillic. Never output Latin transliterations for a Russian target."""


@task(retries=1, name="extract-context-with-llm")
def extract_context_with_llm(chat_data: dict) -> dict | None:
    """Extract chat context (glossary, members, tone) using LLM.

    Returns delta to merge with existing profile, or None if no useful data.
    """
    logger = get_run_logger()

    messages = chat_data["messages"]
    existing = chat_data["existing_profile"] or {}
    target_lang = chat_data["target_language"]

    # Format messages for LLM
    msg_lines = []
    for m in messages[:200]:  # cap at 200 for token efficiency
        sender = m.get("sender_name", "?")
        text = (m.get("original_text") or "")[:300]
        msg_lines.append(f"[{sender}]: {text}")

    messages_text = "\n".join(msg_lines)

    # The removal log is for the prompt's banned list, not for the model to re-read.
    existing_for_prompt = {k: v for k, v in existing.items() if k not in ("glossary_removed", "glossary_flags")}
    existing_json = json.dumps(existing_for_prompt, ensure_ascii=False, indent=2) if existing_for_prompt else "null"

    system_prompt = build_extraction_prompt(target_lang, existing, chat_data.get("global_keys"))

    user_prompt = f"""Existing profile:
{existing_json}

Recent messages:
{messages_text}"""

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Web search verifies school, place and organisation names. That matters when a
    # profile is first built from 90 days of history; a daily delta of one or two names
    # does not justify a paid search per chat per day.
    request: dict = {
        "model": ANALYSIS_MODEL,
        "instructions": system_prompt,
        "input": user_prompt,
        "max_output_tokens": 2000,
        "reasoning": {"effort": "low"},
    }
    if not existing:
        request["tools"] = [{
            "type": "web_search",
            "search_context_size": "low",
            "user_location": {
                "type": "approximate",
                "country": "IL",
                "timezone": "Asia/Jerusalem",
            },
        }]

    try:
        response = llm.respond(client, request, log=logger)

        content = (response.output_text or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        delta = json.loads(content)
        tokens_used = llm.total_tokens(response.usage)
        cost_usd = llm.usage_cost(ANALYSIS_MODEL, response.usage)

        logger.info(
            "Chat %d: extracted delta with %d glossary, %d members, %d mentioned, %d topics (tokens: %d)",
            chat_data["chat_pair_id"],
            len(delta.get("glossary", {})),
            len(delta.get("members", {})),
            len(delta.get("mentioned_people", {})),
            len(delta.get("recurring_topics", [])),
            tokens_used,
        )

        return {
            "chat_pair_id": chat_data["chat_pair_id"],
            "target_language": target_lang,
            "delta": delta,
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
            "messages_analyzed": len(messages),
        }

    except Exception as exc:
        logger.error("LLM extraction failed for chat %d: %s", chat_data["chat_pair_id"], exc)
        return None


@task(retries=1, name="validate-delta-glossary")
def validate_delta_glossary(result: dict, existing_profile: dict | None,
                            global_keys: list[str] | None = None) -> dict:
    """Keep only named entities out of the new glossary entries; drop what was removed
    before and what is pinned service-wide."""
    logger = get_run_logger()
    proposed = result["delta"].get("glossary") or {}
    # Pinned names are dropped silently — not banned for the chat, see drop_global.
    proposed = glossary_rules.drop_global(proposed, global_keys)
    if not proposed:
        return {**result, "delta": {**result["delta"], "glossary": {}}}

    allowed = glossary_rules.drop_removed(proposed, existing_profile)
    banned = {k: "removed earlier" for k in proposed if k not in allowed}
    kept, dropped = glossary_rules.validate_entries(
        allowed, result["target_language"], OpenAI(api_key=OPENAI_API_KEY), log=logger,
    )
    dropped.update(banned)
    if dropped:
        logger.info("Chat %d: validator dropped %d of %d proposed glossary entries: %s",
                    result["chat_pair_id"], len(dropped), len(proposed), ", ".join(dropped))
    return {**result, "delta": {**result["delta"], "glossary": kept}, "dropped": dropped}


def merge_profiles(existing: dict | None, delta: dict) -> dict:
    """Merge delta into existing profile. Delta takes priority for conflicts."""
    if not existing:
        existing = {}

    merged = dict(existing)

    # Simple fields: overwrite if present in delta
    for key in ("chat_type", "chat_description", "tone"):
        if key in delta and delta[key]:
            merged[key] = delta[key]

    # Glossary: merge dicts, delta priority — but never what was removed before
    if "glossary" in delta and delta["glossary"]:
        old_glossary = dict(merged.get("glossary", {}))
        old_glossary.update(glossary_rules.drop_removed(delta["glossary"], existing))
        merged["glossary"] = old_glossary

    # Members: merge dicts, delta priority
    if "members" in delta and delta["members"]:
        old_members = dict(merged.get("members", {}))
        old_members.update(delta["members"])
        merged["members"] = old_members

    # Mentioned people: merge dicts, delta priority
    if "mentioned_people" in delta and delta["mentioned_people"]:
        old_people = dict(merged.get("mentioned_people", {}))
        old_people.update(delta["mentioned_people"])
        merged["mentioned_people"] = old_people

    # Recurring topics: merge lists, deduplicate
    if "recurring_topics" in delta and delta["recurring_topics"]:
        old_topics = list(merged.get("recurring_topics", []))
        seen = {t.lower() for t in old_topics}
        for topic in delta["recurring_topics"]:
            if topic.lower() not in seen:
                old_topics.append(topic)
                seen.add(topic.lower())
        merged["recurring_topics"] = old_topics[:10]

    return merged


def _write_profiles(cur, writes: list[tuple]) -> None:
    """UPSERT profiles and append their history rows, two statements for the whole batch.

    Each write is (chat_pair_id, profile, version, tokens, cost, change_summary). Callers
    read a pair's current profile before building its write and hand over at most one write
    per pair, so deferring the writes to the end of the loop cannot change what they read.
    """
    if not writes:
        return
    cur.executemany("""
        INSERT INTO chat_profiles (chat_pair_id, profile_data, version, tokens_used, estimated_cost, updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (chat_pair_id) DO UPDATE
            SET profile_data = EXCLUDED.profile_data,
                version = EXCLUDED.version,
                tokens_used = chat_profiles.tokens_used + EXCLUDED.tokens_used,
                estimated_cost = chat_profiles.estimated_cost + EXCLUDED.estimated_cost,
                updated_at = now()
    """, [(pid, json.dumps(profile, ensure_ascii=False), version, tokens, cost)
          for pid, profile, version, tokens, cost, _ in writes])
    cur.executemany("""
        INSERT INTO chat_profile_history (chat_pair_id, version, profile_data, change_summary)
        VALUES (%s, %s, %s, %s)
    """, [(pid, version, json.dumps(profile, ensure_ascii=False), change_summary)
          for pid, profile, version, _, _, change_summary in writes])


@task(retries=2, name="store-profiles")
def store_profiles(results: list[dict]) -> int:
    """UPSERT profiles into chat_profiles, record history, drop the Redis copy."""
    logger = get_run_logger()

    if not results:
        logger.info("No profiles to store")
        return 0

    with db_conn() as conn:
        cur = conn.cursor()

        writes: list[tuple] = []
        for r in results:
            chat_pair_id = r["chat_pair_id"]
            delta = r["delta"]
            dropped = r.get("dropped") or {}
            tokens = r.get("tokens_used", 0)
            messages_analyzed = r.get("messages_analyzed", 0)

            # Skip empty deltas (a validator rejection alone is still worth recording)
            if not any(delta.get(k) for k in PROFILE_FIELDS) and not dropped:
                continue

            # Load current profile
            cur.execute(
                "SELECT profile_data, version FROM chat_profiles WHERE chat_pair_id = %s",
                (chat_pair_id,),
            )
            row = cur.fetchone()
            existing = dict(row["profile_data"]) if row else None
            current_version = row["version"] if row else 0

            # Merge
            merged = merge_profiles(existing, delta)
            merged = glossary_rules.record_dropped(merged, dropped)
            merged["messages_analyzed"] = (existing or {}).get("messages_analyzed", 0) + messages_analyzed
            new_version = current_version + 1

            cost = r.get("cost_usd", 0.0)

            change_parts = []
            if delta.get("glossary"):
                change_parts.append(f"+{len(delta['glossary'])} glossary")
            if dropped:
                change_parts.append(f"validator dropped {len(dropped)}")
            if delta.get("members"):
                change_parts.append(f"+{len(delta['members'])} members")
            for k in ("chat_type", "chat_description", "tone"):
                if delta.get(k):
                    change_parts.append(f"updated {k}")
            change_summary = ", ".join(change_parts) if change_parts else "no changes"

            writes.append((chat_pair_id, merged, new_version, tokens, cost, change_summary))

        _write_profiles(cur, writes)

    stored = len(writes)
    touched = [w[0] for w in writes]
    # The processor caches profiles for an hour; a stale copy would keep serving the old glossary.
    invalidate_profile_cache(touched)

    logger.info("Stored %d chat profiles", stored)
    return stored


@task(retries=1, name="apply-quality-feedback")
def apply_quality_feedback() -> list[dict]:
    """Flag glossary entries implicated in last night's bad translations; remove at threshold.

    Reads this morning's translation-quality run (it finishes half an hour before us).
    """
    logger = get_run_logger()
    with db_conn() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT me.chat_pair_id, te.original_text, te.translated_text, te.quality_score, te.issues_found
            FROM translation_evaluations te
            JOIN nightly_analysis_runs nar ON nar.id = te.run_id
            JOIN message_events me ON me.id = te.message_event_id
            WHERE nar.flow_type = 'translation_quality'
              AND nar.run_date = current_date
              AND NOT te.shadow
              AND me.chat_pair_id IS NOT NULL
        """)
        by_pair: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            issues = r["issues_found"]
            if isinstance(issues, str):
                issues = json.loads(issues or "[]")
            by_pair.setdefault(r["chat_pair_id"], []).append({**dict(r), "issues": issues})

        outcomes: list[dict] = []
        writes: list[tuple] = []
        for pair_id, evaluations in by_pair.items():
            cur.execute("SELECT profile_data, version FROM chat_profiles WHERE chat_pair_id = %s", (pair_id,))
            row = cur.fetchone()
            if not row or not (row["profile_data"] or {}).get("glossary"):
                continue
            profile = dict(row["profile_data"])
            hits = glossary_rules.find_hits(evaluations, profile["glossary"])
            if not hits:
                continue
            updated, removed = glossary_rules.apply_flags(profile, hits)
            summary = f"evaluator flagged {len(hits)}"
            if removed:
                summary += f", removed {len(removed)}: " + ", ".join(removed)
            writes.append((pair_id, updated, row["version"] + 1, 0, 0.0, summary))
            outcomes.append({"chat_pair_id": pair_id, "flagged": sorted(hits), "removed": removed})
            logger.info("Chat %d: %s", pair_id, summary)

        _write_profiles(cur, writes)

    invalidate_profile_cache([w[0] for w in writes])
    return outcomes


@flow(name="chat-context-builder", log_prints=True)
def chat_context_builder():
    """Build per-chat translation context: feedback → collect → extract → validate → store."""
    logger = get_run_logger()

    feedback = apply_quality_feedback()

    chat_data_list = collect_per_chat_data()

    results = []
    for chat_data in chat_data_list:
        result = extract_context_with_llm(chat_data)
        if result:
            results.append(validate_delta_glossary(result, chat_data["existing_profile"],
                                                   chat_data.get("global_keys")))

    stored = store_profiles(results) if results else 0
    if not chat_data_list:
        logger.info("No chats with enough messages to extract from")

    # No Telegram message of its own: the morning digest counts glossary additions and
    # removals from chat_profile_history.
    return {
        "chats_analyzed": len(chat_data_list),
        "profiles_extracted": len(results),
        "profiles_stored": stored,
        "glossary_feedback": len(feedback),
        "total_tokens": sum(r.get("tokens_used", 0) for r in results),
    }


def prune_glossaries() -> None:
    """One-off: run the validator over every existing glossary and remove what it rejects."""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("prune_glossaries")

    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT prof.chat_pair_id, prof.profile_data, prof.version,
                   coalesce(cp.target_language, u.target_language, 'Russian') AS target_language
            FROM chat_profiles prof
            JOIN chat_pairs cp ON cp.id = prof.chat_pair_id
            JOIN users u ON u.id = cp.user_id
            ORDER BY prof.chat_pair_id
        """)
        rows = [dict(r) for r in cur.fetchall()]
        client = OpenAI(api_key=OPENAI_API_KEY)

        writes: list[tuple] = []
        for row in rows:
            profile = dict(row["profile_data"] or {})
            glossary = profile.get("glossary") or {}
            if not glossary:
                continue
            _, dropped = glossary_rules.validate_entries(glossary, row["target_language"], client, log=log)
            if not dropped:
                log.info("pair %d: %d entries, nothing to drop", row["chat_pair_id"], len(glossary))
                continue
            updated = glossary_rules.record_dropped(profile, dropped)
            summary = f"prune: validator dropped {len(dropped)}: " + ", ".join(dropped)
            writes.append((row["chat_pair_id"], updated, row["version"] + 1, 0, 0.0, summary))
            log.info("pair %d: dropped %d of %d — %s", row["chat_pair_id"], len(dropped), len(glossary),
                     "; ".join(f"{k} ({v})" for k, v in dropped.items()))

        _write_profiles(cur, writes)

    touched = [w[0] for w in writes]
    invalidate_profile_cache(touched)
    log.info("Done: %d profiles changed", len(touched))


if __name__ == "__main__":
    if "--prune-glossaries" in sys.argv:
        prune_glossaries()
    else:
        chat_context_builder()
