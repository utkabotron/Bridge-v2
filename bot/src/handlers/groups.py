"""Track groups where the bot is added/removed via my_chat_member events.

Stores group info in Redis so wa-service can serve it to the Mini App.
Key pattern: bot:user_groups:{user_id} → HASH { chat_id: JSON({chat_id, title}) }
TTL: 1 hour (reset on each add).
"""
from __future__ import annotations

import json
import logging
import os

import redis.asyncio as aioredis
from telegram import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..templates.messages import render

logger = logging.getLogger(__name__)

TTL_SECONDS = 3600  # 1 hour

_redis: aioredis.Redis | None = None


def _redis_url() -> str:
    """Build the Redis URL. Prefer REDIS_URL, else compose from REDIS_HOST/PORT/DB — the
    vars actually set in docker-compose. (A hardcoded localhost default made every group
    Redis write fail inside the container, killing the my_chat_member tracking.)"""
    url = os.getenv("REDIS_URL")
    if url:
        return url
    host = os.getenv("REDIS_HOST", "localhost")
    port = os.getenv("REDIS_PORT", "6379")
    db = os.getenv("REDIS_DB", "0")
    return f"redis://{host}:{port}/{db}"


async def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(_redis_url(), decode_responses=True)
    return _redis


def _key(user_id: int) -> str:
    return f"bot:user_groups:{user_id}"


async def handle_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle my_chat_member updates: bot added/removed from a group."""
    event: ChatMemberUpdated = update.my_chat_member
    if event is None:
        return

    chat = event.chat
    if chat.type not in ("group", "supergroup"):
        return

    from_user = event.from_user
    if from_user is None:
        return

    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status

    key = _key(from_user.id)

    async def _redis_op(coro_factory):
        """Run a Redis op but never let a Redis failure abort the handler (and thus the
        admin greeting). Group tracking is best-effort."""
        try:
            r = await _get_redis()
            await coro_factory(r)
        except Exception as exc:
            logger.warning("Redis group-tracking op failed: %s", exc)

    if new_status in ("member", "administrator") and old_status in ("left", "kicked"):
        # Bot was added to a group
        value = json.dumps({"chat_id": chat.id, "title": chat.title or ""})
        await _redis_op(lambda r: r.hset(key, str(chat.id), value))
        await _redis_op(lambda r: r.expire(key, TTL_SECONDS))
        logger.info("Bot added to group %s (%s) by user %s", chat.id, chat.title, from_user.id)

        # If added as admin → send ready message with /add hint
        if new_status == "administrator":
            try:
                kb = [[InlineKeyboardButton("➕ Link WhatsApp group", callback_data="cmd:add")]]
                await ctx.bot.send_message(
                    chat_id=chat.id,
                    text=render("bot_added_as_admin"),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb),
                )
            except Exception as exc:
                logger.warning("Could not send admin message to %s: %s", chat.id, exc)

    elif new_status == "administrator" and old_status == "member":
        # Bot promoted to admin in existing group
        logger.info("Bot promoted to admin in group %s (%s) by user %s", chat.id, chat.title, from_user.id)
        try:
            kb = [[InlineKeyboardButton("➕ Link WhatsApp group", callback_data="cmd:add")]]
            await ctx.bot.send_message(
                chat_id=chat.id,
                text=render("bot_added_as_admin"),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception as exc:
            logger.warning("Could not send admin message to %s: %s", chat.id, exc)

    elif new_status in ("left", "kicked") and old_status in ("member", "administrator"):
        # Bot was removed from a group
        await _redis_op(lambda r: r.hdel(key, str(chat.id)))
        logger.info("Bot removed from group %s (%s) by user %s", chat.id, chat.title, from_user.id)


async def cb_cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'Link WhatsApp group' button (callback_data='cmd:add'). Without a
    registered handler this button spun forever. Delegates to the /add flow in-place."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    from .chats import cmd_add
    await cmd_add(update, ctx)
