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
    """Return the active chat_pair for a user+WA chat, or None."""
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
    return dict(row) if row else None


async def insert_message_event(state: dict[str, Any]) -> None:
    """Persist a processed message_event row."""
    pool = await get_pool()
    try:
        await pool.execute(
            """
            insert into public.message_events
              (wa_message_id, chat_pair_id, sender_name, original_text, translated_text,
               message_type, media_s3_key, translation_ms, delivery_status, error_message)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            on conflict (wa_message_id) do update
              set delivery_status = excluded.delivery_status,
                  translated_text = excluded.translated_text,
                  error_message   = excluded.error_message
            """,
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
        )
    except Exception as exc:
        logger.error("Failed to insert message_event: %s", exc)
