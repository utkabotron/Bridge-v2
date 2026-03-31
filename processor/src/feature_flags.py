"""Feature flags with DB + Redis cache + env var fallback.

Flags are stored in the `feature_flags` table, cached in Redis for 60s.
If DB/Redis are unavailable, falls back to env vars (default: enabled).
"""
from __future__ import annotations

import logging
import os

import redis.asyncio as aioredis

from .config import REDIS_HOST, REDIS_PORT, REDIS_DB

logger = logging.getLogger(__name__)

_CACHE_TTL = 60  # seconds
_CACHE_PREFIX = "ff:"

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
            decode_responses=True,
        )
    return _redis


async def is_enabled(flag_name: str) -> bool:
    """Check if a feature flag is enabled (Redis cache → DB → env fallback)."""
    # Try Redis cache first
    try:
        cached = await _get_redis().get(f"{_CACHE_PREFIX}{flag_name}")
        if cached is not None:
            return cached == "1"
    except Exception:
        pass

    # Try DB
    try:
        from .db import get_pool
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT enabled FROM feature_flags WHERE name = $1", flag_name,
        )
        if row is not None:
            enabled = row["enabled"]
            try:
                await _get_redis().setex(
                    f"{_CACHE_PREFIX}{flag_name}", _CACHE_TTL, "1" if enabled else "0",
                )
            except Exception:
                pass
            return enabled
    except Exception as exc:
        logger.warning("Failed to read flag %s from DB: %s", flag_name, exc)

    # Env var fallback (e.g. MEDIA_ANALYSIS_ENABLED=false)
    env_val = os.getenv(flag_name.upper(), "true")
    return env_val.lower() not in ("false", "0", "no")


async def get_all_flags() -> list[dict]:
    """Return all flags from DB."""
    from .db import get_pool
    pool = await get_pool()
    rows = await pool.fetch("SELECT name, enabled, updated_at FROM feature_flags ORDER BY name")
    return [dict(r) for r in rows]


async def set_flag(flag_name: str, enabled: bool) -> bool:
    """Update a flag in DB and invalidate cache. Returns True if flag existed."""
    from .db import get_pool
    pool = await get_pool()
    row = await pool.fetchrow(
        "UPDATE feature_flags SET enabled = $1, updated_at = now() WHERE name = $2 RETURNING name",
        enabled, flag_name,
    )
    if row:
        try:
            await _get_redis().delete(f"{_CACHE_PREFIX}{flag_name}")
        except Exception:
            pass
        return True
    return False
