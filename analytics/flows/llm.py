"""One place for the flows' OpenAI calls: request shape per model family, Flex tier, cost.

Every flow built its own request and guessed its own cost ("tokens * 0.001 / 1000").
Reasoning models (gpt-5/6, o-series) reject `temperature` and want `reasoning_effort` and
`max_completion_tokens`; the older ones want the opposite. And nothing nightly needs an
answer in seconds, so everything here asks for the Flex service tier — half price — and
falls back to the standard tier when Flex is unavailable or the model does not offer it.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# $/1M tokens (input, output) — developers.openai.com/api/docs/pricing, 2026-10-06.
# Flex and Batch are half of these.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-astra": (10.00, 50.00),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-6-sol": (2.00, 10.00),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o-mini": (0.15, 0.60),
    "o3": (2.00, 8.00),
}
FLEX_TIMEOUT = 900  # OpenAI asks for a long timeout on Flex; the nightly flows can wait


def is_reasoning(model: str) -> bool:
    return model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def supports_flex(model: str) -> bool:
    return model.startswith(("gpt-5", "gpt-6", "o3", "o4"))


def build_request(model: str, messages: list[dict], *, max_tokens: int, temperature: float = 0,
                  reasoning: str = "low", json_mode: bool = False) -> dict[str, Any]:
    """Chat Completions kwargs that the given model family accepts."""
    request: dict[str, Any] = {"model": model, "messages": messages}
    if is_reasoning(model):
        request["reasoning_effort"] = reasoning
        request["max_completion_tokens"] = max_tokens
    else:
        request["temperature"] = temperature
        request["max_tokens"] = max_tokens
    if json_mode:
        request["response_format"] = {"type": "json_object"}
    return request


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
    """USD for a response's usage, from the price table; 0 when the model is unknown."""
    if usage is None:
        return 0.0
    price = PRICES.get(model)
    if price is None:
        return 0.0
    tokens_in = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", 0) or 0
    cost = (tokens_in * price[0] + tokens_out * price[1]) / 1e6
    return cost / 2 if flex and supports_flex(model) else cost


def total_tokens(usage) -> int:
    if usage is None:
        return 0
    return getattr(usage, "total_tokens", None) or (
        (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    )
