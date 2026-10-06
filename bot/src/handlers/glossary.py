"""Admin review of proposed glossary names: the buttons under the morning digest's
"Имена на одобрение" message (built by bridge_shared.glossary_review, sent by analytics).

  ✅  accept the resolver's rendering         ✏️  reply with your own        ❌  not a name

Decisions are `verified` / `rejected` with decided_by = admin. ✏️ also stores `verified`,
not `locked`: a locked entry applies in every chat, and a name that is also an everyday word
(עמוס "busy") must stay in the chats that know it. The processor picks a decision up within
GLOSSARY_REFRESH_SECONDS.

Nothing is kept in memory: the batch is in the message's last button, and the ✏️ question
carries a `ref g:` line naming the entry and the message to refresh.
"""
from __future__ import annotations

import logging

from bridge_shared import glossary_review as review
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, filters

from ..db import glossary_decide, glossary_next_batch, glossary_proposed_count, glossary_rows, get_user

logger = logging.getLogger(__name__)


def _markup(keyboard) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                 for row in keyboard])


def _callback_datas(message) -> list[str]:
    markup = getattr(message, "reply_markup", None)
    return [b.callback_data for row in (markup.inline_keyboard if markup else ()) for b in row]


async def _is_admin(tg_user_id: int) -> bool:
    user = await get_user(tg_user_id)
    return bool(user and user.get("is_admin"))


async def _render(ids: list[int]) -> tuple[str, InlineKeyboardMarkup]:
    rows = await glossary_rows(ids)
    text, keyboard = review.build(rows, await glossary_proposed_count(ids))
    return text, _markup(keyboard)


async def _refresh(bot, chat_id: int, message_id: int, ids: list[int]) -> None:
    text, markup = await _render(ids)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                    parse_mode="HTML", reply_markup=markup)
    except Exception as exc:  # "message is not modified", or the message is gone
        logger.info("Review message %s not refreshed: %s", message_id, exc)


async def cb_glossary(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not await _is_admin(update.effective_user.id):
        await query.answer("Access denied.", show_alert=True)
        return

    _, action, arg = query.data.split(":", 2)
    ids = review.batch_ids(_callback_datas(query.message))

    if action == "more":
        rows, remaining = await glossary_next_batch(ids, review.BATCH)
        if not rows:
            await query.answer("Больше имён на одобрение нет")
            await _refresh(ctx.bot, query.message.chat_id, query.message.message_id, ids)
            return
        await query.answer()
        text, keyboard = review.build(rows, remaining)
        await ctx.bot.send_message(query.message.chat_id, text, parse_mode="HTML",
                                   reply_markup=_markup(keyboard))
        return

    entry_id = review.from36(arg)
    if action == "ed":
        rows = await glossary_rows([entry_id])
        if not rows or rows[0]["status"] != "proposed":
            await query.answer("Уже решено")
            await _refresh(ctx.bot, query.message.chat_id, query.message.message_id, ids)
            return
        await query.answer()
        await ctx.bot.send_message(
            query.message.chat_id, review.edit_prompt(rows[0], query.message.message_id, ids),
            parse_mode="HTML", reply_markup=ForceReply(input_field_placeholder=rows[0]["source"]),
        )
        return

    status = {"ok": "verified", "no": "rejected"}.get(action)
    if status is None:
        await query.answer()
        return
    decided = await glossary_decide(entry_id, status)
    await query.answer(("✅ " if status == "verified" else "❌ ") + (decided["source"] if decided else "уже решено"))
    await _refresh(ctx.bot, query.message.chat_id, query.message.message_id, ids)


class _GlossaryEditReply(filters.MessageFilter):
    """A private reply to the bot's ✏️ question (it ends with a `ref g:` line)."""

    def filter(self, message) -> bool:
        replied = message.reply_to_message
        return bool(replied and replied.from_user and replied.from_user.is_bot
                    and review.parse_ref(replied.text or ""))


GLOSSARY_EDIT_REPLY = _GlossaryEditReply()


async def handle_glossary_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not await _is_admin(update.effective_user.id):
        return
    entry_id, review_message_id, ids = review.parse_ref(message.reply_to_message.text)
    rendering = " ".join((message.text or "").split())
    if not rendering:
        await message.reply_text("Пустой вариант — ничего не сохранил.")
        return
    decided = await glossary_decide(entry_id, "verified", rendering)
    if decided is None:
        await message.reply_text("Это имя уже решено — ничего не менял.")
    else:
        scope = (" Пишется как обычное слово — будет работать только в чатах, где встречалось."
                 if decided.get("also_word") else "")
        await message.reply_text(f"✏️ Сохранил: {decided['source']} → {decided['translation']}.{scope}")
    await _refresh(ctx.bot, message.chat_id, review_message_id, ids)
