"""Node functions for the message pipeline (run in order by graph.Pipeline).

Each node receives MessageState, mutates a copy, and returns it. Model calls go through
src/llm.py, which records model, tokens and cost of each one in llm_usage.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time

from bridge_shared.scripts import CYRILLIC_RE, SOURCE_SCRIPT_RE, target_script_re

from ..config import (
    AB_ALWAYS_B_USERS, OPENAI_MODEL,
    TRANSLATION_UNAVAILABLE_NOTE,
    VOICE_AUTO_TRANSCRIBE, VOICE_TRANSCRIPT_TITLE,
    MEDIA_FAILED_NOTE, EDITED_MARK, OWN_MESSAGE_PREFIX,
    ADMIN_NO_PAIR_FALLBACK, ADMIN_TG_IDS,
)
from ..llm import chat as llm_chat
from ..models.message import MessageState
from ..utils.telegram_format import bold, esc
from .cache import (
    get_cached, set_cached, get_cached_global, set_cached_global, get_chat_profile, set_chat_profile,
    lookup_chat_pairs, invalidate_chat_pairs,
)
from .prompts import VARIANTS, choose_variant, get_translate_prompt, format_chat_context

logger = logging.getLogger(__name__)

# Media types eligible for the Analyze button (no video in v1)
_ANALYZABLE_TYPES = {"image", "photo", "audio", "voice", "ptt", "document"}


# ── Node: validate ────────────────────────────────────────

def _is_russian_text(text: str) -> bool:
    """Cyrillic with none of the source scripts — all the admin fallback needs to know.

    Text with no letters at all (a bare emoji, digits) counts as Russian too: there is
    nothing in it to forward, and the old language detector gave up on it the same way.
    """
    if not any(ch.isalpha() for ch in text):
        return True
    return bool(CYRILLIC_RE.search(text)) and not SOURCE_SCRIPT_RE.search(text)


async def validate_node(state: MessageState) -> MessageState:
    """Resolve chat_pair_id, tg_chat_id, target_language.

    The consumer resolves the pairs itself to fan a group message out to every active
    pair of that chat, and hands each run its own pre-resolved pair — or `pairs_resolved`
    when the chat has none — so this node is a pass-through instead of a second lookup.
    The lookup below is for a state that arrives with neither.
    """
    if state.get("chat_pair_id"):
        return state

    if state.get("pairs_resolved"):
        pairs = []
    else:
        pairs = await lookup_chat_pairs(state["user_id"], state["wa_chat_id"])
    pair = pairs[0] if pairs else None

    if not pair:
        logger.warning("No active chat pair for user=%s chat=%s", state["user_id"], state["wa_chat_id"])

        # Fallback to admins only for admin's own WA messages, and only when explicitly
        # enabled — otherwise an unpaired chat is simply skipped.
        if ADMIN_NO_PAIR_FALLBACK and state["user_id"] in ADMIN_TG_IDS:
            has_media = bool(state.get("media_s3_url")) or bool(state.get("media_failed"))
            text = state.get("original_text", "").strip()
            is_russian = _is_russian_text(text)

            # Forward an unpaired admin chat into the bot on any media or any non-Russian
            # text. A caption-less video/photo has empty original_text, which reads as
            # Russian and would be dropped — so media must bypass the language gate.
            # Russian-only text with no media still falls through to skipped.
            if has_media or not is_russian:
                logger.info("Admin no-pair fallback (media=%s, russian=%s) → send to admins",
                            has_media, is_russian)
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


# ── Passthrough guard ─────────────────────────────────────
# ~1% of Hebrew messages used to reach the recipient verbatim: the model handed the
# source back untranslated (prompt rule 3 misfiring, or a lazy reply at temperature 0),
# and translate_node delivered it — a message worthless to a reader who does not read the
# source script. These are the worst-scoring translations we produce. Detect the echo and
# give the model one corrective turn before giving up. Script regexes: bridge_shared.scripts.


def _looks_untranslated(original: str, translated: str, target_language: str) -> bool:
    """True when the model echoed the source instead of translating it.

    Two signals: an exact passthrough (output == input), or output that still carries
    source-script characters while none of the target's own script is present — the source
    was handed back verbatim. Deliberately conservative: a partial translation that mixes
    some leftover source words with real target-script text is NOT flagged, so legitimate
    mixed messages never trigger a needless retry.
    """
    if not translated:
        return False  # empty output is handled by the degenerate-cache guard
    o = original.strip()
    t = translated.strip()
    if t == o:
        return True
    tgt_re = target_script_re(target_language)
    if tgt_re is None:
        return False  # cannot reason about scripts for this target language
    return bool(SOURCE_SCRIPT_RE.search(t)) and not tgt_re.search(t)


# ── Node: translate ───────────────────────────────────────

async def translate_node(state: MessageState) -> MessageState:
    """Translate original_text to target_language using LLM with Redis cache."""
    text = state["original_text"].strip()

    if not text:
        return {**state, "translated_text": text, "translation_ms": 0, "cache_hit": False}

    lang = state.get("target_language", "Russian")
    chat_pair_id = state.get("chat_pair_id")

    # Load chat profile: Redis cache → PostgreSQL. Most pairs have none, so that answer is
    # cached too ({}) — None means "not cached", not "no profile".
    chat_context = ""
    if chat_pair_id:
        profile = await get_chat_profile(chat_pair_id)
        if profile is None:
            from ..db import fetch_chat_profile
            profile = await fetch_chat_profile(chat_pair_id) or {}
            await set_chat_profile(chat_pair_id, profile)
        if profile:
            chat_context = format_chat_context(profile)

    # Determine if this chat has a meaningful profile (glossary or member names)
    has_profile = bool(chat_context)

    # A/B: odd pairs get variant B while the flag is on. The version travels with the
    # message (cache key, LangSmith tag, message_events.prompt_version) so the nightly
    # evaluation can compare the two.
    from ..feature_flags import is_enabled
    variant = choose_variant(chat_pair_id, await is_enabled("prompt_ab_enabled"),
                             user_id=state.get("user_id"), always_b_users=AB_ALWAYS_B_USERS)
    version = VARIANTS[variant]["version"]

    # Cache lookup: pair-specific first; for chats without profile also check global cache
    cached = await get_cached(text, lang, chat_pair_id, context=chat_context, version=version)
    if cached:
        logger.debug("Translation cache HIT (pair-specific)")
        return {**state, "translated_text": cached, "translation_ms": 0, "cache_hit": True,
                "prompt_version": version}

    if not has_profile:
        cached_global = await get_cached_global(text, lang, version=version)
        if cached_global:
            logger.debug("Translation cache HIT (global)")
            await set_cached(text, lang, cached_global, chat_pair_id, context=chat_context,
                             version=version)  # populate pair cache too
            return {**state, "translated_text": cached_global, "translation_ms": 0, "cache_hit": True,
                    "prompt_version": version}

    # LLM call — traced by LangSmith automatically
    t0 = time.monotonic()
    messages = [
        {"role": "system", "content": get_translate_prompt(lang, chat_context, variant)},
        {"role": "user", "content": text},
    ]
    model = VARIANTS[variant]["model"] or OPENAI_MODEL
    try:
        translated = (await llm_chat(messages, model=model, purpose="translate", tag=version)).text
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
            "translation_error": str(exc)[:500],
            "prompt_version": version,
        }

    # If the model echoed the source instead of translating, give it one corrective turn.
    # Most passthroughs are a lazy reply the model fixes when told the previous output was
    # untranslated; if it still refuses we deliver what we have but never cache it.
    passthrough = False
    if _looks_untranslated(text, translated, lang):
        logger.warning("Translation passthrough (lang=%s, src_len=%d) — retrying once", lang, len(text))
        retry_messages = messages + [
            {"role": "assistant", "content": translated},
            {"role": "user", "content": (
                f"Your reply was NOT translated into {lang} — it repeated the source text. "
                f"Translate EVERY word into {lang} now, leaving nothing in the original "
                f"language. Output only the {lang} translation."
            )},
        ]
        try:
            retried = (await llm_chat(retry_messages, model=model, purpose="translate_retry", tag=version)).text
        except Exception as exc:
            logger.error("Passthrough retry failed (%s) — keeping first result", exc)
            retried = ""
        if retried and not _looks_untranslated(text, retried, lang):
            translated = retried
        else:
            passthrough = True
            logger.warning("Translation still untranslated after retry (lang=%s) — delivering as-is", lang)

    translation_ms = int((time.monotonic() - t0) * 1000)

    # Guard against caching a bad translation: degenerate (empty or a tiny fragment of a
    # long source — usually a truncated/filtered response) or an untranslated passthrough.
    # Caching it would serve that bad result for 24h, and globally it would poison every
    # profileless pair. Deliver what we got this once, but do not persist it to cache.
    is_degenerate = (not translated) or (len(text) > 80 and len(translated) < 0.3 * len(text))
    if is_degenerate:
        logger.warning(
            "Skipping cache for degenerate translation (src_len=%d, out_len=%d, lang=%s)",
            len(text), len(translated), lang,
        )
    if not (is_degenerate or passthrough):
        await set_cached(text, lang, translated, chat_pair_id, context=chat_context, version=version)
        if not has_profile:
            await set_cached_global(text, lang, translated, version=version)

    return {
        **state,
        "translated_text": translated,
        "translation_ms": translation_ms,
        "cache_hit": False,
        "translation_passthrough": passthrough,
        "prompt_version": version,
    }



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
    # An edit that rewrites the delivered message needs no mark of ours — Telegram adds
    # its own "edited" label. The mark is for the fallback, where the edit still arrives
    # as a second, near-identical message.
    header_plain = list(header)
    if state.get("is_edited"):
        header.append(esc(EDITED_MARK))

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

    def _compose(head: list[str]) -> str:
        # Sections are separated by a single blank line. Joining blindly left doubled gaps
        # whenever a section was empty (e.g. media that failed to download, which has no text).
        lines = ([" ".join(head), ""] if head else []) + parts
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    return {
        **state,
        "formatted_text": _compose(header),
        "formatted_text_plain": _compose(header_plain),
    }


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
    tg_target, target_event_id = await _resolve_reply_target(state)
    state = {**state, "reply_to_message_id": tg_target, "edit_target_event_id": target_event_id}

    # A location has no text worth translating; send it as a real map pin.
    if state.get("location"):
        return await _deliver_location(state, tg_chat_id)

    # A WhatsApp edit revises a message we already delivered — rewrite that Telegram
    # message instead of sending a second copy. Falls through to the old behaviour when
    # the original predates this feature, was never delivered, or Telegram refuses.
    if state.get("is_edited") and state.get("reply_to_message_id"):
        edited = await _deliver_edit(state, tg_chat_id)
        if edited is not None:
            return edited

    has_media = bool(state.get("media_s3_url"))
    msg_type = state.get("message_type", "text")
    is_analyzable = has_media and msg_type in _ANALYZABLE_TYPES

    if is_analyzable:
        return await _deliver_media_with_button(state, tg_chat_id)

    return await _deliver_simple(state, tg_chat_id)


async def _resolve_reply_target(state: MessageState) -> tuple[int | None, int | None]:
    """(Telegram message_id, message_events.id) of the message this one refers to.

    The event id is what lets an edited photo keep its Analyze button: the button's
    callback names the original event, and Telegram drops the keyboard on edit unless it
    is sent again.
    """
    from ..db import find_delivered_event

    chat_pair_id = state.get("chat_pair_id")
    if not chat_pair_id:
        return None, None

    # An edit should attach to the message it revises; a reply, to the message it quotes.
    target_wa_id = None
    if state.get("is_edited"):
        # Edits are stored under "<original>:edit:<hash>" — strip back to the original.
        target_wa_id = (state.get("wa_message_id") or "").split(":edit:")[0] or None
    elif (state.get("quoted") or {}).get("wa_message_id"):
        target_wa_id = state["quoted"]["wa_message_id"]

    if not target_wa_id:
        return None, None
    return await find_delivered_event(target_wa_id, chat_pair_id)


def _analyze_markup(event_id: int) -> dict:
    """Inline keyboard that offers to analyze the media of `event_id`."""
    return {
        "inline_keyboard": [[
            {"text": "\U0001f50d Analyze", "callback_data": f"analyze:{event_id}"},
        ]],
    }


async def _deliver_edit(state: MessageState, tg_chat_id: int) -> MessageState | None:
    """Rewrite the Telegram message this WhatsApp edit revises.

    Returns None when Telegram refuses the edit — the original may be too old to edit in
    a channel, deleted, or split across several messages — and the caller then delivers
    the edit as a new message the way it always did.
    """
    from ..telegram_sender import edit_message

    tg_message_id = state["reply_to_message_id"]
    # Media keeps its text in a caption; without a file the original went out as plain
    # text, even for a photo whose download failed.
    has_media = bool(state.get("media_s3_url"))
    msg_type = state.get("message_type", "text") if has_media else "text"

    reply_markup = None
    event_id = state.get("edit_target_event_id")
    if event_id and has_media and state.get("message_type") in _ANALYZABLE_TYPES:
        # The button belongs to the original media's event — the edit changed the caption,
        # not the file.
        reply_markup = _analyze_markup(event_id)

    ok, error = await edit_message(
        chat_id=tg_chat_id,
        message_id=tg_message_id,
        text=state.get("formatted_text_plain") or state["formatted_text"],
        message_type=msg_type,
        reply_markup=reply_markup,
    )
    if not ok:
        logger.info("In-place edit of Telegram message %s failed (%s) — sending as a new message",
                    tg_message_id, error)
        return None

    # The edit is recorded against the same Telegram message, so the next edit (and any
    # reply to it) resolves to the very message the reader sees.
    result = {**state, "tg_chat_id": tg_chat_id, "delivery_status": "delivered",
              "error": None, "tg_message_id": tg_message_id}
    await _persist_event(result)
    return result


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
        await _migrate_chat_pair(state, migrate_id)
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
    reply_markup = _analyze_markup(event_id)
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
        await _migrate_chat_pair(state, migrate_id)
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
    """Send message to all admin Telegram IDs (config.ADMIN_TG_IDS)."""
    from ..telegram_sender import send_message

    if not ADMIN_TG_IDS:
        logger.warning("fallback_to_admins=True but ADMIN_TG_IDS is empty")
        result = {**state, "delivery_status": "failed", "error": "no_admin_ids"}
        await _persist_event(result)
        return result

    chat_name = state.get("wa_chat_name", "Unknown")
    text = f"[WA: {chat_name}]\n{state.get('formatted_text', '')}"

    errors = []
    for admin_id in ADMIN_TG_IDS:
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
        logger.info("Fallback message sent to %d admins", len(ADMIN_TG_IDS))

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
    # The cached lookup still lists this pair; without this every later message retries the
    # dead chat (and trips the failure-rate alert) until the entry expires.
    await invalidate_chat_pairs(state.get("user_id"), state.get("wa_chat_id") or "")
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


async def _migrate_chat_pair(state: MessageState, new_tg_chat_id: int) -> None:
    """Update chat_pairs tg_chat_id when Telegram group migrates to supergroup."""
    chat_pair_id = state.get("chat_pair_id")
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
    # The cached lookup holds the old tg_chat_id (or a pair that was just merged away).
    await invalidate_chat_pairs(state.get("user_id"), state.get("wa_chat_id") or "")


async def _persist_event(state: MessageState) -> None:
    from ..db import insert_message_event
    await insert_message_event(state)
