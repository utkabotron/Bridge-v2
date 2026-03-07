"""Handlers for direct messages in private chat — text translation & media analysis."""
from __future__ import annotations

import logging
import os

import httpx
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

logger = logging.getLogger(__name__)

PROCESSOR_URL = os.getenv("PROCESSOR_URL", "http://processor:8000")


async def handle_direct_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Translate text sent directly to the bot in private chat."""
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if not text:
        return

    user_id = msg.from_user.id if msg.from_user else 0

    # Phase 1: instant preview with hourglass
    preview_msg = await msg.reply_text(f"{text}\n\n⏳", parse_mode="Markdown")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{PROCESSOR_URL}/translate",
                json={"text": text, "user_id": user_id},
            )

        if r.status_code == 200:
            data = r.json()
            original = data.get("original", text)
            translated = data.get("translated", "")
            lang = data.get("target_language", "")
            ms = data.get("translation_ms")

            reply = f"{original}\n\n{translated}"
            if ms:
                reply += f"\n\n_({ms}ms, {lang})_"

            # Phase 2: edit preview with translation
            await preview_msg.edit_text(reply, parse_mode="Markdown")

            # Phase 3: delete original user message for clean feed
            try:
                await msg.delete()
            except Exception as del_exc:
                logger.warning("Could not delete user message: %s", del_exc)
        else:
            error = r.text
            logger.error("Processor /translate returned %s: %s", r.status_code, error)
            await preview_msg.edit_text(f"{text}\n\n❌ Translation failed. Try again later.")

    except httpx.TimeoutException:
        logger.error("Processor /translate timeout")
        await preview_msg.edit_text(f"{text}\n\n❌ Translation timed out. Try again later.")
    except Exception as exc:
        logger.error("Translate handler error: %s", exc)
        await preview_msg.edit_text(f"{text}\n\n❌ Something went wrong. Try again later.")


async def handle_direct_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Analyze media sent directly to the bot in private chat."""
    msg = update.message
    if not msg:
        return

    user_id = msg.from_user.id if msg.from_user else 0

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
    preview_msg = await msg.reply_text("⏳ Analyzing...", parse_mode="Markdown")

    try:
        # Download file from Telegram
        file_bytes = await tg_file.download_as_bytearray()

        # Send to processor
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{PROCESSOR_URL}/analyze-direct",
                files={"file": (filename, bytes(file_bytes), mime_type)},
                data={"user_id": str(user_id), "mime_type": mime_type, "filename": filename},
            )

        if r.status_code == 200:
            data = r.json()
            result_text = data.get("result_text", "")
            analysis_type = data.get("analysis_type", "")
            ms = data.get("processing_ms")

            reply = result_text
            if ms:
                reply += f"\n\n_({ms}ms, {analysis_type})_"

            await preview_msg.edit_text(reply, parse_mode="Markdown")
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
