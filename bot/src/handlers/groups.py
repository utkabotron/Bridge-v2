"""Track the Telegram groups the bot belongs to, so the Mini App can offer them.

This used to live in a Redis hash keyed by whoever added the bot, with a one-hour TTL,
written only on the my_chat_member event. A group the bot was already in never produced
that event and so never appeared; a group added by another admin landed under that admin's
key; and an hour later the list emptied on its own. The picker was blank for most users.

Now it is a table (tg_groups), one row per (group, admin), refreshed from three places:
the membership event, a throttled peek at ordinary group messages — which is what finally
picks up groups the bot joined long ago — and /add.
"""
from __future__ import annotations

import logging
import time

from telegram import Chat, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..db import delete_tg_group, replace_tg_group_admins
from ..templates.messages import render

logger = logging.getLogger(__name__)

# A busy group must not trigger getChatAdministrators on every message.
SYNC_INTERVAL_SECONDS = 3600
_last_sync: dict[int, float] = {}


async def sync_group(bot, chat: Chat, force: bool = False) -> None:
    """Record this group against every human admin who may link it.

    Best-effort: a Telegram or database hiccup here must never block the message or
    command that triggered it.
    """
    if chat.type not in ("group", "supergroup"):
        return

    now = time.monotonic()
    if not force and now - _last_sync.get(chat.id, 0) < SYNC_INTERVAL_SECONDS:
        return
    _last_sync[chat.id] = now

    try:
        admins = await bot.get_chat_administrators(chat.id)
    except Exception as exc:
        # Most often "not enough rights" — the bot can still bridge, it just cannot
        # enumerate admins, so we leave whatever rows already exist alone.
        logger.warning("Could not list admins of %s: %s", chat.id, exc)
        _last_sync.pop(chat.id, None)
        return

    rows = [
        (member.user.id, "creator" if member.status == "creator" else "administrator")
        for member in admins
        if member.user and not member.user.is_bot
    ]

    try:
        await replace_tg_group_admins(chat.id, chat.title or "", rows)
    except Exception as exc:
        logger.warning("Could not store group %s: %s", chat.id, exc)
        _last_sync.pop(chat.id, None)


async def handle_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh membership from ordinary traffic in a group.

    Without this, a group the bot joined before any of this existed would stay invisible
    to the picker forever — there is no event to replay and no API to list the bot's own
    chats. Throttled to once an hour per group.
    """
    chat = update.effective_chat
    if chat is None:
        return
    await sync_group(ctx.bot, chat)


async def handle_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle my_chat_member updates: bot added to / removed from a group."""
    event: ChatMemberUpdated = update.my_chat_member
    if event is None:
        return

    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status

    if new_status in ("member", "administrator"):
        # force: the whole point of this event is that membership just changed.
        await sync_group(ctx.bot, chat, force=True)
        logger.info("Bot is now %s in group %s (%s)", new_status, chat.id, chat.title)

        # Only greet on a transition into the group or into admin — not on every event.
        became_member = old_status in ("left", "kicked")
        became_admin = new_status == "administrator" and old_status == "member"
        if became_member or became_admin:
            try:
                kb = [[InlineKeyboardButton("➕ Link WhatsApp chat", callback_data="cmd:add")]]
                await ctx.bot.send_message(
                    chat_id=chat.id,
                    text=render("bot_added_as_admin"),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb),
                )
            except Exception as exc:
                logger.warning("Could not send greeting to %s: %s", chat.id, exc)

    elif new_status in ("left", "kicked") and old_status in ("member", "administrator"):
        _last_sync.pop(chat.id, None)
        try:
            await delete_tg_group(chat.id)
        except Exception as exc:
            logger.warning("Could not forget group %s: %s", chat.id, exc)
        logger.info("Bot removed from group %s (%s)", chat.id, chat.title)


async def cb_cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'Link WhatsApp chat' button (callback_data='cmd:add'). Without a
    registered handler this button spun forever. Delegates to the /add flow in-place."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    from .chats import cmd_add
    await cmd_add(update, ctx)
