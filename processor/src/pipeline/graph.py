"""
LangGraph StateGraph for the message processing pipeline.

Flow:
  validate ──(has tg_chat?)──► translate ──► format ──► deliver
             └─(no pair)──────────────────────────────► deliver
"""
from __future__ import annotations

import re

from langgraph.graph import END, StateGraph

from ..models.message import MessageState
from .nodes import deliver_node, format_node, translate_node, validate_node

# Emoji pattern: Unicode emoji ranges + variation selectors + ZWJ sequences
_EMOJI_RE = re.compile(
    r'^[\U0001F600-\U0001FAFF\U00002702-\U000027B0\U0000FE00-\U0000FE0F'
    r'\U0000200D\U000020E3\U00003030\U0000303D\U00002049\U0000203C'
    r'\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF'
    r'\U00002600-\U000026FF\U00002700-\U000027BF\s]+$'
)

_URL_RE = re.compile(r'^https?://\S+$')


def _is_translatable(text: str) -> bool:
    """Return False if text is only emojis or a bare URL — no translation needed."""
    t = text.strip()
    if not t:
        return False
    if _EMOJI_RE.match(t):
        return False
    if _URL_RE.match(t):
        return False
    return True


def _should_translate(state: MessageState) -> str:
    """Route after validate: translate only when chat pair was resolved."""
    if state.get("delivery_status") in ("failed", "skipped"):
        return "deliver"
    if state.get("fallback_to_admins"):
        return "translate"
    text = state.get("original_text", "").strip()
    if not _is_translatable(text):
        return "format"
    return "translate"


def build_graph() -> StateGraph:
    graph = StateGraph(MessageState)

    graph.add_node("validate", validate_node)
    graph.add_node("translate", translate_node)
    graph.add_node("format", format_node)
    graph.add_node("deliver", deliver_node)

    graph.set_entry_point("validate")

    graph.add_conditional_edges(
        "validate",
        _should_translate,
        {"translate": "translate", "format": "format", "deliver": "deliver"},
    )
    graph.add_edge("translate", "format")
    graph.add_edge("format", "deliver")
    graph.add_edge("deliver", END)

    return graph.compile()


# Singleton — compiled once at import
pipeline = build_graph()
