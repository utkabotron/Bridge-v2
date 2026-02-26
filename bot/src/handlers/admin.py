"""Admin-only handlers: /whitelist, /users, /broadcast."""
from __future__ import annotations

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

from ..db import add_to_whitelist, get_all_users, get_user
from ..templates.messages import render

logger = logging.getLogger(__name__)

def admin_only(handler):
    """Decorator: reject non-admins (checks is_admin flag in DB)."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from ..db import get_user
        u = await get_user(update.effective_user.id)
        if not u or not u.get("is_admin"):
            await update.message.reply_text("Access denied.")
            return
        return await handler(update, ctx)
    return wrapper


# ── /whitelist add <user_id or @username> ─────────────────

@admin_only
async def cmd_whitelist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    if len(args) < 2 or args[0] not in ("add", "remove"):
        await update.message.reply_text(
            "Usage: /whitelist add <tg_user_id> [@username]"
        )
        return

    action = args[0]
    try:
        tg_user_id = int(args[1].lstrip("@"))
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    username = args[2].lstrip("@") if len(args) > 2 else None

    if action == "add":
        await add_to_whitelist(tg_user_id, username)
        await update.message.reply_text(
            render("admin_whitelist_added", username=username or str(tg_user_id)),
            parse_mode="Markdown",
        )
    else:
        # remove = deactivate
        from ..db import get_pool
        pool = await get_pool()
        await pool.execute(
            "update public.users set is_active = false where tg_user_id = $1", tg_user_id
        )
        await update.message.reply_text(
            render("admin_whitelist_removed", username=username or str(tg_user_id)),
            parse_mode="Markdown",
        )


# ── /users ────────────────────────────────────────────────

@admin_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    users = await get_all_users()
    if not users:
        await update.message.reply_text("No users yet.")
        return

    lines = [render("admin_users_header")]
    for u in users:
        status = "active" if u["is_active"] else "inactive"
        lines.append(
            render(
                "admin_user_item",
                tg_user_id=u["tg_user_id"],
                username=u.get("tg_username") or "—",
                status=status,
            )
        )

    await update.message.reply_text("".join(lines), parse_mode="Markdown")


# ── /broadcast <message> ──────────────────────────────────

@admin_only
async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    text = " ".join(ctx.args)
    users = await get_all_users()
    active = [u for u in users if u["is_active"]]

    sent, failed = 0, 0
    for u in active:
        try:
            await ctx.bot.send_message(chat_id=u["tg_user_id"], text=text)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"Broadcast done: {sent} sent, {failed} failed.")
