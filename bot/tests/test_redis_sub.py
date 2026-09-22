"""Tests for the WhatsApp session event subscriber.

A dead session used to be silent: wa-service logged LOGOUT, cleared the flag, and nobody
told the user. These cover the notification that closes that gap.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.src import redis_sub


@pytest.fixture(autouse=True)
def _reset_module_state():
    redis_sub._bot_app = None
    redis_sub._pending_events.clear()
    yield
    redis_sub._bot_app = None
    redis_sub._pending_events.clear()


def _fake_app():
    app = MagicMock()
    app.bot.username = "bridge_bot"
    app.bot.send_message = AsyncMock()
    return app


@pytest.mark.asyncio
async def test_disconnect_notifies_user_with_reconnect_button():
    app = _fake_app()
    redis_sub._bot_app = app

    await redis_sub.handle_wa_disconnected({"userId": 359406176, "reason": "LOGOUT"})

    app.bot.send_message.assert_awaited_once()
    kwargs = app.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 359406176
    assert "disconnected" in kwargs["text"].lower()
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_disconnect_before_bot_ready_is_buffered():
    """Events during startup must survive — pub/sub has no replay."""
    await redis_sub.handle_wa_disconnected({"userId": 7, "reason": "LOGOUT"})

    assert len(redis_sub._pending_events) == 1
    data, handler = redis_sub._pending_events[0]
    assert data["userId"] == 7
    assert handler is redis_sub.handle_wa_disconnected


@pytest.mark.asyncio
async def test_disconnect_without_user_id_is_ignored():
    app = _fake_app()
    redis_sub._bot_app = app

    await redis_sub.handle_wa_disconnected({"reason": "LOGOUT"})

    app.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_failure_does_not_propagate():
    """A blocked user must not kill the subscriber thread."""
    app = _fake_app()
    app.bot.send_message = AsyncMock(side_effect=RuntimeError("bot was blocked"))
    redis_sub._bot_app = app

    await redis_sub.handle_wa_disconnected({"userId": 7, "reason": "LOGOUT"})


def test_buffered_events_keep_their_own_handler():
    """Draining must not send every buffered event through the QR handler."""
    dispatched = []
    redis_sub._pending_events.append(({"userId": 1}, redis_sub.handle_wa_disconnected))
    redis_sub._pending_events.append(({"userId": 2}, redis_sub.handle_qr_event))

    loop = MagicMock()
    loop.is_running.return_value = True
    redis_sub._loop = loop

    with patch.object(redis_sub, "_dispatch", side_effect=lambda d, h: dispatched.append((d, h))):
        redis_sub.set_bot_app(_fake_app())

    assert dispatched == [
        ({"userId": 1}, redis_sub.handle_wa_disconnected),
        ({"userId": 2}, redis_sub.handle_qr_event),
    ]
