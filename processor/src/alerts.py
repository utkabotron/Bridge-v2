"""Admin alerting plumbing: one Telegram notifier and one sliding-window counter.

Each alert in main.py used to carry its own copy of both — a hand-rolled sendMessage loop
over a throwaway httpx client, and a deque with its own "already alerted" flag.
"""
from __future__ import annotations

import logging
import time
from collections import deque

from . import telegram_sender as tg
from .config import ADMIN_TG_IDS
from .feature_flags import is_enabled

logger = logging.getLogger(__name__)


async def notify_admins(text: str) -> int:
    """Send an HTML alert to every admin. Returns how many received it.

    Sends nothing (0) when the admin_alerts_enabled flag is off or no bot token / admin id
    is configured. Reuses the delivery client, so alerts add no connection pool of their
    own. Link previews are off: an alert that carries a billing URL should not unfurl it.
    """
    if not await is_enabled("admin_alerts_enabled"):
        return 0
    if not tg.BOT_TOKEN or not ADMIN_TG_IDS:
        return 0

    sent = 0
    for admin_id in ADMIN_TG_IDS:
        try:
            r = await tg.get_client().post(
                f"{tg.BASE_URL}/sendMessage",
                json={"chat_id": admin_id, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
            )
        except Exception as exc:
            logger.error("Failed to send alert to admin %s: %s", admin_id, exc)
            continue
        if r.status_code == 200:
            sent += 1
        else:
            logger.error("Alert to admin %s rejected (%s): %s", admin_id, r.status_code, r.text)
    return sent


class SlidingWindow:
    """Events from the last `window` seconds, and a latch so one incident alerts once.

    record() adds an event; total / hits describe the window; fire() answers "should this
    incident alert now?" — True once, then False until the latch re-arms: when the tracker
    calls rearm() because it judges the incident over (failure rate, 401s), or, with
    cooldown=N, by itself N seconds after the last alert however busy the window stays — for
    failures a long outage keeps feeding (translation).
    """

    def __init__(self, window: float, cooldown: float | None = None) -> None:
        self.window = window
        self.cooldown = cooldown
        self._events: deque[tuple[float, bool]] = deque()  # (monotonic time, is_hit)
        self._fired_at: float | None = None

    @property
    def total(self) -> int:
        return len(self._events)

    @property
    def hits(self) -> int:
        return sum(1 for _, hit in self._events if hit)

    def record(self, hit: bool = True) -> None:
        now = time.monotonic()
        while self._events and self._events[0][0] < now - self.window:
            self._events.popleft()
        self._events.append((now, hit))

    def rearm(self) -> None:
        self._fired_at = None

    def fire(self) -> bool:
        now = time.monotonic()
        if self._fired_at is not None and (self.cooldown is None or now - self._fired_at < self.cooldown):
            return False
        self._fired_at = now
        return True
