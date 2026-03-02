"""
LangGraph StateGraph for the message processing pipeline.

Flow:
  validate ──(has tg_chat?)──► translate ──► format ──► deliver
             └─(no pair)──────────────────────────────► deliver
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from ..models.message import MessageState
from .nodes import deliver_node, format_node, translate_node, validate_node


def _should_translate(state: MessageState) -> str:
    """Route after validate: translate only when chat pair was resolved."""
    if state.get("delivery_status") in ("failed", "skipped"):
        return "deliver"
    if state.get("fallback_to_admins"):
        return "translate"
    text = state.get("original_text", "").strip()
    if not text:
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
