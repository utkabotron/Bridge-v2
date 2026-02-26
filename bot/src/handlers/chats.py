"""Handler for /chats, /add, /pause, /resume commands."""
from __future__ import annotations

import logging
import os

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..db import get_chat_pairs, set_chat_pair_status, add_chat_pair, is_whitelisted
from ..onboarding.wizard import finish_onboarding
from ..templates.messages import render

logger = logging.getLogger(__name__)
WA_SERVICE_URL = os.getenv("WA_SERVICE_URL", "http://wa-service:3000")


# ── /chats ────────────────────────────────────────────────

async def cmd_chats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg_id = update.effective_user.id
    if not await is_whitelisted(tg_id):
        await update.message.reply_text(render("not_authorized"), parse_mode="Markdown")
        return

    pairs = await get_chat_pairs(tg_id)
    if not pairs:
        await update.message.reply_text(render("chats_empty"), parse_mode="Markdown")
        return

    lines = [render("chats_header")]
    buttons = []
    for i, p in enumerate(pairs, 1):
        status = "active" if p["status"] == "active" else "paused"
        lines.append(render("chat_item", idx=i, wa_name=p["wa_chat_name"], tg_title=p["tg_chat_title"], status=status))

        action = "pause" if p["status"] == "active" else "resume"
        buttons.append([
            InlineKeyboardButton(
                f"{'⏸' if action == 'pause' else '▶️'} {p['wa_chat_name'][:30]}",
                callback_data=f"chat:{action}:{p['id']}",
            )
        ])

    await update.message.reply_text(
        "".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── /add (must be called from inside a TG group) ──────────

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    tg_id = update.effective_user.id
    chat = update.effective_chat

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(render("add_group_only"), parse_mode="Markdown")
        return

    if not await is_whitelisted(tg_id):
        await update.message.reply_text(render("not_authorized"), parse_mode="Markdown")
        return

    # Fetch WA status + groups
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{WA_SERVICE_URL}/status/{tg_id}")
            data = r.json()
    except Exception as exc:
        logger.error("WA status error: %s", exc)
        await update.message.reply_text(render("error_wa_service"), parse_mode="Markdown")
        return

    if not data.get("isReady"):
        await update.message.reply_text(render("add_not_connected"), parse_mode="Markdown")
        return

    groups = data.get("groups", [])
    if not groups:
        await update.message.reply_text(render("error_no_wa_groups"), parse_mode="Markdown")
        return

    # Store tg_chat context and group list for callback
    ctx.user_data["linking_tg_chat_id"] = chat.id
    ctx.user_data["linking_tg_chat_title"] = chat.title or str(chat.id)
    ctx.user_data["wa_groups"] = groups[:20]

    # Use index as callback_data to stay within 64-byte Telegram limit
    kb = [
        [InlineKeyboardButton(g["name"][:50], callback_data=f"link:{i}")]
        for i, g in enumerate(groups[:20])
    ]

    await update.message.reply_text(
        render("add_select_header", tg_group=chat.title or "this group"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── Callback: link a WA chat ──────────────────────────────

async def cb_link_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(":", 1)[1])
    tg_id = query.from_user.id

    wa_groups = ctx.user_data.get("wa_groups", [])
    if idx >= len(wa_groups):
        await query.edit_message_text("Session expired. Please run /add again.")
        return

    wa_chat_id = wa_groups[idx]["id"]
    wa_chat_name = wa_groups[idx]["name"]

    tg_chat_id = ctx.user_data.get("linking_tg_chat_id")
    tg_chat_title = ctx.user_data.get("linking_tg_chat_title", "")

    if not tg_chat_id:
        await query.edit_message_text("Session expired. Please run /add again.")
        return

    await finish_onboarding(tg_id, wa_chat_id, wa_chat_name, tg_chat_id, tg_chat_title)
    await query.edit_message_text(
        render("add_success", wa_name=wa_chat_name, tg_title=tg_chat_title),
        parse_mode="Markdown",
    )


# ── Callback: pause / resume chat ─────────────────────────

async def cb_chat_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, action, pair_id_str = query.data.split(":", 2)
    pair_id = int(pair_id_str)

    new_status = "paused" if action == "pause" else "active"
    await set_chat_pair_status(pair_id, new_status)

    msg = render("chat_paused") if new_status == "paused" else render("chat_resumed")
    await query.edit_message_text(msg, parse_mode="Markdown")
