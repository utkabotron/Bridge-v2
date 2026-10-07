"""The prompt-suggestion call of translation_quality: same request shape, tier and cost as the judge."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from flows import llm
from flows import translation_quality as tq

EVALS = [
    {"quality_score": 2, "accuracy_score": 2, "naturalness_score": 3, "source": "bridge",
     "original_text": "שלום", "translated_text": "bye", "target_language": "English",
     "issues": [{"type": "mistranslation", "detail": "wrong greeting"}]},
]


class _Client:
    """Records chat.completions.create calls and answers with a one-suggestion JSON array."""

    def __init__(self):
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=100_000, total_tokens=1_100_000)
        content = json.dumps([{"suggestion": "Greet in kind", "rationale": "see example 1"}])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


@pytest.fixture
def patched(monkeypatch):
    client = _Client()
    requests: list[dict] = []
    real_complete = llm.complete

    def spy(c, request, **kw):
        requests.append(request)
        return real_complete(c, request, **kw)

    monkeypatch.setattr(tq, "OpenAI", lambda **_: client)
    monkeypatch.setattr(tq, "get_run_logger", lambda: logging.getLogger("test"))
    monkeypatch.setattr(tq, "_load_prompt_from_db", lambda: ("v-test", "Translate {target_language}."))
    monkeypatch.setattr(tq, "EVAL_MODEL", "gpt-6.1-sol")
    monkeypatch.setattr(llm, "complete", spy)
    return client, requests


def test_suggestions_go_through_llm_complete_with_a_reasoning_request(patched, monkeypatch):
    client, requests = patched
    monkeypatch.setattr(tq, "PROMPT_SUGGESTIONS_ENABLED", True)

    result = tq.generate_suggestions.fn(EVALS)

    assert len(requests) == 1  # llm.complete, not a direct client call
    req = requests[0]
    assert req["model"] == "gpt-6.1-sol"
    assert req["reasoning_effort"] == "low" and req["max_completion_tokens"] == 4000
    assert "temperature" not in req and "max_tokens" not in req  # reasoning models reject both

    assert len(client.calls) == 1
    assert client.calls[0]["service_tier"] == "flex"  # nightly work takes the half-price tier

    assert result["suggestions"] == [{"suggestion": "Greet in kind", "rationale": "see example 1"}]
    assert result["tokens_used"] == 1_100_000
    assert result["cost_usd"] == 1.5  # (1M * $2 + 0.1M * $10) / 2 for Flex


def test_nothing_is_called_while_suggestions_are_disabled(patched, monkeypatch):
    client, requests = patched
    monkeypatch.setattr(tq, "PROMPT_SUGGESTIONS_ENABLED", False)

    result = tq.generate_suggestions.fn(EVALS)

    assert requests == [] and client.calls == []
    assert result["suggestions"] == [] and result["tokens_used"] == 0


def test_parse_evaluations_accepts_json_mode_and_rejects_broken_answers():
    assert tq._parse_evaluations('{"evaluations": [{"sample_index": 0}]}') == [{"sample_index": 0}]
    assert tq._parse_evaluations('```json\n[{"sample_index": 1}]\n```') == [{"sample_index": 1}]
    assert tq._parse_evaluations('[{"detail": "ביה"ס"}]') is None          # 07.10: unescaped quote
    assert tq._parse_evaluations('{"other": 1}') is None
    assert tq._parse_evaluations("") is None


def test_a_broken_batch_is_retried_alone_then_skipped(monkeypatch):
    """One bad answer used to fail the task and Prefect re-paid every batch."""
    answers = iter([
        json.dumps({"evaluations": [{"sample_index": i, "quality_score": 5, "issues": []} for i in range(10)]}),
        '{"evaluations": [{"detail": "broken"',          # batch 2, attempt 1
        '[{"detail": "ביה"ס"}]',                          # batch 2, attempt 2: skipped
        json.dumps({"evaluations": [{"sample_index": 0, "quality_score": 4, "issues": []}]}),
    ])
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(answers)))],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10, total_tokens=20))

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(tq, "OpenAI", lambda **_: client)
    monkeypatch.setattr(tq, "get_run_logger", lambda: logging.getLogger("test"))
    samples = [{"id": i, "original_text": f"טקסט {i}", "translated_text": f"текст {i}",
                "target_language": "Russian", "source": "bridge"} for i in range(21)]

    result = tq.evaluate_translations.fn(samples)

    assert len(calls) == 4                                     # 3 batches, one retried
    assert all(c.get("response_format") == {"type": "json_object"} for c in calls)
    assert [e["message_event_id"] for e in result["evaluations"]] == list(range(10)) + [20]
