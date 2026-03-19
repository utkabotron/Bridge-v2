"""Prefect flow: daily chat summaries sent to TG groups.

Runs every 30 minutes, checks which chats are due for a summary based on
individually computed optimal send hours, generates summaries with LLM,
and sends them directly to the corresponding Telegram groups.

Deploy:
  cron "*/30 * * * *"
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
from openai import OpenAI
from prefect import flow, get_run_logger, task

from .shared import esc, send_to_chat

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TZ = "Asia/Jerusalem"

ANALYSIS_MODEL = "gpt-4.1-mini"
MIN_MESSAGES = 3
SCHEDULE_REFRESH_DAYS = 7
DEFAULT_HOUR = 22


@task(retries=2, name="recompute-schedules")
def recompute_schedules() -> int:
    """Recompute optimal send hours for all active chat pairs (every 7 days)."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Check if recompute is needed
    cur.execute("""
        SELECT min(computed_at) AS oldest
        FROM chat_summary_schedule
    """)
    row = cur.fetchone()
    oldest = row["oldest"] if row else None

    # Also check if there are active pairs without a schedule
    cur.execute("""
        SELECT count(*) AS missing
        FROM chat_pairs cp
        JOIN users u ON u.id = cp.user_id
        WHERE cp.status = 'active' AND u.is_active = true
          AND NOT EXISTS (
              SELECT 1 FROM chat_summary_schedule css WHERE css.chat_pair_id = cp.id
          )
    """)
    missing = cur.fetchone()["missing"]

    if oldest and missing == 0:
        age_days = (datetime.utcnow() - oldest.replace(tzinfo=None)).days
        if age_days < SCHEDULE_REFRESH_DAYS:
            logger.info("Schedules fresh (%d days old), skipping recompute", age_days)
            cur.close()
            conn.close()
            return 0

    # Compute optimal hour for each active chat pair
    cur.execute("""
        SELECT cp.id AS chat_pair_id
        FROM chat_pairs cp
        JOIN users u ON u.id = cp.user_id
        WHERE cp.status = 'active' AND u.is_active = true
    """)
    pairs = [r["chat_pair_id"] for r in cur.fetchall()]

    updated = 0
    for pair_id in pairs:
        cur.execute("""
            SELECT extract(hour FROM created_at AT TIME ZONE %s)::int AS h
            FROM message_events
            WHERE chat_pair_id = %s
              AND created_at >= now() - interval '30 days'
              AND delivery_status = 'delivered'
              AND extract(hour FROM created_at AT TIME ZONE %s) >= 8
        """, (TZ, pair_id, TZ))

        hours = [r["h"] for r in cur.fetchall()]
        sample_size = len(hours)

        if sample_size < 10:
            optimal_hour = DEFAULT_HOUR
            mean_h = None
            std_h = None
        else:
            mean_h = sum(hours) / len(hours)
            variance = sum((h - mean_h) ** 2 for h in hours) / len(hours)
            std_h = math.sqrt(variance)
            raw = mean_h + 2 * std_h
            optimal_hour = max(20, min(23, round(raw)))

        cur.execute("""
            INSERT INTO chat_summary_schedule
                (chat_pair_id, optimal_hour, optimal_minute, mean_hour, std_hour, sample_size, computed_at)
            VALUES (%s, %s, 0, %s, %s, %s, now())
            ON CONFLICT (chat_pair_id) DO UPDATE
                SET optimal_hour = EXCLUDED.optimal_hour,
                    optimal_minute = EXCLUDED.optimal_minute,
                    mean_hour = EXCLUDED.mean_hour,
                    std_hour = EXCLUDED.std_hour,
                    sample_size = EXCLUDED.sample_size,
                    computed_at = now()
        """, (pair_id, optimal_hour, mean_h, std_h, sample_size))
        updated += 1

    conn.commit()
    cur.close()
    conn.close()

    logger.info("Recomputed schedules for %d chat pairs", updated)
    return updated


@task(retries=2, name="find-chats-due-now")
def find_chats_due_now() -> list[dict]:
    """Find chats whose optimal send time matches the current half-hour slot."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT
            css.chat_pair_id,
            css.optimal_hour,
            css.optimal_minute,
            cp.tg_chat_id,
            cp.wa_chat_name,
            cp.tg_chat_title,
            u.target_language,
            u.tg_user_id
        FROM chat_summary_schedule css
        JOIN chat_pairs cp ON cp.id = css.chat_pair_id
        JOIN users u ON u.id = cp.user_id
        WHERE cp.status = 'active'
          AND u.is_active = true
          AND css.optimal_hour = extract(hour FROM now() AT TIME ZONE %s)::int
          AND css.optimal_minute = (CASE
              WHEN extract(minute FROM now() AT TIME ZONE %s)::int < 30 THEN 0
              ELSE 30
          END)
          AND NOT EXISTS (
              SELECT 1 FROM daily_chat_summaries dcs
              WHERE dcs.chat_pair_id = css.chat_pair_id
                AND dcs.summary_date = (now() AT TIME ZONE %s)::date
                AND dcs.sent = true
          )
    """, (TZ, TZ, TZ))

    chats = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()

    logger.info("Found %d chats due for summary now", len(chats))
    return chats


@task(retries=2, name="collect-chat-messages")
def collect_chat_messages(chat_info: dict) -> dict | None:
    """Collect today's messages for a specific chat pair."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    chat_pair_id = chat_info["chat_pair_id"]

    # Get messages for today (Asia/Jerusalem timezone)
    cur.execute("""
        SELECT sender_name, original_text, message_type,
               created_at AT TIME ZONE %s AS local_time
        FROM message_events
        WHERE chat_pair_id = %s
          AND created_at >= (now() AT TIME ZONE %s)::date AT TIME ZONE %s
          AND delivery_status = 'delivered'
          AND original_text IS NOT NULL
          AND original_text != ''
        ORDER BY created_at
    """, (TZ, chat_pair_id, TZ, TZ))

    messages = [dict(r) for r in cur.fetchall()]

    # Load chat profile for context
    cur.execute("""
        SELECT profile_data FROM chat_profiles WHERE chat_pair_id = %s
    """, (chat_pair_id,))
    profile_row = cur.fetchone()
    profile = profile_row["profile_data"] if profile_row else None

    cur.close()
    conn.close()

    if len(messages) < MIN_MESSAGES:
        logger.info("Chat %d has only %d messages, skipping", chat_pair_id, len(messages))
        return None

    # Count unique senders
    senders = set()
    for m in messages:
        if m.get("sender_name"):
            senders.add(m["sender_name"])

    return {
        **chat_info,
        "messages": messages,
        "message_count": len(messages),
        "unique_senders": len(senders),
        "profile": profile,
    }


@task(retries=1, name="generate-summary-with-llm")
def generate_summary_with_llm(chat_data: dict) -> dict | None:
    """Generate daily summary using LLM."""
    logger = get_run_logger()

    target_lang = chat_data.get("target_language") or "Russian"
    messages = chat_data["messages"]
    profile = chat_data.get("profile")
    chat_name = chat_data.get("wa_chat_name") or chat_data.get("tg_chat_title") or "Chat"

    # Format messages for LLM
    msg_lines = []
    for m in messages[:300]:
        sender = m.get("sender_name", "?")
        text = (m.get("original_text") or "")[:500]
        time_str = ""
        if m.get("local_time"):
            t = m["local_time"]
            time_str = f" ({t.strftime('%H:%M')})" if hasattr(t, "strftime") else f" ({t})"
        msg_lines.append(f"[{sender}]{time_str}: {text}")

    messages_text = "\n".join(msg_lines)

    profile_context = ""
    if profile:
        profile_json = json.dumps(profile, ensure_ascii=False, indent=2) if isinstance(profile, dict) else str(profile)
        profile_context = f"\n\nChat profile (for context):\n{profile_json}"

    system_prompt = f"""You summarize daily WhatsApp group chat conversations.
The chat is "{chat_name}".
Write the summary and all output in {target_lang}.

Analyze the messages and produce a JSON object with exactly these fields:
- "summary": string — concise thematic summary of what was discussed today. Group by topics, not chronologically. Use 3-8 short paragraphs. Don't list every message, capture the essence.
- "plans": list of objects — events, plans, deadlines, appointments, or action items extracted from the conversation.
  Each plan: {{"text": "description", "date": "YYYY-MM-DD or null if unknown", "who": "person name or null"}}
  If no plans found, return empty list [].
- "title": string — short title for the summary header (2-5 words), in {target_lang}.

Rules:
- Write naturally and concisely in {target_lang}
- Don't include greetings, emoji reactions, or trivial messages in the summary
- Focus on substance: decisions, information shared, questions asked, plans made
- Extract ALL plans, events, deadlines, and action items — even implicit ones
- Return ONLY the JSON object, no markdown fences or extra text"""

    user_prompt = f"Messages from today ({date.today().isoformat()}):\n\n{messages_text}{profile_context}"

    client = OpenAI(api_key=OPENAI_API_KEY)

    try:
        response = client.chat.completions.create(
            model=ANALYSIS_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=2000,
            temperature=0.3,
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        result = json.loads(content)
        tokens_used = response.usage.total_tokens if response.usage else 0

        logger.info(
            "Chat %d: summary generated (%d plans, %d tokens)",
            chat_data["chat_pair_id"],
            len(result.get("plans", [])),
            tokens_used,
        )

        return {
            "chat_pair_id": chat_data["chat_pair_id"],
            "tg_chat_id": chat_data["tg_chat_id"],
            "target_language": target_lang,
            "message_count": chat_data["message_count"],
            "unique_senders": chat_data["unique_senders"],
            "optimal_send_hour": chat_data.get("optimal_hour"),
            "summary_text": result.get("summary", ""),
            "title": result.get("title", ""),
            "plans": result.get("plans", []),
            "tokens_used": tokens_used,
        }

    except Exception as exc:
        logger.error("LLM summary failed for chat %d: %s", chat_data["chat_pair_id"], exc)
        return None


@task(retries=2, name="send-summary-to-chat")
def send_summary_to_chat(summary_data: dict) -> dict:
    """Format and send summary to the Telegram group."""
    logger = get_run_logger()

    tg_chat_id = summary_data["tg_chat_id"]
    title = esc(summary_data.get("title", ""))
    summary = esc(summary_data.get("summary_text", ""))
    plans = summary_data.get("plans", [])
    count = summary_data["message_count"]
    senders = summary_data["unique_senders"]

    lines = []

    # Header
    header = title if title else summary_data.get("title", "")
    lines.append(f"📋 <b>{header}</b>\n")

    # Summary
    lines.append(summary)

    # Plans
    if plans:
        lines.append("")
        lines.append("📅 <b>Plans:</b>")
        for p in plans:
            text = esc(p.get("text", ""))
            date_str = p.get("date") or ""
            who = p.get("who") or ""
            parts = [f"• {text}"]
            if date_str:
                parts.append(f" ({date_str})")
            if who:
                parts.append(f" — {esc(who)}")
            lines.append("".join(parts))

    # Footer
    lines.append("")
    lines.append(f"💬 {count} messages, {senders} participants")

    text = "\n".join(lines)

    message_id = send_to_chat(tg_chat_id, text)

    if message_id:
        logger.info("Sent summary to chat %d (msg_id=%d)", tg_chat_id, message_id)
    else:
        logger.warning("Failed to send summary to chat %d", tg_chat_id)

    return {
        **summary_data,
        "sent": message_id is not None,
        "tg_message_id": message_id,
    }


@task(retries=2, name="store-summary")
def store_summary(result: dict) -> None:
    """UPSERT summary result into daily_chat_summaries."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    tokens = result.get("tokens_used", 0)
    # gpt-4.1-mini: ~$0.40/1M input + $1.60/1M output
    cost = tokens * 0.001 / 1000

    cur.execute("""
        INSERT INTO daily_chat_summaries
            (chat_pair_id, summary_date, message_count, unique_senders,
             summary_text, plans_extracted, optimal_send_hour,
             sent, sent_at, tg_message_id, tokens_used, estimated_cost)
        VALUES (%s, (now() AT TIME ZONE %s)::date, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s THEN now() ELSE NULL END,
                %s, %s, %s)
        ON CONFLICT (chat_pair_id, summary_date) DO UPDATE
            SET message_count = EXCLUDED.message_count,
                unique_senders = EXCLUDED.unique_senders,
                summary_text = EXCLUDED.summary_text,
                plans_extracted = EXCLUDED.plans_extracted,
                optimal_send_hour = EXCLUDED.optimal_send_hour,
                sent = EXCLUDED.sent,
                sent_at = EXCLUDED.sent_at,
                tg_message_id = EXCLUDED.tg_message_id,
                tokens_used = EXCLUDED.tokens_used,
                estimated_cost = EXCLUDED.estimated_cost
    """, (
        result["chat_pair_id"],
        TZ,
        result["message_count"],
        result["unique_senders"],
        result.get("summary_text", ""),
        json.dumps(result.get("plans", []), ensure_ascii=False),
        result.get("optimal_send_hour"),
        result.get("sent", False),
        result.get("sent", False),
        result.get("tg_message_id"),
        tokens,
        cost,
    ))

    conn.commit()
    cur.close()
    conn.close()

    logger.info("Stored summary for chat_pair %d", result["chat_pair_id"])


@flow(name="daily-chat-summary", log_prints=True)
def daily_chat_summary():
    """Daily chat summary: schedule → find due chats → collect → generate → send → store."""
    logger = get_run_logger()

    # Recompute schedules if needed
    recompute_schedules()

    # Find chats due now
    chats_due = find_chats_due_now()

    if not chats_due:
        logger.info("No chats due for summary right now")
        return {"chats_processed": 0, "summaries_sent": 0}

    sent_count = 0
    processed = 0

    for chat_info in chats_due:
        # Collect messages
        chat_data = collect_chat_messages(chat_info)
        if not chat_data:
            continue

        # Generate summary
        summary = generate_summary_with_llm(chat_data)
        if not summary:
            continue

        # Send to TG group
        result = send_summary_to_chat(summary)

        # Store in DB
        store_summary(result)

        processed += 1
        if result.get("sent"):
            sent_count += 1

    logger.info("Processed %d chats, sent %d summaries", processed, sent_count)
    return {
        "chats_due": len(chats_due),
        "chats_processed": processed,
        "summaries_sent": sent_count,
    }


if __name__ == "__main__":
    daily_chat_summary()
