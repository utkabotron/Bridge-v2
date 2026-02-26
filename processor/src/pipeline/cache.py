"""Redis translation cache.

Key: translation:{lang}:{sha256(text)}
TTL: TRANSLATION_CACHE_TTL env var (default 86400s = 24h)
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

import redis.asyncio as aioredis

_client: Optional[aioredis.Redis] = None
CACHE_TTL = int(os.getenv("TRANSLATION_CACHE_TTL", 86400))


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            db=int(os.getenv("REDIS_DB", 0)),
            decode_responses=True,
        )
    return _client


def _cache_key(text: str, language: str) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return f"translation:{language}:{digest}"


async def get_cached(text: str, language: str) -> Optional[str]:
    try:
        return await get_redis().get(_cache_key(text, language))
    except Exception:
        return None


async def set_cached(text: str, language: str, translation: str) -> None:
    try:
        await get_redis().setex(_cache_key(text, language), CACHE_TTL, translation)
    except Exception:
        pass  # cache is best-effort
