"""The flows' DB writes: one connection per task, plain inserts batched with executemany."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import pytest
from fake_db import FakeConn, patch_db_conn
from flows import chat_context_builder as ccb
from flows import daily_chat_summary as dcs
from flows import nightly_problems as np_
from flows import translation_quality as tq


@pytest.fixture(autouse=True)
def _run_logger(monkeypatch):
    """Tasks log through Prefect's run logger, which only exists inside a run."""
    for module in (ccb, dcs, np_, tq):
        monkeypatch.setattr(module, "get_run_logger", lambda: logging.getLogger("test"))


def _insert_into(conn: FakeConn, table: str, kind: str = "executemany") -> list[tuple]:
    return [s for s in conn.executed(kind) if f"INSERT INTO {table}" in s[1]]


# ── translation_quality ───────────────────────────────────────

def test_evaluations_are_stored_in_one_batch_with_measured_cost(monkeypatch):
    conn = FakeConn(fetchone=[(7,)])  # RETURNING id; the suggestion dedup SELECT then finds nothing
    opened = patch_db_conn(monkeypatch, tq, conn)
    monkeypatch.setattr(tq, "_load_prompt_from_db", lambda: ("v-test", "prompt"))

    llm_eval = {"message_event_id": 1, "original_text": "a", "translated_text": "b", "quality_score": 4,
                "issues": [], "source": "bridge", "chat_pair_id": 3}
    run_id = tq.store_quality_results.fn(
        {"evaluations": [llm_eval, {**llm_eval, "message_event_id": 2}], "tokens_used": 100, "cost_usd": 0.25},
        {"suggestions": [{"suggestion": "do x", "rationale": "because"}], "tokens_used": 50, "cost_usd": 0.5},
        shadow_evaluations=[{**llm_eval, "message_event_id": 3, "evaluator": "jev"}],
    )

    assert run_id == 7 and len(opened) == 1  # the prompt lookup no longer overlaps a second connection
    assert conn.events == []  # the (fake) db_conn owns commit/close, the task does not
    (batch,) = _insert_into(conn, "translation_evaluations")
    rows = batch[2]
    assert [r[0] for r in rows] == [7, 7, 7] and [r[1] for r in rows] == [1, 2, 3]  # run_id, message_event_id
    assert [r[9] for r in rows] == [False, False, True]  # shadow flag
    run_params = next(s[2] for s in conn.executed("execute") if "INSERT INTO nightly_analysis_runs" in s[1])
    assert run_params[2] == 150 and run_params[3] == 0.75  # tokens and cost of both calls added up
    assert len(_insert_into(conn, "prompt_suggestions", "execute")) == 1


# ── nightly_problems ──────────────────────────────────────────

ISSUES = [
    {"severity": "critical", "category": "delivery", "title": "Queue stuck", "description": "d"},
    {"severity": "warning", "category": "latency", "title": "Slow", "suggested_fix": "f"},
]


def _store_issues(monkeypatch, dedup_hit: bool):
    # run_id, then the backlog dedup SELECT for the one critical issue
    conn = FakeConn(fetchone=[(11,), (1,) if dedup_hit else None])
    opened = patch_db_conn(monkeypatch, np_, conn)
    stats = {"overview": {"mapped_total": 0}}
    np_.store_results.fn(stats, {"issues": ISSUES, "tokens_used": 1, "cost_usd": 0.01})
    return conn, opened


def test_detected_issues_are_one_batch_and_the_backlog_keeps_its_dedup(monkeypatch):
    conn, opened = _store_issues(monkeypatch, dedup_hit=False)

    assert len(opened) == 1
    (batch,) = _insert_into(conn, "detected_issues")
    assert [r[0] for r in batch[2]] == [11, 11] and [r[3] for r in batch[2]] == ["Queue stuck", "Slow"]
    # Only the critical issue is considered for the backlog, one SELECT then one INSERT.
    assert len([s for s in conn.executed("execute") if "FROM issues_backlog" in s[1]]) == 1
    (backlog,) = _insert_into(conn, "issues_backlog", "execute")
    assert backlog[2][3] == "Queue stuck"


def test_a_duplicate_critical_issue_is_not_copied_to_the_backlog(monkeypatch):
    conn, _ = _store_issues(monkeypatch, dedup_hit=True)

    assert _insert_into(conn, "issues_backlog", "execute") == []
    assert len(_insert_into(conn, "detected_issues")[0][2]) == 2  # the run's own list is unaffected


# ── chat_context_builder ──────────────────────────────────────

def test_profiles_and_history_are_written_in_two_batches(monkeypatch):
    conn = FakeConn(fetchone=[None, None])  # neither chat has a profile yet
    opened = patch_db_conn(monkeypatch, ccb, conn)
    invalidated: list[list[int]] = []
    monkeypatch.setattr(ccb, "invalidate_profile_cache", invalidated.append)

    results = [
        {"chat_pair_id": 5, "delta": {"tone": "casual"}, "tokens_used": 10, "cost_usd": 0.1, "messages_analyzed": 4},
        {"chat_pair_id": 6, "delta": {"glossary": {"גן": "сад"}}, "tokens_used": 20, "cost_usd": 0.2},
        {"chat_pair_id": 7, "delta": {}},  # nothing learned: skipped, not written
    ]
    stored = ccb.store_profiles.fn(results)

    assert stored == 2 and len(opened) == 1
    (profiles,) = _insert_into(conn, "chat_profiles")
    (history,) = _insert_into(conn, "chat_profile_history")
    assert [r[0] for r in profiles[2]] == [5, 6] and [r[2] for r in profiles[2]] == [1, 1]  # version
    assert [r[0] for r in history[2]] == [5, 6]
    assert history[2][0][3] == "updated tone" and history[2][1][3] == "+1 glossary"
    assert invalidated == [[5, 6]]  # the cache is dropped after the batch, for exactly the pairs written


def test_no_profile_writes_leave_the_database_alone(monkeypatch):
    conn = FakeConn()
    patch_db_conn(monkeypatch, ccb, conn)
    monkeypatch.setattr(ccb, "invalidate_profile_cache", lambda ids: None)

    ccb.store_profiles.fn([{"chat_pair_id": 7, "delta": {}}])

    assert conn.executed("executemany") == []


# ── daily_chat_summary ────────────────────────────────────────

def _messages(n: int) -> list[dict]:
    return [{"sender_name": f"s{i % 2}", "original_text": "hi", "message_type": "chat",
             "local_time": datetime(2026, 10, 7, 20, i)} for i in range(n)]


def test_messages_for_all_due_chats_come_from_one_connection(monkeypatch):
    # per chat: messages (fetchall) then profile (fetchone); chat 2 has too few messages
    conn = FakeConn(fetchall=[_messages(5), _messages(1), _messages(3)],
                    fetchone=[{"profile_data": {"tone": "x"}}, None, None])
    opened = patch_db_conn(monkeypatch, dcs, conn)

    collected = dcs.collect_chat_messages.fn([{"chat_pair_id": 1}, {"chat_pair_id": 2}, {"chat_pair_id": 3}])

    assert len(opened) == 1
    assert [c["chat_pair_id"] for c in collected] == [1, 3]
    assert collected[0]["message_count"] == 5 and collected[0]["unique_senders"] == 2
    assert collected[0]["profile"] == {"tone": "x"} and collected[1]["profile"] is None


def test_summaries_are_stored_in_one_batch(monkeypatch):
    conn = FakeConn()
    opened = patch_db_conn(monkeypatch, dcs, conn)
    base = {"message_count": 3, "unique_senders": 2, "plans": [], "optimal_send_hour": 22}

    dcs.store_summaries.fn([
        {**base, "chat_pair_id": 1, "sent": True, "tg_message_id": 99, "tokens_used": 5, "cost_usd": 0.1},
        {**base, "chat_pair_id": 2, "sent": False},
    ])
    dcs.store_summaries.fn([])  # nothing to store: no connection at all

    assert len(opened) == 1
    (batch,) = _insert_into(conn, "daily_chat_summaries")
    assert [r[0] for r in batch[2]] == [1, 2] and [r[7] for r in batch[2]] == [True, False]


def test_schedules_are_recomputed_on_one_connection_and_written_as_a_batch(monkeypatch):
    conn = FakeConn(
        fetchone=[{"oldest": None}, {"missing": 2}],
        fetchall=[[{"chat_pair_id": 1}, {"chat_pair_id": 2}], [], [{"h": 21}] * 12],
    )
    opened = patch_db_conn(monkeypatch, dcs, conn)

    assert dcs.recompute_schedules.fn() == 2

    assert len(opened) == 1
    (batch,) = _insert_into(conn, "chat_summary_schedule")
    assert batch[2][0][:2] == (1, dcs.DEFAULT_HOUR)  # fewer than 10 samples: the default hour
    assert batch[2][1][0] == 2 and batch[2][1][4] == 12


def test_fresh_schedules_are_not_recomputed(monkeypatch):
    conn = FakeConn(fetchone=[{"oldest": datetime.now(timezone.utc)}, {"missing": 0}])
    patch_db_conn(monkeypatch, dcs, conn)

    assert dcs.recompute_schedules.fn() == 0
    assert conn.executed("executemany") == []


def _stub_flow(monkeypatch, send):
    stored: list[list[dict]] = []
    monkeypatch.setattr(dcs, "recompute_schedules", lambda: 0)
    monkeypatch.setattr(dcs, "find_chats_due_now", lambda: [{"chat_pair_id": 1}, {"chat_pair_id": 2}])
    monkeypatch.setattr(dcs, "collect_chat_messages", lambda chats: [{"chat_pair_id": c["chat_pair_id"]} for c in chats])
    monkeypatch.setattr(dcs, "generate_summary_with_llm", lambda d: {"chat_pair_id": d["chat_pair_id"]})
    monkeypatch.setattr(dcs, "send_summary_to_chat", send)
    monkeypatch.setattr(dcs, "store_summaries", lambda results: stored.append(list(results)))
    return stored


def test_the_flow_records_each_summary_right_after_sending_it(monkeypatch):
    """A deferred write would let a hard kill forget a summary that already reached the
    group, and the next slot would send it again."""
    stored = _stub_flow(monkeypatch, lambda s: {**s, "sent": s["chat_pair_id"] == 1})

    out = dcs.daily_chat_summary.fn()

    assert [[r["chat_pair_id"] for r in batch] for batch in stored] == [[1], [2]]
    assert out == {"chats_due": 2, "chats_processed": 2, "summaries_sent": 1}


def test_summaries_already_sent_are_stored_even_if_a_later_chat_fails(monkeypatch):
    def send(summary):
        if summary["chat_pair_id"] == 2:
            raise RuntimeError("telegram is down")
        return {**summary, "sent": True}

    stored = _stub_flow(monkeypatch, send)

    with pytest.raises(RuntimeError, match="telegram is down"):
        dcs.daily_chat_summary.fn()

    # chat 1 was already sent: it must be on record so a re-run in this slot does not send it again
    assert [[r["chat_pair_id"] for r in batch] for batch in stored] == [[1]]


def test_builder_routes_names_to_the_service_glossary(monkeypatch):
    """Settled names stay out of the profile, new ones become candidates, names the service
    left to the chats (several readings) are still written into the profile."""
    conn = FakeConn(fetchone=[None])
    patch_db_conn(monkeypatch, ccb, conn)
    monkeypatch.setattr(ccb, "invalidate_profile_cache", lambda ids: None)

    statuses = {"גבעולים": "locked", "אורי": "rejected", "עמוס": "verified"}
    delta = {"tone": "warm",
             "glossary": {"גבעולים": {"translation": "Геваулим"}, "צמרות": {"translation": "Цмарот"},
                          "א-1": {"translation": "Алеф-1"}},
             "members": {"אורי": "Ури", "עמוס": "Амос", "דנה": "Дана"}}
    ccb.store_profiles.fn([{"chat_pair_id": 9, "target_language": "Russian", "delta": delta,
                            "statuses": statuses}])

    names = [s for s in conn.statements if s[0] == "executemany" and "INSERT INTO glossary" in s[1]]
    assert {r["s"] for r in names[0][2]} == {"גבעולים", "צמרות", "אורי", "עמוס", "דנה"}
    assert all(r["p"] == 9 for r in names[0][2])                  # this chat joins each name's scope

    (profiles,) = _insert_into(conn, "chat_profiles")
    profile = json.loads(profiles[2][0][1])
    assert profile["glossary"] == {"א-1": {"translation": "Алеф-1"}}   # not a name: kept as before
    assert profile["members"] == {"אורי": "Ури"}                      # left to the chat
    (history,) = _insert_into(conn, "chat_profile_history")
    assert "2 new names → service glossary" in history[2][0][3]       # צמרות, דנה
