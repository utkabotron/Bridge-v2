"""The one morning message: what is in it, and what only appears when something is wrong."""
from __future__ import annotations

from datetime import date, datetime

from flows.daily_digest import _plural, format_digest

TODAY = date(2026, 10, 7)


def _data(**over):
    base = {
        "date": TODAY,
        "system": {"wa_ready": 4, "wa_active": 4, "wa_ready_ids": {1, 2}, "queue": 0, "dlq": 0,
                   "dlq_dead": 0, "disk_pct": 73, "processor": {"failed": 0, "db_write_failed": 0}},
        "traffic": {"delivered": 70, "skipped": 260, "failed": 0, "translated": 34, "cached": 1,
                    "untranslated": 0, "passthrough": 0, "media": 5},
        "quality": {
            "avg_scores": {"quality": 4.6},
            "breakdown": {"by_source": {"bridge": {"n": 29, "bad_pct": 12.0}}},
            "worst_pair": {"key": 29, "bad": 3, "n": 9},
            "jev": {"mode": "off"},
        },
        "worst_pair_name": "צהרון א׳ 3 גבעולים 💓",
        "pending_suggestions": 0,
        "issues": [],
        "glossary": {"added": 4, "removed": 2},
        "users": [
            {"tg_user_id": 1, "tg_username": "pavelbrick", "wa_connected": True, "pairs": 9,
             "delivered": 31, "last_delivered": datetime(2026, 10, 6, 23, 10)},
            {"tg_user_id": 2, "tg_username": None, "wa_connected": True, "pairs": 1,
             "delivered": 2, "last_delivered": datetime(2026, 10, 6, 9, 11)},
            {"tg_user_id": 3, "tg_username": "ghost", "wa_connected": True, "pairs": 2,
             "delivered": 0, "last_delivered": None},
        ],
    }
    base.update(over)
    return base


def test_quiet_morning_has_no_attention_block():
    text = format_digest(_data())

    assert text.startswith("☀️ <b>Bridge — ср 7 окт</b>")
    assert "<b>Система:</b> ✅ WA 4/4 · очереди 0 · DLQ 0 · диск 73% · OpenAI ок" in text
    assert "<b>За сутки:</b> 70 доставлено · 260 без моста · 0 ошибок" in text
    assert "переводов 34 · кэш 1 · медиа 5" in text
    assert "<b>Качество:</b> 4.6 · плохих 12.0% (n=29) · худшая пара #29 צהרון א׳ 3 גבעולים 💓 (3 из 9)" in text
    assert "<b>Глоссарий:</b> +4 · снято 2" in text
    assert "Внимание" not in text
    assert "Неделя" not in text


def test_users_split_into_connected_and_not_by_live_wa_state():
    text = format_digest(_data())

    assert "<b>Пользователи</b> (2 из 3 подключены):" in text
    assert "<code>pavelbrick    </code> 9 пар · 31 · вчера 23:10" in text
    assert "<code>2             </code> 1 пара · 2 · вчера 09:11" in text  # no username → id
    assert "не подключены: ghost" in text  # wa_connected=true in DB, but no live client


def test_problems_surface_under_attention():
    text = format_digest(_data(
        system={"wa_ready": 3, "wa_active": 4, "wa_ready_ids": {1}, "queue": 12, "dlq": 3,
                "dlq_dead": 1, "disk_pct": 91, "processor": {"db_write_failed": 2}},
        traffic={"delivered": 10, "skipped": 0, "failed": 2, "translated": 8, "cached": 0,
                 "untranslated": 5, "passthrough": 1, "media": 0},
        issues=[{"severity": "critical", "title": "Delivery failures spiked"},
                {"severity": "warning", "title": "Slow translations"}],
        pending_suggestions=7,
    ))

    assert "<b>Система:</b> ⚠️ WA 3/4 · очереди 12 · DLQ 3 · диск 91% · OpenAI ⚠️" in text
    assert "· эхо 1" in text
    assert "⚠️ <b>Внимание:</b>" in text
    for expected in (
        "WhatsApp: готовы 3 из 4 клиентов",
        "DLQ: 3 в повторе",
        "DLQ: 1 сдались",
        "диск заполнен на 91%",
        "processor: 2 записей в БД не прошли",
        "5 сообщ. ушли без перевода (OpenAI)",
        "2 сообщ. не доставлены в Telegram",
        "🚨 Delivery failures spiked",
        "⚠️ Slow translations",
        "7 предложений к промпту ждут",
    ):
        assert expected in text, expected


def test_ab_line_appears_only_when_two_prompt_versions_were_evaluated():
    q = _data()["quality"]
    q["breakdown"]["by_prompt_version"] = [
        {"key": "v3.0", "n": 14, "quality": 4.71, "bad_pct": 7.1},
        {"key": "v2.10", "n": 15, "quality": 4.53, "bad_pct": 13.3},
    ]
    text = format_digest(_data(quality=q))
    assert "<b>A/B:</b> v2.10 4.53 (n=15, плохих 13.3%) vs v3.0 4.71 (n=14, плохих 7.1%)" in text

    q["breakdown"]["by_prompt_version"] = [{"key": "v2.10", "n": 29, "quality": 4.6, "bad_pct": 12.0}]
    assert "A/B" not in format_digest(_data(quality=q))


def test_missing_quality_run_is_itself_a_warning():
    text = format_digest(_data(quality=None))
    assert "<b>Качество:</b> ночная оценка не отработала" in text
    assert "translation-quality не записал результат" in text


def test_monday_carries_the_weekly_summary_and_three_recommendations():
    recs = [{"action": f"do {i}"} for i in range(5)]
    text = format_digest(_data(weekly={"summary": "Week was <fine>", "recommendations": recs}))
    assert "📈 <b>Неделя:</b> Week was &lt;fine&gt;" in text
    assert "3. do 2" in text and "4. do 3" not in text

    text = format_digest(_data(weekly={"pending": True}))
    assert "отчёт ещё считается" in text


def test_services_down_are_marked_not_crashed():
    text = format_digest(_data(system={"wa_error": "timeout", "redis_error": "refused"}))
    assert "WA ❌" in text and "очереди ?" in text and "processor ❌" in text
    assert "wa-service не отвечает: timeout" in text


def test_russian_plurals():
    assert [_plural(n, "пара", "пары", "пар") for n in (1, 2, 5, 11, 21, 22)] == \
        ["пара", "пары", "пар", "пар", "пара", "пары"]


def test_names_line_shows_auto_accepted_and_waiting():
    text = format_digest(_data(names={"auto_accepted": 456, "proposed": 244}))
    assert "Словарь имён:</b> ждут одобрения 244 · принято автоматически 456" in text
    assert "Словарь имён" not in format_digest(_data(names={"auto_accepted": 0, "proposed": 0}))


def test_review_message_carries_the_first_batch_and_its_buttons(monkeypatch):
    from fake_db import FakeConn, patch_db_conn

    from flows import daily_digest

    rows = [{"id": 1, "source": "שגיא", "translation": "Саги", "kind": "person", "status": "proposed",
             "evidence": None, "chat_renderings": {"Шаги": 2, "Саги": 1}, "also_word": False}]
    patch_db_conn(monkeypatch, daily_digest, FakeConn(fetchone=[{"n": 31}], fetchall=[rows]))
    text, markup = daily_digest.review_message()
    assert "<b>שגיא</b> → Саги" in text
    buttons = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert buttons == ["gl:ok:1", "gl:ed:1", "gl:no:1", "gl:more:1"]
    assert markup["inline_keyboard"][-1][0]["text"] == "Следующие → (ещё 30)"

    patch_db_conn(monkeypatch, daily_digest, FakeConn(fetchall=[[]]))
    assert daily_digest.review_message() is None


def test_names_line_shows_candidates_and_entries_in_question():
    text = format_digest(_data(names={"candidates": 12, "new_candidates": 3, "flagged": 2,
                                      "proposed": 0, "auto_accepted": 0}))
    assert "Словарь имён:</b> новых имён ждут разбора 12 (+3 за сутки) · под вопросом 2" in text
