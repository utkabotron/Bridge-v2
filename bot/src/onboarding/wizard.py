"""Entry point and pair creation.

/start is the only entry: it hands the user the Mini App, which drives connecting
WhatsApp and linking chats over HTTP. The old inline wizard (connect → create group →
confirm → pick) was unreachable — nothing emitted its first callback — and the Mini App's
tg.sendData() path never worked either, because sendData only reaches the bot from a
reply-keyboard button and the app opens from an inline one.

finish_onboarding stays: /add inside a group still uses it.
"""
from __future__ import annotations

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import ContextTypes

from ..db import (
    add_chat_pair,
    add_to_whitelist,
    create_user,
    get_chat_pairs,
    get_onboarding_state,
    get_pool,
    is_whitelisted,
    mark_onboarding_done,
    set_onboarding_state,
)
from ..onboarding.states import DONE
from ..templates.messages import render

logger = logging.getLogger(__name__)

WA_SERVICE_URL = os.getenv("WA_SERVICE_URL", "http://wa-service:3000")
MINIAPP_URL = os.getenv("WA_SERVICE_PUBLIC_URL", "http://localhost:3000") + "/miniapp"


# ── /start ────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    tg_id = user.id

    # Bootstrap admins from ADMIN_TG_IDS. This used to promote whoever sent /start first
    # while the users table was empty — so a stranger who found the bot right after a
    # volume reset or a fresh deploy inherited /whitelist, /users and /broadcast.
    admin_ids = [int(x.strip()) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()]
    if tg_id in admin_ids:
        await add_to_whitelist(tg_id, user.username)
        pool = await get_pool()
        await pool.execute(
            "update public.users set is_admin = true where tg_user_id = $1", tg_id
        )
        logger.info("Bootstrapped admin %s from ADMIN_TG_IDS", tg_id)

    whitelisted = await is_whitelisted(tg_id)
    if not whitelisted:
        await update.message.reply_text(render("not_authorized"), parse_mode="Markdown")
        return

    await create_user(tg_id, user.username)

    # Fallback: migrated users without onboarding record
    state = await get_onboarding_state(tg_id)
    if state != DONE:
        pairs = await get_chat_pairs(tg_id)
        if pairs:
            logger.info("Fallback: user %s has %d chat pairs but state=%s, marking done", tg_id, len(pairs), state)
            await set_onboarding_state(tg_id, DONE)

    # Always show description + Mini App button
    me = await ctx.bot.get_me()
    miniapp_url = f"{MINIAPP_URL}?bot={me.username}"
    kb = [[InlineKeyboardButton("📱 Open Mini App", web_app=WebAppInfo(url=miniapp_url))]]
    await update.message.reply_text(
        render("welcome_start"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── Chat pair selection (called from /add in group) ───────

async def finish_onboarding(
    tg_user_id: int,
    wa_chat_id: str,
    wa_chat_name: str,
    tg_chat_id: int,
    tg_chat_title: str,
) -> None:
    await add_chat_pair(tg_user_id, wa_chat_id, wa_chat_name, tg_chat_id, tg_chat_title)
    await mark_onboarding_done(tg_user_id)
