"""Callback handler for the inline 'Analyze' button on media messages."""
from __future__ import annotations

import logging
import os

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from ..utils.telegram_format import esc

logger = logging.getLogger(__name__)

PROCESSOR_URL = os.getenv("PROCESSOR_URL", "http://processor:8000")


async def cb_analyze_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback_query from 'Analyze' button press."""
    query = update.callback_query
    if not query or not query.data:
        return

    # Parse event_id from callback_data "analyze:123"
    parts = query.data.split(":")
    if len(parts) != 2:
        await query.answer("Invalid callback data")
        return

    try:
        event_id = int(parts[1])
    except ValueError:
        await query.answer("Invalid event ID")
        return

    requested_by = query.from_user.id if query.from_user else 0

    # Instant feedback
    await query.answer("Analyzing...")

    # Update button to show progress
    try:
        await query.edit_message_reply_markup(
            reply_markup={"inline_keyboard": [[
                {"text": "\u23f3 Analyzing...", "callback_data": "noop"},
            ]]},
        )
    except Exception as exc:
        logger.warning("Failed to edit button to analyzing state: %s", exc)

    # Call processor /analyze
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{PROCESSOR_URL}/analyze",
                json={"message_event_id": event_id, "requested_by": requested_by},
            )

        if r.status_code == 200:
            data = r.json()
            result_text = data.get("result_text", "(no result)")
            analysis_type = data.get("analysis_type", "")
            processing_ms = data.get("processing_ms")

            # Format result
            type_emoji = {"image": "\U0001f5bc", "audio": "\U0001f3a4", "document": "\U0001f4c4"}.get(analysis_type, "\U0001f50d")
            header = f"{type_emoji} <b>Analysis</b>"
            if processing_ms:
                header += f" ({processing_ms}ms)"
            reply_text = f"{header}\n\n{esc(result_text)}"

            # Send as reply to the original message
            msg = query.message
            await context.bot.send_message(
                chat_id=msg.chat_id,
                text=reply_text,
                reply_to_message_id=msg.message_id,
                parse_mode="HTML",
            )

            # Update button to "Analyzed"
            try:
                await query.edit_message_reply_markup(
                    reply_markup={"inline_keyboard": [[
                        {"text": "\u2705 Analyzed", "callback_data": "noop"},
                    ]]},
                )
            except Exception:
                pass
        else:
            error = r.json().get("error", "unknown error") if r.headers.get("content-type", "").startswith("application/json") else r.text
            logger.error("Processor /analyze returned %s: %s", r.status_code, error)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"\u274c Analysis failed: {error}",
                reply_to_message_id=query.message.message_id,
            )
            # Restore button
            try:
                await query.edit_message_reply_markup(
                    reply_markup={"inline_keyboard": [[
                        {"text": "\U0001f50d Analyze", "callback_data": f"analyze:{event_id}"},
                    ]]},
                )
            except Exception:
                pass

    except httpx.TimeoutException:
        logger.error("Processor /analyze timeout for event %s", event_id)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="\u274c Analysis timed out. Try again later.",
            reply_to_message_id=query.message.message_id,
        )
        try:
            await query.edit_message_reply_markup(
                reply_markup={"inline_keyboard": [[
                    {"text": "\U0001f50d Analyze", "callback_data": f"analyze:{event_id}"},
                ]]},
            )
        except Exception:
            pass
    except Exception as exc:
        logger.error("Unexpected error in analyze handler: %s", exc)
        try:
            await query.edit_message_reply_markup(
                reply_markup={"inline_keyboard": [[
                    {"text": "\U0001f50d Analyze", "callback_data": f"analyze:{event_id}"},
                ]]},
            )
        except Exception:
            pass


async def cb_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """No-op handler for disabled buttons."""
    if update.callback_query:
        await update.callback_query.answer()
