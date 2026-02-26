"""5-step onboarding wizard handlers.

Steps:
  1. /start → show welcome + "Connect WhatsApp" button
  2. Bot hits wa-service /connect/:userId → sends QR page URL
  3. Redis pub/sub: wa_connected event → show "Create TG group" instruction
  4. User confirms group created → bot gets added → list WA groups
  5. User selects WA group → INSERT chat_pairs → DONE
"""
from __future__ import annotations

import logging
import os

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..db import (
    add_chat_pair,
    add_to_whitelist,
    count_users,
    create_user,
    get_onboarding_state,
    get_pool,
    is_whitelisted,
    mark_onboarding_done,
    set_onboarding_state,
)
from ..onboarding.states import DONE, IDLE, LINKING, QR_PENDING, WA_CONNECTED
from ..templates.messages import render

logger = logging.getLogger(__name__)

WA_SERVICE_URL = os.getenv("WA_SERVICE_URL", "http://wa-service:3000")


async def _wa_connect(user_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{WA_SERVICE_URL}/connect/{user_id}")
        r.raise_for_status()
        return r.json()


async def _wa_status(user_id: int) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{WA_SERVICE_URL}/status/{user_id}")
        r.raise_for_status()
        return r.json()


# ── Step 1: /start ────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    tg_id = user.id

    # First user ever → auto-promote to admin
    if await count_users() == 0:
        await add_to_whitelist(tg_id, user.username)
        pool = await get_pool()
        await pool.execute(
            "update public.users set is_admin = true where tg_user_id = $1", tg_id
        )
        logger.info("First user %s auto-promoted to admin", tg_id)

    whitelisted = await is_whitelisted(tg_id)
    if not whitelisted:
        await update.message.reply_text(render("not_authorized"), parse_mode="Markdown")
        return

    await create_user(tg_id, user.username)
    state = await get_onboarding_state(tg_id)

    if state == DONE:
        kb = [
            [InlineKeyboardButton("📋 My chats", callback_data="nav:chats")],
            [InlineKeyboardButton("➕ Add chat", callback_data="nav:add")],
        ]
        await update.message.reply_text(
            render("welcome_back"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    # First time or incomplete onboarding → step 1
    await set_onboarding_state(tg_id, IDLE)
    kb = [[InlineKeyboardButton("🔗 Connect WhatsApp", callback_data="onboarding:connect_wa")]]
    await update.message.reply_text(
        render("welcome_new"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── Step 2: user pressed "Connect WhatsApp" ───────────────

async def cb_connect_wa(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id

    await set_onboarding_state(tg_id, QR_PENDING)

    try:
        result = await _wa_connect(tg_id)
        qr_url = f"{WA_SERVICE_URL}{result.get('qrPageUrl', f'/qr/page/{tg_id}')}"
        # Replace internal hostname with public URL if set
        public_wa = os.getenv("WA_SERVICE_PUBLIC_URL", "")
        if public_wa:
            qr_url = qr_url.replace(WA_SERVICE_URL, public_wa)

        text = render("onboarding_step2_wait", qr_url=qr_url)
    except Exception as exc:
        logger.error("WA connect error: %s", exc)
        text = render("error_wa_service")

    await query.edit_message_text(text, parse_mode="Markdown")


# ── Step 3: user confirmed TG group created ───────────────

async def cb_group_created(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id

    await set_onboarding_state(tg_id, LINKING)

    me = await ctx.bot.get_me()
    text = render("onboarding_step3", bot_username=me.username)
    kb = [[InlineKeyboardButton("✅ Bot is in the group", callback_data="onboarding:bot_added")]]

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))


# ── Step 4: bot was added to TG group → show WA group list ─

async def cb_bot_added(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    tg_id = query.from_user.id

    # At this point the user should have the bot in a Telegram group.
    # We ask them to use /add in that group to finish linking.
    text = (
        "✅ *Great!*\n\n"
        "Now go to the Telegram group you just created, and type:\n\n"
        "`/add`\n\n"
        "I'll show you a list of WhatsApp chats to link."
    )
    await query.edit_message_text(text, parse_mode="Markdown")


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
