"""Tests for bot handlers: admin and direct translate."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_update(user_id=1, text=""):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.from_user.id = user_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_ctx(args=None):
    ctx = MagicMock()
    ctx.args = args or []
    ctx.bot.send_message = AsyncMock()
    return ctx


# ── admin_only decorator ───────────────────────────────────

@pytest.mark.asyncio
async def test_admin_only_rejects_non_admin():
    from bot.src.handlers.admin import cmd_users
    update = _make_update()
    ctx = _make_ctx()

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": False})):
        await cmd_users(update, ctx)

    update.message.reply_text.assert_called_once_with("Access denied.")


@pytest.mark.asyncio
async def test_admin_only_rejects_unknown_user():
    from bot.src.handlers.admin import cmd_users
    update = _make_update()
    ctx = _make_ctx()

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value=None)):
        await cmd_users(update, ctx)

    update.message.reply_text.assert_called_once_with("Access denied.")


# ── /broadcast ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_broadcast_no_args():
    from bot.src.handlers.admin import cmd_broadcast
    update = _make_update()
    ctx = _make_ctx(args=[])

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})):
        await cmd_broadcast(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_broadcast_sends_to_active_only():
    from bot.src.handlers.admin import cmd_broadcast
    update = _make_update()
    ctx = _make_ctx(args=["Hello", "everyone"])

    users = [
        {"tg_user_id": 111, "is_active": True},
        {"tg_user_id": 222, "is_active": True},
        {"tg_user_id": 333, "is_active": False},
    ]

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})), \
         patch("bot.src.handlers.admin.get_all_users", new=AsyncMock(return_value=users)):
        await cmd_broadcast(update, ctx)

    assert ctx.bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_cmd_broadcast_counts_failures():
    from bot.src.handlers.admin import cmd_broadcast
    update = _make_update()
    ctx = _make_ctx(args=["test"])
    ctx.bot.send_message = AsyncMock(side_effect=Exception("blocked"))

    users = [{"tg_user_id": 111, "is_active": True}]

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})), \
         patch("bot.src.handlers.admin.get_all_users", new=AsyncMock(return_value=users)):
        await cmd_broadcast(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "1 failed" in reply


# ── /users ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_users_empty():
    from bot.src.handlers.admin import cmd_users
    update = _make_update()
    ctx = _make_ctx()

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})), \
         patch("bot.src.handlers.admin.get_all_users", new=AsyncMock(return_value=[])):
        await cmd_users(update, ctx)

    update.message.reply_text.assert_called_once_with("No users yet.")


@pytest.mark.asyncio
async def test_cmd_users_lists_users():
    from bot.src.handlers.admin import cmd_users
    update = _make_update()
    ctx = _make_ctx()

    users = [
        {"tg_user_id": 111, "is_active": True, "tg_username": "alice"},
        {"tg_user_id": 222, "is_active": False, "tg_username": None},
    ]

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})), \
         patch("bot.src.handlers.admin.get_all_users", new=AsyncMock(return_value=users)):
        await cmd_users(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "111" in reply


# ── /whitelist ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_whitelist_no_args():
    from bot.src.handlers.admin import cmd_whitelist
    update = _make_update()
    ctx = _make_ctx(args=[])

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})):
        await cmd_whitelist(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_whitelist_invalid_action():
    from bot.src.handlers.admin import cmd_whitelist
    update = _make_update()
    ctx = _make_ctx(args=["delete", "123"])

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})):
        await cmd_whitelist(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "Usage" in reply


@pytest.mark.asyncio
async def test_cmd_whitelist_invalid_user_id():
    from bot.src.handlers.admin import cmd_whitelist
    update = _make_update()
    ctx = _make_ctx(args=["add", "notanumber"])

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})):
        await cmd_whitelist(update, ctx)

    reply = update.message.reply_text.call_args[0][0]
    assert "Invalid" in reply


@pytest.mark.asyncio
async def test_cmd_whitelist_add():
    from bot.src.handlers.admin import cmd_whitelist
    update = _make_update()
    ctx = _make_ctx(args=["add", "99999", "newuser"])

    with patch("bot.src.handlers.admin.get_user", new=AsyncMock(return_value={"is_admin": True})), \
         patch("bot.src.handlers.admin.add_to_whitelist", new=AsyncMock()):
        await cmd_whitelist(update, ctx)

    update.message.reply_text.assert_called_once()


# ── handle_direct_text ────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_direct_text_success():
    from bot.src.handlers.translate import handle_direct_text

    preview_msg = AsyncMock()
    update = _make_update(user_id=42, text="שלום")
    update.message.reply_text = AsyncMock(return_value=preview_msg)
    ctx = MagicMock()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "translated": "Привет",
        "target_language": "Russian",
        "translation_ms": 150,
    }

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("bot.src.handlers.translate.httpx.AsyncClient", return_value=mock_client):
        await handle_direct_text(update, ctx)

    preview_msg.edit_text.assert_called_once()
    text = preview_msg.edit_text.call_args[0][0]
    assert "Привет" in text


@pytest.mark.asyncio
async def test_handle_direct_text_processor_error():
    from bot.src.handlers.translate import handle_direct_text

    preview_msg = AsyncMock()
    update = _make_update(user_id=42, text="hello")
    update.message.reply_text = AsyncMock(return_value=preview_msg)
    ctx = MagicMock()

    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("bot.src.handlers.translate.httpx.AsyncClient", return_value=mock_client):
        await handle_direct_text(update, ctx)

    text = preview_msg.edit_text.call_args[0][0]
    assert "❌" in text


@pytest.mark.asyncio
async def test_handle_direct_text_timeout():
    import httpx
    from bot.src.handlers.translate import handle_direct_text

    preview_msg = AsyncMock()
    update = _make_update(user_id=42, text="hello")
    update.message.reply_text = AsyncMock(return_value=preview_msg)
    ctx = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("bot.src.handlers.translate.httpx.AsyncClient", return_value=mock_client):
        await handle_direct_text(update, ctx)

    text = preview_msg.edit_text.call_args[0][0]
    assert "❌" in text


@pytest.mark.asyncio
async def test_handle_direct_text_empty_message():
    """Empty text should return early without calling processor."""
    from bot.src.handlers.translate import handle_direct_text

    update = MagicMock()
    update.message.text = "   "
    update.message.from_user.id = 42
    update.message.reply_text = AsyncMock()
    ctx = MagicMock()

    await handle_direct_text(update, ctx)

    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handle_direct_text_no_message():
    from bot.src.handlers.translate import handle_direct_text

    update = MagicMock()
    update.message = None
    ctx = MagicMock()

    await handle_direct_text(update, ctx)  # must not raise
