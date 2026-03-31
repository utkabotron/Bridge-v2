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
    return dict(row)


async def set_chat_pair_status(pair_id: int, status: str) -> None:
    pool = await get_pool()
    await pool.execute(
        "update public.chat_pairs set status = $1 where id = $2", status, pair_id
    )


# ── All users (admin) ─────────────────────────────────────

async def get_all_users() -> list[dict]:
    pool = await get_pool()
    rows = await pool.fetch("select * from public.users order by created_at")
    return [dict(r) for r in rows]
