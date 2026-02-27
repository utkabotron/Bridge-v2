"""Redis pub/sub subscriber loop for onboarding QR events.

Ported from services/bot.py — redis_subscriber_loop().
Listens to onboarding:qr_scanned:* and notifies users when WA connects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import redis
import redis.exceptions

logger = logging.getLogger(__name__)

# Module-level references injected by main.py at startup
_bot_app = None
_loop = None


def set_bot_app(app):
    global _bot_app
    _bot_app = app


def set_event_loop(loop):
    global _loop
    _loop = loop


def _make_redis():
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


async def handle_qr_event(data: dict) -> None:
    """Called when WhatsApp emits ready/authenticated for a user."""
    user_id = data.get("userId")
    event = data.get("event", "")

    if not user_id or not _bot_app:
        return

    from .db import get_onboarding_state, set_onboarding_state, set_wa_connected

    # Always mark WA as connected in users table
    await set_wa_connected(int(user_id), str(user_id))

    state = await get_onboarding_state(int(user_id))
    if state != "qr_pending":
        return

    await set_onboarding_state(int(user_id), "wa_connected")
    logger.info("User %s WA connected (event=%s)", user_id, event)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from .templates.messages import render

    me = await _bot_app.bot.get_me()
    text = render("onboarding_wa_connected", bot_username=me.username)
    kb = [[InlineKeyboardButton("✅ Done — bot is in the group", callback_data="onboarding:group_created")]]

    try:
        await _bot_app.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
    except Exception as exc:
        logger.error("Failed to notify user %s: %s", user_id, exc)


def redis_subscriber_loop():
    """Blocking pub/sub loop — runs in a thread (via asyncio.to_thread)."""
    client = _make_redis()
    pubsub = client.pubsub()

    def on_message(message):
        if message.get("type") != "pmessage":
            return
        try:
            data = json.loads(message["data"])
            if _loop and _loop.is_running():
                asyncio.run_coroutine_threadsafe(handle_qr_event(data), _loop)
            else:
                logger.warning("No running event loop — skipping QR event")
        except Exception as exc:
            logger.error("QR event handler error: %s", exc)

    while True:
        try:
            pubsub.psubscribe(**{"onboarding:qr_scanned:*": on_message})
            logger.info("Subscribed to onboarding:qr_scanned:*")
            for msg in pubsub.listen():
                pass  # callbacks handle it
        except redis.exceptions.TimeoutError:
            continue
        except Exception as exc:
            logger.error("Redis subscriber error: %s — reconnecting in 5s", exc)
            time.sleep(5)
            client = _make_redis()
            pubsub = client.pubsub()
