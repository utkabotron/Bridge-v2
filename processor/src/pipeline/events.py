"""Real-time pipeline event bus for SSE dashboard."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

# Recent events buffer (last 50)
_history: deque[dict] = deque(maxlen=50)

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
        q.put_nowait(event)


async def subscribe() -> asyncio.Queue:
    """Create a new subscriber queue. Caller must call unsubscribe() when done."""
    q: asyncio.Queue = asyncio.Queue()
    # Send history as initial burst
    for evt in _history:
        q.put_nowait(evt)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


def get_history() -> list[dict]:
    return list(_history)
