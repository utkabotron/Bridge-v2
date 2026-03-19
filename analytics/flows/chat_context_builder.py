"""Prefect flow: per-chat context builder (glossaries, members, tone).

Analyzes recent messages per chat_pair, extracts transliteration glossary,
member names, and chat tone using LLM. Profiles are merged incrementally
and injected into the translation prompt.

Deploy:
  cron "0 5 * * *" (after translation-quality)
"""
from __future__ import annotations

import json
import os
from datetime import date

import psycopg2
import psycopg2.extras
from openai import OpenAI
from prefect import flow, get_run_logger, task

from .shared import esc, notify_telegram

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

ANALYSIS_MODEL = "gpt-4.1"

# Minimum messages per chat to trigger analysis
MIN_MESSAGES = 5


@task(retries=2, name="collect-per-chat-data")
def collect_per_chat_data() -> list[dict]:
    """Collect messages grouped by chat_pair_id.

    For chats WITH existing profile: last 24h messages.
    For chats WITHOUT profile: last 7 days (bootstrap).
    """
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Get all active chat pairs with their existing profiles
    cur.execute("""
        SELECT cp.id AS chat_pair_id,
               cp.wa_chat_id,
               u.target_language,
               prof.profile_data,
               prof.version
        FROM chat_pairs cp
        JOIN users u ON u.id = cp.user_id
        LEFT JOIN chat_profiles prof ON prof.chat_pair_id = cp.id
        WHERE cp.status = 'active'
    """)
    pairs = [dict(r) for r in cur.fetchall()]

    result = []
    for pair in pairs:
        has_profile = pair["profile_data"] is not None
        interval = "1 day" if has_profile else "90 days"

        cur.execute("""
            SELECT original_text, sender_name, translated_text
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

        if len(messages) < MIN_MESSAGES:
            continue

        result.append({
            "chat_pair_id": pair["chat_pair_id"],
            "wa_chat_id": pair["wa_chat_id"],
            "target_language": pair.get("target_language") or "Russian",
            "existing_profile": pair["profile_data"],
            "existing_version": pair.get("version") or 0,
            "messages": messages,
        })

    cur.close()
    conn.close()

    logger.info("Collected data for %d chats (of %d active pairs)", len(result), len(pairs))
    return result


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
        translated = (m.get("translated_text") or "")[:300]
        msg_lines.append(f"[{sender}]: {text}")
        if translated:
            msg_lines.append(f"  → {translated}")

    messages_text = "\n".join(msg_lines)

    existing_json = json.dumps(existing, ensure_ascii=False, indent=2) if existing else "null"

    system_prompt = f"""You analyze WhatsApp group chat messages to build a translation context profile.
The messages are translated from Hebrew to {target_lang}.

Your task: extract ONLY NEW or UPDATED items not already in the existing profile.

Return a JSON object with these fields (include only fields with new data):
- "chat_type": string — type of chat (e.g. "parents_group", "work_team", "family", "neighbors")
- "chat_description": string — brief description of what the group is about
- "tone": string — communication style (e.g. "informal, warm, emoji-heavy")
- "glossary": object — Hebrew words/phrases that should be transliterated into {target_lang} script (NOT Latin/English).
  Format: {{"word": {{"translation": "{target_lang} transliteration", "note": "context"}}}}
  Example for Russian: {{"אופק": {{"translation": "Офек", "note": "school platform"}}}} — NOT "Ofek".
  Focus on: proper nouns, place names, cultural terms, slang that should stay in original form.
  Do NOT include common words that translate normally.
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
- If you see a better transliteration than what's in the current profile, include the updated version
- If nothing new to add, return an empty object {{}}
- Return ONLY the JSON object, no markdown fences
- Use web search to verify names of places, schools, organizations, and public figures mentioned in messages. This helps ensure correct transliterations in the glossary.
- ALL transliterations MUST use {target_lang} script. For Russian → Cyrillic. Never output Latin transliterations for a Russian target."""

    user_prompt = f"""Existing profile:
{existing_json}

Recent messages:
{messages_text}"""

    client = OpenAI(api_key=OPENAI_API_KEY)

    try:
        response = client.responses.create(
            model=ANALYSIS_MODEL,
            tools=[{
                "type": "web_search",
                "search_context_size": "low",
                "user_location": {
                    "type": "approximate",
                    "country": "IL",
                    "timezone": "Asia/Jerusalem",
                },
            }],
            instructions=system_prompt,
            input=user_prompt,
            max_output_tokens=2000,
            temperature=0,
        )

        content = (response.output_text or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        delta = json.loads(content)
        tokens_used = response.usage.total_tokens if response.usage else 0

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
            "delta": delta,
            "tokens_used": tokens_used,
            "messages_analyzed": len(messages),
        }

    except Exception as exc:
        logger.error("LLM extraction failed for chat %d: %s", chat_data["chat_pair_id"], exc)
        return None


def merge_profiles(existing: dict | None, delta: dict) -> dict:
    """Merge delta into existing profile. Delta takes priority for conflicts."""
    if not existing:
        existing = {}

    merged = dict(existing)

    # Simple fields: overwrite if present in delta
    for key in ("chat_type", "chat_description", "tone"):
        if key in delta and delta[key]:
            merged[key] = delta[key]

    # Glossary: merge dicts, delta priority
    if "glossary" in delta and delta["glossary"]:
        old_glossary = merged.get("glossary", {})
        old_glossary.update(delta["glossary"])
        merged["glossary"] = old_glossary

    # Members: merge dicts, delta priority
    if "members" in delta and delta["members"]:
        old_members = merged.get("members", {})
        old_members.update(delta["members"])
        merged["members"] = old_members

    # Mentioned people: merge dicts, delta priority
    if "mentioned_people" in delta and delta["mentioned_people"]:
        old_people = merged.get("mentioned_people", {})
        old_people.update(delta["mentioned_people"])
        merged["mentioned_people"] = old_people

    # Recurring topics: merge lists, deduplicate
    if "recurring_topics" in delta and delta["recurring_topics"]:
        old_topics = merged.get("recurring_topics", [])
        seen = {t.lower() for t in old_topics}
        for topic in delta["recurring_topics"]:
            if topic.lower() not in seen:
                old_topics.append(topic)
                seen.add(topic.lower())
        merged["recurring_topics"] = old_topics[:10]

    return merged


@task(retries=2, name="store-profiles")
def store_profiles(results: list[dict]) -> int:
    """UPSERT profiles into chat_profiles, record history."""
    logger = get_run_logger()

    if not results:
        logger.info("No profiles to store")
        return 0

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    stored = 0
    for r in results:
        chat_pair_id = r["chat_pair_id"]
        delta = r["delta"]
        tokens = r.get("tokens_used", 0)
        messages_analyzed = r.get("messages_analyzed", 0)

        # Skip empty deltas
        if not any(delta.get(k) for k in ("chat_type", "chat_description", "tone", "glossary", "members", "mentioned_people", "recurring_topics")):
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
        merged["messages_analyzed"] = (existing or {}).get("messages_analyzed", 0) + messages_analyzed
        new_version = current_version + 1

        # Cost estimate: gpt-4.1 ~$2/1M input + $8/1M output
        cost = tokens * 0.005 / 1000

        # UPSERT profile
        cur.execute("""
            INSERT INTO chat_profiles (chat_pair_id, profile_data, version, tokens_used, estimated_cost, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (chat_pair_id) DO UPDATE
                SET profile_data = EXCLUDED.profile_data,
                    version = EXCLUDED.version,
                    tokens_used = chat_profiles.tokens_used + EXCLUDED.tokens_used,
                    estimated_cost = chat_profiles.estimated_cost + EXCLUDED.estimated_cost,
                    updated_at = now()
        """, (chat_pair_id, json.dumps(merged, ensure_ascii=False), new_version, tokens, cost))

        # Record history
        change_parts = []
        if delta.get("glossary"):
            change_parts.append(f"+{len(delta['glossary'])} glossary")
        if delta.get("members"):
            change_parts.append(f"+{len(delta['members'])} members")
        for k in ("chat_type", "chat_description", "tone"):
            if delta.get(k):
                change_parts.append(f"updated {k}")
        change_summary = ", ".join(change_parts) if change_parts else "no changes"

        cur.execute("""
            INSERT INTO chat_profile_history (chat_pair_id, version, profile_data, change_summary)
            VALUES (%s, %s, %s, %s)
        """, (chat_pair_id, new_version, json.dumps(merged, ensure_ascii=False), change_summary))

        stored += 1

    conn.commit()
    cur.close()
    conn.close()

    logger.info("Stored %d chat profiles", stored)
    return stored


@task(retries=1, name="notify-profile-changes")
def notify_profile_changes(results: list[dict]) -> int:
    """Notify admins about new glossary entries and members."""
    logger = get_run_logger()

    if not results:
        return 0

    # Filter to results with actual changes
    changes = []
    for r in results:
        delta = r["delta"]
        if delta.get("glossary") or delta.get("members") or delta.get("mentioned_people"):
            changes.append(r)

    if not changes:
        logger.info("No glossary/member changes to notify")
        return 0

    lines = [f"📖 <b>Chat Context Update</b> ({date.today().isoformat()})\n"]

    total_glossary = 0
    total_members = 0

    for r in changes[:10]:  # cap at 10 chats to avoid message_too_long
        delta = r["delta"]
        pair_id = r["chat_pair_id"]
        glossary = delta.get("glossary", {})
        members = delta.get("members", {})
        mentioned = delta.get("mentioned_people", {})

        total_glossary += len(glossary)
        total_members += len(members)

        chat_desc = delta.get("chat_description") or f"pair #{pair_id}"
        lines.append(f"<b>{esc(str(chat_desc))}</b>")

        if glossary:
            items = []
            for word, info in list(glossary.items())[:8]:
                if isinstance(info, dict):
                    trans = info.get("translation", "")
                    items.append(f"{esc(word)} → {esc(trans)}")
                else:
                    items.append(f"{esc(word)} → {esc(str(info))}")
            lines.append(f"  Glossary: {', '.join(items)}")

        if members:
            items = [f"{esc(k)} → {esc(v)}" for k, v in list(members.items())[:8]]
            lines.append(f"  Members: {', '.join(items)}")

        if mentioned:
            items = []
            for name, info in list(mentioned.items())[:8]:
                if isinstance(info, dict):
                    trans = info.get("transliteration", "")
                    rel = info.get("relation", "")
                    items.append(f"{esc(name)} → {esc(trans)} ({esc(rel)})")
                else:
                    items.append(f"{esc(name)} → {esc(str(info))}")
            lines.append(f"  Mentioned: {', '.join(items)}")

        lines.append("")

    lines.append(f"<b>Total:</b> +{total_glossary} glossary, +{total_members} members across {len(changes)} chats")

    total_tokens = sum(r.get("tokens_used", 0) for r in results)
    if total_tokens:
        lines.append(f"Tokens: {total_tokens}")

    text = "\n".join(lines)
    sent = notify_telegram(text)

    logger.info("Sent context update to %d admins", sent)
    return sent


@flow(name="chat-context-builder", log_prints=True)
def chat_context_builder():
    """Build per-chat translation context: collect → extract → merge → store → notify."""
    logger = get_run_logger()

    chat_data_list = collect_per_chat_data()

    if not chat_data_list:
        logger.info("No chats with enough messages, skipping")
        return {"chats_processed": 0, "profiles_stored": 0}

    # Extract context for each chat
    results = []
    for chat_data in chat_data_list:
        result = extract_context_with_llm(chat_data)
        if result:
            results.append(result)

    # Store profiles
    stored = store_profiles(results)

    # Notify admins
    notified = notify_profile_changes(results)

    return {
        "chats_analyzed": len(chat_data_list),
        "profiles_extracted": len(results),
        "profiles_stored": stored,
        "admins_notified": notified,
        "total_tokens": sum(r.get("tokens_used", 0) for r in results),
    }


if __name__ == "__main__":
    chat_context_builder()
