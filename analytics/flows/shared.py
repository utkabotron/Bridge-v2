"""Shared utilities for analytics flows."""
from __future__ import annotations

import os

import httpx
from prefect import get_run_logger

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TG_IDS = [int(x) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()]


def esc(s: str) -> str:
    """Escape HTML special chars in LLM-generated text."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify_telegram(text: str, timeout: int = 10) -> int:
    """Send an HTML message to all admin Telegram chats. Returns count of successful sends."""
    logger = get_run_logger()

    if not TELEGRAM_BOT_TOKEN or not ADMIN_TG_IDS:
        logger.warning("Telegram not configured, cannot send notification")
        return 0

    if len(text) > 4096:
        text = text[:4090] + "\n…"

    sent = 0
    for chat_id in ADMIN_TG_IDS:
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=timeout,
            )
            resp.raise_for_status()
            sent += 1
        except Exception as e:
            logger.error("Failed to notify admin %d: %s", chat_id, e)

    return sent
