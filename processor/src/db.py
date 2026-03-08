"""Database helpers for the processor.

Uses asyncpg directly (no ORM) to keep the processor lightweight.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge"),
            min_size=2,
            max_size=10,
        )
    return _pool


async def fetch_active_chat_pair(user_id: int, wa_chat_id: str) -> Optional[dict]:
    """Return the active chat_pair for a user+WA chat, or None.

    First tries exact match by user_id + wa_chat_id.
    If not found, falls back to ANY active pair for this wa_chat_id
    (handles race condition when multiple WA clients are in the same group
    and a different client wins the Redis dedup race).
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        select cp.id, cp.tg_chat_id, u.target_language
        from public.chat_pairs cp
        join public.users u on u.id = cp.user_id
        where cp.user_id = (select id from public.users where tg_user_id = $1)
          and cp.wa_chat_id = $2
          and cp.status = 'active'
        limit 1
        """,
        user_id,
        wa_chat_id,
    )
    if row:
        return dict(row)

    # Fallback: any active pair for this wa_chat_id regardless of user
    row = await pool.fetchrow(
        """
        select cp.id, cp.tg_chat_id, u.target_language
        from public.chat_pairs cp
        join public.users u on u.id = cp.user_id
        where cp.wa_chat_id = $1
          and cp.status = 'active'
        limit 1
        """,
        wa_chat_id,
    )
    if row:
        logger.info("Chat pair fallback: user_id=%s not matched, using pair %s for chat %s",
                     user_id, row["id"], wa_chat_id)
    return dict(row) if row else None


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
            on conflict (wa_message_id) do update
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
            return row["id"] if row else None
        else:
            await pool.execute(query, *params)
            return None
    except Exception as exc:
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
