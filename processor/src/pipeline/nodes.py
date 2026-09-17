"""LangGraph node functions for the message pipeline.

Each node receives MessageState, mutates a copy, and returns it.
LangSmith traces every node automatically via LANGCHAIN_TRACING_V2=true.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from typing import Any


from langdetect import detect, LangDetectException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..config import (
    LLM_TIMEOUT, LLM_MAX_RETRIES, TRANSLATION_UNAVAILABLE_NOTE,
    VOICE_AUTO_TRANSCRIBE, VOICE_TRANSCRIPT_TITLE,
    MEDIA_FAILED_NOTE, EDITED_MARK, OWN_MESSAGE_PREFIX,
)
from ..models.message import MessageState
from ..utils.telegram_format import bold, esc
from .cache import get_cached, set_cached, get_cached_global, set_cached_global, get_chat_profile, set_chat_profile
from .prompts import PROMPT_VERSION, get_translate_prompt, format_chat_context

logger = logging.getLogger(__name__)

# Shared LLM instance — model pinned for reproducibility
_llm: Any = None

# Media types eligible for the Analyze button (no video in v1)
_ANALYZABLE_TYPES = {"image", "photo", "audio", "voice", "ptt", "document"}


def get_llm() -> ChatOpenAI:
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            temperature=0,
            tags=["bridge-v2", f"prompt-{PROMPT_VERSION}"],
            # Unbounded, the SDK waits 600s and langchain retries twice, so one sick
            # request could hold the single-threaded consumer for half an hour while
            # every user's messages queued behind it.
            timeout=LLM_TIMEOUT,
            max_retries=LLM_MAX_RETRIES,
        )
    return _llm


# ── DB helpers (lazy import to avoid circular deps) ──────

async def _fetch_chat_pairs(user_id: int, wa_chat_id: str) -> list[dict]:
    from ..db import fetch_active_chat_pairs
    return await fetch_active_chat_pairs(user_id, wa_chat_id)


# ── Node: validate ────────────────────────────────────────

async def validate_node(state: MessageState) -> MessageState:
    """Resolve chat_pair_id, tg_chat_id, target_language from DB.

    The consumer resolves the pairs itself to fan a group message out to every active
    pair of that chat, and hands each run its own pre-resolved pair — in that case this
    node is a pass-through instead of a second identical query.
    """
    if state.get("chat_pair_id"):
        return state

    pairs = await _fetch_chat_pairs(state["user_id"], state["wa_chat_id"])
    pair = pairs[0] if pairs else None

    if not pair:
        logger.warning("No active chat pair for user=%s chat=%s", state["user_id"], state["wa_chat_id"])

        # Fallback to admins only for admin's own WA messages
        admin_ids = [int(x.strip()) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()]
        if state["user_id"] in admin_ids:
            text = state.get("original_text", "").strip()
            if text:
                try:
                    lang = detect(text)
                except LangDetectException:
                    lang = "ru"
            else:
                lang = "ru"

            if lang != "ru":
                logger.info("Admin no-pair fallback (lang=%s) → send to admins", lang)
                return {**state, "chat_pair_id": None, "tg_chat_id": None,
                        "target_language": "Russian",
                        "fallback_to_admins": True}

        return {**state, "chat_pair_id": None, "tg_chat_id": None,
                "target_language": state.get("target_language", "Russian"),
                "delivery_status": "skipped", "error": "no_chat_pair"}

    return {
        **state,
        "chat_pair_id": pair["id"],
        "tg_chat_id": pair["tg_chat_id"],
        "target_language": pair.get("target_language") or "Russian",
    }


# ── Node: translate ───────────────────────────────────────

async def translate_node(state: MessageState) -> MessageState:
    """Translate original_text to target_language using LLM with Redis cache."""
    text = state["original_text"].strip()

    if not text:
        return {**state, "translated_text": text, "translation_ms": 0, "cache_hit": False}

    lang = state.get("target_language", "Russian")
    chat_pair_id = state.get("chat_pair_id")

    # Load chat profile: Redis cache → PostgreSQL → None
    chat_context = ""
    if chat_pair_id:
        profile = await get_chat_profile(chat_pair_id)
        if profile is None:
            from ..db import fetch_chat_profile
            profile = await fetch_chat_profile(chat_pair_id)
            if profile:
                await set_chat_profile(chat_pair_id, profile)
        if profile:
            chat_context = format_chat_context(profile)

    # Determine if this chat has a meaningful profile (glossary or member names)
    has_profile = bool(chat_context)

    # Cache lookup: pair-specific first; for chats without profile also check global cache
    cached = await get_cached(text, lang, chat_pair_id)
    if cached:
        logger.debug("Translation cache HIT (pair-specific)")
        return {**state, "translated_text": cached, "translation_ms": 0, "cache_hit": True}

    if not has_profile:
        cached_global = await get_cached_global(text, lang)
        if cached_global:
            logger.debug("Translation cache HIT (global)")
            await set_cached(text, lang, cached_global, chat_pair_id)  # populate pair cache too
            return {**state, "translated_text": cached_global, "translation_ms": 0, "cache_hit": True}

    # LLM call — traced by LangSmith automatically
    t0 = time.monotonic()
    messages = [
        SystemMessage(content=get_translate_prompt(lang, chat_context)),
        HumanMessage(content=text),
    ]
    try:
        response = await get_llm().ainvoke(messages)
    except Exception as exc:
        # An OpenAI outage used to raise here, escape the graph and send the message to a
        # dead-letter queue nobody drained — so the whole bridge went quiet and stayed
        # quiet. The original text is still worth delivering; the reader can see it is
        # untranslated and we keep the pipe flowing.
        translation_ms = int((time.monotonic() - t0) * 1000)
        logger.error("Translation failed (%s) — delivering the original untranslated", exc)
        return {
            **state,
            "translated_text": "",
            "translation_ms": translation_ms,
            "cache_hit": False,
            "translation_failed": True,
        }

    translation_ms = int((time.monotonic() - t0) * 1000)

    translated = response.content.strip()

    # Guard against caching a degenerate translation (empty or a tiny fragment of a long
    # source — usually a truncated/filtered LLM response). Caching it would serve that bad
    # result for 24h, and globally it would poison every profileless pair. Deliver what we
    # got this once, but do not persist it to cache.
    is_degenerate = (not translated) or (len(text) > 80 and len(translated) < 0.3 * len(text))
    if is_degenerate:
        logger.warning(
            "Skipping cache for degenerate translation (src_len=%d, out_len=%d, lang=%s)",
            len(text), len(translated), lang,
        )
    else:
        await set_cached(text, lang, translated, chat_pair_id)
        if not has_profile:
            await set_cached_global(text, lang, translated)

    return {**state, "translated_text": translated, "translation_ms": translation_ms, "cache_hit": False}



# ── Voice transcription ───────────────────────────────────

_VOICE_TYPES = {"ptt", "voice"}

# Used when media could not be fetched, so the note names what was lost.
_MEDIA_KIND_NAMES = {
    "image": "фото", "photo": "фото", "sticker": "стикер", "video": "видео",
    "audio": "аудио", "ptt": "голосовое сообщение", "voice": "голосовое сообщение",
    "document": "документ",
}

# Detached tasks are kept referenced; without this the event loop may garbage-collect a
# running task mid-flight.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _transcribe_voice_note(
    state: MessageState, tg_chat_id: int, reply_to: int | None, event_id: int | None,
) -> None:
    """Transcribe a delivered voice note and post the text as a reply to it."""
    from ..db import insert_media_analysis
    from ..feature_flags import is_enabled
    from ..media_analyzer import transcribe_audio
    from ..pipeline.cache import get_cached_media, set_cached_media
    from ..telegram_sender import download_media, send_message

    if not await is_enabled("voice_transcribe_enabled"):
        return

    url = state.get("media_s3_url")
    if not url:
        return

    lang = state.get("target_language") or "Russian"
    t0 = time.monotonic()
    try:
        downloaded = await download_media(url, state.get("media_filename"), state.get("media_mime"))
        if not downloaded:
            logger.warning("Voice transcript: could not fetch %s", state.get("wa_message_id"))
            return
        content, filename, _ = downloaded

        # Same cache the Analyze button uses, keyed by file content — a forwarded or
        # repeated recording is transcribed once.
        digest = hashlib.sha256(content).hexdigest()
        text = await get_cached_media(digest, lang)
        if not text:
            text = await transcribe_audio(content, filename or "voice.ogg", lang)
            await set_cached_media(digest, lang, text)

        elapsed = int((time.monotonic() - t0) * 1000)
        await send_message(
            chat_id=tg_chat_id,
            text=f"{bold('🎤 ' + esc(VOICE_TRANSCRIPT_TITLE))}\n\n{esc(text)}",
            reply_to_message_id=reply_to,
        )

        # Record it so tapping Analyze on the same message returns this result instead of
        # paying for a second transcription.
        if event_id:
            await insert_media_analysis(event_id, "audio", text, "completed", elapsed, 0)
    except Exception as exc:
        # Best-effort: the voice note itself is already delivered.
        logger.warning("Voice transcript failed for %s: %s", state.get("wa_message_id"), exc)


# ── Node: format ──────────────────────────────────────────

def format_node(state: MessageState) -> MessageState:
    """Compose the final Telegram message text.

    Format:
        *Sender Name*
        original text

        translated text
    """
    original = state.get("original_text", "")
    translated = state.get("translated_text")
    sender = state.get("sender_name", "")

    parts = []
    header = []
    if state.get("from_me"):
        # Own outgoing messages are bridged too; mark them so the Telegram copy reads as
        # a conversation instead of an unattributed stream.
        header.append(esc(OWN_MESSAGE_PREFIX))
    if sender:
        header.append(bold(sender))
    if state.get("is_edited"):
        # This used to arrive as a second, near-identical message with no explanation.
        header.append(esc(EDITED_MARK))
    if header:
        parts.append(" ".join(header))
        parts.append("")

    # Quoted message: Telegram's own reply threading does the work when we know the
    # original's message_id; this preview is the fallback when we don't.
    quoted = state.get("quoted") or {}
    if quoted.get("body") and not state.get("reply_to_message_id"):
        preview = quoted["body"].strip().replace("\n", " ")[:120]
        who = quoted.get("sender")
        prefix = f"{who}: " if who else ""
        parts.append(f"<blockquote>↩︎ {esc(prefix + preview)}</blockquote>")
        parts.append("")

    contacts = state.get("contacts") or []
    if contacts:
        # A raw vCard used to be forwarded verbatim and translated field by field.
        for c in contacts:
            name = esc(c.get("name") or "—")
            phones = ", ".join(esc(p) for p in (c.get("phones") or []))
            parts.append(f"👤 {bold(name)}" + (f"\n{phones}" if phones else ""))
        parts.append("")

    if original:
        parts.append(esc(original))
        # Add translated only if it exists and differs from original
        if translated and translated.strip() != original.strip():
            parts.append("")
            parts.append(esc(translated))

    if state.get("translation_failed"):
        parts.append("")
        parts.append(esc(TRANSLATION_UNAVAILABLE_NOTE))

    if state.get("media_failed"):
        # The recipient previously got a bare sender name with no sign that a photo or
        # voice note had been sent at all.
        kind = _MEDIA_KIND_NAMES.get(state.get("message_type", ""), "файл")
        parts.append("")
        parts.append(esc(MEDIA_FAILED_NOTE.format(kind=kind)))

    # Sections are separated by a single blank line. Joining blindly left doubled gaps
    # whenever a section was empty (e.g. media that failed to download, which has no text).
    formatted = re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()
    return {**state, "formatted_text": formatted}


# ── Node: deliver ─────────────────────────────────────────

async def deliver_node(state: MessageState) -> MessageState:
    """Send formatted message to Telegram and persist to DB."""
    # Short-circuit if validation failed or skipped (no chat pair)
    if state.get("delivery_status") in ("failed", "skipped"):
        await _persist_event(state)
        return state

    # Fallback: send to each admin personally
    if state.get("fallback_to_admins"):
        return await _deliver_to_admins(state)

    tg_chat_id = state.get("tg_chat_id")
    if not tg_chat_id:
        result = {**state, "delivery_status": "failed", "error": "missing tg_chat_id"}
        await _persist_event(result)  # record the failure — otherwise it vanishes from the DB
        return result

    # Resolve the Telegram message this one answers, so replies keep their thread.
    state = {**state, "reply_to_message_id": await _resolve_reply_target(state)}

    # A location has no text worth translating; send it as a real map pin.
    if state.get("location"):
        return await _deliver_location(state, tg_chat_id)

    has_media = bool(state.get("media_s3_url"))
    msg_type = state.get("message_type", "text")
    is_analyzable = has_media and msg_type in _ANALYZABLE_TYPES

    if is_analyzable:
        return await _deliver_media_with_button(state, tg_chat_id)

    return await _deliver_simple(state, tg_chat_id)


async def _resolve_reply_target(state: MessageState) -> int | None:
    """Telegram message_id of the message this one quotes, if we delivered it."""
    from ..db import find_tg_message_id

    chat_pair_id = state.get("chat_pair_id")
    if not chat_pair_id:
        return None

    # An edit should attach to the message it revises; a reply, to the message it quotes.
    target_wa_id = None
    if state.get("is_edited"):
        # Edits are stored under "<original>:edit:<hash>" — strip back to the original.
        target_wa_id = (state.get("wa_message_id") or "").split(":edit:")[0] or None
    elif (state.get("quoted") or {}).get("wa_message_id"):
        target_wa_id = state["quoted"]["wa_message_id"]

    if not target_wa_id:
        return None
    return await find_tg_message_id(target_wa_id, chat_pair_id)


async def _deliver_location(state: MessageState, tg_chat_id: int) -> MessageState:
    """Send a WhatsApp location as a Telegram map pin.

    Locations used to fall through the text path: `body` holds a base64 thumbnail, so the
    recipient saw either nothing or a wall of encoded data — which was also billed as a
    translation.
    """
    from ..telegram_sender import send_location

    loc = state["location"]
    ok, error, tg_msg_id = await send_location(
        chat_id=tg_chat_id,
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        title=loc.get("name"),
        sender=state.get("sender_name"),
        reply_to_message_id=state.get("reply_to_message_id"),
    )

    if not ok:
        await _pause_dead_chat(state, error)

    result = {**state, "tg_chat_id": tg_chat_id,
              "delivery_status": "delivered" if ok else "failed",
              "error": error, "tg_message_id": tg_msg_id}
    await _persist_event(result)
    return result


async def _deliver_simple(state: MessageState, tg_chat_id: int) -> MessageState:
    """Standard delivery without inline buttons."""
    from ..telegram_sender import send_message
    ok, error, migrate_id, tg_msg_id = await send_message(
        chat_id=tg_chat_id,
        text=state["formatted_text"],
        media_url=state.get("media_s3_url"),
        message_type=state.get("message_type", "text"),
        media_filename=state.get("media_filename"),
        media_mime=state.get("media_mime"),
        reply_to_message_id=state.get("reply_to_message_id"),
    )

    # Auto-migrate supergroup: update chat_pairs and retry
    if not ok and migrate_id:
        await _migrate_chat_pair(state.get("chat_pair_id"), migrate_id)
        ok, error, _, tg_msg_id = await send_message(
            chat_id=migrate_id,
            text=state["formatted_text"],
            media_url=state.get("media_s3_url"),
            message_type=state.get("message_type", "text"),
            media_filename=state.get("media_filename"),
            media_mime=state.get("media_mime"),
        )
        if ok:
            tg_chat_id = migrate_id

    if not ok:
        await _pause_dead_chat(state, error)

    new_status = "delivered" if ok else "failed"
    result = {**state, "tg_chat_id": tg_chat_id, "delivery_status": new_status,
              "error": error, "tg_message_id": tg_msg_id}

    await _persist_event(result)
    return result


async def _deliver_media_with_button(state: MessageState, tg_chat_id: int) -> MessageState:
    """Two-phase delivery: INSERT pending → send with Analyze button → UPDATE."""
    from ..db import insert_message_event, update_event_after_send
    from ..telegram_sender import send_message

    # Phase 1: INSERT message_event (pending) to get event_id for callback_data
    pending_state = {**state, "delivery_status": "pending"}
    event_id = await insert_message_event(pending_state, return_id=True)
    if not event_id:
        # DB write failed (or the row was already delivered). The Analyze button needs a
        # real event_id, but the message itself MUST still be delivered — falling back to a
        # plain send is what keeps media flowing when persistence is degraded. (This exact
        # path silently dropped all media from July 15 until the id-propagation fix.)
        logger.error("No event_id for two-phase delivery — delivering media without Analyze button")
        return await _deliver_simple(state, tg_chat_id)

    # Phase 2: Send with inline keyboard
    reply_markup = {
        "inline_keyboard": [[
            {"text": "\U0001f50d Analyze", "callback_data": f"analyze:{event_id}"},
        ]],
    }
    ok, error, migrate_id, tg_msg_id = await send_message(
        chat_id=tg_chat_id,
        text=state["formatted_text"],
        media_url=state.get("media_s3_url"),
        message_type=state.get("message_type", "text"),
        media_filename=state.get("media_filename"),
        media_mime=state.get("media_mime"),
        reply_markup=reply_markup,
        reply_to_message_id=state.get("reply_to_message_id"),
    )

    # Auto-migrate supergroup
    if not ok and migrate_id:
        await _migrate_chat_pair(state.get("chat_pair_id"), migrate_id)
        ok, error, _, tg_msg_id = await send_message(
            chat_id=migrate_id,
            text=state["formatted_text"],
            media_url=state.get("media_s3_url"),
            message_type=state.get("message_type", "text"),
            media_filename=state.get("media_filename"),
            media_mime=state.get("media_mime"),
            reply_markup=reply_markup,
        )
        if ok:
            tg_chat_id = migrate_id

    if not ok:
        await _pause_dead_chat(state, error)

    # Phase 3: UPDATE event with delivery status + tg_message_id
    new_status = "delivered" if ok else "failed"
    await update_event_after_send(event_id, new_status, error, tg_msg_id)

    # A voice note is unreadable to someone who does not speak the language, which is the
    # whole point of this bridge — so transcribe and translate it without making the
    # reader tap anything. Runs detached: Whisper takes seconds and the consumer handles
    # one message at a time, so awaiting it here would stall everyone else's messages.
    if ok and VOICE_AUTO_TRANSCRIBE and state.get("message_type") in _VOICE_TYPES:
        _spawn(_transcribe_voice_note(state, tg_chat_id, tg_msg_id, event_id))

    return {**state, "tg_chat_id": tg_chat_id, "delivery_status": new_status,
            "error": error, "tg_message_id": tg_msg_id}


async def _deliver_to_admins(state: MessageState) -> MessageState:
    """Send message to all admin Telegram IDs from ADMIN_TG_IDS env."""
    from ..telegram_sender import send_message

    raw_ids = os.getenv("ADMIN_TG_IDS", "")
    admin_ids = [int(x.strip()) for x in raw_ids.split(",") if x.strip()]

    if not admin_ids:
        logger.warning("fallback_to_admins=True but ADMIN_TG_IDS is empty")
        result = {**state, "delivery_status": "failed", "error": "no_admin_ids"}
        await _persist_event(result)
        return result

    chat_name = state.get("wa_chat_name", "Unknown")
    text = f"[WA: {chat_name}]\n{state.get('formatted_text', '')}"

    errors = []
    for admin_id in admin_ids:
        ok, error, _, _ = await send_message(
            chat_id=admin_id,
            text=text,
            media_url=state.get("media_s3_url"),
            message_type=state.get("message_type", "text"),
            media_filename=state.get("media_filename"),
            media_mime=state.get("media_mime"),
        )
        if not ok:
            errors.append(f"admin {admin_id}: {error}")
            logger.error("Failed to send to admin %s: %s", admin_id, error)

    if errors:
        result = {**state, "delivery_status": "failed", "error": "; ".join(errors)}
    else:
        result = {**state, "delivery_status": "delivered"}
        logger.info("Fallback message sent to %d admins", len(admin_ids))

    await _persist_event(result)
    return result


async def _pause_dead_chat(state: MessageState, error: str | None) -> None:
    """Pause a pair whose Telegram chat is gone and tell its owner once.

    Without this every later message from that WA chat burns a translation and a failed
    Telegram call forever — and the owner never learns why their group went quiet.
    """
    from ..db import pause_chat_pair
    from ..telegram_sender import is_dead_chat, send_message

    chat_pair_id = state.get("chat_pair_id")
    if not chat_pair_id or not is_dead_chat(error):
        return

    owner_tg_id = await pause_chat_pair(chat_pair_id)
    logger.warning("Paused chat_pair %s — Telegram chat unreachable: %s", chat_pair_id, error)
    if not owner_tg_id:
        return  # already paused by an earlier message — do not notify twice

    chat_name = state.get("wa_chat_name") or "WhatsApp chat"
    await send_message(
        chat_id=owner_tg_id,
        text=(
            f"\u23f8 Bridge paused for {bold(esc(chat_name))} \u2014 the linked Telegram "
            "group is no longer reachable.\n\n"
            "Create a new group, add the bot to it, and link the chat again with /add."
        ),
    )


async def _migrate_chat_pair(chat_pair_id: int | None, new_tg_chat_id: int) -> None:
    """Update chat_pairs tg_chat_id when Telegram group migrates to supergroup."""
    if not chat_pair_id:
        return
    import asyncpg

    try:
        from ..db import get_pool
        pool = await get_pool()
        await pool.execute(
            "UPDATE chat_pairs SET tg_chat_id = $1 WHERE id = $2",
            int(new_tg_chat_id), chat_pair_id,  # column is bigint — passing str raised DataError, so the update never persisted
        )
        logger.info("Migrated chat_pair %s to new tg_chat_id %s", chat_pair_id, new_tg_chat_id)
    except asyncpg.exceptions.UniqueViolationError:
        # The user already re-linked this WA chat to the new supergroup by hand, so a second
        # row holds the target tg_chat_id. Fold the stale pair into it — leaving both active
        # would deliver every message into that group twice under fan-out.
        from ..db import find_chat_pair_by_tg_chat, merge_chat_pairs
        target_id = await find_chat_pair_by_tg_chat(chat_pair_id, int(new_tg_chat_id))
        if not target_id:
            logger.error("chat_pair %s collides on tg_chat_id %s but no target pair found",
                         chat_pair_id, new_tg_chat_id)
            return
        try:
            await merge_chat_pairs(stale_id=chat_pair_id, target_id=target_id)
        except Exception as exc:
            logger.error("Failed to merge chat_pair %s into %s: %s", chat_pair_id, target_id, exc)
    except Exception as exc:
        logger.error("Failed to migrate chat_pair %s: %s", chat_pair_id, exc)


async def _persist_event(state: MessageState) -> None:
    from ..db import insert_message_event
    await insert_message_event(state)
