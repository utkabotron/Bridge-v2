"""Feature flags: a 5s in-process copy in front of the Redis read, cleared by set_flag."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def flags():
    from processor.src import feature_flags as ff

    ff._memory.clear()
    ff._last_known.clear()
    redis = MagicMock()
    redis.get = AsyncMock(return_value="1")
    redis.delete = AsyncMock()
    redis.setex = AsyncMock()
    with patch.object(ff, "_get_redis", return_value=redis):
        yield ff, redis
    ff._memory.clear()
    ff._last_known.clear()


@pytest.mark.asyncio
async def test_repeated_checks_read_redis_once(flags):
    """is_enabled runs several times per message; each used to be a Redis round trip."""
    ff, redis = flags

    assert [await ff.is_enabled("prompt_ab_enabled") for _ in range(3)] == [True, True, True]

    redis.get.assert_awaited_once_with("ff:prompt_ab_enabled")


@pytest.mark.asyncio
async def test_a_stale_memory_entry_is_read_again(flags):
    ff, redis = flags

    await ff.is_enabled("prompt_ab_enabled")
    ff._memory["prompt_ab_enabled"] = (0.0, True)  # expired
    redis.get.return_value = "0"

    assert await ff.is_enabled("prompt_ab_enabled") is False
    assert redis.get.await_count == 2


@pytest.mark.asyncio
async def test_flags_are_remembered_separately(flags):
    ff, redis = flags
    redis.get.side_effect = lambda key: "1" if key == "ff:a" else "0"

    assert await ff.is_enabled("a") is True
    assert await ff.is_enabled("b") is False
    assert await ff.is_enabled("a") is True
    assert redis.get.await_count == 2


@pytest.mark.asyncio
async def test_set_flag_takes_effect_on_the_next_check(flags):
    """A toggle from the dashboard must not wait out the in-process copy."""
    ff, redis = flags

    assert await ff.is_enabled("media_analysis_enabled") is True  # now remembered

    redis.get.return_value = None  # set_flag drops the Redis copy...
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=[{"name": "media_analysis_enabled"}, {"enabled": False}])
    with patch("processor.src.db.get_pool", new=AsyncMock(return_value=pool)):
        assert await ff.set_flag("media_analysis_enabled", False) is True
        assert await ff.is_enabled("media_analysis_enabled") is False  # ...and the memory one

    redis.delete.assert_awaited_once_with("ff:media_analysis_enabled")


@pytest.mark.asyncio
async def test_set_flag_for_an_unknown_flag_keeps_memory(flags):
    ff, _ = flags
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    await ff.is_enabled("x")
    with patch("processor.src.db.get_pool", new=AsyncMock(return_value=pool)):
        assert await ff.set_flag("nope", True) is False

    assert "x" in ff._memory


@pytest.mark.asyncio
async def test_outage_fallbacks_are_not_remembered(flags, monkeypatch):
    """Redis and DB both down → env default; the real value must show up as soon as they are back."""
    ff, redis = flags
    monkeypatch.delenv("PROMPT_AB_ENABLED", raising=False)
    redis.get.side_effect = Exception("redis down")
    with patch("processor.src.db.get_pool", new=AsyncMock(side_effect=Exception("db down"))):
        assert await ff.is_enabled("prompt_ab_enabled") is True  # permissive env default

    assert "prompt_ab_enabled" not in ff._memory

    redis.get.side_effect = None
    redis.get.return_value = "0"
    assert await ff.is_enabled("prompt_ab_enabled") is False


@pytest.mark.asyncio
async def test_db_read_is_remembered_and_cached_in_redis(flags):
    ff, redis = flags
    redis.get.return_value = None
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"enabled": False})

    with patch("processor.src.db.get_pool", new=AsyncMock(return_value=pool)):
        assert await ff.is_enabled("voice_transcribe_enabled") is False
        assert await ff.is_enabled("voice_transcribe_enabled") is False

    pool.fetchrow.assert_awaited_once()
    redis.setex.assert_awaited_once_with("ff:voice_transcribe_enabled", 60, "0")
