"""Feature flags with DB + Redis cache + env var fallback.

Flags are stored in the `feature_flags` table, cached in Redis for 60s and in this
process for FLAG_MEMORY_TTL seconds (set_flag clears both).
If DB/Redis are unavailable, falls back to env vars (default: enabled).
"""
from __future__ import annotations

import logging
import os
import time

import redis.asyncio as aioredis

from .config import FLAG_MEMORY_TTL, redis_kwargs

logger = logging.getLogger(__name__)

_CACHE_TTL = 60  # seconds
_CACHE_PREFIX = "ff:"

_redis: aioredis.Redis | None = None

# Last value we successfully read from Redis/DB, per flag. Used as a fallback when BOTH
# Redis and DB are unreachable, so a flag an admin turned OFF (e.g. to stop runaway LLM
# cost) does not silently flip back ON via the permissive env default during an outage.
_last_known: dict[str, bool] = {}

# flag name -> (monotonic expiry, value). is_enabled runs several times per message and
# each call was a Redis round trip; only values read from Redis/DB land here, never the
# outage fallbacks, so recovery is not delayed.
_memory: dict[str, tuple[float, bool]] = {}


def _remember(flag_name: str, value: bool) -> bool:
    _last_known[flag_name] = value
    _memory[flag_name] = (time.monotonic() + FLAG_MEMORY_TTL, value)
    return value


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.Redis(**redis_kwargs())
    return _redis


async def is_enabled(flag_name: str) -> bool:
    """Check if a feature flag is enabled (memory → Redis cache → DB → env fallback)."""
    hit = _memory.get(flag_name)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]

    # Try Redis cache first
    try:
        cached = await _get_redis().get(f"{_CACHE_PREFIX}{flag_name}")
        if cached is not None:
            return _remember(flag_name, cached == "1")
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
            enabled = _remember(flag_name, row["enabled"])
            try:
                await _get_redis().setex(
                    f"{_CACHE_PREFIX}{flag_name}", _CACHE_TTL, "1" if enabled else "0",
                )
            except Exception:
                pass
            return enabled
    except Exception as exc:
        logger.warning("Failed to read flag %s from DB: %s", flag_name, exc)

    # Both Redis and DB unavailable: prefer the last value we actually observed over the
    # permissive env default, so an intentionally-disabled flag stays disabled in an outage.
    if flag_name in _last_known:
        logger.warning("Flag %s: Redis+DB down, using last-known value %s", flag_name, _last_known[flag_name])
        return _last_known[flag_name]

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
        # After the Redis delete: a read that raced it cannot put the old value back.
        _memory.pop(flag_name, None)
        return True
    return False
