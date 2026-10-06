"""/api/stats: one grouped pass over a bounded window, cached in-process."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

USER_ROW = {
    "tg_username": "pasha", "tg_user_id": 1, "wa_connected": True, "target_language": "Hebrew",
    "pairs": 2, "delivered": 40, "failed": 1, "avg_ms": 850, "last_msg": None,
    "dir_tl": 3, "dir_ma": 0,
}


@pytest.fixture
def stats(monkeypatch):
    import processor.src.main as main

    monkeypatch.setattr(main, "_stats_cache", None)
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[USER_ROW])
    pool.fetchrow = AsyncMock(return_value={"total_skipped": 5, "total_avg_ms": 900})
    with patch("processor.src.db.get_pool", new=AsyncMock(return_value=pool)):
        yield main, pool


@pytest.mark.asyncio
async def test_stats_keeps_the_shape_the_dashboard_reads(stats):
    main, _ = stats

    result = await main.api_stats()

    assert result["total_skipped"] == 5
    assert result["total_avg_ms"] == 900
    assert set(result["users"][0]) == {
        "tg_username", "tg_user_id", "wa_connected", "target_language", "pairs",
        "delivered", "failed", "avg_ms", "last_msg", "dir_tl", "dir_ma",
    }


@pytest.mark.asyncio
async def test_stats_are_bounded_to_the_window_and_two_queries(stats):
    from processor.src.config import STATS_WINDOW_DAYS
    main, pool = stats

    await main.api_stats()

    # One grouped query for the users, one for the totals; both take the window as a bind
    # parameter instead of scanning message_events from the beginning.
    pool.fetch.assert_awaited_once()
    pool.fetchrow.assert_awaited_once()
    assert pool.fetch.await_args.args[-1] == STATS_WINDOW_DAYS
    assert pool.fetchrow.await_args.args[-1] == STATS_WINDOW_DAYS


@pytest.mark.asyncio
async def test_stats_poll_inside_the_ttl_does_not_touch_the_database(stats):
    main, pool = stats

    first = await main.api_stats()
    second = await main.api_stats()

    assert second is first
    assert pool.fetch.await_count == 1


@pytest.mark.asyncio
async def test_stats_are_recomputed_after_the_ttl(stats, monkeypatch):
    from processor.src.config import STATS_CACHE_TTL
    main, pool = stats

    await main.api_stats()
    stamp, cached = main._stats_cache
    monkeypatch.setattr(main, "_stats_cache", (stamp - STATS_CACHE_TTL - 1, cached))
    await main.api_stats()

    assert pool.fetch.await_count == 2
