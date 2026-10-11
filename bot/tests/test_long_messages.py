"""Telegram's 4096-character limit: long analyses go out in several messages."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.src.utils.telegram_format import TELEGRAM_LIMIT, pack_messages, split_text


def test_split_text_cuts_at_paragraphs_then_lines_then_spaces():
    para = "слово " * 300                                   # 1800 chars
    pieces = split_text("\n\n".join([para] * 5), limit=3500)
    assert pieces == [para] * 5                 # two paragraphs (3602) exceed the limit: one each
    lines = split_text("\n".join(["строка " * 100] * 10), limit=3500)
    assert all(len(p) <= 3500 for p in lines) and all(p.startswith("строка") for p in lines)
    assert split_text("а" * 8000, limit=3500) == ["а" * 3500, "а" * 3500, "а" * 1000]   # one long word
    assert split_text("short") == ["short"] and split_text("") == [""]


def test_pack_messages_never_cuts_a_block():
    blocks = ["<b>Analysis</b>"] + ["x" * 3000, "<code>" + "y" * 3000 + "</code>", "z" * 10]
    messages = pack_messages(blocks)
    assert all(len(m) <= TELEGRAM_LIMIT for m in messages)
    assert messages[0].startswith("<b>Analysis</b>\n\nxxx")
    assert messages[1].startswith("<code>") and messages[1].endswith("</code>\n\nzzzzzzzzzz")


@pytest.mark.asyncio
async def test_a_long_analysis_reaches_the_reader_in_several_messages():
    """10.10 13:51: a 7736-character PDF analysis failed with "Message is too long"."""
    from bot.src.handlers.analyze import cb_analyze_media

    query = MagicMock()
    query.data = "analyze:23043"
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.message.chat_id, query.message.message_id = -100, 7
    update = MagicMock(callback_query=query)
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"result_text": ("Абзац анализа. " * 60 + "\n\n") * 9,
                              "analysis_type": "document", "processing_ms": 5000}

    with patch("bot.src.handlers.analyze.http_client.post", new_callable=AsyncMock, return_value=resp), \
         patch("bot.src.db.event_in_chat", new=AsyncMock(return_value=True)):
        await cb_analyze_media(update, ctx)

    sent = [c.kwargs["text"] for c in ctx.bot.send_message.await_args_list]
    assert len(sent) >= 2 and all(len(t) <= TELEGRAM_LIMIT for t in sent)
    assert sent[0].startswith("\U0001f4c4 <b>Analysis</b>")
    assert all(c.kwargs["reply_to_message_id"] == 7 for c in ctx.bot.send_message.await_args_list)
