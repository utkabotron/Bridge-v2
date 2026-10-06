"""Redis translation cache.

Key: translation:{lang}:{chat_pair_id}:{sha256(prompt_version + chat_context + text)}
TTL: TRANSLATION_CACHE_TTL env var (default 86400s = 24h)

The prompt version and the chat context (glossary, members, tone) are hashed into the
key, so a new prompt or a glossary fix takes effect on the next message instead of
serving the old translation for up to 24h — which is how a nightly evaluation used to
score translations the fix had already made obsolete.

Global key (no glossary): translation_global:{lang}:{sha256(prompt_version + text)}
Used when chat has no profile — same text across multiple pairs hits cache.

Chat profile cache:
Key: chat_profile:{chat_pair_id}
Value: the profile as JSON; {} caches "this pair has no profile", so unprofiled pairs do not
       hit Postgres on every message (analytics DELs the key when it builds a profile)
TTL: PROFILE_CACHE_TTL env var (default 3600s = 1h)

Chat pairs cache (the pair lookup every message needs):
Key: chat_pairs:user:{user_id}:chat:{wa_chat_id}
Value: JSON list of {id, tg_chat_id, target_language}; [] caches "no active pair"
TTL: PAIRS_CACHE_TTL (default 1h), PAIRS_NEGATIVE_CACHE_TTL (default 60s) for []
Anything that creates, pauses, resumes, deletes or retargets a pair must DEL the key
(invalidate_chat_pairs does it here, for the pauses and supergroup migrations the processor
makes itself). wa-service and the bot own the other changes and must DEL it as well — until
they do, those changes take up to PAIRS_CACHE_TTL to apply. A group's lookup is
user-independent but cached per payload user_id, so DEL every chat_pairs:user:*:chat:<id>.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

import redis.asyncio as aioredis

from ..config import (
    redis_kwargs,
    TRANSLATION_CACHE_TTL, PROFILE_CACHE_TTL, MEDIA_CACHE_TTL,
    PAIRS_CACHE_TTL, PAIRS_NEGATIVE_CACHE_TTL,
)
from .prompts import PROMPT_VERSION

_client: Optional[aioredis.Redis] = None
CACHE_TTL = TRANSLATION_CACHE_TTL


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.Redis(**redis_kwargs())
    return _client


def _cache_key(text: str, language: str, chat_pair_id: int | None = None, context: str = "",
               version: str | None = None) -> str:
    version = version or PROMPT_VERSION
    digest = hashlib.sha256(f"{version}\x00{context}\x00{text}".encode()).hexdigest()
    pair_id = chat_pair_id or 0
    return f"translation:{language}:{pair_id}:{digest}"


async def get_cached(text: str, language: str, chat_pair_id: int | None = None,
                     context: str = "", version: str | None = None) -> Optional[str]:
    try:
        return await get_redis().get(_cache_key(text, language, chat_pair_id, context, version))
    except Exception:
        return None


async def set_cached(text: str, language: str, translation: str, chat_pair_id: int | None = None,
                     context: str = "", version: str | None = None) -> None:
    try:
        await get_redis().setex(_cache_key(text, language, chat_pair_id, context, version), CACHE_TTL, translation)
    except Exception:
        pass  # cache is best-effort


def _global_cache_key(text: str, language: str, version: str | None = None) -> str:
    version = version or PROMPT_VERSION
    digest = hashlib.sha256(f"{version}\x00{text}".encode()).hexdigest()
    return f"translation_global:{language}:{digest}"


async def get_cached_global(text: str, language: str, version: str | None = None) -> Optional[str]:
    """Get cached translation without pair context (for chats with no profile)."""
    try:
        return await get_redis().get(_global_cache_key(text, language, version))
    except Exception:
        return None


async def set_cached_global(text: str, language: str, translation: str, version: str | None = None) -> None:
    """Cache translation without pair context."""
    try:
        await get_redis().setex(_global_cache_key(text, language, version), CACHE_TTL, translation)
    except Exception:
        pass  # cache is best-effort


async def get_chat_profile(chat_pair_id: int) -> Optional[dict]:
    """Get cached chat profile from Redis. None = nothing cached; {} = known to have none."""
    try:
        raw = await get_redis().get(f"chat_profile:{chat_pair_id}")
        return json.loads(raw) if raw is not None else None
    except Exception:
        return None


async def set_chat_profile(chat_pair_id: int, profile: dict) -> None:
    """Cache chat profile in Redis; pass {} to cache that the pair has none."""
    try:
        await get_redis().setex(
            f"chat_profile:{chat_pair_id}",
            PROFILE_CACHE_TTL,
            json.dumps(profile, ensure_ascii=False),
        )
    except Exception:
        pass  # cache is best-effort


# ── Chat pairs cache ─────────────────────────────────────


def _pairs_key(user_id: int | str, wa_chat_id: str) -> str:
    return f"chat_pairs:user:{user_id}:chat:{wa_chat_id}"


async def get_chat_pairs(user_id: int, wa_chat_id: str) -> Optional[list[dict]]:
    """Cached active pairs of a chat. None = nothing cached; [] = known to have no pair."""
    try:
        raw = await get_redis().get(_pairs_key(user_id, wa_chat_id))
        pairs = json.loads(raw) if raw is not None else None
        return pairs if isinstance(pairs, list) else None
    except Exception:
        return None


async def set_chat_pairs(user_id: int, wa_chat_id: str, pairs: list[dict]) -> None:
    """Cache the active pairs of a chat, an empty list included."""
    try:
        await get_redis().setex(
            _pairs_key(user_id, wa_chat_id),
            PAIRS_CACHE_TTL if pairs else PAIRS_NEGATIVE_CACHE_TTL,
            json.dumps(pairs),
        )
    except Exception:
        pass  # cache is best-effort


async def invalidate_chat_pairs(user_id: int | None, wa_chat_id: str) -> None:
    """Forget the cached pairs of a chat after one of them changed."""
    try:
        r = get_redis()
        if wa_chat_id.endswith("@g.us"):
            # One lookup per user_id that ever won the dedup race for this group, all
            # holding the same fan-out — a pair change has to clear every copy.
            keys = [k async for k in r.scan_iter(match=_pairs_key("*", wa_chat_id), count=500)]
            if keys:
                await r.delete(*keys)
        else:
            await r.delete(_pairs_key(user_id, wa_chat_id))
    except Exception:
        pass  # worst case the entry expires on its own


async def lookup_chat_pairs(user_id: int, wa_chat_id: str) -> list[dict]:
    """Active pairs of a chat: Redis first, Postgres on a miss, caching either answer."""
    cached = await get_chat_pairs(user_id, wa_chat_id)
    if cached is not None:
        return cached
    from ..db import fetch_active_chat_pairs  # db imports this package
    pairs = await fetch_active_chat_pairs(user_id, wa_chat_id)
    await set_chat_pairs(user_id, wa_chat_id, pairs)
    return pairs


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
