"""Model bake-off: Claude requests, the judge's JSON, per-judge summary."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from flows import model_bakeoff as mb


def _resp(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, stop_details=SimpleNamespace(category="cyber"),
                           content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=50, output_tokens=10))


class _Anthropic:
    def __init__(self, resp):
        self.calls, self.resp = [], resp
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        return self.resp


def test_claude_candidates_are_asked_without_thinking_where_allowed():
    client = _Anthropic(_resp("Привет"))
    clients = SimpleNamespace(anthropic=client, openai=None)
    r = asyncio.run(mb.translate(clients, "claude-haiku-5-5", "sys", "שלום"))
    assert r["text"] == "Привет" and r["in"] == 50
    assert client.calls[0]["thinking"] == {"type": "disabled"} and client.calls[0]["system"] == "sys"
    asyncio.run(mb.translate(clients, "claude-sonnet-5-5", "sys", "שלום"))
    assert client.calls[1]["thinking"] == {"type": "between_tools"}


def test_a_refusal_is_an_error_not_a_translation():
    clients = SimpleNamespace(anthropic=_Anthropic(_resp("", stop="refusal")), openai=None)
    r = asyncio.run(mb.translate(clients, "claude-opus-5-5", "sys", "שלום"))
    assert "refusal" in r["error"] and "text" not in r


def test_parse_json_object_and_judge_summary():
    assert mb.parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(ValueError):
        mb.parse_json_object("no json")
    verdicts = {
        1: {"scores": {"x": {"quality": 5, "accuracy": 5, "naturalness": 5, "issues": []},
                       "y": {"quality": 2, "accuracy": 2, "naturalness": 3, "issues": ["mistranslation"]}},
            "ranking": ["x", "y"]},
        2: {"scores": {"x": {"quality": 4, "accuracy": 4, "naturalness": 4, "issues": []}}, "ranking": ["x"]},
    }
    s = mb.judge_summary(["x", "y"], verdicts)
    assert s["x"]["quality"] == 4.5 and s["x"]["win_pct"] == 100.0
    assert s["y"]["bad_pct"] == 100.0 and s["y"]["issues"] == {"mistranslation": 1}
