"""Real-time pipeline event bus for SSE dashboard."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

# Recent events buffer (last 50)
_history: deque[dict] = deque(maxlen=50)

# Per-subscriber cap; see subscribe().
SUBSCRIBER_QUEUE_MAX = 500

# Active SSE subscribers
_subscribers: set[asyncio.Queue] = set()


def emit(event_type: str, data: dict[str, Any]) -> None:
    """Publish an event to all SSE subscribers."""
    event = {
        "type": event_type,
        "ts": time.time(),
        **data,
    }
    _history.append(event)
    for q in _subscribers:
        if q.full():
            # Drop the oldest so a stalled reader cannot grow this without limit.
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def subscribe() -> asyncio.Queue:
    """Create a new subscriber queue. Caller must call unsubscribe() when done."""
    # Bounded: an unbounded queue grew without limit behind a dashboard tab left open on a
    # slow connection (nginx holds SSE for 24h), inside the processor's own memory.
    # emit() drops the oldest event when full — stale pipeline events are worth less than
    # the messages the processor is carrying.
    q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
    # Send history as initial burst
    for evt in _history:
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            break
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def get_history() -> list[dict]:
    return list(_history)
