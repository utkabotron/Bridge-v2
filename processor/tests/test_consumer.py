"""The consumer: several workers over messages:in, one chat at a time in order."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

import processor.src.main as main


class FakeRedis:
    """BLMOVE hands out the queued items in order, then returns None until shutdown."""

    def __init__(self, items):
        self.items = list(items)
        self.processing = []
        self.dlq = []

    async def blmove(self, src, dst, timeout, *_):
        if self.items:
            raw = self.items.pop(0)
            self.processing.append(raw)
            return raw
        await asyncio.sleep(0.01)
        if not self.items:
            main._shutting_down.set()
        return None

    async def lrem(self, key, count, raw):
        self.processing.remove(raw)

    async def lpush(self, key, raw):
        self.dlq.append(raw)

    async def aclose(self):
        pass


def _msg(chat: str, n: int) -> str:
    return json.dumps({"user_id": 1, "wa_chat_id": chat, "wa_message_id": f"{chat}-{n}"})


@pytest.fixture(autouse=True)
def _reset():
    main._shutting_down.clear()
    main._chat_locks.clear()
    yield
    main._shutting_down.clear()
    main._chat_locks.clear()


@pytest.mark.asyncio
async def test_different_chats_are_processed_side_by_side():
    """One slow chat used to hold every other chat behind it."""
    r = FakeRedis([_msg("slow", 1), _msg("fast", 1)])
    started, finished = [], []

    async def process(_r, raw):
        msg_id = json.loads(raw)["wa_message_id"]
        started.append(msg_id)
        await asyncio.sleep(0.2 if msg_id.startswith("slow") else 0.01)
        finished.append(msg_id)

    with patch.object(main, "_process_message", new=process), \
         patch.object(main, "_requeue_inflight", new=AsyncMock()), \
         patch.object(main, "CONSUMER_WORKERS", 2), \
         patch("processor.src.main.aioredis.Redis", return_value=r):
        await asyncio.wait_for(main.consume_loop(), timeout=5)

    assert finished == ["fast-1", "slow-1"]  # fast did not wait for slow
    assert r.processing == []                # both removed from the in-flight list


@pytest.mark.asyncio
async def test_messages_of_one_chat_keep_their_order():
    """An edit must never overtake the message it revises, however many workers run."""
    r = FakeRedis([_msg("x", 1), _msg("x", 2), _msg("x", 3)])
    order, overlap = [], 0
    active = 0

    async def process(_r, raw):
        nonlocal active, overlap
        active += 1
        overlap = max(overlap, active)
        await asyncio.sleep(0.02)
        order.append(json.loads(raw)["wa_message_id"])
        active -= 1

    with patch.object(main, "_process_message", new=process), \
         patch.object(main, "_requeue_inflight", new=AsyncMock()), \
         patch.object(main, "CONSUMER_WORKERS", 3), \
         patch("processor.src.main.aioredis.Redis", return_value=r):
        await asyncio.wait_for(main.consume_loop(), timeout=5)

    assert order == ["x-1", "x-2", "x-3"]
    assert overlap == 1


@pytest.mark.asyncio
async def test_a_worker_crash_outside_processing_dead_letters_the_item():
    r = FakeRedis([_msg("x", 1)])

    async def process(_r, raw):
        return None

    async def lrem_boom(key, count, raw):
        raise RuntimeError("redis hiccup")

    calls = {"n": 0}

    async def lrem(key, count, raw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("redis hiccup")
        r.processing.remove(raw)

    r.lrem = lrem
    with patch.object(main, "_process_message", new=process), \
         patch.object(main, "_requeue_inflight", new=AsyncMock()), \
         patch.object(main, "CONSUMER_WORKERS", 1), \
         patch("processor.src.main.aioredis.Redis", return_value=r), \
         patch("processor.src.main.asyncio.sleep", new=AsyncMock()):
        await asyncio.wait_for(main.consume_loop(), timeout=5)

    assert len(r.dlq) == 1 and "redis hiccup" in r.dlq[0]
