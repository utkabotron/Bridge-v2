"""✅ / ✏️ / ❌ under the digest's names message: decisions, the ✏️ reply, access."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

H = "bot.src.handlers.glossary"


def _row(i, status="proposed"):
    return {"id": i, "source": f"שם{i}", "translation": f"Имя{i}", "kind": "person", "status": status,
            "evidence": None, "chat_renderings": {}, "also_word": False}


def _callback(data, batch="1,2"):
    button = lambda d: SimpleNamespace(callback_data=d)  # noqa: E731
    message = SimpleNamespace(chat_id=77, message_id=500,
                              reply_markup=SimpleNamespace(inline_keyboard=[[button("gl:ok:1")],
                                                                            [button(f"gl:more:{batch}")]]))
    query = SimpleNamespace(data=data, message=message, answer=AsyncMock())
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
    ctx = MagicMock()
    ctx.bot.edit_message_text = AsyncMock()
    ctx.bot.send_message = AsyncMock()
    return update, ctx


def _admin(is_admin=True):
    return patch(f"{H}.get_user", new=AsyncMock(return_value={"is_admin": is_admin}))


@pytest.mark.asyncio
async def test_accept_records_the_decision_and_rebuilds_the_message():
    from bot.src.handlers.glossary import cb_glossary

    update, ctx = _callback("gl:ok:1")
    decide = AsyncMock(return_value={"id": 1, "source": "שם1", "translation": "Имя1", "status": "verified"})
    with _admin(), patch(f"{H}.glossary_decide", new=decide), \
         patch(f"{H}.glossary_rows", new=AsyncMock(return_value=[_row(1, "verified"), _row(2)])) as rows, \
         patch(f"{H}.glossary_proposed_count", new=AsyncMock(return_value=0)):
        await cb_glossary(update, ctx)

    decide.assert_awaited_once_with(1, "verified")
    rows.assert_awaited_once_with([1, 2])
    kwargs = ctx.bot.edit_message_text.await_args.kwargs
    assert kwargs["message_id"] == 500 and "✅ 1." in kwargs["text"]


@pytest.mark.asyncio
async def test_reject_and_a_name_already_decided_by_another_admin():
    from bot.src.handlers.glossary import cb_glossary

    update, ctx = _callback("gl:no:2")
    decide = AsyncMock(return_value=None)
    with _admin(), patch(f"{H}.glossary_decide", new=decide), \
         patch(f"{H}.glossary_rows", new=AsyncMock(return_value=[_row(1), _row(2, "verified")])), \
         patch(f"{H}.glossary_proposed_count", new=AsyncMock(return_value=0)):
        await cb_glossary(update, ctx)
    decide.assert_awaited_once_with(2, "rejected")
    assert "уже решено" in update.callback_query.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_non_admin_cannot_decide():
    from bot.src.handlers.glossary import cb_glossary

    update, ctx = _callback("gl:ok:1")
    decide = AsyncMock()
    with _admin(False), patch(f"{H}.glossary_decide", new=decide):
        await cb_glossary(update, ctx)
    decide.assert_not_awaited()
    assert update.callback_query.answer.await_args.kwargs.get("show_alert") is True


@pytest.mark.asyncio
async def test_edit_asks_for_a_reply_carrying_the_reference():
    from bridge_shared.glossary_review import parse_ref
    from bot.src.handlers.glossary import cb_glossary

    update, ctx = _callback("gl:ed:2")
    with _admin(), patch(f"{H}.glossary_rows", new=AsyncMock(return_value=[_row(2)])):
        await cb_glossary(update, ctx)
    text = ctx.bot.send_message.await_args.args[1]
    plain = text.replace("<code>", "").replace("</code>", "")
    assert parse_ref(plain) == (2, 500, [1, 2])


@pytest.mark.asyncio
async def test_more_sends_the_next_batch_without_this_one():
    from bot.src.handlers.glossary import cb_glossary

    update, ctx = _callback("gl:more:1,2")
    nxt = AsyncMock(return_value=([_row(3), _row(4)], 7))
    with _admin(), patch(f"{H}.glossary_next_batch", new=nxt):
        await cb_glossary(update, ctx)
    nxt.assert_awaited_once_with([1, 2], 10)
    assert "שם3" in ctx.bot.send_message.await_args.args[1]


@pytest.mark.asyncio
async def test_the_edit_reply_stores_the_admins_rendering():
    from bridge_shared.glossary_review import edit_prompt
    from bot.src.handlers.glossary import GLOSSARY_EDIT_REPLY, handle_glossary_edit

    prompt = edit_prompt(_row(2), 500, [1, 2]).replace("<b>", "").replace("</b>", "")
    prompt = prompt.replace("<code>", "").replace("</code>", "")
    replied = SimpleNamespace(text=prompt, from_user=SimpleNamespace(is_bot=True))
    message = SimpleNamespace(text="  Саги ", chat_id=77, reply_to_message=replied, reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1))
    ctx = MagicMock()
    ctx.bot.edit_message_text = AsyncMock()

    assert GLOSSARY_EDIT_REPLY.filter(message)
    assert not GLOSSARY_EDIT_REPLY.filter(SimpleNamespace(reply_to_message=None))

    decide = AsyncMock(return_value={"id": 2, "source": "שגיא", "translation": "Саги",
                                     "status": "verified", "also_word": True})
    with _admin(), patch(f"{H}.glossary_decide", new=decide), \
         patch(f"{H}.glossary_rows", new=AsyncMock(return_value=[_row(1), _row(2, "verified")])), \
         patch(f"{H}.glossary_proposed_count", new=AsyncMock(return_value=3)):
        await handle_glossary_edit(update, ctx)

    decide.assert_awaited_once_with(2, "verified", "Саги")
    assert "только в чатах" in message.reply_text.await_args.args[0]
    assert ctx.bot.edit_message_text.await_args.kwargs["message_id"] == 500
