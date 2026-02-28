"""LangGraph node functions for the message pipeline.

Each node receives MessageState, mutates a copy, and returns it.
LangSmith traces every node automatically via LANGCHAIN_TRACING_V2=true.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any


from langdetect import detect, LangDetectException
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from ..models.message import MessageState
from .cache import get_cached, set_cached
from .prompts import PROMPT_VERSION, get_translate_prompt

logger = logging.getLogger(__name__)

# Shared LLM instance — model pinned for reproducibility
_llm: Any = None


def get_llm() -> ChatOpenAI:
    global _llm
    if _llm is None:
        _llm = ChatOpenAI(
            model="gpt-4o-mini",
            temperature=0,
            tags=["bridge-v2", f"prompt-{PROMPT_VERSION}"],
        )
    return _llm


# ── DB helpers (lazy import to avoid circular deps) ──────

async def _fetch_chat_pair(user_id: int, wa_chat_id: str) -> dict | None:
    from ..db import fetch_active_chat_pair
    return await fetch_active_chat_pair(user_id, wa_chat_id)


# ── Node: validate ────────────────────────────────────────

async def validate_node(state: MessageState) -> MessageState:
    """Resolve chat_pair_id, tg_chat_id, target_language from DB."""
    pair = await _fetch_chat_pair(state["user_id"], state["wa_chat_id"])

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
                "delivery_status": "failed", "error": "no_chat_pair"}

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

    # Cache lookup
    cached = await get_cached(text, lang)
    if cached:
        logger.debug("Translation cache HIT")
        return {**state, "translated_text": cached, "translation_ms": 0, "cache_hit": True}

    # LLM call — traced by LangSmith automatically
    t0 = time.monotonic()
    messages = [
        SystemMessage(content=get_translate_prompt(lang)),
        HumanMessage(content=text),
    ]
    response = await get_llm().ainvoke(messages)
    translation_ms = int((time.monotonic() - t0) * 1000)

    translated = response.content.strip()

    # Store in cache
    await set_cached(text, lang, translated)

    return {**state, "translated_text": translated, "translation_ms": translation_ms, "cache_hit": False}


# ── Node: format ──────────────────────────────────────────

def format_node(state: MessageState) -> MessageState:
    """Compose the final Telegram message text.

    Format:
        *Sender Name*
        original text

        translated text
    """
    original = state.get("original_text", "")
    translated = state.get("translated_text") or original
    sender = state.get("sender_name", "")

    parts = []
    if sender:
        parts.append(f"*{sender}*")
        parts.append("")
    parts.append(original)
    parts.append("")
    parts.append(translated)

    formatted = "\n".join(parts)
    return {**state, "formatted_text": formatted}


# ── Node: deliver ─────────────────────────────────────────

async def deliver_node(state: MessageState) -> MessageState:
    """Send formatted message to Telegram and persist to DB."""
    # Short-circuit if validation failed
    if state.get("delivery_status") == "failed":
        await _persist_event(state)
        return state

    # Fallback: send to each admin personally
    if state.get("fallback_to_admins"):
        return await _deliver_to_admins(state)

    tg_chat_id = state.get("tg_chat_id")
    if not tg_chat_id:
        return {**state, "delivery_status": "failed", "error": "missing tg_chat_id"}

    from ..telegram_sender import send_message
    ok, error = await send_message(
        chat_id=tg_chat_id,
        text=state["formatted_text"],
        media_url=state.get("media_s3_url"),
        message_type=state.get("message_type", "text"),
        media_filename=state.get("media_filename"),
    )

    new_status = "delivered" if ok else "failed"
    result = {**state, "delivery_status": new_status, "error": error}

    await _persist_event(result)
    return result


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
        ok, error = await send_message(
            chat_id=admin_id,
            text=text,
            media_url=state.get("media_s3_url"),
            message_type=state.get("message_type", "text"),
            media_filename=state.get("media_filename"),
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


async def _persist_event(state: MessageState) -> None:
    from ..db import insert_message_event
    await insert_message_event(state)
