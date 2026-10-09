"""OpenAI model facts every LLM caller needs: prices, which models reason, which take Flex.

processor/src/llm.py (async, writes llm_usage) and analytics/flows/llm.py (sync, Flex
tier) stay the per-service entry points; this is what they used to copy — the processor's
config, the analytics client and the model bake-off each kept a price table of their own,
with different models in each, so a model added to one cost $0 in the others.
"""
from __future__ import annotations

from typing import Any

# $/1M tokens (input, output), standard tier — developers.openai.com/api/docs/pricing,
# 2026-10-06. A model missing here costs 0 in llm_usage and in every report: add it before
# switching anything to it.
MODEL_PRICES: dict[str, tuple[float, float]] = {
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
    # Anthropic — platform.claude.com/docs/en/about-claude/pricing, 2026-10-09 (Haiku 5.5:
    # prompts up to 100k tokens; ours are ~2k). Candidates in the model bake-off.
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
}
# $/minute of audio
TRANSCRIBE_PRICES: dict[str, float] = {
    "gpt-transcribe": 0.0045,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
    "whisper-1": 0.006,
}
# Flex (and Batch) bill this share of the standard price.
FLEX_DISCOUNT = 0.5

# Reasoning models reject `temperature` and `max_tokens`; they take `reasoning_effort` and
# `max_completion_tokens`. The bake-off used to test a bare "o" prefix, which is not the
# rule the services run by.
REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
# Models the Flex service tier accepts — o1 reasons but has no Flex.
FLEX_PREFIXES = ("gpt-5", "gpt-6", "o3", "o4")


def is_reasoning(model: str) -> bool:
    return model.startswith(REASONING_PREFIXES)


def supports_flex(model: str) -> bool:
    return model.startswith(FLEX_PREFIXES)


def chat_request(model: str, messages: list[dict], *, max_tokens: int | None = None,
                 temperature: float = 0, reasoning: str = "none",
                 json_mode: bool = False) -> dict[str, Any]:
    """Chat Completions kwargs the model family accepts.

    The defaults are the translator's request (processor/src/llm.py:build_request): a chat
    message needs no thinking and no sampling. The bake-off relies on that to measure a
    candidate the way production would call it. `temperature` only reaches older models,
    `reasoning` only reasoning ones; a falsy `max_tokens` leaves the limit to the API.
    """
    request: dict[str, Any] = {"model": model, "messages": messages}
    if is_reasoning(model):
        request["reasoning_effort"] = reasoning
        if max_tokens:
            request["max_completion_tokens"] = max_tokens
    else:
        request["temperature"] = temperature
        if max_tokens:
            request["max_tokens"] = max_tokens
    if json_mode:
        request["response_format"] = {"type": "json_object"}
    return request


def token_cost(model: str, tokens_in: int, tokens_out: int, flex: bool = False) -> float:
    """USD for one call, 0 for a model not in MODEL_PRICES.

    `flex` means the call asked for the Flex tier; it only discounts models that have one,
    because for the rest the request went out on the standard tier.
    """
    price = MODEL_PRICES.get(model)
    if price is None:
        return 0.0
    cost = (tokens_in * price[0] + tokens_out * price[1]) / 1e6
    return cost * FLEX_DISCOUNT if flex and supports_flex(model) else cost


def transcribe_cost(model: str, seconds: float | None) -> float:
    """USD for a transcription billed by the minute, 0 when the duration is unknown."""
    if not seconds:
        return 0.0
    return seconds / 60 * TRANSCRIBE_PRICES.get(model, 0.0)
