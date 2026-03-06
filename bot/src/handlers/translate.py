"""Handler for direct text messages — translate and reply."""
from __future__ import annotations

import logging
import os

import httpx
from telegram import Update
from telegram.ext import ContextTypes

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
