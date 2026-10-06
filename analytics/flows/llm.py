"""One place for the flows' OpenAI calls: request shape per model family, Flex tier, cost.

Every flow built its own request and guessed its own cost ("tokens * 0.001 / 1000").
Reasoning models (gpt-5/6, o-series) reject `temperature` and want `reasoning_effort` and
`max_completion_tokens`; the older ones want the opposite. And nothing nightly needs an
answer in seconds, so everything here asks for the Flex service tier — half price — and
falls back to the standard tier when Flex is unavailable or the model does not offer it.

Prices, the reasoning/Flex model rules and the request shape come from bridge_shared.llm —
the same table the processor writes llm_usage by.
"""
from __future__ import annotations

import logging
from typing import Any

from bridge_shared.llm import chat_request, supports_flex, token_cost

logger = logging.getLogger(__name__)

FLEX_TIMEOUT = 900  # OpenAI asks for a long timeout on Flex; the nightly flows can wait


def build_request(model: str, messages: list[dict], *, max_tokens: int, temperature: float = 0,
                  reasoning: str = "low", json_mode: bool = False) -> dict[str, Any]:
    """Chat Completions kwargs that the given model family accepts.

    Unlike the translator, a nightly analysis is worth a little thinking: reasoning "low".
    """
    return chat_request(model, messages, max_tokens=max_tokens, temperature=temperature,
                        reasoning=reasoning, json_mode=json_mode)


def complete(client, request: dict[str, Any], *, flex: bool = True, log=None):
    """client.chat.completions.create on the Flex tier, standard tier if Flex won't take it."""
    log = log or logger
    if flex and supports_flex(request["model"]):
        try:
            return client.chat.completions.create(**request, service_tier="flex", timeout=FLEX_TIMEOUT)
        except Exception as exc:
            log.warning("Flex tier failed for %s (%s) — retrying on standard", request["model"], str(exc)[:120])
    return client.chat.completions.create(**request)


def respond(client, request: dict[str, Any], *, flex: bool = True, log=None):
    """client.responses.create with the same Flex-then-standard policy."""
    log = log or logger
    if flex and supports_flex(request["model"]):
        try:
            return client.responses.create(**request, service_tier="flex", timeout=FLEX_TIMEOUT)
        except Exception as exc:
            log.warning("Flex tier failed for %s (%s) — retrying on standard", request["model"], str(exc)[:120])
    return client.responses.create(**request)


def usage_cost(model: str, usage, *, flex: bool = True) -> float:
    """USD for a response's usage (Chat Completions or Responses shape), from the shared
    price table; 0 when the model is unknown."""
    if usage is None:
        return 0.0
    tokens_in = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", 0) or 0
    return token_cost(model, tokens_in, tokens_out, flex=flex)


def total_tokens(usage) -> int:
    if usage is None:
        return 0
    return getattr(usage, "total_tokens", None) or (
        (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    )
