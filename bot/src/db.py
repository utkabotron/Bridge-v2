"""Database helpers for the bot service."""
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
            command_timeout=10,
        )
    return _pool


# ── Users ─────────────────────────────────────────────────

async def get_user(tg_user_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "select * from public.users where tg_user_id = $1", tg_user_id
    )
    return dict(row) if row else None


async def create_user(tg_user_id: int, tg_username: Optional[str] = None) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        insert into public.users (tg_user_id, tg_username)
        values ($1, $2)
        on conflict (tg_user_id) do update set tg_username = excluded.tg_username
        returning *
        """,
        tg_user_id,
        tg_username,
    )
    return dict(row)


async def count_users() -> int:
    pool = await get_pool()
    return await pool.fetchval("select count(*) from public.users")


async def is_whitelisted(tg_user_id: int) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow(
        "select is_active from public.users where tg_user_id = $1 and is_active = true",
        tg_user_id,
    )
    return row is not None


async def add_to_whitelist(tg_user_id: int, tg_username: Optional[str] = None) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        insert into public.users (tg_user_id, tg_username, is_active)
        values ($1, $2, true)
        on conflict (tg_user_id) do update set is_active = true, tg_username = excluded.tg_username
        """,
        tg_user_id,
        tg_username,
    )


async def set_wa_connected(tg_user_id: int, session_id: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "update public.users set wa_session_id = $1, wa_connected = true where tg_user_id = $2",
        session_id,
        tg_user_id,
    )


# ── Onboarding sessions ───────────────────────────────────

async def get_onboarding_state(tg_user_id: int) -> Optional[str]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        select os.state from public.onboarding_sessions os
        join public.users u on u.id = os.user_id
        where u.tg_user_id = $1
        """,
        tg_user_id,
    )
    return row["state"] if row else None


async def set_onboarding_state(tg_user_id: int, state: str) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        insert into public.onboarding_sessions (user_id, state)
        select id, $2 from public.users where tg_user_id = $1
        on conflict (user_id) do update set state = excluded.state
        """,
        tg_user_id,
        state,
    )


async def mark_onboarding_done(tg_user_id: int) -> None:
    pool = await get_pool()
    await pool.execute(
        """
        update public.onboarding_sessions
        set state = 'done', done_at = now()
        where user_id = (select id from public.users where tg_user_id = $1)
        """,
        tg_user_id,
    )


# ── Chat pairs ────────────────────────────────────────────

async def get_chat_pairs(tg_user_id: int) -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        select cp.* from public.chat_pairs cp
        join public.users u on u.id = cp.user_id
        where u.tg_user_id = $1
        order by cp.created_at desc
        """,
        tg_user_id,
    )
    return [dict(r) for r in rows]


async def add_chat_pair(
    tg_user_id: int,
    wa_chat_id: str,
    wa_chat_name: str,
    tg_chat_id: int,
    tg_chat_title: str,
) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        insert into public.chat_pairs
          (user_id, wa_chat_id, wa_chat_name, tg_chat_id, tg_chat_title)
        select u.id, $2, $3, $4, $5 from public.users u where u.tg_user_id = $1
        on conflict (user_id, wa_chat_id, tg_chat_id) do update
          set wa_chat_name = excluded.wa_chat_name,
              tg_chat_title = excluded.tg_chat_title,
              status = 'active'
        returning *
        """,
        tg_user_id,
        wa_chat_id,
        wa_chat_name,
        tg_chat_id,
        tg_chat_title,
    )
    # row is None if no users row matched tg_user_id — return None so callers don't dict(None).
    return dict(row) if row else None


async def set_chat_pair_status(pair_id: int, status: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "update public.chat_pairs set status = $1 where id = $2", status, pair_id
    )


async def set_pair_language_owned(pair_id: int, tg_user_id: int, language: str | None) -> bool:
    """Set this bridge's target language, or clear it to follow the account setting.

    The column did not exist before: one person bridging a Hebrew school group and a
    Spanish work chat had to choose a single language for both.
    """
    pool = await get_pool()
    tag = await pool.execute(
        """
        update public.chat_pairs
        set target_language = $1
        where id = $2
          and user_id = (select id from public.users where tg_user_id = $3)
        """,
        language, pair_id, tg_user_id,
    )
    return bool(tag) and tag.rsplit(" ", 1)[-1] != "0"


async def get_pair_owned(pair_id: int, tg_user_id: int) -> Optional[dict]:
    """A single pair, only if the caller owns it."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        select cp.*, coalesce(cp.target_language, u.target_language) as effective_language,
               coalesce(css.enabled, true) as summary_enabled
        from public.chat_pairs cp
        join public.users u on u.id = cp.user_id
        left join public.chat_summary_schedule css on css.chat_pair_id = cp.id
        where cp.id = $1 and u.tg_user_id = $2
        """,
        pair_id, tg_user_id,
    )
    return dict(row) if row else None


async def toggle_pair_summary_owned(pair_id: int, tg_user_id: int) -> Optional[bool]:
    """Flip daily summaries for a pair. Returns the new state, or None if not owned.

    Creates the schedule row when absent so the preference sticks even before the
    scheduling flow has computed an hour for this chat.
    """
    pool = await get_pool()
    owned = await pool.fetchval(
        """
        select cp.id from public.chat_pairs cp
        join public.users u on u.id = cp.user_id
        where cp.id = $1 and u.tg_user_id = $2
        """,
        pair_id, tg_user_id,
    )
    if not owned:
        return None

    return await pool.fetchval(
        """
        insert into public.chat_summary_schedule (chat_pair_id, enabled)
        values ($1, false)
        on conflict (chat_pair_id) do update set enabled = not public.chat_summary_schedule.enabled
        returning enabled
        """,
        pair_id,
    )


async def set_chat_pair_status_owned(pair_id: int, tg_user_id: int, status: str) -> bool:
    """Update a pair's status only if it belongs to tg_user_id. Prevents a forged
    `chat:pause:<id>` callback from pausing/resuming another user's bridge.
    Returns True if a row was updated."""
    pool = await get_pool()
    status_tag = await pool.execute(
        """
        update public.chat_pairs
        set status = $1
        where id = $2
          and user_id = (select id from public.users where tg_user_id = $3)
        """,
        status, pair_id, tg_user_id,
    )
    return bool(status_tag) and status_tag.rsplit(" ", 1)[-1] != "0"


async def event_in_chat(event_id: int, tg_chat_id: int) -> bool:
    """True if message_event `event_id` belongs to a chat pair delivered to `tg_chat_id`.
    Used to reject an Analyze callback forged against another chat's event."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        select 1 from public.message_events me
        join public.chat_pairs cp on cp.id = me.chat_pair_id
        where me.id = $1 and cp.tg_chat_id = $2
        """,
        event_id, tg_chat_id,
    )
    return row is not None


# ── All users (admin) ─────────────────────────────────────

async def get_all_users() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("select * from public.users order by created_at")
    return [dict(r) for r in rows]
