"""Handlers for direct messages in private chat — text translation & media analysis."""
from __future__ import annotations

import logging
import os

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from ..utils import http_client
from telegram.ext import ContextTypes
from ..utils.telegram_format import esc, italic
from ..config import DIRECT_LANGUAGES, DIRECT_LANG_BY_NAME, DIRECT_LANG_DEFAULT
from ..db import is_whitelisted
from ..templates.messages import render

logger = logging.getLogger(__name__)

PROCESSOR_URL = os.getenv("PROCESSOR_URL", "http://processor:8000")


def _lang_keyboard(delivered_language: str) -> InlineKeyboardMarkup:
    """One button per offered language; the one already shown is inert."""
    active = DIRECT_LANG_BY_NAME.get(delivered_language)
    row = []
    for code, (_, label) in DIRECT_LANGUAGES.items():
        if code == active:
            # Nothing to translate again — reuse the shared no-op callback.
            row.append(InlineKeyboardButton(f"\u2713 {label}", callback_data="noop"))
        else:
            row.append(InlineKeyboardButton(label, callback_data=f"tr:{code}"))
    return InlineKeyboardMarkup([row])


async def _edit(message, text: str, **kwargs) -> None:
    """Rewrite a message, tolerating Telegram's refusal to rewrite it as it already is."""
    try:
        await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
        logger.debug("Translation unchanged, nothing to rewrite")


async def _translate(text: str, user_id: int, language: str) -> tuple[dict | None, str | None]:
    """Ask the processor for a translation. Returns (payload, message for the user)."""
    try:
        r = await http_client.post(
            f"{PROCESSOR_URL}/translate",
            json={"text": text, "user_id": user_id, "target_language": language},
            timeout=30,
        )
    except httpx.TimeoutException:
        logger.error("Processor /translate timeout")
        return None, "❌ Translation timed out. Try again later."
    except Exception as exc:
        logger.error("Translate handler error: %s", exc)
        return None, "❌ Something went wrong. Try again later."

    if r.status_code != 200:
        logger.error("Processor /translate returned %s: %s", r.status_code, r.text)
        return None, "❌ Translation failed. Try again later."

    return r.json(), None


def _render(data: dict, language: str) -> tuple[str, InlineKeyboardMarkup]:
    """The message body and buttons for a finished translation."""
    translated = data.get("translated", "")
    lang = data.get("target_language", "") or language
    ms = data.get("translation_ms")

    # A tap on the monospace block copies the translation and nothing else — pasting it
    # straight into WhatsApp is the entire point of typing it here.
    text = f"<code>{esc(translated)}</code>"
    meta = " · ".join(part for part in (f"{ms}ms" if ms else "", lang) if part)
    if meta:
        text += f"\n\n{italic(meta)}"
    return text, _lang_keyboard(lang)


async def handle_direct_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate text sent directly to the bot in private chat."""
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if not text:
        return

    user_id = msg.from_user.id if msg.from_user else 0
    # Gate the LLM behind the whitelist — otherwise any stranger who finds the bot gets
    # free translation/analysis on our OpenAI bill.
    if not await is_whitelisted(user_id):
        await msg.reply_text(render("not_authorized"), parse_mode="Markdown")
        return

    # Phase 1: instant preview with hourglass
    preview_msg = await msg.reply_text("⏳")

    # Phase 2: the default language, with a button for the other one.
    language = DIRECT_LANGUAGES[DIRECT_LANG_DEFAULT][0]
    data, error = await _translate(text, user_id, language)
    if error:
        await _edit(preview_msg, error)
        return

    body, keyboard = _render(data, language)
    await _edit(preview_msg, body, parse_mode="HTML", reply_markup=keyboard)


async def cb_translate_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retranslate into the language whose button was pressed."""
    query = update.callback_query
    if not query or not query.data:
        return

    code = query.data.split(":", 1)[1]
    entry = DIRECT_LANGUAGES.get(code)
    if not entry:
        await query.answer("Unknown language")
        return

    user_id = query.from_user.id if query.from_user else 0
    if not await is_whitelisted(user_id):
        await query.answer("Not allowed", show_alert=True)
        return

    # The source is the message this translation replies to, so a button still works
    # after the bot restarts — nothing is kept in memory between updates.
    source = query.message.reply_to_message if query.message else None
    text = (source.text or "").strip() if source else ""
    if not text:
        await query.answer("Send the text again, please", show_alert=True)
        return

    await query.answer()
    data, error = await _translate(text, user_id, entry[0])
    if error:
        # Leave the translation already on screen alone — rewriting it with an error
        # would cost the user both the text and the buttons to try again.
        await query.answer(error, show_alert=True)
        return

    body, keyboard = _render(data, entry[0])
    await _edit(query.message, body, parse_mode="HTML", reply_markup=keyboard)


async def handle_direct_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analyze media sent directly to the bot in private chat."""
    msg = update.message
    if not msg:
        return

    user_id = msg.from_user.id if msg.from_user else 0
    if not await is_whitelisted(user_id):
        await msg.reply_text(render("not_authorized"), parse_mode="Markdown")
        return

    # Determine media type and get file
    if msg.photo:
        tg_file = await msg.photo[-1].get_file()  # largest size
        mime_type = "image/jpeg"
        filename = "photo.jpg"
    elif msg.document:
        tg_file = await msg.document.get_file()
        mime_type = msg.document.mime_type or "application/octet-stream"
        filename = msg.document.file_name or "document"
    elif msg.audio:
        tg_file = await msg.audio.get_file()
        mime_type = msg.audio.mime_type or "audio/mpeg"
        filename = msg.audio.file_name or "audio.mp3"
    elif msg.voice:
        tg_file = await msg.voice.get_file()
        mime_type = msg.voice.mime_type or "audio/ogg"
        filename = "voice.ogg"
    elif msg.video_note:
        tg_file = await msg.video_note.get_file()
        mime_type = "video/mp4"
        filename = "video_note.mp4"
    else:
        return

    # Phase 1: instant preview with hourglass
    preview_msg = await msg.reply_text("⏳ Analyzing...")

    try:
        # Download file from Telegram
        file_bytes = await tg_file.download_as_bytearray()

        # Send to processor
        r = await http_client.post(
            f"{PROCESSOR_URL}/analyze-direct",
            files={"file": (filename, bytes(file_bytes), mime_type)},
            data={"user_id": str(user_id), "mime_type": mime_type, "filename": filename},
            timeout=120,
        )

        if r.status_code == 200:
            data = r.json()
            result_text = data.get("result_text", "")
            analysis_type = data.get("analysis_type", "")
            ms = data.get("processing_ms")

            reply = esc(result_text)
            if ms:
                reply += f"\n\n{italic(f'{ms}ms, {analysis_type}')}"

            await preview_msg.edit_text(reply, parse_mode="HTML")
        else:
            error = r.text
            logger.error("Processor /analyze-direct returned %s: %s", r.status_code, error)
            await preview_msg.edit_text("❌ Analysis failed. Try again later.")

    except httpx.TimeoutException:
        logger.error("Processor /analyze-direct timeout")
        await preview_msg.edit_text("❌ Analysis timed out. Try again later.")
    except Exception as exc:
        logger.error("Direct media handler error: %s", exc)
        await preview_msg.edit_text("❌ Something went wrong. Try again later.")
