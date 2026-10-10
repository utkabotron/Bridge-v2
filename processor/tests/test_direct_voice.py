"""Voice notes sent to the bot in private: translation plus Hebrew in Latin letters."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _chat(*answers):
    return AsyncMock(side_effect=[MagicMock(text=a) for a in answers])


@pytest.mark.asyncio
async def test_russian_speech_becomes_hebrew_with_a_latin_reading():
    """It used to translate into the account language: Russian in, the same Russian out twice."""
    from processor.src import media_analyzer as ma

    chat = _chat("שלום, מה נשמע?", "Shalom, ma nishma?")
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="Привет, как дела?")), \
         patch.object(ma.llm, "chat", new=chat):
        out = await ma.direct_voice(b"ogg", "voice.ogg")

    assert out == [{"text": "Привет, как дела?", "copy": False},
                   {"text": "שלום, מה נשמע?", "copy": True},
                   {"text": "Shalom, ma nishma?", "copy": False}]
    assert ma.voice_text(out) == "Привет, как дела?\n\nשלום, מה נשמע?\n\nShalom, ma nishma?"
    translate_call, latin_call = chat.await_args_list
    assert "into Hebrew" in translate_call.args[0][0]["content"]          # the translator's prompt
    assert latin_call.args[0][1]["content"] == "שלום, מה נשמע?"           # read out: the Hebrew


@pytest.mark.asyncio
async def test_an_answer_outside_hebrew_script_is_retried():
    """13:22 on 10.10: the Russian text came back under the Hebrew flag."""
    from processor.src import media_analyzer as ma

    chat = _chat("Хен написала…", "חן כתבה…", "Khen katva…")
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="Хэн отправила сообщение")), \
         patch.object(ma.llm, "chat", new=chat):
        out = await ma.direct_voice(b"ogg", "voice.ogg")

    assert [p["text"] for p in out] == ["Хэн отправила сообщение", "חן כתבה…", "Khen katva…"]
    assert chat.await_args_list[1].kwargs["purpose"] == "voice_translate_retry"


@pytest.mark.asyncio
async def test_hebrew_speech_becomes_russian_and_the_original_is_read_out():
    from processor.src import media_analyzer as ma

    chat = _chat("Привет, как дела?", "Shalom, ma nishma?")
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="שלום, מה נשמע?")), \
         patch.object(ma.llm, "chat", new=chat):
        out = await ma.direct_voice(b"ogg", "voice.ogg")

    assert out == [{"text": "שלום, מה נשמע?", "copy": True},
                   {"text": "Shalom, ma nishma?", "copy": False},
                   {"text": "Привет, как дела?", "copy": False}]
    assert chat.await_args_list[1].args[0][1]["content"] == "שלום, מה נשמע?"


@pytest.mark.asyncio
async def test_no_latin_reading_still_delivers_and_silence_is_said_so():
    from processor.src import media_analyzer as ma

    chat = AsyncMock(side_effect=[MagicMock(text="שלום"), RuntimeError("down")])
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="Привет")), \
         patch.object(ma.llm, "chat", new=chat):
        assert [p["text"] for p in await ma.direct_voice(b"ogg", "voice.ogg")] == ["Привет", "שלום"]
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="")):
        assert await ma.direct_voice(b"ogg", "voice.ogg") == [{"text": "(empty audio)", "copy": False}]


def test_a_hebrew_name_in_a_russian_sentence_is_still_russian_speech():
    from processor.src.media_analyzer import is_hebrew_speech
    assert not is_hebrew_speech("Завтра в גבעולים спортивный день")
    assert is_hebrew_speech("מחר יום ספורט בגבעולים, תביאו מים")


@pytest.mark.asyncio
async def test_voice_rules_reach_the_prompt_and_hebrew_left_in_the_reading_is_retried():
    from processor.src import media_analyzer as ma

    chat = _chat("חן כתבה הודעה", "Khen katva הודעה", "Khen katva hoda'a")
    with patch.object(ma.llm, "transcribe", new=AsyncMock(return_value="Хэн написала сообщение")), \
         patch.object(ma.llm, "chat", new=chat):
        out = await ma.direct_voice(b"ogg", "voice.ogg")

    assert out[-1]["text"] == "Khen katva hoda'a"
    system = chat.await_args_list[0].args[0][0]["content"]
    assert "Palmal Demi Silver" in system and "Хэн → חן" in system
    assert chat.await_args_list[2].kwargs["purpose"] == "voice_latin_retry"
