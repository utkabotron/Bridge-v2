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
    message_type: str          # text | image | video | audio | document
    media_s3_url: Optional[str]
    media_mime: Optional[str]
    media_filename: Optional[str]
    timestamp: int
    from_me: bool
    is_edited: bool

    # Resolved by validate node
    chat_pair_id: Optional[int]
    tg_chat_id: Optional[int]
    target_language: str

    # Set by translate node
    translated_text: Optional[str]
    translation_ms: Optional[int]
    cache_hit: bool

    # Set by format node
    formatted_text: Optional[str]

    # Fallback routing
    fallback_to_admins: bool

    # Set by deliver node
    delivery_status: str       # pending | delivered | failed
    error: Optional[str]
