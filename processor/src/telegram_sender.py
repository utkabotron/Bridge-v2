"""Thin Telegram API client for the processor.

We use raw HTTPX calls (not python-telegram-bot) to keep the processor
dependency-free from the bot library. Only sendMessage/sendPhoto/sendVideo/
sendDocument/sendAudio are needed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30)
    return _client


def _parse_migrate(resp_text: str) -> Optional[int]:
    """Extract migrate_to_chat_id from Telegram error response."""
    try:
        data = json.loads(resp_text)
        return data.get("parameters", {}).get("migrate_to_chat_id")
    except (json.JSONDecodeError, AttributeError):
        return None


def _parse_retry_after(resp_text: str) -> Optional[int]:
    """Extract retry_after seconds from Telegram 429 response."""
    try:
        data = json.loads(resp_text)
        if data.get("error_code") == 429:
            return data.get("parameters", {}).get("retry_after")
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


MAX_RETRY_AFTER = 60  # cap wait time to avoid blocking consumer too long


def _parse_message_id(resp_text: str) -> Optional[int]:
    """Extract message_id from successful Telegram response."""
    try:
        data = json.loads(resp_text)
        if data.get("ok") and data.get("result"):
            return data["result"].get("message_id")
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def _to_internal_url(url: str) -> str:
    """Convert public MinIO URL to internal Docker URL.

    e.g. http://83.217.222.126:9000/bridge-media/... → http://minio:9000/bridge-media/...
    """
    parsed = urlparse(url)
    internal = urlparse(S3_ENDPOINT)
    return parsed._replace(netloc=internal.netloc, scheme=internal.scheme).geturl()


def _filename_from_url(url: str, media_filename: Optional[str] = None) -> str:
    """Extract filename from URL path or use provided media_filename."""
    if media_filename:
        return media_filename
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] if "/" in path else "file"
    return name or "file"


async def download_media(
    url: str, media_filename: Optional[str] = None, media_mime: Optional[str] = None,
) -> Optional[Tuple[bytes, str, str]]:
    """Download media from MinIO. Returns (bytes, filename, content_type) or None."""
    internal_url = _to_internal_url(url)
    try:
        r = await get_client().get(internal_url)
        if r.status_code != 200:
            logger.warning("Failed to download media from %s: %s", internal_url, r.status_code)
            return None
        content_type = media_mime or r.headers.get("content-type", "application/octet-stream")
        filename = _filename_from_url(url, media_filename)
        return r.content, filename, content_type
    except Exception as exc:
        logger.warning("Error downloading media from %s: %s", internal_url, exc)
        return None


# Keep old name as alias for backward compatibility in tests
_download_media = download_media


async def send_message(
    chat_id: int,
    text: str,
    media_url: Optional[str] = None,
    message_type: str = "text",
    media_filename: Optional[str] = None,
    media_mime: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> Tuple[bool, Optional[str], Optional[int], Optional[int]]:
    """Send a message to Telegram.

    Returns (success, error_message, migrate_to_chat_id, tg_message_id).
    migrate_to_chat_id is set when group was upgraded to supergroup.
    Retries once on 429 Too Many Requests after waiting retry_after seconds.
    """
    for attempt in range(2):  # max 1 retry for 429
        try:
            if media_url and message_type in ("image", "photo"):
                ok, err, msg_id = await _send_photo(chat_id, text, media_url, media_filename, media_mime, reply_markup)
            elif media_url and message_type in ("video",):
                ok, err, msg_id = await _send_video(chat_id, text, media_url, media_filename, media_mime, reply_markup)
            elif media_url and message_type in ("audio", "voice"):
                ok, err, msg_id = await _send_audio(chat_id, text, media_url, media_filename, media_mime, reply_markup)
            elif media_url and message_type == "document":
                ok, err, msg_id = await _send_document(chat_id, text, media_url, media_filename, media_mime, reply_markup)
            else:
                ok, err, msg_id = await _send_text(chat_id, text)

            # Handle 429 Too Many Requests — wait and retry once
            if not ok and err and attempt == 0:
                retry_after = _parse_retry_after(err)
                if retry_after:
                    wait = min(retry_after, MAX_RETRY_AFTER)
                    logger.warning("429 rate limited, waiting %ds before retry (chat %s)", wait, chat_id)
                    await asyncio.sleep(wait)
                    continue

            migrate_id = _parse_migrate(err) if err else None
            if migrate_id:
                logger.warning("Group %s migrated to supergroup %s", chat_id, migrate_id)
            return ok, err, migrate_id, msg_id
        except Exception as exc:
            logger.error("Telegram send error: %s", exc)
            return False, str(exc), None, None

    # Should not reach here, but just in case
    return False, err, None, None


async def _send_text(chat_id: int, text: str) -> Tuple[bool, Optional[str], Optional[int]]:
    r = await get_client().post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
    )
    if r.status_code == 200:
        return True, None, _parse_message_id(r.text)
    return False, r.text, None


async def _send_media_multipart(
    endpoint: str,
    field_name: str,
    chat_id: int,
    caption: str,
    url: str,
    media_filename: Optional[str] = None,
    media_mime: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> Tuple[bool, Optional[str], Optional[int]]:
    """Generic multipart media sender with fallback chain.

    1. Download from MinIO + multipart upload to Telegram
    2. If download fails → try sending URL directly (JSON)
    3. If URL also fails → text with link
    """
    data_fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "Markdown"}
    if reply_markup:
        data_fields["reply_markup"] = json.dumps(reply_markup)

    downloaded = await download_media(url, media_filename, media_mime)
    if downloaded:
        content_bytes, filename, content_type = downloaded
        r = await get_client().post(
            f"{BASE_URL}/{endpoint}",
            data=data_fields,
            files={field_name: (filename, content_bytes, content_type)},
        )
        if r.status_code == 200:
            logger.info("Sent %s via multipart upload to chat %s", endpoint, chat_id)
            return True, None, _parse_message_id(r.text)
        logger.warning("%s multipart failed (%s): %s", endpoint, r.status_code, r.text)

    # Fallback: try URL directly
    payload = {
        "chat_id": chat_id,
        field_name: url,
        "caption": caption,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = await get_client().post(f"{BASE_URL}/{endpoint}", json=payload)
    if r.status_code == 200:
        logger.info("Sent %s via URL to chat %s", endpoint, chat_id)
        return True, None, _parse_message_id(r.text)

    # Final fallback: text with link
    logger.warning("%s URL fallback also failed: %s", endpoint, r.text)
    return await _send_text(chat_id, f"{caption}\n[Media: {url}]")


async def _send_photo(
    chat_id: int, caption: str, url: str,
    media_filename: Optional[str] = None, media_mime: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> Tuple[bool, Optional[str], Optional[int]]:
    return await _send_media_multipart(
        "sendPhoto", "photo", chat_id, caption, url, media_filename, media_mime, reply_markup,
    )


async def _send_video(
    chat_id: int, caption: str, url: str,
    media_filename: Optional[str] = None, media_mime: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> Tuple[bool, Optional[str], Optional[int]]:
    return await _send_media_multipart(
        "sendVideo", "video", chat_id, caption, url, media_filename, media_mime, reply_markup,
    )


async def _send_audio(
    chat_id: int, caption: str, url: str,
    media_filename: Optional[str] = None, media_mime: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> Tuple[bool, Optional[str], Optional[int]]:
    return await _send_media_multipart(
        "sendAudio", "audio", chat_id, caption, url, media_filename, media_mime, reply_markup,
    )


async def _send_document(
    chat_id: int, caption: str, url: str,
    media_filename: Optional[str] = None, media_mime: Optional[str] = None,
    reply_markup: Optional[dict] = None,
) -> Tuple[bool, Optional[str], Optional[int]]:
    return await _send_media_multipart(
        "sendDocument", "document", chat_id, caption, url, media_filename, media_mime, reply_markup,
    )
