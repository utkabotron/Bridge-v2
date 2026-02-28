"""Thin Telegram API client for the processor.

We use raw HTTPX calls (not python-telegram-bot) to keep the processor
dependency-free from the bot library. Only sendMessage/sendPhoto/sendVideo/
sendDocument/sendAudio are needed here.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30)
    return _client


async def send_message(
    chat_id: int,
    text: str,
    media_url: Optional[str] = None,
    message_type: str = "text",
    media_filename: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Send a message to Telegram. Returns (success, error_message)."""
    try:
        if media_url and message_type in ("image", "photo"):
            return await _send_photo(chat_id, text, media_url)
        elif media_url and message_type in ("video",):
            return await _send_video(chat_id, text, media_url)
        elif media_url and message_type in ("audio", "voice"):
            return await _send_audio(chat_id, text, media_url)
        elif media_url and message_type == "document":
            return await _send_document(chat_id, text, media_url, media_filename)
        else:
            return await _send_text(chat_id, text)
    except Exception as exc:
        logger.error("Telegram send error: %s", exc)
        return False, str(exc)


async def _send_text(chat_id: int, text: str) -> Tuple[bool, Optional[str]]:
    r = await get_client().post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
    )
    if r.status_code == 200:
        return True, None
    return False, r.text


async def _send_photo(chat_id: int, caption: str, url: str) -> Tuple[bool, Optional[str]]:
    r = await get_client().post(
        f"{BASE_URL}/sendPhoto",
        json={"chat_id": chat_id, "photo": url, "caption": caption, "parse_mode": "Markdown"},
    )
    if r.status_code == 200:
        return True, None
    # Fallback to text if photo fails
    return await _send_text(chat_id, f"{caption}\n[Photo: {url}]")


async def _send_video(chat_id: int, caption: str, url: str) -> Tuple[bool, Optional[str]]:
    r = await get_client().post(
        f"{BASE_URL}/sendVideo",
        json={"chat_id": chat_id, "video": url, "caption": caption, "parse_mode": "Markdown"},
    )
    if r.status_code == 200:
        return True, None
    return await _send_text(chat_id, f"{caption}\n[Video: {url}]")


async def _send_audio(chat_id: int, caption: str, url: str) -> Tuple[bool, Optional[str]]:
    r = await get_client().post(
        f"{BASE_URL}/sendAudio",
        json={"chat_id": chat_id, "audio": url, "caption": caption, "parse_mode": "Markdown"},
    )
    if r.status_code == 200:
        return True, None
    return await _send_text(chat_id, f"{caption}\n[Audio: {url}]")


async def _send_document(
    chat_id: int, caption: str, url: str, filename: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    payload = {
        "chat_id": chat_id,
        "document": url,
        "caption": caption,
        "parse_mode": "Markdown",
    }
    if filename:
        # Telegram uses this to display the file name in the chat
        payload["filename"] = filename
    r = await get_client().post(f"{BASE_URL}/sendDocument", json=payload)
    if r.status_code == 200:
        return True, None
    return await _send_text(chat_id, f"{caption}\n[Document: {url}]")
