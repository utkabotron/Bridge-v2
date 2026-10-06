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
    source, lang, kind_, note, renderings, chats = by_source["גבעולים"]
    assert (lang, kind_, chats) == ("Russian", "other", 2)
    assert json.loads(renderings) == {"Гивъолим": 1, "Геваулим": 1}
    assert by_source["דנה"][5] == 2


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
    assert dana[1:3] == ("person", None) and dana[5] == "verified" and dana[7] is True


def test_a_failed_answer_leaves_the_name_a_candidate():
    client = _Client(people={}, entity={})
    client.responses = SimpleNamespace(create=lambda **_: (_ for _ in ()).throw(RuntimeError("down")))
    conn = FakeConn(fetchall=[_candidates()[:2]])
    result = gr.resolve(conn, client)
    assert {d["source"]: d["status"] for d in result["decisions"]} == {"גבעולים": "candidate", "דנה": "candidate"}
    (_, _, updates), = conn.executed("executemany")
    assert updates == []
