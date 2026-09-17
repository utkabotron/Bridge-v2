"""Handler for /chats, /add, /pause, /resume commands."""
from __future__ import annotations

import logging
import os

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ..db import (
    get_chat_pairs,
    get_pair_owned,
    is_whitelisted,
    set_chat_pair_status_owned,
    set_pair_language_owned,
    toggle_pair_summary_owned,
)
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
        lines.append(render("chat_item", escape=True, idx=i, wa_name=p["wa_chat_name"], tg_title=p["tg_chat_title"], status=status))

        action = "pause" if p["status"] == "active" else "resume"
        buttons.append([
            InlineKeyboardButton(
                f"{'⏸' if action == 'pause' else '▶️'} {p['wa_chat_name'][:24]}",
                callback_data=f"chat:{action}:{p['id']}",
            ),
            # Per-bridge settings: language and daily summary. Until now both were
            # reachable only by editing the database by hand.
            InlineKeyboardButton("⚙️", callback_data=f"chat:settings:{p['id']}"),
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
    # effective_message works whether /add came as a command or via the cmd:add button.
    reply = update.effective_message.reply_text

    if chat.type not in ("group", "supergroup"):
        await reply(render("add_group_only"), parse_mode="Markdown")
        return

    if not await is_whitelisted(tg_id):
        await reply(render("not_authorized"), parse_mode="Markdown")
        return

    # Fetch WA status + groups
    try:
        from ..utils.http_client import get as http_get, internal_headers
        r = await http_get(f"{WA_SERVICE_URL}/status/{tg_id}", timeout=10, headers=internal_headers(tg_id))
        data = r.json()
    except Exception as exc:
        logger.error("WA status error: %s", exc)
        await reply(render("error_wa_service"), parse_mode="Markdown")
        return

    if not data.get("isReady"):
        await reply(render("add_not_connected"), parse_mode="Markdown")
        return

    groups = data.get("groups", [])
    if not groups:
        await reply(render("error_no_wa_groups"), parse_mode="Markdown")
        return

    # Store the WA group list keyed by THIS TG chat, so a second /add in another group
    # can't overwrite it and make an old keyboard link the wrong pair. The target TG chat
    # is read back from the callback message itself (see cb_link_chat), not from user_data.
    groups_by_chat = ctx.user_data.setdefault("wa_groups_by_chat", {})
    groups_by_chat[chat.id] = groups[:20]

    # Use index as callback_data to stay within 64-byte Telegram limit
    kb = [
        [InlineKeyboardButton(g["name"][:50], callback_data=f"link:{i}")]
        for i, g in enumerate(groups[:20])
    ]

    await reply(
        render("add_select_header", escape=True, tg_group=chat.title or "this group"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── Callback: link a WA chat ──────────────────────────────

async def cb_link_chat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    tg_id = query.from_user.id
    if not await is_whitelisted(tg_id):
        await query.answer(render("not_authorized"), show_alert=True)
        return

    idx = int(query.data.split(":", 1)[1])

    # The target TG chat is the chat the button lives in — immune to a concurrent /add in
    # another group overwriting shared state.
    tg_chat = query.message.chat
    tg_chat_id = tg_chat.id
    tg_chat_title = tg_chat.title or str(tg_chat_id)

    wa_groups = ctx.user_data.get("wa_groups_by_chat", {}).get(tg_chat_id, [])
    if idx >= len(wa_groups):
        await query.edit_message_text("Session expired. Please run /add again.")
        return

    wa_chat_id = wa_groups[idx]["id"]
    wa_chat_name = wa_groups[idx]["name"]

    await finish_onboarding(tg_id, wa_chat_id, wa_chat_name, tg_chat_id, tg_chat_title)
    await query.edit_message_text(
        render("add_success", escape=True, wa_name=wa_chat_name, tg_title=tg_chat_title),
        parse_mode="Markdown",
    )


# ── Callback: pause / resume chat ─────────────────────────

# Offered per bridge; picking one clears nothing else. The account-wide language stays
# the fallback for bridges that never set their own.
_LANGUAGES = [
    ("Russian", "Русский"),
    ("Hebrew", "עברית"),
    ("English", "English"),
    ("Ukrainian", "Українська"),
    ("Spanish", "Español"),
]


async def _render_settings(query, tg_id: int, pair_id: int) -> None:
    """Show one bridge's settings: translation language and the daily summary switch."""
    pair = await get_pair_owned(pair_id, tg_id)
    if not pair:
        await query.answer(render("not_authorized"), show_alert=True)
        return

    summary_on = pair["summary_enabled"]
    rows = [[InlineKeyboardButton(
        f"{'🔔' if summary_on else '🔕'} Сводка дня: {'вкл' if summary_on else 'выкл'}",
        callback_data=f"chat:summary:{pair_id}",
    )]]
    for code, label in _LANGUAGES:
        mark = "✅ " if pair["effective_language"] == code else ""
        rows.append([InlineKeyboardButton(f"{mark}{label}", callback_data=f"chat:lang:{pair_id}:{code}")])
    rows.append([InlineKeyboardButton("⬅️ Закрыть", callback_data=f"chat:close:{pair_id}")])

    inherited = "" if pair.get("target_language") else " — по умолчанию аккаунта"
    text = (
        f"*{pair['wa_chat_name']}* → {pair['tg_chat_title']}\n\n"
        f"Язык перевода: *{pair['effective_language']}*{inherited}"
    )
    await query.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup(rows))


async def cb_chat_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    tg_id = query.from_user.id
    parts = query.data.split(":")
    action = parts[1]
    pair_id = int(parts[2])

    if action == "settings":
        await _render_settings(query, tg_id, pair_id)
        return

    if action == "lang":
        language = parts[3]
        if not await set_pair_language_owned(pair_id, tg_id, language):
            await query.answer(render("not_authorized"), show_alert=True)
            return
        await _render_settings(query, tg_id, pair_id)
        return

    if action == "summary":
        new_state = await toggle_pair_summary_owned(pair_id, tg_id)
        if new_state is None:
            await query.answer(render("not_authorized"), show_alert=True)
            return
        await _render_settings(query, tg_id, pair_id)
        return

    if action == "close":
        await query.edit_message_text(render("chat_settings_closed"), parse_mode="Markdown")
        return

    new_status = "paused" if action == "pause" else "active"
    # Ownership-scoped: a forged chat:pause:<id> for someone else's pair updates nothing.
    ok = await set_chat_pair_status_owned(pair_id, tg_id, new_status)
    if not ok:
        await query.answer(render("not_authorized"), show_alert=True)
        return

    msg = render("chat_paused") if new_status == "paused" else render("chat_resumed")
    await query.edit_message_text(msg, parse_mode="Markdown")


# ── /done (link pending WA group to this TG group) ───────

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    tg_id = update.effective_user.id

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            render("onboarding_done_group_only"), parse_mode="Markdown"
        )
        return

    if not await is_whitelisted(tg_id):
        await update.message.reply_text(render("not_authorized"), parse_mode="Markdown")
        return

    pending = ctx.user_data.get("pending_wa_chat")
    if not pending:
        await update.message.reply_text(
            render("onboarding_done_no_pending"), parse_mode="Markdown"
        )
        return

    wa_chat_id = pending["wa_chat_id"]
    wa_chat_name = pending["wa_chat_name"]

    await finish_onboarding(tg_id, wa_chat_id, wa_chat_name, chat.id, chat.title or str(chat.id))

    # Clear pending data
    ctx.user_data.pop("pending_wa_chat", None)

    await update.message.reply_text(
        render("onboarding_done_success", escape=True, wa_name=wa_chat_name, tg_title=chat.title or str(chat.id)),
        parse_mode="Markdown",
    )
