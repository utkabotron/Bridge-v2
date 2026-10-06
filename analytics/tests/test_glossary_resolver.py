"""Glossary resolver: candidates from profiles, independent reading, auto-accept rule."""
from __future__ import annotations

import json
from types import SimpleNamespace

from fake_db import FakeConn

from flows import glossary_resolver as gr

PROFILES = [
    {"chat_pair_id": 11, "target_language": "Russian", "profile_data": {
        "glossary": {"גבעולים": {"translation": "Гивъолим", "note": "название школы"},
                     "ביה״ס גבעולים": {"translation": "школа Гиволим"}},
        "members": {"אורי": "Ори", "דנה דרחי": "Дана Драхи", "69": "69", "Yael": "Яэль"},
        "mentioned_people": {"הגר": {"transliteration": "Агар", "relation": "дочь Даны"}},
    }},
    {"chat_pair_id": 29, "target_language": "Russian", "profile_data": json.dumps({
        "glossary": {"גבעולים": {"translation": "Геваулим", "note": "школа"}},
        "members": {"אורי": "Ури", "דנה": "Дана", "יעל כהן": "Яэль"},
    })},
]


def test_collect_names_splits_people_by_word_and_keeps_no_relations():
    names = gr.collect_names(PROFILES)
    giv = names[("גבעולים", "Russian")]
    assert giv["kind"] == "other"
    assert {r: sorted(p) for r, p in giv["renderings"].items()} == {"Гивъолим": [11], "Геваулим": [29]}
    assert giv["notes"] == ["название школы", "школа"]

    assert names[("דנה", "Russian")]["renderings"] == {"Дана": {11, 29}}
    assert names[("דרחי", "Russian")]["renderings"] == {"Драхи": {11}}
    assert set(names[("אורי", "Russian")]["renderings"]) == {"Ори", "Ури"}
    assert names[("yael", "Russian")]["kind"] == "person"
    assert ("דנה דרחי", "Russian") not in names           # people go in word by word
    assert ("69", "Russian") not in names                 # not a name
    assert ("כהן", "Russian") not in names                # 2 Hebrew words, 1 rendering: skipped
    assert names[("הגר", "Russian")]["notes"] == []       # "дочь Даны" stays in the profile


def test_decide_accepts_only_an_agreement_with_unanimous_chats():
    sure = {"is_name": True, "translation": "Гиволим", "confidence": 0.95}
    assert gr.decide(sure, {"Гиволим": 3}) == "verified"
    assert gr.decide(sure, {"гиволим.": 1}) == "verified"                  # case, punctuation
    assert gr.decide(sure, {"Гиволим": 2, "Геваулим": 2}) == "proposed"   # chats disagree
    assert gr.decide(sure, {"Геваулим": 3}) == "proposed"                 # resolver disagrees
    assert gr.decide({**sure, "confidence": 0.5}, {"Гиволим": 3}) == "proposed"
    assert gr.decide({"is_name": False, "confidence": 0.9}, {"савев": 1}) == "rejected"
    assert gr.decide({"is_name": False, "confidence": 0.4}, {"савев": 1}) == "proposed"
    assert gr.decide({"is_name": True, "translation": "", "confidence": 1}, {"x": 1}) == "proposed"
    assert gr.decide(None, {"x": 1}) == "candidate"
    assert gr.norm_rendering("Бейт-Сефер  Ёлка") == gr.norm_rendering("бейт сефер елка")


def test_import_upserts_candidates_with_chat_renderings():
    conn = FakeConn(fetchall=[PROFILES])
    n = gr.import_candidates(conn)
    (kind, sql, rows), = conn.executed("executemany")
    assert "INSERT INTO glossary" in sql and "CASE WHEN glossary.status = 'candidate'" in sql
    by_source = {r[0]: r for r in rows}
    assert n == len(rows)
    source, lang, kind_, note, renderings, chats, pairs = by_source["גבעולים"]
    assert (lang, kind_, chats, pairs) == ("Russian", "other", 2, [11, 29])
    assert json.loads(renderings) == {"Гивъолим": 1, "Геваулим": 1}
    assert by_source["דנה"][5:] == (2, [11, 29])
    assert by_source["דרחי"][6] == [11]
    assert "chat_pairs = EXCLUDED.chat_pairs" in sql


class _Client:
    """Answers the person batch and the per-entity web call."""

    def __init__(self, people: dict, entity: dict):
        self.people, self.entity, self.batches = people, entity, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._complete))
        self.responses = SimpleNamespace(create=self._respond)

    def _complete(self, **request):
        batch = json.loads(request["messages"][1]["content"])
        self.batches.append(batch)
        items = [{"source": s, **self.people[s]} for s in batch if s in self.people]
        content = json.dumps({"items": items}, ensure_ascii=False)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50))

    def _respond(self, **request):
        return SimpleNamespace(output_text=json.dumps(self.entity, ensure_ascii=False),
                               output=[SimpleNamespace(type="web_search_call"), SimpleNamespace(type="message")],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=200))


def _candidates():
    return [
        {"id": 1, "source": "גבעולים", "target_language": "Russian", "kind": "other", "note": "школа",
         "chat_renderings": {"Гивъолим": 1, "Геваулим": 2}, "chats_seen": 3},
        {"id": 2, "source": "דנה", "target_language": "Russian", "kind": "person", "note": None,
         "chat_renderings": {"Дана": 2}, "chats_seen": 2},
        {"id": 3, "source": "אורי", "target_language": "Russian", "kind": "person", "note": None,
         "chat_renderings": {"Ори": 1, "Ури": 1}, "chats_seen": 2},
    ]


def test_resolve_writes_decisions_and_the_dry_run_writes_nothing():
    client = _Client(
        people={"דנה": {"is_name": True, "translation": "Дана", "confidence": 0.95},
                "אורי": {"is_name": True, "translation": "Ори", "confidence": 0.9}},
        entity={"is_name": True, "kind": "org", "latin": "Givolim", "url": "https://givolim.example",
                "translation": "Гиволим", "note": "школа в Рамат-Гане", "confidence": 0.9},
    )
    conn = FakeConn(fetchall=[_candidates()])
    result = gr.resolve(conn, client, dry_run=True)
    assert {d["source"]: d["status"] for d in result["decisions"]} == {
        "גבעולים": "proposed", "דנה": "verified", "אורי": "proposed"}
    assert result["searches"] == 1 and result["cost"] > 0
    assert client.batches == [["דנה", "אורי"]]          # people in one batch, no web search
    assert conn.executed("executemany") == []

    conn = FakeConn(fetchall=[_candidates()])
    gr.resolve(conn, client)
    (_, sql, updates), = conn.executed("executemany")
    assert "WHERE id = %s AND status = 'candidate'" in sql
    giv = next(u for u in updates if u[-1] == 1)
    assert giv[:6] == ("Гиволим", "org", "школа в Рамат-Гане", "Givolim · https://givolim.example", 0.9, "proposed")
    dana = next(u for u in updates if u[-1] == 2)
    assert dana[1:3] == ("person", None) and dana[5] == "verified" and dana[7] is dana[8] is True
    assert giv[7] is giv[8] is False                     # proposed: nobody decided yet


def test_a_failed_answer_leaves_the_name_a_candidate():
    client = _Client(people={}, entity={})
    client.responses = SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(RuntimeError("down")))
    conn = FakeConn(fetchall=[_candidates()[:2]])
    result = gr.resolve(conn, client)
    assert {d["source"]: d["status"] for d in result["decisions"]} == {"גבעולים": "candidate", "דנה": "candidate"}
    (_, _, updates), = conn.executed("executemany")
    assert updates == []


def test_classify_marks_everyday_words_and_shortens_notes():
    rows = [
        {"id": 1, "source": "עמוס", "translation": "Амос", "kind": "person", "note": None, "status": "verified"},
        {"id": 2, "source": "אופק", "translation": "Офек", "kind": "other",
         "note": "Школьная образовательная платформа CET (МАТАХ)", "status": "verified"},
        {"id": 3, "source": "גבעולים", "translation": "Гиволим", "kind": "org",
         "note": "название школы в Рамат-Гане", "status": "locked"},
        {"id": 4, "source": "פייבוקס", "translation": "Пейбокс", "kind": "other", "note": None, "status": "verified"},
    ]
    answer = {"items": [
        {"source": "עמוס", "also_word": True, "hint": "имя"},
        {"source": "אופק", "also_word": True, "hint": "школьная платформа"},
        {"source": "גבעולים", "also_word": True, "hint": "школа"},
        {"source": "פייבוקס", "also_word": False, "hint": "платёжное приложение"},
    ]}
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer, ensure_ascii=False)))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10)))))
    conn = FakeConn(fetchall=[rows])
    result = gr.classify(conn, client)
    (_, sql, updates), = conn.executed("executemany")
    assert "also_word = %s" in sql
    assert updates == [
        (True, None, 1),                    # people carry no note
        (True, "школьная платформа", 2),   # the long note that leaked into translations goes
        (True, None, 3),                    # the admin's note on a locked entry stays
        (False, "платёжное приложение", 4),
    ]
    assert result["also_word"] == 3

    conn = FakeConn(fetchall=[rows])
    gr.classify(conn, client, dry_run=True)
    assert conn.executed("executemany") == []


def test_arbiter_admits_only_a_confident_single_reading():
    one = {"decision": "one", "translation": "Саги", "confidence": 0.9, "evidence": "Sagi · https://he.wikipedia.org"}
    assert gr.arbiter_decision(one) == ("verified", "Саги", "Sagi · https://he.wikipedia.org")
    status, tr, ev = gr.arbiter_decision({"decision": "several", "readings": ["Ори", "Ури"], "confidence": 0.9})
    assert (status, tr) == ("rejected", None) and ev == "решает чат: несколько прочтений: Ори, Ури"
    assert gr.arbiter_decision({"decision": "not_name", "confidence": 0.9})[2] == "решает чат: не имя"
    assert gr.arbiter_decision({**one, "confidence": 0.5})[0] == "rejected"
    assert gr.arbiter_decision({**one, "translation": ""})[0] == "rejected"
    assert gr.arbiter_decision(None) == ("proposed", None, None)


def test_arbitrate_commits_as_it_goes_and_never_touches_admin_decisions():
    rows = [{"id": i, "source": f"שם{i}", "target_language": "Russian", "kind": "person", "note": None,
             "translation": "Х", "evidence": None, "chat_renderings": json.dumps({"А": 1, "Б": 1})}
            for i in range(1, 12)]
    answers = iter([{"decision": "one", "translation": "Саги", "confidence": 0.9}] * 10 + [None])

    def respond(**_):
        a = next(answers)
        if a is None:
            raise RuntimeError("down")
        return SimpleNamespace(output_text=json.dumps(a, ensure_ascii=False), output=[SimpleNamespace(type="web_search_call")],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=10))

    client = SimpleNamespace(responses=SimpleNamespace(create=respond))
    conn = FakeConn(fetchall=[rows])
    result = gr.arbitrate(conn, client)

    select = conn.executed("execute")[0][1]
    assert "decided_by IS DISTINCT FROM 'admin'" in select
    updates = conn.executed("execute")[1:]
    assert len(updates) == 10                         # the failed one stays proposed
    assert all("decided_by IS DISTINCT FROM 'admin'" in u[1] for u in updates)
    assert conn.events.count("commit") == 1           # every 10
    assert result["decisions"][-1]["status"] == "proposed" and result["searches"] == 10

    conn = FakeConn(fetchall=[rows[:1]])
    answers = iter([{"decision": "one", "translation": "Саги", "confidence": 0.9}])
    gr.arbitrate(conn, client, dry_run=True)
    assert len(conn.executed("execute")) == 1         # the SELECT only
