"""
The message processing pipeline.

Flow:
  validate ──(has tg_chat?)──► translate ──► format ──► deliver
             │                └─(nothing to translate)─► format
             └─(no pair)──────────────────────────────► deliver

Four steps and one branch. LangGraph ran this until 2026-10-06; a straight line did not
need a graph engine, and dropping it took nine packages and a quarter of the processor's
memory with it. Pipeline.astream keeps LangGraph's contract — one {node: output} dict per
step — so the consumer loop, the SSE events and the dashboard did not change.
"""
from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterator

# A message written in none of the source scripts, in a chat whose target language uses
# a script we know, is already readable to the recipient.
from bridge_shared.scripts import SOURCE_SCRIPT_RE, target_script_re

from ..models.message import MessageState
from .nodes import deliver_node, format_node, translate_node, validate_node

_URL_RE = re.compile(r'^https?://\S+$')


def _is_translatable(text: str) -> bool:
    """Return False when there are no words to translate: emoji, digits, a bare URL.

    This used to be an emoji-range regex with gaps — 👍 (U+1F44D) and skin tones fell
    outside it, so a lone thumbs-up cost two LLM calls (translate + passthrough retry)
    to come back unchanged. "Has no letter" covers every emoji WhatsApp will ever add.
    """
    t = text.strip()
    if not any(ch.isalpha() for ch in t):
        return False
    if _URL_RE.match(t):
        return False
    return True


def _already_in_target_script(text: str, target_language: str) -> bool:
    """True when the text is plainly already written in the target's script.

    Every message went to the LLM regardless of the language it was in, so a Russian
    message in a Hebrew→Russian chat was sent to OpenAI to be "translated" into the
    language it was already in. The model returns it unchanged (prompt rule 3) and
    format_node collapses the duplicate, so nobody saw it — but it was paid for, and it
    sat in the queue ahead of messages that did need translating.

    Deliberately conservative: it only skips when the source scripts are entirely absent
    AND the target's own script is present, so a mixed Hebrew/Russian message still goes
    through the model.
    """
    target_re = target_script_re(target_language)
    if target_re is None:
        return False
    if SOURCE_SCRIPT_RE.search(text):
        return False
    return bool(target_re.search(text))


def _should_translate(state: MessageState) -> str:
    """Route after validate: translate only when chat pair was resolved."""
    if state.get("delivery_status") in ("failed", "skipped"):
        return "deliver"
    if state.get("fallback_to_admins"):
        return "translate"
    text = state.get("original_text", "").strip()
    if not _is_translatable(text):
        return "format"
    if _already_in_target_script(text, state.get("target_language", "")):
        return "format"
    return "translate"


class Pipeline:
    """validate → translate → format → deliver, streamed one step at a time."""

    steps = {
        "validate": validate_node,
        "translate": translate_node,
        "format": format_node,
        "deliver": deliver_node,
    }

    async def _run(self, name: str, state: MessageState) -> MessageState:
        out = self.steps[name](state)
        if inspect.isawaitable(out):
            out = await out
        # Merge, as LangGraph did: a node may return only the keys it changed.
        return {**state, **(out or {})}

    async def astream(self, state: MessageState, stream_mode: str = "updates") -> AsyncIterator[dict]:
        state = await self._run("validate", state)
        yield {"validate": state}

        route = _should_translate(state)
        if route == "translate":
            state = await self._run("translate", state)
            yield {"translate": state}
        if route in ("translate", "format"):
            state = await self._run("format", state)
            yield {"format": state}

        state = await self._run("deliver", state)
        yield {"deliver": state}

    async def ainvoke(self, state: MessageState) -> MessageState:
        async for chunk in self.astream(state):
            state = next(iter(chunk.values()))
        return state


# Singleton
pipeline = Pipeline()
