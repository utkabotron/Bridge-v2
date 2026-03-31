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
from typing import Optional

import redis.asyncio as aioredis

from ..config import (
    REDIS_HOST, REDIS_PORT, REDIS_DB,
    TRANSLATION_CACHE_TTL, PROFILE_CACHE_TTL, MEDIA_CACHE_TTL,
)

_client: Optional[aioredis.Redis] = None
CACHE_TTL = TRANSLATION_CACHE_TTL


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
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


# ── Media analysis cache ─────────────────────────────────


async def get_cached_media(file_hash: str, language: str) -> Optional[str]:
    """Get cached media analysis result by file content hash."""
    try:
        return await get_redis().get(f"media_analysis:{language}:{file_hash}")
    except Exception:
        return None


async def set_cached_media(file_hash: str, language: str, result: str) -> None:
    """Cache media analysis result by file content hash."""
    try:
        await get_redis().setex(
            f"media_analysis:{language}:{file_hash}",
            MEDIA_CACHE_TTL,
            result,
        )
    except Exception:
        pass  # cache is best-effort
