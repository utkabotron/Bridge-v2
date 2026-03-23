"""Unit tests for Redis translation cache module."""
from __future__ import annotations

import hashlib
import json
import pytest
from unittest.mock import AsyncMock, patch


# ── Key generation (pure) ──────────────────────────────────

def test_cache_key_format():
    from processor.src.pipeline.cache import _cache_key
    digest = hashlib.sha256("hello".encode()).hexdigest()
    assert _cache_key("hello", "Russian", 42) == f"translation:Russian:42:{digest}"


def test_cache_key_no_pair_uses_zero():
    from processor.src.pipeline.cache import _cache_key
    key = _cache_key("hello", "Russian")
    assert key.startswith("translation:Russian:0:")


def test_global_cache_key_format():
    from processor.src.pipeline.cache import _global_cache_key
    digest = hashlib.sha256("hello world".encode()).hexdigest()
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
