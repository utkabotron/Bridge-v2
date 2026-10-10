"""Voice notes sent to the bot in private: translation plus Hebrew in Latin letters."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _chat(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return AsyncMock(return_value=MagicMock(text=text))


@pytest.mark.asyncio
async def test_russian_speech_becomes_hebrew_with_a_latin_reading():
    """It used to translate into the account language: Russian in, the same Russian out twice."""
    from processor.src import media_analyzer as ma

    chat = _chat({"translation": "שלום, מה נשמע?", "latin": "Shalom, ma nishma?"})
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="Привет, как дела?")), \
         patch.object(ma.llm, "chat", new=chat):
        out = await ma.direct_voice(b"ogg", "voice.ogg")

    assert out == "🎙 Привет, как дела?\n\n🇮🇱 שלום, מה נשמע?\n🔤 Shalom, ma nishma?"
    system = chat.await_args.args[0][0]["content"]
    assert "into Hebrew" in system and "translated Hebrew" in system


@pytest.mark.asyncio
async def test_hebrew_speech_becomes_russian_and_the_original_is_read_out():
    from processor.src import media_analyzer as ma

    chat = _chat({"translation": "Привет, как дела?", "latin": "Shalom, ma nishma?"})
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="שלום, מה נשמע?")), \
         patch.object(ma.llm, "chat", new=chat):
        out = await ma.direct_voice(b"ogg", "voice.ogg")

    assert out == "🎙 שלום, מה נשמע?\n🔤 Shalom, ma nishma?\n\n🇷🇺 Привет, как дела?"
    assert "into Russian" in chat.await_args.args[0][0]["content"]


@pytest.mark.asyncio
async def test_a_non_json_answer_is_still_delivered_and_silence_is_said_so():
    from processor.src import media_analyzer as ma

    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="Привет")), \
         patch.object(ma.llm, "chat", new=_chat("שלום")):
        assert await ma.direct_voice(b"ogg", "voice.ogg") == "🎙 Привет\n\n🇮🇱 שלום"
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="")):
        assert await ma.direct_voice(b"ogg", "voice.ogg") == "(empty audio)"


def test_a_hebrew_name_in_a_russian_sentence_is_still_russian_speech():
    from processor.src.media_analyzer import is_hebrew_speech
    assert not is_hebrew_speech("Завтра в גבעולים спортивный день")
    assert is_hebrew_speech("מחר יום ספורט בגבעולים, תביאו מים")
