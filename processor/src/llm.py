"""The processor's one way to call OpenAI: request shape per model family, timeouts, cost.

Replaces three: langchain-openai's ChatOpenAI for bridge and DM translation, and raw httpx
posts for media analysis. Each call writes a row to llm_usage (model, tokens, cost, what
it was for) — the cost record LangSmith used to keep, now in our own database and read by
GET /api/costs.

Callers get a Completion back and never touch the SDK, so tests patch `chat`/`transcribe`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from .config import LLM_MAX_RETRIES, LLM_TIMEOUT, MODEL_PRICES, TRANSCRIBE_PRICES

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None
# Ledger writes run in the background; keep references so they are not collected mid-flight.
_pending: set[asyncio.Task] = set()


def client() -> AsyncOpenAI:
    """Created on first use: importing this module must not need an API key (tests, tools)."""
    global _client
    if _client is None:
        # Unbounded, the SDK waits 600s and retries twice, so one sick request could hold
        # the single-threaded consumer for half an hour while every message queued behind it.
        _client = AsyncOpenAI(timeout=LLM_TIMEOUT, max_retries=LLM_MAX_RETRIES)
    return _client


def is_reasoning(model: str) -> bool:
    return model.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4"))


def build_request(model: str, messages: list[dict], max_tokens: int | None = None) -> dict[str, Any]:
    """Chat Completions kwargs the model family accepts.

    Reasoning models (gpt-5/6, o-series) reject `temperature` and `max_tokens`; they take
    `max_completion_tokens`, and a chat message or a caption needs no thinking.
    """
    request: dict[str, Any] = {"model": model, "messages": messages}
    if is_reasoning(model):
        request["reasoning_effort"] = "none"
        if max_tokens:
            request["max_completion_tokens"] = max_tokens
    else:
        request["temperature"] = 0
        if max_tokens:
            request["max_tokens"] = max_tokens
    return request


def token_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price = MODEL_PRICES.get(model)
    if price is None:
        return 0.0
    return (tokens_in * price[0] + tokens_out * price[1]) / 1e6


@dataclass
class Completion:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    ms: int = 0


async def chat(messages: list[dict], *, model: str, purpose: str, tag: str | None = None,
               max_tokens: int | None = None, timeout: float | None = None) -> Completion:
    """One chat completion. Raises on API failure — callers decide how to degrade."""
    request = build_request(model, messages, max_tokens)
    if timeout:
        request["timeout"] = timeout
    t0 = time.monotonic()
    response = await client().chat.completions.create(**request)
    ms = int((time.monotonic() - t0) * 1000)
    usage = response.usage
    tokens_in = (usage.prompt_tokens or 0) if usage else 0
    tokens_out = (usage.completion_tokens or 0) if usage else 0
    result = Completion(
        text=(response.choices[0].message.content or "").strip(),
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=token_cost(model, tokens_in, tokens_out),
        ms=ms,
    )
    _record(purpose, model, tag, tokens_in, tokens_out, result.cost_usd, ms)
    return result


async def transcribe(audio: bytes, filename: str, *, model: str, purpose: str = "transcribe",
                     timeout: float | None = None) -> str:
    """Speech to text. Returns "" for silence or noise — gpt-transcribe does not invent text."""
    t0 = time.monotonic()
    kwargs: dict[str, Any] = {"model": model, "file": (filename, audio)}
    if timeout:
        kwargs["timeout"] = timeout
    response = await client().audio.transcriptions.create(**kwargs)
    ms = int((time.monotonic() - t0) * 1000)
    usage = getattr(response, "usage", None)
    seconds = getattr(usage, "seconds", None) if usage is not None else None
    tokens_in = getattr(usage, "input_tokens", 0) or 0 if usage is not None else 0
    tokens_out = getattr(usage, "output_tokens", 0) or 0 if usage is not None else 0
    cost = (seconds / 60 * TRANSCRIBE_PRICES.get(model, 0.0)) if seconds else 0.0
    _record(purpose, model, None, tokens_in, tokens_out, cost, ms)
    return (getattr(response, "text", "") or "").strip()


def _record(purpose: str, model: str, tag: str | None, tokens_in: int, tokens_out: int,
            cost: float, ms: int) -> None:
    """Write one llm_usage row in the background; a ledger hiccup never delays a message."""
    async def write() -> None:
        try:
            from .db import get_pool
            pool = await get_pool()
            await pool.execute(
                """
                insert into public.llm_usage (purpose, model, tag, tokens_in, tokens_out, cost_usd, ms)
                values ($1, $2, $3, $4, $5, $6, $7)
                """,
                purpose, model, tag, tokens_in, tokens_out, cost, ms,
            )
        except Exception as exc:
            logger.warning("llm_usage write failed (%s/%s): %s", purpose, model, exc)

    try:
        task = asyncio.get_running_loop().create_task(write())
    except RuntimeError:
        return  # no loop (sync context) — the ledger is best-effort
    _pending.add(task)
    task.add_done_callback(_pending.discard)
