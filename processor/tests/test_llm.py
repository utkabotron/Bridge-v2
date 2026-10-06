"""src/llm.py: request shape per model family, cost, ledger, and media routing through it."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from processor.src import llm


def test_reasoning_models_get_their_own_parameters():
    msgs = [{"role": "user", "content": "hi"}]
    new = llm.build_request("gpt-6-luna", msgs, max_tokens=500)
    assert new == {"model": "gpt-6-luna", "messages": msgs, "reasoning_effort": "none", "max_completion_tokens": 500}

    old = llm.build_request("gpt-4.1-mini", msgs, max_tokens=500)
    assert old == {"model": "gpt-4.1-mini", "messages": msgs, "temperature": 0, "max_tokens": 500}
    assert "max_tokens" not in llm.build_request("gpt-4.1-mini", msgs)


def test_cost_from_the_price_table():
    assert llm.token_cost("gpt-6-luna", 1_000_000, 1_000_000) == 0.60
    assert llm.token_cost("gpt-4.1-mini", 1_000_000, 0) == 0.40
    assert llm.token_cost("no-such-model", 10, 10) == 0.0


def _fake_client(text="Привет", prompt_tokens=100, completion_tokens=20):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=f"  {text}\n"))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )
    fake = MagicMock()
    fake.chat.completions.create = AsyncMock(return_value=response)
    return fake


@pytest.mark.asyncio
async def test_chat_returns_text_and_cost_and_writes_the_ledger():
    fake = _fake_client()
    with patch.object(llm, "client", return_value=fake), \
         patch.object(llm, "_record") as record:
        result = await llm.chat([{"role": "user", "content": "שלום"}], model="gpt-6-luna",
                                purpose="translate", tag="v2.10@gpt-6-luna", timeout=5)

    assert result.text == "Привет"
    assert (result.tokens_in, result.tokens_out) == (100, 20)
    assert result.cost_usd == pytest.approx((100 * 0.10 + 20 * 0.50) / 1e6)
    sent = fake.chat.completions.create.await_args.kwargs
    assert sent["reasoning_effort"] == "none" and sent["timeout"] == 5
    record.assert_called_once()
    assert record.call_args.args[:3] == ("translate", "gpt-6-luna", "v2.10@gpt-6-luna")


@pytest.mark.asyncio
async def test_ledger_write_failure_never_breaks_a_translation():
    fake = _fake_client()
    pool = MagicMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("db down"))
    with patch.object(llm, "client", return_value=fake), \
         patch("processor.src.db.get_pool", new=AsyncMock(return_value=pool)):
        result = await llm.chat([{"role": "user", "content": "x"}], model="gpt-6-luna", purpose="translate")
        for task in list(llm._pending):
            await task
    assert result.text == "Привет"
    pool.execute.assert_awaited()


@pytest.mark.asyncio
async def test_transcription_cost_is_per_minute_and_empty_audio_is_empty():
    fake = MagicMock()
    fake.audio.transcriptions.create = AsyncMock(return_value=SimpleNamespace(
        text="  שלום  ", usage=SimpleNamespace(type="duration", seconds=120),
    ))
    with patch.object(llm, "client", return_value=fake), patch.object(llm, "_record") as record:
        text = await llm.transcribe(b"ogg", "voice.ogg", model="gpt-transcribe")
    assert text == "שלום"
    assert record.call_args.args[5] == pytest.approx(2 * 0.0045)

    fake.audio.transcriptions.create = AsyncMock(return_value=SimpleNamespace(text="", usage=None))
    with patch.object(llm, "client", return_value=fake), patch.object(llm, "_record"):
        assert await llm.transcribe(b"noise", "voice.ogg", model="gpt-transcribe") == ""


@pytest.mark.asyncio
async def test_media_analysis_goes_through_the_shared_client():
    from processor.src import media_analyzer

    chat = AsyncMock(return_value=llm.Completion(text="Перевод", model="m"))
    with patch.object(media_analyzer.llm, "chat", new=chat), \
         patch.object(media_analyzer.llm, "transcribe", new=AsyncMock(return_value="")):
        assert await media_analyzer.analyze_image(b"\x89PNG", "image/png", "Russian") == "Перевод"
        assert chat.await_args.kwargs["purpose"] == "image"
        assert chat.await_args.args[0][1]["content"][0]["type"] == "image_url"
        # silence never reaches the translation step
        assert await media_analyzer.transcribe_audio(b"ogg", "v.ogg", "Russian") == "(empty audio)"
        assert chat.await_count == 1
