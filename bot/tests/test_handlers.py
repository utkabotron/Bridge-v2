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

    with patch("bot.src.handlers.translate.http_client.post", new_callable=AsyncMock, return_value=mock_resp), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
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

    with patch("bot.src.handlers.translate.http_client.post", new_callable=AsyncMock, return_value=mock_resp), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
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

    with patch("bot.src.handlers.translate.http_client.post", new_callable=AsyncMock, side_effect=httpx.TimeoutException("timeout")), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
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


# ── Direct translation: language buttons ───────────────────

def _translate_resp(translated="שלום", language="Hebrew", ms=150):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "translated": translated,
        "target_language": language,
        "translation_ms": ms,
    }
    return resp


@pytest.mark.asyncio
async def test_direct_text_answers_in_hebrew_with_an_english_button():
    """Russian is what gets typed, so the profile language (Russian by default) is no use."""
    from bot.src.handlers.translate import handle_direct_text

    preview_msg = AsyncMock()
    update = _make_update(user_id=42, text="Привет, когда встречаемся?")
    update.message.reply_text = AsyncMock(return_value=preview_msg)

    post = AsyncMock(return_value=_translate_resp())
    with patch("bot.src.handlers.translate.http_client.post", new=post), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
        await handle_direct_text(update, MagicMock())

    assert post.await_args.kwargs["json"]["target_language"] == "Hebrew"

    kwargs = preview_msg.edit_text.call_args.kwargs
    text = preview_msg.edit_text.call_args[0][0]
    # Tapping the monospace block copies the translation and nothing else.
    assert "<code>שלום</code>" in text
    assert "150ms" in text

    buttons = kwargs["reply_markup"].inline_keyboard[0]
    labels = [b.text for b in buttons]
    assert "✓ עברית" in labels
    assert "English" in labels
    # The language already on screen is inert; the other one retranslates.
    assert [b.callback_data for b in buttons] == ["noop", "tr:en"]


@pytest.mark.asyncio
async def test_language_button_retranslates_the_message_it_replies_to():
    from bot.src.handlers.translate import cb_translate_lang

    query = AsyncMock()
    query.data = "tr:en"
    query.from_user.id = 42
    query.message = AsyncMock()
    query.message.reply_to_message.text = "Привет, когда встречаемся?"

    update = MagicMock()
    update.callback_query = query

    post = AsyncMock(return_value=_translate_resp("Hi, when are we meeting?", "English", 120))
    with patch("bot.src.handlers.translate.http_client.post", new=post), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
        await cb_translate_lang(update, MagicMock())

    sent = post.await_args.kwargs["json"]
    assert sent["target_language"] == "English"
    # The source text comes off the replied-to message, so a restart does not break it.
    assert sent["text"] == "Привет, когда встречаемся?"

    kwargs = query.message.edit_text.call_args.kwargs
    assert "Hi, when are we meeting?" in query.message.edit_text.call_args[0][0]
    assert [b.callback_data for b in kwargs["reply_markup"].inline_keyboard[0]] == ["tr:he", "noop"]


@pytest.mark.asyncio
async def test_language_button_without_the_original_text_asks_for_it_again():
    from bot.src.handlers.translate import cb_translate_lang

    query = AsyncMock()
    query.data = "tr:en"
    query.from_user.id = 42
    query.message = AsyncMock()
    query.message.reply_to_message = None

    update = MagicMock()
    update.callback_query = query

    post = AsyncMock()
    with patch("bot.src.handlers.translate.http_client.post", new=post), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
        await cb_translate_lang(update, MagicMock())

    post.assert_not_called()
    query.answer.assert_awaited_once()
    assert query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_language_button_is_whitelist_gated():
    """The button costs an LLM call, so a stranger must not be able to press it."""
    from bot.src.handlers.translate import cb_translate_lang

    query = AsyncMock()
    query.data = "tr:en"
    query.from_user.id = 999
    query.message = AsyncMock()

    update = MagicMock()
    update.callback_query = query

    post = AsyncMock()
    with patch("bot.src.handlers.translate.http_client.post", new=post), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=False)):
        await cb_translate_lang(update, MagicMock())

    post.assert_not_called()
    query.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_direct_text_failure_leaves_no_buttons():
    from bot.src.handlers.translate import handle_direct_text

    preview_msg = AsyncMock()
    update = _make_update(user_id=42, text="Привет")
    update.message.reply_text = AsyncMock(return_value=preview_msg)

    resp = MagicMock()
    resp.status_code = 500
    resp.text = "Internal Server Error"

    with patch("bot.src.handlers.translate.http_client.post", new=AsyncMock(return_value=resp)), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
        await handle_direct_text(update, MagicMock())

    assert "❌" in preview_msg.edit_text.call_args[0][0]
    assert "reply_markup" not in preview_msg.edit_text.call_args.kwargs


@pytest.mark.asyncio
async def test_a_failed_button_keeps_the_translation_on_screen():
    """Rewriting the message with an error would cost both the text and the buttons."""
    from bot.src.handlers.translate import cb_translate_lang

    query = AsyncMock()
    query.data = "tr:en"
    query.from_user.id = 42
    query.message = AsyncMock()
    query.message.reply_to_message.text = "Привет"

    update = MagicMock()
    update.callback_query = query

    resp = MagicMock()
    resp.status_code = 503
    resp.text = "Translation is temporarily disabled"

    with patch("bot.src.handlers.translate.http_client.post", new=AsyncMock(return_value=resp)), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
        await cb_translate_lang(update, MagicMock())

    query.message.edit_text.assert_not_called()
    assert query.answer.await_args.kwargs.get("show_alert") is True
    assert "❌" in query.answer.await_args[0][0]


@pytest.mark.asyncio
async def test_direct_text_quotes_the_source_so_the_buttons_can_find_it():
    """Without an explicit quote a private chat gets no reply_to_message, and every
    language button answers 'Send the text again'."""
    from bot.src.handlers.translate import handle_direct_text

    update = _make_update(user_id=42, text="Привет, как дела")
    update.message.message_id = 777
    update.message.reply_text = AsyncMock(return_value=AsyncMock())

    with patch("bot.src.handlers.translate.http_client.post", new=AsyncMock(return_value=_translate_resp())), \
         patch("bot.src.handlers.translate.is_whitelisted", new=AsyncMock(return_value=True)):
        await handle_direct_text(update, MagicMock())

    params = update.message.reply_text.call_args.kwargs["reply_parameters"]
    assert params.message_id == 777
    assert params.allow_sending_without_reply is True
