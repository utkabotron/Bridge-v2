"""Redis pub/sub subscriber loop for WhatsApp session events.

Ported from services/bot.py — redis_subscriber_loop().
Listens to onboarding:qr_scanned:* (WA connected) and wa:disconnected:* (session lost)
and tells the user either way — a dead session is otherwise silent.
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
_pending_events: list[tuple[dict, object]] = []


def _dispatch(data: dict, handler=None) -> None:
    """Schedule the handler on the bot loop and LOG any failure — a bare
    run_coroutine_threadsafe drops the returned future, so exceptions inside
    (bad userId, DB down) would otherwise vanish and the user would hang in qr_pending."""
    handler = handler or handle_qr_event
    fut = asyncio.run_coroutine_threadsafe(handler(data), _loop)

    def _log_result(f):
        try:
            f.result()
        except Exception as exc:
            logger.error("%s failed for %s: %s", handler.__name__, data.get("userId"), exc)

    fut.add_done_callback(_log_result)


def set_bot_app(app):
    global _bot_app
    _bot_app = app
    # Drain buffered events that arrived before bot was ready
    if _pending_events and _loop and _loop.is_running():
        logger.info("Draining %d buffered WA events", len(_pending_events))
        for evt, handler in _pending_events:
            _dispatch(evt, handler)
        _pending_events.clear()


def set_event_loop(loop):
    global _loop
    _loop = loop


def _make_pubsub_redis():
    # NOTE: no socket_timeout here. A read timeout on a blocking pubsub.listen() raises
    # every few seconds, forcing a resubscribe; any qr_scanned published during that gap is
    # lost (pub/sub has no replay). health_check_interval keeps the connection alive instead.
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=30,
    )


async def handle_qr_event(data: dict) -> None:
    """Called when WhatsApp emits ready/authenticated for a user."""
    user_id = data.get("userId")
    event = data.get("event", "")

    if not user_id:
        logger.warning("QR event missing userId, raw data: %s", data)
        return
    if not _bot_app:
        logger.warning("QR event for user %s but _bot_app not ready, buffering", user_id)
        _pending_events.append((data, handle_qr_event))
        return

    from .db import set_wa_connected

    # wa-service writes this flag itself when a client goes ready; doing it here too costs
    # one statement and covers the case where that write failed.
    await set_wa_connected(int(user_id), str(user_id))

    # Only the terminal event is worth telling the user about: 'authenticated' fires
    # before the chat sync finishes, and the Mini App is already showing live status.
    if event != "ready":
        return

    logger.info("User %s WA connected (event=%s)", user_id, event)

    from .templates.messages import render

    # The old code gated this on onboarding_state == 'qr_pending' and then advanced the
    # state machine — but nothing ever set qr_pending (its only setter was an unreachable
    # callback), so this notification never fired at all.
    try:
        await _bot_app.bot.send_message(
            chat_id=user_id,
            text=render("wa_connected_notice"),
            parse_mode="Markdown",
        )
    except Exception as exc:
        logger.error("Failed to notify user %s: %s", user_id, exc)


async def handle_wa_disconnected(data: dict) -> None:
    """Called when a WhatsApp session dies for good (logout, or recovery gave up).

    Nothing used to be sent here, so a user's bridge could sit dead for days while the bot
    kept answering /start with a cheerful welcome screen.
    """
    user_id = data.get("userId")
    reason = data.get("reason", "unknown")

    if not user_id:
        logger.warning("Disconnect event missing userId, raw data: %s", data)
        return
    if not _bot_app:
        logger.warning("Disconnect event for user %s but _bot_app not ready, buffering", user_id)
        _pending_events.append((data, handle_wa_disconnected))
        return

    logger.info("User %s WA disconnected (reason=%s) — notifying", user_id, reason)

    from .templates.messages import render

    try:
        await _bot_app.bot.send_message(
            chat_id=user_id,
            text=render("wa_disconnected_notice"),
            parse_mode="Markdown",
            reply_markup=_miniapp_keyboard(_bot_app.bot.username),
        )
    except Exception as exc:
        logger.error("Failed to notify user %s about disconnect: %s", user_id, exc)


def _miniapp_keyboard(bot_username: str):
    """The notice is only useful if reconnecting is one tap away.

    ?bot= is what the app's "add me to a group" link is built from — same URL /start uses.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    from .onboarding.wizard import MINIAPP_URL

    url = f"{MINIAPP_URL}?bot={bot_username}" if bot_username else MINIAPP_URL
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📱 Open Mini App", web_app=WebAppInfo(url=url))]]
    )


def redis_subscriber_loop():
    """Blocking pub/sub loop — runs in a thread (via asyncio.to_thread)."""
    client = _make_pubsub_redis()
    pubsub = client.pubsub()

    def _make_on_message(handler):
        def on_message(message):
            if message.get("type") != "pmessage":
                return
            try:
                data = json.loads(message["data"])
                if _loop and _loop.is_running():
                    _dispatch(data, handler)
                else:
                    logger.warning("No running event loop — buffering WA event for user %s", data.get("userId"))
                    _pending_events.append((data, handler))
            except Exception as exc:
                logger.error("WA event handler error: %s", exc)

        return on_message

    while True:
        try:
            pubsub.psubscribe(**{
                "onboarding:qr_scanned:*": _make_on_message(handle_qr_event),
                "wa:disconnected:*": _make_on_message(handle_wa_disconnected),
            })
            logger.info("Subscribed to onboarding:qr_scanned:* and wa:disconnected:*")
            for msg in pubsub.listen():
                pass  # callbacks handle it
        except redis.exceptions.TimeoutError:
            continue
        except Exception as exc:
            logger.error("Redis subscriber error: %s — reconnecting in 5s", exc)
            time.sleep(5)
            client = _make_pubsub_redis()
            pubsub = client.pubsub()
