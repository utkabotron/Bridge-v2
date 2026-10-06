"""Quality breakdowns over a night's evaluations: by source, chat pair, language, type, prompt.

One average over everything hid the picture: admin-fallback chats and delivery failures
pulled the headline down to 3.68 while real paired chats sat at 4.6, and the one chat
responsible for a third of the bad translations was never named. Reports and suggestions
use the bridge slice; the rest is shown next to it, not averaged in.

Pure functions, no Prefect, no database.
"""
from __future__ import annotations

BAD_SCORE = 3  # LLM quality_score at or below this = a bad translation


def is_bridge(ev: dict) -> bool:
    return (ev.get("source") or "bridge") == "bridge"


def bridge_only(evaluations: list[dict]) -> list[dict]:
    return [ev for ev in evaluations if is_bridge(ev)]


def _stats(evals: list[dict]) -> dict:
    scores = [ev.get("quality_score") for ev in evals if ev.get("quality_score")]
    if not scores:
        return {"n": 0, "quality": None, "bad": 0, "bad_pct": None}
    bad = sum(s <= BAD_SCORE for s in scores)
    return {
        "n": len(scores),
        "quality": round(sum(scores) / len(scores), 2),
        "bad": bad,
        "bad_pct": round(100 * bad / len(scores), 1),
    }


def _group(evals: list[dict], key: str, label=lambda v: v) -> list[dict]:
    groups: dict = {}
    for ev in evals:
        groups.setdefault(ev.get(key), []).append(ev)
    rows = [{"key": label(k), **_stats(v)} for k, v in groups.items() if k is not None]
    # Worst first by bad share, then by volume — the chat that needs looking at tops the list.
    return sorted(rows, key=lambda r: (-(r["bad_pct"] or 0), -r["n"]))


def quality_breakdown(evaluations: list[dict]) -> dict:
    """Slices of a night's evaluations. Pair/language/type/prompt rows are bridge-only."""
    bridge = bridge_only(evaluations)
    by_source = {}
    for ev in evaluations:
        by_source.setdefault(ev.get("source") or "bridge", []).append(ev)
    return {
        "by_source": {src: _stats(evs) for src, evs in by_source.items()},
        "by_pair": _group(bridge, "chat_pair_id"),
        "by_language": _group(bridge, "target_language"),
        "by_type": _group(bridge, "message_type"),
        "by_prompt_version": _group(bridge, "prompt_version"),
    }


def worst_pair(breakdown: dict, min_n: int = 5) -> dict | None:
    """The pair most worth attention tonight: enough samples and the highest bad share."""
    candidates = [r for r in breakdown.get("by_pair", []) if r["n"] >= min_n and r["bad"]]
    return candidates[0] if candidates else None
