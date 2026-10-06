"""Shared utilities for analytics flows."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager

import httpx
import psycopg2
import psycopg2.extras
from prefect import get_run_logger

# Telegram calls put the bot token in the URL, and httpx logs request URLs at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TG_IDS = [int(x) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()]

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")


@contextmanager
def db_conn(cursor_factory=psycopg2.extras.RealDictCursor):
    """One connection per block: commit if it finishes, roll back if it raises, always close.

    Every flow used to repeat connect/cursor/commit/close by hand, and an exception between
    connect and close leaked the connection (the long-running flow server keeps the process,
    so leaks add up). `conn.cursor()` returns dict rows; pass cursor_factory=None for tuples.
    Read-only blocks commit too, which is a no-op.
    """
    conn = psycopg2.connect(DB_URL, cursor_factory=cursor_factory)
    try:
        yield conn
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except psycopg2.Error:
            pass  # the connection is already gone; the original error is the one worth seeing
        raise
    finally:
        conn.close()


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


def send_to_chat(chat_id: int, text: str, parse_mode: str = "HTML", timeout: int = 10) -> int | None:
    """Send an HTML message to a specific Telegram chat. Returns message_id or None."""
    logger = get_run_logger()

    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram not configured, cannot send message")
        return None

    if len(text) > 4096:
        text = text[:4090] + "\n…"

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("result", {}).get("message_id")
    except Exception as e:
        logger.error("Failed to send to chat %d: %s", chat_id, e)
        return None


REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))


def invalidate_profile_cache(chat_pair_ids: list[int]) -> int:
    """Drop the processor's Redis copy of these chat profiles (chat_profile:{id}).

    The processor caches a profile for an hour; without this a glossary fix kept being
    served from the stale copy. Best-effort: a Redis hiccup must not fail the flow, the
    cache expires on its own.
    """
    if not chat_pair_ids:
        return 0
    import redis

    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, socket_timeout=3)
        return int(r.delete(*[f"chat_profile:{pid}" for pid in chat_pair_ids]))
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not invalidate profile cache: %s", exc)
        return 0
