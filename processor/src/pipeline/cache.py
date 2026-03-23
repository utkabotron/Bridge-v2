"""Redis translation cache.

Key: translation:{lang}:{chat_pair_id}:{sha256(text)}
TTL: TRANSLATION_CACHE_TTL env var (default 86400s = 24h)

Global key (no glossary): translation_global:{lang}:{sha256(text)}
Used when chat has no profile — same text across multiple pairs hits cache.

Chat profile cache:
Key: chat_profile:{chat_pair_id}
TTL: PROFILE_CACHE_TTL env var (default 3600s = 1h)
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

import redis.asyncio as aioredis

_client: Optional[aioredis.Redis] = None
CACHE_TTL = int(os.getenv("TRANSLATION_CACHE_TTL", 86400))
PROFILE_CACHE_TTL = int(os.getenv("PROFILE_CACHE_TTL", 3600))


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


def _cache_key(text: str, language: str, chat_pair_id: int | None = None) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()
    pair_id = chat_pair_id or 0
    return f"translation:{language}:{pair_id}:{digest}"


async def get_cached(text: str, language: str, chat_pair_id: int | None = None) -> Optional[str]:
    try:
        return await get_redis().get(_cache_key(text, language, chat_pair_id))
    except Exception:
        return None


async def set_cached(text: str, language: str, translation: str, chat_pair_id: int | None = None) -> None:
    try:
        await get_redis().setex(_cache_key(text, language, chat_pair_id), CACHE_TTL, translation)
    except Exception:
        pass  # cache is best-effort


def _global_cache_key(text: str, language: str) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return f"translation_global:{language}:{digest}"


async def get_cached_global(text: str, language: str) -> Optional[str]:
    """Get cached translation without pair context (for chats with no profile)."""
    try:
        return await get_redis().get(_global_cache_key(text, language))
    except Exception:
        return None


async def set_cached_global(text: str, language: str, translation: str) -> None:
    """Cache translation without pair context."""
    try:
        await get_redis().setex(_global_cache_key(text, language), CACHE_TTL, translation)
    except Exception:
        pass  # cache is best-effort


async def get_chat_profile(chat_pair_id: int) -> Optional[dict]:
    """Get cached chat profile from Redis."""
    try:
        raw = await get_redis().get(f"chat_profile:{chat_pair_id}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def set_chat_profile(chat_pair_id: int, profile: dict) -> None:
    """Cache chat profile in Redis."""
    try:
        await get_redis().setex(
            f"chat_profile:{chat_pair_id}",
            PROFILE_CACHE_TTL,
            json.dumps(profile, ensure_ascii=False),
        )
    except Exception:
        pass  # cache is best-effort
