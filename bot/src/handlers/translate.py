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

            await msg.reply_text(reply, parse_mode="Markdown")
        else:
            error = r.text
            logger.error("Processor /translate returned %s: %s", r.status_code, error)
            await msg.reply_text("Translation failed. Try again later.")

    except httpx.TimeoutException:
        logger.error("Processor /translate timeout")
        await msg.reply_text("Translation timed out. Try again later.")
    except Exception as exc:
        logger.error("Translate handler error: %s", exc)
        await msg.reply_text("Something went wrong. Try again later.")
