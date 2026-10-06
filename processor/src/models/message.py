"""Pydantic models and LangGraph state for the message pipeline."""
from __future__ import annotations

from typing import Optional
from typing_extensions import TypedDict


class MessageState(TypedDict):
    """State flowing through the LangGraph pipeline."""

    # Input fields (set by consumer on arrival)
    wa_message_id: str
    wa_chat_id: str
    wa_chat_name: str
    user_id: int
    sender_name: str
    original_text: str
    message_type: str          # text | image | video | audio | ptt | document | location | vcard
    media_s3_url: Optional[str]
    media_mime: Optional[str]
    media_filename: Optional[str]
    timestamp: int
    from_me: bool
    is_edited: bool
    # Media that WhatsApp would not hand over; the reader is told what was lost.
    media_failed: bool
    # {wa_message_id, body, sender} of the quoted message, when this is a reply.
    quoted: Optional[dict]
    # {latitude, longitude, name} — delivered as a Telegram map pin, never translated.
    location: Optional[dict]
    # [{name, phones}] parsed from a shared vCard.
    contacts: Optional[list]

    # Set by the consumer once it has looked the chat's pairs up (and found none), so
    # validate does not repeat the lookup.
    pairs_resolved: bool

    # Resolved by validate node
    chat_pair_id: Optional[int]
    tg_chat_id: Optional[int]
    target_language: str

    # Set by translate node
    translated_text: Optional[str]
    translation_ms: Optional[int]
    cache_hit: bool
    # True when the LLM was unreachable and the original was delivered untranslated.
    translation_failed: bool
    # Why it failed (the LLM exception text) — tells an outage from an empty balance.
    translation_error: Optional[str]
    # The model echoed the source and one corrective retry did not fix it.
    translation_passthrough: bool
    # Which prompt (A/B variant) produced translated_text.
    prompt_version: Optional[str]

    # Set by format node
    formatted_text: Optional[str]
    # Same text without the "edited" mark — used when the edit rewrites the delivered
    # Telegram message, which Telegram labels as edited by itself.
    formatted_text_plain: Optional[str]

    # Fallback routing
    fallback_to_admins: bool

    # Set by deliver node
    # Telegram message this one replies to (a quote, or the message an edit revises).
    reply_to_message_id: Optional[int]
    # message_events.id of that same message — lets an edited photo keep its Analyze button.
    edit_target_event_id: Optional[int]
    # The quoted message exists in Telegram, so the reply itself shows the quote.
    quote_threaded: bool
    tg_message_id: Optional[int]
    delivery_status: str       # pending | delivered | failed | skipped
    error: Optional[str]
