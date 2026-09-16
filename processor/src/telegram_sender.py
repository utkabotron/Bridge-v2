"""Thin Telegram API client for the processor.

We use raw HTTPX calls (not python-telegram-bot) to keep the processor
dependency-free from the bot library. Only sendMessage/sendPhoto/sendVideo/
sendDocument/sendAudio are needed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_SEND_TIMEOUT, MAX_RETRY_AFTER

logger = logging.getLogger(__name__)

BOT_TOKEN = TELEGRAM_BOT_TOKEN
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Telegram hard limits. Exceeding them returns a 400 that no retry recovers, so we split
# up front instead of silently losing long messages / oversized media captions.
TG_MAX_TEXT = 4096
TG_MAX_CAPTION = 1024

_client: Optional[httpx.AsyncClient] = None


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into <=limit chunks, preferring newline boundaries."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            head, text = text[:limit], text[limit:]
        else:
            head, text = text[:cut], text[cut + 1:]  # drop the newline we split on
        chunks.append(head)
    chunks.append(text)
    return chunks


def _split_caption(caption: str) -> Tuple[str, Optional[str]]:
    """Return (caption<=1024, overflow_text_or_None). Keeps media native; the remainder
    is sent as a follow-up text message so nothing is lost."""
    if len(caption) <= TG_MAX_CAPTION:
        return caption, None
    head = caption[:TG_MAX_CAPTION]
    nl = head.rfind("\n")
    if nl > TG_MAX_CAPTION // 2:  # avoid cutting mid-line when a reasonable break exists
        head = head[:nl]
    overflow = caption[len(head):].lstrip("\n")
    return head, overflow


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=TELEGRAM_SEND_TIMEOUT)
    return _client


def _parse_migrate(resp_text: str) -> Optional[int]:
    """Extract migrate_to_chat_id from Telegram error response."""
    try:
        data = json.loads(resp_text)
        return data.get("parameters", {}).get("migrate_to_chat_id")
    except (json.JSONDecodeError, AttributeError):
        return None


# Telegram errors that mean the chat is gone for good: retrying it costs a failed API call
# per message forever (pair 15 burned 14 deliveries a day this way). A supergroup migration
# is NOT in this list — that one is recoverable via migrate_to_chat_id.
_DEAD_CHAT_MARKERS = (
    "group chat was deleted",
    "bot was kicked",
    "bot is not a member",
    "chat not found",
    "user is deactivated",
)


def is_dead_chat(resp_text: Optional[str]) -> bool:
    """True when Telegram says this chat can never accept messages again."""
    if not resp_text:
        return False
    if _parse_migrate(resp_text):
        return False
    lowered = resp_text.lower()
    return any(marker in lowered for marker in _DEAD_CHAT_MARKERS)


def _parse_retry_after(resp_text: str) -> Optional[int]:
    """Extract retry_after seconds from Telegram 429 response."""
    try:
        data = json.loads(resp_text)
        if data.get("error_code") == 429:
            return data.get("parameters", {}).get("retry_after")
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


# Telegram 5xx (502/503 during their deploys) and network blips are worth another try.
SERVER_ERROR_BACKOFF = (2, 5)


def _is_server_error(resp_text: Optional[str]) -> bool:
    """True for transient Telegram-side failures."""
    if not resp_text:
        return False
    try:
        code = json.loads(resp_text).get("error_code")
        return isinstance(code, int) and 500 <= code < 600
    except (json.JSONDecodeError, AttributeError):
        # Not JSON at all — a transport error string from httpx, also transient.
        return "timeout" in resp_text.lower() or "connection" in resp_text.lower()


def _is_unauthorized(resp_text: str) -> bool:
    """Check if Telegram response is 401 Unauthorized."""
    try:
        data = json.loads(resp_text)
        return data.get("error_code") == 401
    except (json.JSONDecodeError, AttributeError):
        return False




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
    """Rehost a media URL onto the internal MinIO endpoint and sign it.

    Only the object path is reused, so a hostile media_s3_key can never redirect this
    request off the compose network. The signature is required now that the bucket
    rejects anonymous reads.
    """
    from .s3 import presign_internal

    return presign_internal(url)


def _to_public_url(url: str) -> str:
    """Presigned URL a third party can fetch (Telegram, or the user tapping the link)."""
    from .s3 import presign_public

    return presign_public(url)


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

# Media type → (Telegram API endpoint, multipart field name)
_MEDIA_TYPE_MAP = {
    "image": ("sendPhoto", "photo"),
    "photo": ("sendPhoto", "photo"),
    "sticker": ("sendPhoto", "photo"),  # WA stickers (webp) → Telegram photo with caption
    "video": ("sendVideo", "video"),
    "audio": ("sendAudio", "audio"),
    "voice": ("sendAudio", "audio"),
    "document": ("sendDocument", "document"),
}


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
    last_err: Optional[str] = None
    for attempt in range(1 + len(SERVER_ERROR_BACKOFF)):
        try:
            media_type = _MEDIA_TYPE_MAP.get(message_type) if media_url else None
            if media_type:
                endpoint, field = media_type
                ok, err, msg_id = await _send_media_multipart(
                    endpoint, field, chat_id, text, media_url, media_filename, media_mime, reply_markup,
                )
            else:
                ok, err, msg_id = await _send_text(chat_id, text)

            last_err = err

            # Handle 429 Too Many Requests — wait and retry once
            if not ok and err and attempt == 0:
                retry_after = _parse_retry_after(err)
                if retry_after:
                    wait = min(retry_after, MAX_RETRY_AFTER)
                    logger.warning("429 rate limited, waiting %ds before retry (chat %s)", wait, chat_id)
                    await asyncio.sleep(wait)
                    continue

            # Telegram 5xx is transient, but a single attempt marked the message failed
            # for good — it never reached the DLQ either, since nothing raised.
            if not ok and _is_server_error(err) and attempt < len(SERVER_ERROR_BACKOFF):
                wait = SERVER_ERROR_BACKOFF[attempt]
                logger.warning("Telegram server error, retrying in %ds (chat %s): %s", wait, chat_id, err)
                await asyncio.sleep(wait)
                continue

            migrate_id = _parse_migrate(err) if err else None
            if migrate_id:
                logger.warning("Group %s migrated to supergroup %s", chat_id, migrate_id)
            return ok, err, migrate_id, msg_id
        except Exception as exc:
            logger.error("Telegram send error: %s", exc)
            return False, str(exc), None, None

    # Exhausted the retries above.
    return False, last_err, None, None


async def _send_text(chat_id: int, text: str) -> Tuple[bool, Optional[str], Optional[int]]:
    """Send text, splitting into <=4096-char chunks. Returns the last chunk's result;
    stops and reports the first failing chunk."""
    if len(text) <= TG_MAX_TEXT:
        return await _send_text_single(chat_id, text)

    result: Tuple[bool, Optional[str], Optional[int]] = (True, None, None)
    for chunk in _split_text(text, TG_MAX_TEXT):
        result = await _send_text_single(chat_id, chunk)
        if not result[0]:
            return result  # abort on first failure
    return result


async def _send_text_single(chat_id: int, text: str) -> Tuple[bool, Optional[str], Optional[int]]:
    r = await get_client().post(
        f"{BASE_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
    )
    if r.status_code == 200:
        return True, None, _parse_message_id(r.text)
    # Fallback: retry without parse_mode on parse errors
    if r.status_code == 400 and "can't parse entities" in r.text.lower():
        logger.warning("HTML parse failed for chat %s, retrying without parse_mode", chat_id)
        r2 = await get_client().post(
            f"{BASE_URL}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        if r2.status_code == 200:
            return True, None, _parse_message_id(r2.text)
        return False, r2.text, None
    if r.status_code == 401 or _is_unauthorized(r.text):
        logger.critical("401 Unauthorized for chat %s — bot removed from chat or token invalid", chat_id)
        return False, "401_UNAUTHORIZED", None
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
    """Send media natively, splitting an over-long caption so the media stays native and
    the caption remainder follows as a separate text message."""
    caption, overflow = _split_caption(caption)
    ok, err, msg_id = await _do_send_media(
        endpoint, field_name, chat_id, caption, url, media_filename, media_mime, reply_markup,
    )
    if ok and overflow:
        # best-effort — don't fail the media delivery if the overflow text errors
        try:
            await _send_text(chat_id, overflow)
        except Exception as exc:
            logger.warning("Failed to send caption overflow for chat %s: %s", chat_id, exc)
    return ok, err, msg_id


async def _do_send_media(
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
    data_fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
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
        # Fallback: retry without parse_mode on parse errors
        if r.status_code == 400 and "can't parse entities" in r.text.lower():
            logger.warning("HTML caption parse failed, retrying without parse_mode")
            data_no_pm = {k: v for k, v in data_fields.items() if k != "parse_mode"}
            r2 = await get_client().post(
                f"{BASE_URL}/{endpoint}",
                data=data_no_pm,
                files={field_name: (filename, content_bytes, content_type)},
            )
            if r2.status_code == 200:
                return True, None, _parse_message_id(r2.text)
        if r.status_code == 401 or _is_unauthorized(r.text):
            logger.critical("401 Unauthorized on %s for chat %s — bot removed from chat or token invalid", endpoint, chat_id)
            return False, "401_UNAUTHORIZED", None
        logger.warning("%s multipart failed (%s): %s", endpoint, r.status_code, r.text)

    # Fallback: hand Telegram a link and let it fetch the file itself. The bucket is
    # private, so the link has to be presigned — it expires on its own.
    public_url = _to_public_url(url)
    payload = {
        "chat_id": chat_id,
        field_name: public_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = await get_client().post(f"{BASE_URL}/{endpoint}", json=payload)
    if r.status_code == 200:
        logger.info("Sent %s via URL to chat %s", endpoint, chat_id)
        return True, None, _parse_message_id(r.text)

    # Final fallback: text with link
    logger.warning("%s URL fallback also failed: %s", endpoint, r.text)
    return await _send_text(chat_id, f"{caption}\n[Media: {public_url}]")


