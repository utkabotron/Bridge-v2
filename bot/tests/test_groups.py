"""Tests for tracking the Telegram groups a user may link.

The previous implementation kept this in a Redis hash keyed by whoever added the bot,
with a one-hour TTL, written only from the my_chat_member event. A group the bot was
already in never appeared, a group added by another admin landed under that admin's key,
and an hour later the list emptied itself — so the Mini App's picker was usually blank.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _chat(chat_id=-100123, title="Work RU", chat_type="supergroup"):
    chat = MagicMock()
    chat.id = chat_id
    chat.title = title
    chat.type = chat_type
    return chat


def _admin(user_id, status="administrator", is_bot=False):
    member = MagicMock()
    member.status = status
    member.user.id = user_id
    member.user.is_bot = is_bot
    return member


def _bot(admins):
    bot = MagicMock()
    bot.get_chat_administrators = AsyncMock(return_value=admins)
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture(autouse=True)
def _clear_throttle():
    from bot.src.handlers import groups
    groups._last_sync.clear()
    yield
    groups._last_sync.clear()


@pytest.mark.asyncio
async def test_group_is_recorded_for_every_human_admin():
    """Any admin may link the group, so each gets a row — not just whoever added the bot."""
    from bot.src.handlers.groups import sync_group

    replace = AsyncMock()
    with patch("bot.src.handlers.groups.replace_tg_group_admins", new=replace):
        await sync_group(_bot([_admin(11, "creator"), _admin(22), _admin(99, is_bot=True)]), _chat())

    chat_id, title, rows = replace.await_args.args
    assert chat_id == -100123
    assert title == "Work RU"
    assert rows == [(11, "creator"), (22, "administrator")]  # the bot itself is excluded


@pytest.mark.asyncio
async def test_ordinary_group_traffic_registers_a_long_joined_group():
    """The only way a group the bot joined long ago can reach the picker: there is no
    event to replay and no API to list the bot's own chats."""
    from bot.src.handlers.groups import handle_group_message

    update = MagicMock()
    update.effective_chat = _chat()
    ctx = MagicMock()
    ctx.bot = _bot([_admin(11)])

    with patch("bot.src.handlers.groups.replace_tg_group_admins", new=AsyncMock()) as replace:
        await handle_group_message(update, ctx)

    replace.assert_awaited_once()


@pytest.mark.asyncio
async def test_repeat_traffic_is_throttled():
    """A busy group must not call getChatAdministrators on every message."""
    from bot.src.handlers.groups import sync_group

    bot = _bot([_admin(11)])
    with patch("bot.src.handlers.groups.replace_tg_group_admins", new=AsyncMock()):
        await sync_group(bot, _chat())
        await sync_group(bot, _chat())
        await sync_group(bot, _chat())

    assert bot.get_chat_administrators.await_count == 1


@pytest.mark.asyncio
async def test_membership_change_bypasses_the_throttle():
    from bot.src.handlers.groups import sync_group

    bot = _bot([_admin(11)])
    with patch("bot.src.handlers.groups.replace_tg_group_admins", new=AsyncMock()):
        await sync_group(bot, _chat())
        await sync_group(bot, _chat(), force=True)

    assert bot.get_chat_administrators.await_count == 2


@pytest.mark.asyncio
async def test_a_failure_to_list_admins_is_not_fatal_and_retries():
    """Usually "not enough rights": the bridge still works, so this must not raise — and
    it must not poison the throttle, or one failure would blind us for an hour."""
    from bot.src.handlers.groups import sync_group

    bot = MagicMock()
    bot.get_chat_administrators = AsyncMock(side_effect=RuntimeError("not enough rights"))

    with patch("bot.src.handlers.groups.replace_tg_group_admins", new=AsyncMock()) as replace:
        await sync_group(bot, _chat())
        await sync_group(bot, _chat())

    replace.assert_not_awaited()
    assert bot.get_chat_administrators.await_count == 2


@pytest.mark.asyncio
async def test_removal_forgets_the_group():
    from bot.src.handlers.groups import handle_my_chat_member

    update = MagicMock()
    update.my_chat_member.chat = _chat()
    update.my_chat_member.new_chat_member.status = "kicked"
    update.my_chat_member.old_chat_member.status = "administrator"
    ctx = MagicMock()
    ctx.bot = _bot([])

    with patch("bot.src.handlers.groups.delete_tg_group", new=AsyncMock()) as delete:
        await handle_my_chat_member(update, ctx)

    delete.assert_awaited_once_with(-100123)


@pytest.mark.asyncio
async def test_joining_records_the_group_and_greets_once():
    from bot.src.handlers.groups import handle_my_chat_member

    update = MagicMock()
    update.my_chat_member.chat = _chat()
    update.my_chat_member.new_chat_member.status = "administrator"
    update.my_chat_member.old_chat_member.status = "left"
    ctx = MagicMock()
    ctx.bot = _bot([_admin(11)])

    with patch("bot.src.handlers.groups.replace_tg_group_admins", new=AsyncMock()) as replace:
        await handle_my_chat_member(update, ctx)

    replace.assert_awaited_once()
    ctx.bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_chats_are_never_recorded():
    from bot.src.handlers.groups import sync_group

    bot = _bot([_admin(11)])
    with patch("bot.src.handlers.groups.replace_tg_group_admins", new=AsyncMock()) as replace:
        await sync_group(bot, _chat(chat_type="private"))

    replace.assert_not_awaited()
    bot.get_chat_administrators.assert_not_awaited()
