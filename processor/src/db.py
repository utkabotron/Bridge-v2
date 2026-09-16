"""Database helpers for the processor.

Uses asyncpg directly (no ORM) to keep the processor lightweight.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import asyncpg

from .config import DATABASE_URL, DB_POOL_MIN, DB_POOL_MAX, DB_COMMAND_TIMEOUT

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

# Counts persistence failures so silent DB-write breakage (like the July 15 message_events
# freeze) surfaces in /metrics instead of hiding for days behind a swallowed exception.
_db_write_failures = 0


def get_db_write_failures() -> int:
    return _db_write_failures


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=DB_POOL_MIN,
            max_size=DB_POOL_MAX,
            command_timeout=DB_COMMAND_TIMEOUT,
        )
    return _pool


async def fetch_active_chat_pairs(user_id: int, wa_chat_id: str) -> list[dict]:
    """Return every active chat_pair a WhatsApp message must be delivered to.

    A group message is seen by EVERY WhatsApp client that sits in that group, and dedup
    collapses those copies into a single payload carrying one arbitrary client's user_id.
    So for groups we fan out to all active pairs of the chat, regardless of who owns them —
    otherwise whichever user did not win the dedup race silently stops receiving messages
    (that is how pair 15 starved while pair 11 delivered, both on the same WA group).

    Private chats are scoped to the owning user and NEVER fanned out: a @c.us wa_chat_id is
    just the other party's JID, so two users talking to the same contact would otherwise be
    delivered each other's private conversations.
    """
    pool = await get_pool()

    if wa_chat_id.endswith("@g.us"):
        rows = await pool.fetch(
            """
            select cp.id, cp.tg_chat_id, u.target_language
            from public.chat_pairs cp
            join public.users u on u.id = cp.user_id
            where cp.wa_chat_id = $1
              and cp.status = 'active'
            order by cp.id
            """,
            wa_chat_id,
        )
    else:
        rows = await pool.fetch(
            """
            select cp.id, cp.tg_chat_id, u.target_language
            from public.chat_pairs cp
            join public.users u on u.id = cp.user_id
            where cp.user_id = (select id from public.users where tg_user_id = $1)
              and cp.wa_chat_id = $2
              and cp.status = 'active'
            order by cp.id
            """,
            user_id,
            wa_chat_id,
        )

    return [dict(row) for row in rows]


async def merge_chat_pairs(stale_id: int, target_id: int) -> None:
    """Fold a stale chat_pair into the pair that already holds its new tg_chat_id.

    Happens when a Telegram group is upgraded to a supergroup and the user re-linked the
    chat manually before the migration landed: two rows then describe the same bridge, and
    with fan-out both would deliver into the same group.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Drop rows that would collide on unique (wa_message_id, chat_pair_id)
            await conn.execute(
                """
                delete from public.message_events a
                using public.message_events b
                where a.chat_pair_id = $1
                  and b.chat_pair_id = $2
                  and a.wa_message_id = b.wa_message_id
                """,
                stale_id, target_id,
            )
            await conn.execute(
                "update public.message_events set chat_pair_id = $1 where chat_pair_id = $2",
                target_id, stale_id,
            )
            await conn.execute("delete from public.chat_pairs where id = $1", stale_id)
    logger.warning("Merged stale chat_pair %s into %s", stale_id, target_id)


async def find_chat_pair_by_tg_chat(chat_pair_id: int, tg_chat_id: int) -> Optional[int]:
    """Id of the pair that already bridges the same WA chat to tg_chat_id, if any."""
    pool = await get_pool()
    return await pool.fetchval(
        """
        select other.id
        from public.chat_pairs other
        join public.chat_pairs stale
          on stale.user_id = other.user_id
         and stale.wa_chat_id = other.wa_chat_id
        where stale.id = $1
          and other.tg_chat_id = $2
          and other.id <> stale.id
        limit 1
        """,
        chat_pair_id, tg_chat_id,
    )


async def pause_chat_pair(chat_pair_id: int) -> Optional[int]:
    """Pause a pair whose Telegram chat is gone. Returns the owner's tg_user_id."""
    pool = await get_pool()
    try:
        return await pool.fetchval(
            """
            update public.chat_pairs cp
            set status = 'paused'
            from public.users u
            where cp.id = $1 and u.id = cp.user_id and cp.status = 'active'
            returning u.tg_user_id
            """,
            chat_pair_id,
        )
    except Exception as exc:
        logger.error("Failed to pause chat_pair %s: %s", chat_pair_id, exc)
        return None


async def fetch_chat_profile(chat_pair_id: int) -> Optional[dict]:
    """Return chat profile data for a given chat pair, or None."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT profile_data FROM chat_profiles WHERE chat_pair_id = $1",
        chat_pair_id,
    )
    if not row:
        return None
    data = row["profile_data"]
    if isinstance(data, str):
        data = json.loads(data)
    return data


async def insert_message_event(state: dict[str, Any], return_id: bool = False) -> Optional[int]:
    """Persist a processed message_event row.

    If return_id=True, returns the row id (for two-phase media delivery).
    """
    pool = await get_pool()
    returning = "returning id" if return_id else ""
    try:
        query = f"""
            insert into public.message_events
              (wa_message_id, chat_pair_id, sender_name, original_text, translated_text,
               message_type, media_s3_key, translation_ms, delivery_status, error_message,
               tg_message_id)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            on conflict (wa_message_id, chat_pair_id) do update
              set delivery_status = excluded.delivery_status,
                  translated_text = excluded.translated_text,
                  error_message   = excluded.error_message,
                  tg_message_id   = excluded.tg_message_id
              where message_events.delivery_status != 'delivered'
            {returning}
            """
        params = (
            state.get("wa_message_id"),
            state.get("chat_pair_id"),
            state.get("sender_name"),
            state.get("original_text"),
            state.get("translated_text"),
            state.get("message_type", "text"),
            state.get("media_s3_url"),
            state.get("translation_ms"),
            state.get("delivery_status", "pending"),
            state.get("error"),
            state.get("tg_message_id"),
        )
        if return_id:
            row = await pool.fetchrow(query, *params)
            if row is None:
                # Upsert matched an existing row whose WHERE guard blocked the update
                # (already delivered) so RETURNING is empty. With unique per-message ids
                # this should be rare; log it so a regression is visible.
                logger.warning(
                    "insert_message_event(return_id) affected 0 rows for wa_message_id=%s",
                    state.get("wa_message_id"),
                )
            return row["id"] if row else None
        else:
            status = await pool.execute(query, *params)
            # asyncpg returns e.g. "INSERT 0 1"; a trailing 0 means nothing was written.
            if status and status.rsplit(" ", 1)[-1] == "0":
                logger.warning(
                    "insert_message_event wrote 0 rows (status=%s) for wa_message_id=%s",
                    status, state.get("wa_message_id"),
                )
            return None
    except Exception as exc:
        global _db_write_failures
        _db_write_failures += 1
        logger.error("Failed to insert message_event: %s", exc)
        return None


async def update_event_after_send(
    event_id: int, status: str, error: Optional[str], tg_message_id: Optional[int],
) -> None:
    """Update message_event after Telegram send (two-phase delivery)."""
    pool = await get_pool()
    try:
        await pool.execute(
            """
            update public.message_events
            set delivery_status = $1, error_message = $2, tg_message_id = $3
            where id = $4
            """,
            status, error, tg_message_id, event_id,
        )
    except Exception as exc:
        logger.error("Failed to update event %s after send: %s", event_id, exc)


async def get_event_for_analysis(event_id: int) -> Optional[dict]:
    """Fetch event data needed for media analysis."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        select me.id, me.media_s3_key, me.message_type,
               me.tg_message_id,
               cp.tg_chat_id,
               u.target_language
        from public.message_events me
        left join public.chat_pairs cp on cp.id = me.chat_pair_id
        left join public.users u on u.id = cp.user_id
        where me.id = $1
        """,
        event_id,
    )
    return dict(row) if row else None


async def get_existing_analysis(event_id: int) -> Optional[dict]:
    """Check if media was already analyzed."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        select id, result_text, analysis_type from public.media_analysis
        where message_event_id = $1 and status = 'completed'
        limit 1
        """,
        event_id,
    )
    return dict(row) if row else None


async def insert_media_analysis(
    event_id: int, analysis_type: str, result_text: str,
    status: str, processing_ms: int, requested_by: int,
) -> None:
    """Insert media analysis result."""
    pool = await get_pool()
    try:
        await pool.execute(
            """
            insert into public.media_analysis
              (message_event_id, analysis_type, result_text, status, processing_ms, requested_by)
            values ($1, $2, $3, $4, $5, $6)
            """,
            event_id, analysis_type, result_text, status, processing_ms, requested_by,
        )
    except Exception as exc:
        logger.error("Failed to insert media_analysis: %s", exc)


async def insert_direct_translation(
    user_id: int, original_text: str, translated_text: str,
    target_language: str, translation_ms: int, cache_hit: bool,
) -> None:
    """Record a direct translation from bot private chat."""
    pool = await get_pool()
    try:
        await pool.execute(
            """
            insert into public.direct_interactions
              (user_id, interaction_type, original_text, translated_text,
               target_language, translation_ms, cache_hit)
            values (
              (select id from public.users where tg_user_id = $1),
              'translation', $2, $3, $4, $5, $6
            )
            """,
            user_id, original_text, translated_text, target_language, translation_ms, cache_hit,
        )
    except Exception as exc:
        logger.error("Failed to insert direct_translation: %s", exc)


async def insert_direct_media_analysis(
    user_id: int, analysis_type: str, mime_type: str, filename: str,
    result_text: str, processing_ms: int,
    status: str = "completed", error_message: str | None = None,
) -> None:
    """Record a direct media analysis from bot private chat."""
    pool = await get_pool()
    try:
        await pool.execute(
            """
            insert into public.direct_interactions
              (user_id, interaction_type, analysis_type, media_mime_type, media_filename,
               result_text, processing_ms, status, error_message)
            values (
              (select id from public.users where tg_user_id = $1),
              'media_analysis', $2, $3, $4, $5, $6, $7, $8
            )
            """,
            user_id, analysis_type, mime_type, filename,
            result_text, processing_ms, status, error_message,
        )
    except Exception as exc:
        logger.error("Failed to insert direct_media_analysis: %s", exc)
