"""bridge_shared.glossary_review: the admin's ✅ / ✏️ / ❌ message under the digest."""
from __future__ import annotations

from bridge_shared import glossary_review as review


def _row(i, status="proposed", **over):
    return {"id": i, "source": f"שם{i}", "translation": f"Имя{i}", "kind": "person", "status": status,
            "evidence": None, "chat_renderings": {f"Имя{i}": 2, f"Имья{i}": 1}, "also_word": False, **over}


def test_batch_message_has_three_buttons_per_open_name_and_carries_the_batch():
    rows = [_row(1), _row(2, evidence="Givolim · https://www.givolim.org.il/about", also_word=True),
            _row(3, status="verified")]
    text, keyboard = review.build(rows, remaining=40)

    assert "1. <b>שם1</b> → Имя1" in text
    assert "в чатах: Имя1×2, Имья1×1" in text
    assert "Givolim · givolim.org.il" in text and "🔸" in text
    assert "✅ 3. <b>שם3</b>" in text                       # decided: marked, no buttons
    assert [b[1] for b in keyboard[0]] == ["gl:ok:1", "gl:ed:1", "gl:no:1"]
    assert len(keyboard) == 3                                 # 2 open names + the batch row
    assert keyboard[-1] == [("Следующие → (ещё 40)", "gl:more:1,2,3")]
    assert review.batch_ids([d for row in keyboard for _, d in row]) == [1, 2, 3]


def test_everything_decided_and_nothing_left():
    text, keyboard = review.build([_row(1, status="rejected")], remaining=0)
    assert "❌ 1." in text and "Всё разобрано" in text
    assert keyboard == [[("Обновить", "gl:more:1")]]


def test_callback_data_fits_telegram_limit_for_large_ids():
    rows = [_row(40000 + i) for i in range(review.BATCH)]
    _, keyboard = review.build(rows, remaining=5)
    assert all(len(d.encode()) <= 64 for row in keyboard for _, d in row)
    assert review.batch_ids([keyboard[-1][0][1]]) == [r["id"] for r in rows]


def test_edit_prompt_round_trips_its_reference():
    prompt = review.edit_prompt({"id": 123, "source": "שגיא", "translation": "Саги"}, 456, [123, 7])
    assert "שגיא" in prompt and "Саги" in prompt
    plain = prompt.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", "")
    assert review.parse_ref(plain) == (123, 456, [123, 7])
    assert review.parse_ref("просто текст") is None
    assert review.parse_ref("ref g:zz:notanumber:1") is None
