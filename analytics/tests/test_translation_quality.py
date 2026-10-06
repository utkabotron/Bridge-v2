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
