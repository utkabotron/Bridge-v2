"""Unit tests for the LangGraph pipeline nodes."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def _base_state(**overrides):
    state = {
        "wa_message_id": "test-123",
        "wa_chat_id": "1234567890@g.us",
        "wa_chat_name": "Test Group",
        "user_id": 100,
        "sender_name": "Alice",
        "original_text": "שלום, מה שלומך?",
        "message_type": "text",
        "media_s3_url": None,
        "timestamp": 1700000000,
        "from_me": False,
        "is_edited": False,
        "chat_pair_id": None,
        "tg_chat_id": None,
        "target_language": "Russian",
        "translated_text": None,
        "translation_ms": None,
        "cache_hit": False,
        "formatted_text": None,
        "delivery_status": "pending",
        "error": None,
    }
    state.update(overrides)
    return state


# ── validate_node ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_node_no_pair():
    """When no chat pair exists, delivery_status should be 'failed'."""
    from processor.src.pipeline.nodes import validate_node

    with patch("processor.src.pipeline.nodes._fetch_chat_pair", new=AsyncMock(return_value=None)):
        result = await validate_node(_base_state())

    assert result["delivery_status"] == "failed"
    assert result["error"] == "no_chat_pair"


@pytest.mark.asyncio
async def test_validate_node_with_pair():
    """When a chat pair exists, tg_chat_id and target_language are set."""
    from processor.src.pipeline.nodes import validate_node

    pair = {"id": 7, "tg_chat_id": -1001234567890, "target_language": "Hebrew"}
    with patch("processor.src.pipeline.nodes._fetch_chat_pair", new=AsyncMock(return_value=pair)):
        result = await validate_node(_base_state())

    assert result["chat_pair_id"] == 7
    assert result["tg_chat_id"] == -1001234567890
    assert result["target_language"] == "Hebrew"
    assert result["delivery_status"] == "pending"


# ── translate_node ────────────────────────────────────────

@pytest.mark.asyncio
async def test_translate_node_cache_hit():
    """Cache hit should return translation without calling LLM."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(chat_pair_id=1, tg_chat_id=-100, target_language="Russian")

    with patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value="Привет, как дела?")):
        result = await translate_node(state)

    assert result["translated_text"] == "Привет, как дела?"
    assert result["cache_hit"] is True
    assert result["translation_ms"] == 0


@pytest.mark.asyncio
async def test_translate_node_llm_call():
    """Cache miss should call LLM and store result."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(chat_pair_id=1, tg_chat_id=-100, target_language="Russian")

    mock_response = MagicMock()
    mock_response.content = "Привет, как дела?"

    with patch("processor.src.pipeline.nodes.get_cached", new=AsyncMock(return_value=None)), \
         patch("processor.src.pipeline.nodes.set_cached", new=AsyncMock()), \
         patch("processor.src.pipeline.nodes.get_llm") as mock_llm:

        mock_llm.return_value.ainvoke = AsyncMock(return_value=mock_response)
        result = await translate_node(state)

    assert result["translated_text"] == "Привет, как дела?"
    assert result["cache_hit"] is False
    assert result["translation_ms"] >= 0


@pytest.mark.asyncio
async def test_translate_node_empty_text():
    """Empty text should be passed through without LLM call."""
    from processor.src.pipeline.nodes import translate_node

    state = _base_state(original_text="", target_language="Russian")
    result = await translate_node(state)

    assert result["translated_text"] == ""
    assert result["translation_ms"] == 0


# ── format_node ───────────────────────────────────────────

def test_format_node_with_sender():
    from processor.src.pipeline.nodes import format_node

    state = _base_state(
        sender_name="Bob",
        wa_chat_name="Team Chat",
        translated_text="Hello world",
        chat_pair_id=1,
        tg_chat_id=-100,
    )
    result = format_node(state)
    assert "Bob" in result["formatted_text"]
    assert "Hello world" in result["formatted_text"]


def test_format_node_with_media():
    """Media URL is no longer embedded in formatted_text — sent natively via Telegram API."""
    from processor.src.pipeline.nodes import format_node

    state = _base_state(
        translated_text="Check this out",
        media_s3_url="https://s3.amazonaws.com/bucket/file.jpg",
    )
    result = format_node(state)
    # Media link removed from formatted text — delivered natively by telegram_sender
    assert "s3.amazonaws.com" not in result["formatted_text"]
    assert "Check this out" in result["formatted_text"]


# ── graph routing ─────────────────────────────────────────

def test_should_translate_routing():
    from processor.src.pipeline.graph import _should_translate

    # Failed state → go to deliver
    assert _should_translate({"delivery_status": "failed", "original_text": "text"}) == "deliver"

    # No text → skip translation
    assert _should_translate({"delivery_status": "pending", "original_text": ""}) == "format"

    # Normal → translate
    assert _should_translate({"delivery_status": "pending", "original_text": "hello"}) == "translate"
