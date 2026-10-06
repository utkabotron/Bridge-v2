"""Unit tests for Redis translation cache module."""
from __future__ import annotations

import hashlib
import json
import pytest
from unittest.mock import AsyncMock, patch


# ── Key generation (pure) ──────────────────────────────────

def test_cache_key_format():
    from processor.src.pipeline.cache import _cache_key
    from processor.src.pipeline.prompts import PROMPT_VERSION
    digest = hashlib.sha256(f"{PROMPT_VERSION}\x00\x00hello".encode()).hexdigest()
    assert _cache_key("hello", "Russian", 42) == f"translation:Russian:42:{digest}"


def test_cache_key_changes_with_chat_context():
    """A glossary fix must not keep serving the translation made with the old glossary."""
    from processor.src.pipeline.cache import _cache_key
    before = _cache_key("hello", "Russian", 42, context="Glossary: כדורסל → кадурсаль")
    after = _cache_key("hello", "Russian", 42, context="Glossary: (empty)")
    assert before != after


def test_cache_key_changes_with_prompt_version(monkeypatch):
    from processor.src.pipeline import cache
    pair_before = cache._cache_key("hello", "Russian", 1)
    global_before = cache._global_cache_key("hello", "Russian")
    monkeypatch.setattr(cache, "PROMPT_VERSION", "v0.0-test")
    assert cache._cache_key("hello", "Russian", 1) != pair_before
    assert cache._global_cache_key("hello", "Russian") != global_before


def test_ab_variants_never_share_a_cache_entry():
    from processor.src.pipeline.cache import _cache_key, _global_cache_key
    assert _cache_key("hello", "Russian", 29, version="v2.10") != _cache_key("hello", "Russian", 29, version="v3.0")
    assert _global_cache_key("hello", "Russian", "v2.10") != _global_cache_key("hello", "Russian", "v3.0")


def test_cache_key_no_pair_uses_zero():
    from processor.src.pipeline.cache import _cache_key
    key = _cache_key("hello", "Russian")
    assert key.startswith("translation:Russian:0:")


def test_global_cache_key_format():
    from processor.src.pipeline.cache import _global_cache_key
    from processor.src.pipeline.prompts import PROMPT_VERSION
    digest = hashlib.sha256(f"{PROMPT_VERSION}\x00hello world".encode()).hexdigest()
    assert _global_cache_key("hello world", "Hebrew") == f"translation_global:Hebrew:{digest}"


def test_cache_key_different_texts_differ():
    from processor.src.pipeline.cache import _cache_key
    assert _cache_key("abc", "Russian", 1) != _cache_key("xyz", "Russian", 1)


def test_global_key_different_languages_differ():
    from processor.src.pipeline.cache import _global_cache_key
    assert _global_cache_key("hello", "Russian") != _global_cache_key("hello", "Hebrew")


# ── get_cached / set_cached (pair-specific) ───────────────

@pytest.mark.asyncio
async def test_get_cached_hit():
    from processor.src.pipeline.cache import get_cached
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="Привет")

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        result = await get_cached("hello", "Russian", 1)

    assert result == "Привет"


@pytest.mark.asyncio
async def test_get_cached_miss():
    from processor.src.pipeline.cache import get_cached
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        result = await get_cached("hello", "Russian", 1)

    assert result is None


@pytest.mark.asyncio
async def test_get_cached_redis_error_returns_none():
    from processor.src.pipeline.cache import get_cached
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(side_effect=Exception("connection error"))

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        result = await get_cached("hello", "Russian", 1)

    assert result is None


@pytest.mark.asyncio
async def test_set_cached_calls_setex_with_correct_key():
    from processor.src.pipeline.cache import set_cached
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(return_value="OK")

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_cached("hello", "Russian", "Привет", 1)

    mock_redis.setex.assert_called_once()
    key = mock_redis.setex.call_args[0][0]
    assert key.startswith("translation:Russian:1:")


@pytest.mark.asyncio
async def test_set_cached_redis_error_is_silent():
    from processor.src.pipeline.cache import set_cached
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(side_effect=Exception("connection error"))

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_cached("hello", "Russian", "Привет", 1)  # must not raise


# ── get_cached_global / set_cached_global ─────────────────

@pytest.mark.asyncio
async def test_get_cached_global_hit():
    from processor.src.pipeline.cache import get_cached_global
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="Шалом")

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        result = await get_cached_global("שלום", "Russian")

    assert result == "Шалом"
    called_key = mock_redis.get.call_args[0][0]
    assert called_key.startswith("translation_global:Russian:")


@pytest.mark.asyncio
async def test_get_cached_global_miss():
    from processor.src.pipeline.cache import get_cached_global
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        result = await get_cached_global("שלום", "Russian")

    assert result is None


@pytest.mark.asyncio
async def test_set_cached_global_uses_global_key():
    from processor.src.pipeline.cache import set_cached_global
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(return_value="OK")

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_cached_global("שלום", "Russian", "Шалом")

    key = mock_redis.setex.call_args[0][0]
    assert key.startswith("translation_global:Russian:")


@pytest.mark.asyncio
async def test_set_cached_global_error_is_silent():
    from processor.src.pipeline.cache import set_cached_global
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(side_effect=Exception("redis down"))

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_cached_global("שלום", "Russian", "Шалом")  # must not raise


# ── get_chat_profile / set_chat_profile ───────────────────

@pytest.mark.asyncio
async def test_get_chat_profile_hit():
    from processor.src.pipeline.cache import get_chat_profile
    profile = {"glossary": {"שלום": "Hello"}, "tone": "casual"}
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=json.dumps(profile))

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        result = await get_chat_profile(42)

    assert result == profile
    mock_redis.get.assert_called_once_with("chat_profile:42")


@pytest.mark.asyncio
async def test_get_chat_profile_miss():
    from processor.src.pipeline.cache import get_chat_profile
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value=None)

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        result = await get_chat_profile(42)

    assert result is None


@pytest.mark.asyncio
async def test_get_chat_profile_known_empty_is_not_a_miss():
    """{} is the cached "this pair has no profile"; None means ask the DB."""
    from processor.src.pipeline.cache import get_chat_profile
    mock_redis = AsyncMock()
    mock_redis.get = AsyncMock(return_value="{}")

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        result = await get_chat_profile(42)

    assert result == {}
    assert result is not None


@pytest.mark.asyncio
async def test_set_chat_profile_caches_an_empty_profile():
    from processor.src.config import PROFILE_CACHE_TTL
    from processor.src.pipeline.cache import set_chat_profile
    mock_redis = AsyncMock()

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_chat_profile(7, {})

    mock_redis.setex.assert_awaited_once_with("chat_profile:7", PROFILE_CACHE_TTL, "{}")


@pytest.mark.asyncio
async def test_set_chat_profile_correct_key():
    from processor.src.pipeline.cache import set_chat_profile
    profile = {"glossary": {}, "tone": "formal"}
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(return_value="OK")

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_chat_profile(7, profile)

    key = mock_redis.setex.call_args[0][0]
    assert key == "chat_profile:7"


@pytest.mark.asyncio
async def test_set_chat_profile_error_is_silent():
    from processor.src.pipeline.cache import set_chat_profile
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(side_effect=Exception("redis down"))

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_chat_profile(7, {})  # must not raise


# ── chat pairs cache ──────────────────────────────────────

PAIRS = [{"id": 11, "tg_chat_id": -1001, "target_language": "Hebrew"}]


def test_pairs_key_format():
    """The key wa-service documents (CLAUDE.md) and is expected to DEL on pair changes."""
    from processor.src.pipeline.cache import _pairs_key
    assert _pairs_key(100, "123@g.us") == "chat_pairs:user:100:chat:123@g.us"


@pytest.mark.asyncio
async def test_get_chat_pairs_tells_empty_from_unknown():
    from processor.src.pipeline.cache import get_chat_pairs
    mock_redis = AsyncMock()

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        mock_redis.get = AsyncMock(return_value=json.dumps(PAIRS))
        assert await get_chat_pairs(100, "123@g.us") == PAIRS
        mock_redis.get = AsyncMock(return_value="[]")
        assert await get_chat_pairs(100, "123@g.us") == []  # known: no pair
        mock_redis.get = AsyncMock(return_value=None)
        assert await get_chat_pairs(100, "123@g.us") is None  # unknown: ask the DB
        mock_redis.get = AsyncMock(return_value="not json")
        assert await get_chat_pairs(100, "123@g.us") is None
        mock_redis.get = AsyncMock(return_value='{"id": 1}')  # not a list
        assert await get_chat_pairs(100, "123@g.us") is None
        mock_redis.get = AsyncMock(side_effect=Exception("redis down"))
        assert await get_chat_pairs(100, "123@g.us") is None


@pytest.mark.asyncio
async def test_set_chat_pairs_caches_empty_briefly_and_pairs_for_the_full_ttl():
    from processor.src.config import PAIRS_CACHE_TTL, PAIRS_NEGATIVE_CACHE_TTL
    from processor.src.pipeline.cache import set_chat_pairs
    mock_redis = AsyncMock()

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_chat_pairs(100, "123@g.us", PAIRS)
        await set_chat_pairs(100, "123@g.us", [])

    (key, ttl, value), (_, neg_ttl, neg_value) = [c.args for c in mock_redis.setex.await_args_list]
    assert key == "chat_pairs:user:100:chat:123@g.us"
    assert (ttl, json.loads(value)) == (PAIRS_CACHE_TTL, PAIRS)
    assert (neg_ttl, json.loads(neg_value)) == (PAIRS_NEGATIVE_CACHE_TTL, [])
    assert PAIRS_NEGATIVE_CACHE_TTL < PAIRS_CACHE_TTL


@pytest.mark.asyncio
async def test_set_chat_pairs_error_is_silent():
    from processor.src.pipeline.cache import set_chat_pairs
    mock_redis = AsyncMock()
    mock_redis.setex = AsyncMock(side_effect=Exception("redis down"))

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await set_chat_pairs(100, "123@g.us", PAIRS)  # must not raise


@pytest.mark.asyncio
async def test_lookup_chat_pairs_hit_skips_the_database():
    from processor.src.pipeline.cache import lookup_chat_pairs
    fetch = AsyncMock()

    with patch("processor.src.pipeline.cache.get_chat_pairs", new=AsyncMock(return_value=PAIRS)), \
         patch("processor.src.db.fetch_active_chat_pairs", new=fetch):
        assert await lookup_chat_pairs(100, "123@g.us") == PAIRS

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_chat_pairs_empty_answer_is_a_hit_too():
    """Every unbridged chat of a connected account would otherwise hit Postgres per message."""
    from processor.src.pipeline.cache import lookup_chat_pairs
    fetch = AsyncMock()

    with patch("processor.src.pipeline.cache.get_chat_pairs", new=AsyncMock(return_value=[])), \
         patch("processor.src.db.fetch_active_chat_pairs", new=fetch):
        assert await lookup_chat_pairs(100, "123@c.us") == []

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_lookup_chat_pairs_miss_queries_once_and_caches_the_answer():
    from processor.src.pipeline.cache import lookup_chat_pairs
    fetch = AsyncMock(return_value=[])
    store = AsyncMock()

    with patch("processor.src.pipeline.cache.get_chat_pairs", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.cache.set_chat_pairs", new=store), \
         patch("processor.src.db.fetch_active_chat_pairs", new=fetch):
        assert await lookup_chat_pairs(100, "123@c.us") == []

    fetch.assert_awaited_once_with(100, "123@c.us")
    store.assert_awaited_once_with(100, "123@c.us", [])  # the empty answer is cached too


@pytest.mark.asyncio
async def test_invalidate_chat_pairs_private_chat_deletes_the_users_key():
    from processor.src.pipeline.cache import invalidate_chat_pairs
    mock_redis = AsyncMock()

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await invalidate_chat_pairs(100, "972501234567@c.us")

    mock_redis.delete.assert_awaited_once_with("chat_pairs:user:100:chat:972501234567@c.us")


@pytest.mark.asyncio
async def test_invalidate_chat_pairs_group_clears_every_users_copy():
    """A group's fan-out is cached under whichever user_id won the dedup race."""
    from processor.src.pipeline.cache import invalidate_chat_pairs

    async def scan_iter(match, count):
        assert match == "chat_pairs:user:*:chat:123@g.us"
        for key in ("chat_pairs:user:100:chat:123@g.us", "chat_pairs:user:200:chat:123@g.us"):
            yield key

    mock_redis = AsyncMock()
    mock_redis.scan_iter = scan_iter
    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await invalidate_chat_pairs(100, "123@g.us")

    mock_redis.delete.assert_awaited_once_with(
        "chat_pairs:user:100:chat:123@g.us", "chat_pairs:user:200:chat:123@g.us",
    )


@pytest.mark.asyncio
async def test_invalidate_chat_pairs_error_is_silent():
    from processor.src.pipeline.cache import invalidate_chat_pairs
    mock_redis = AsyncMock()
    mock_redis.delete = AsyncMock(side_effect=Exception("redis down"))

    with patch("processor.src.pipeline.cache.get_redis", return_value=mock_redis):
        await invalidate_chat_pairs(100, "123@c.us")  # must not raise
