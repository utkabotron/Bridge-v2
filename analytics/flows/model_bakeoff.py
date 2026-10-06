"""Translation model bake-off on real messages, judged blind by a stronger model.

No public benchmark covers Hebrew → Russian WhatsApp chat, so candidates are measured on
our own traffic: the same production prompt (variant A from prompt_registry) and the same
chat context (glossary, members, tone) that the pipeline would send, on recent delivered
messages. A strong judge sees every candidate's rendering of one message side by side,
in shuffled anonymous order, and scores each on the nightly rubric plus a ranking.

Read-only against the database. Writes a JSON report next to this file (or --out).

  docker compose exec analytics python -m flows.model_bakeoff --limit 150 \\
      --candidates gpt-4.1-mini,gpt-6-luna,gpt-5.6-luna,gpt-5.4-mini,gpt-5-mini
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import statistics
import time
from datetime import datetime

from openai import AsyncOpenAI

from .shared import db_conn

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

DEFAULT_CANDIDATES = ["gpt-4.1-mini", "gpt-6-luna", "gpt-5.6-luna", "gpt-5.4-mini", "gpt-5-mini"]
DEFAULT_JUDGE = "gpt-6.1-sol"

# $/1M tokens (input, output), developers.openai.com/api/docs/pricing, 2026-10-06
PRICES = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6-sol": (2.00, 10.00),
    "gpt-6.1-sol": (2.00, 10.00),
}
HEBREW_RE = re.compile(r"[֐-׿]")
CONCURRENCY = 6


# ── Data ──────────────────────────────────────────────────

def load_messages(limit: int, days: int) -> list[dict]:
    """Recent delivered Hebrew messages from paired chats, spread across pairs."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT me.id, me.chat_pair_id, me.original_text, me.translated_text, me.message_type,
                   coalesce(cp.target_language, u.target_language, 'Russian') AS target_language,
                   prof.profile_data
            FROM message_events me
            JOIN chat_pairs cp ON cp.id = me.chat_pair_id
            JOIN users u ON u.id = cp.user_id
            LEFT JOIN chat_profiles prof ON prof.chat_pair_id = cp.id
            WHERE me.created_at >= now() - (%s || ' days')::interval
              AND me.delivery_status = 'delivered'
              AND me.message_type IN ('chat', 'text', 'image', 'document')
              AND length(me.original_text) BETWEEN 20 AND 600
              AND me.original_text ~ '[א-ת]'
            ORDER BY random()
        """, (days,))
        rows = [dict(r) for r in cur.fetchall()]

    # Round-robin over pairs so one busy chat does not become the whole benchmark
    by_pair: dict[int, list[dict]] = {}
    for r in rows:
        by_pair.setdefault(r["chat_pair_id"], []).append(r)
    picked: list[dict] = []
    while len(picked) < limit and any(by_pair.values()):
        for pair_rows in by_pair.values():
            if pair_rows and len(picked) < limit:
                picked.append(pair_rows.pop())
    return picked


def load_prompt() -> str:
    with db_conn(cursor_factory=None) as conn:
        cur = conn.cursor()
        cur.execute("SELECT content FROM prompt_registry WHERE key = 'translate'")
        row = cur.fetchone()

    if not row:
        raise SystemExit("prompt_registry has no 'translate' row — is the processor running?")
    return row[0]


def format_chat_context(profile: dict | None) -> str:
    """Mirror of processor/src/pipeline/prompts.py:format_chat_context."""
    if not profile:
        return ""
    parts = []
    if profile.get("chat_description"):
        parts.append(f"- Group: {profile['chat_description']}")
    if profile.get("tone"):
        parts.append(f"- Tone: {profile['tone']}")
    glossary = profile.get("glossary") or {}
    if glossary:
        items = []
        for word, info in glossary.items():
            if isinstance(info, dict):
                entry = f"{word} → {info.get('translation', '')}"
                if info.get("note"):
                    entry += f" ({info['note']})"
            else:
                entry = f"{word} → {info}"
            items.append(entry)
        parts.append(
            "- Glossary — established renderings of names, places, organisations and "
            "programmes. Use them for these names only; translate everything else normally:\n  "
            + "\n  ".join(items)
        )
    members = profile.get("members") or {}
    if members:
        parts.append("- Member names:\n  " + "\n  ".join(f"{k} → {v}" for k, v in members.items()))
    return "\nChat context:\n" + "\n".join(parts) if parts else ""


# ── Translation ───────────────────────────────────────────

def _is_reasoning_model(model: str) -> bool:
    return model.startswith(("gpt-5", "gpt-6", "o"))


async def translate(client: AsyncOpenAI, model: str, system: str, text: str) -> dict:
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
    }
    if _is_reasoning_model(model):
        # Chat-sized messages do not need thinking; the pipeline wants the lowest latency.
        kwargs["reasoning_effort"] = "none"
    else:
        kwargs["temperature"] = 0

    t0 = time.monotonic()
    try:
        resp = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "reasoning_effort" in kwargs and "reasoning" in str(exc).lower():
            kwargs["reasoning_effort"] = "low"
            resp = await client.chat.completions.create(**kwargs)
        else:
            return {"error": str(exc)[:200], "ms": int((time.monotonic() - t0) * 1000)}
    ms = int((time.monotonic() - t0) * 1000)
    usage = resp.usage
    return {
        "text": (resp.choices[0].message.content or "").strip(),
        "ms": ms,
        "in": usage.prompt_tokens if usage else 0,
        "out": usage.completion_tokens if usage else 0,
    }


# ── Judging ───────────────────────────────────────────────

JUDGE_SYSTEM = """You are a strict bilingual judge of Hebrew → {target_language} translation for a WhatsApp \
parents' group. You see one source message and several candidate translations, labelled T1..Tn \
in random order. Judge each candidate on its own, then rank them.

Scores are 1-5: 5 perfect or near-perfect; 4 good, minor issues; 3 acceptable but noticeable \
problems; 2 poor, significant errors; 1 unusable or wrong.
- quality: overall
- accuracy: meaning preserved (names, numbers, times, negations, who does what)
- naturalness: reads like a native speaker wrote it; no raw transliterations of everyday words
issues: any of mistranslation, omission, unnecessary_addition, wrong_tone, grammar, \
lost_formatting, untranslated — only when real.

The chat context (glossary of names, members) is given; a candidate that follows it for names \
is right, one that transliterates everyday words is wrong.

Return ONLY JSON: {{"scores": {{"T1": {{"quality": n, "accuracy": n, "naturalness": n, \
"issues": [..]}}, ...}}, "ranking": ["T3", "T1", ...]}} — ranking best first; ties allowed only \
when translations are identical."""


async def judge(client: AsyncOpenAI, model: str, msg: dict, context: str,
                candidates: dict[str, str]) -> dict:
    order = list(candidates)
    random.shuffle(order)
    labels = {f"T{i + 1}": name for i, name in enumerate(order)}
    block = "\n\n".join(f"[{label}]\n{candidates[name]}" for label, name in labels.items())
    user = (
        f"{context}\n\nSource (Hebrew):\n{msg['original_text']}\n\nCandidates:\n{block}"
        if context else f"Source (Hebrew):\n{msg['original_text']}\n\nCandidates:\n{block}"
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM.format(target_language=msg["target_language"])},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        reasoning_effort="low",
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    scores = {labels[k]: v for k, v in (data.get("scores") or {}).items() if k in labels}
    ranking = [labels[k] for k in (data.get("ranking") or []) if k in labels]
    usage = resp.usage
    return {"scores": scores, "ranking": ranking,
            "in": usage.prompt_tokens if usage else 0, "out": usage.completion_tokens if usage else 0}


# ── Orchestration ─────────────────────────────────────────

async def run(limit: int, days: int, candidates: list[str], judge_model: str, out: str) -> dict:
    log = logging.getLogger("bakeoff")
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    prompt = load_prompt()
    messages = load_messages(limit, days)
    log.info("%d messages from %d pairs; candidates %s; judge %s",
             len(messages), len({m["chat_pair_id"] for m in messages}), candidates, judge_model)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(model: str, msg: dict, system: str) -> tuple[str, int, dict]:
        async with sem:
            return model, msg["id"], await translate(client, model, system, msg["original_text"])

    tasks = []
    contexts: dict[int, str] = {}
    for msg in messages:
        contexts[msg["id"]] = format_chat_context(msg.get("profile_data"))
        system = prompt.format(target_language=msg["target_language"]) + (
            contexts[msg["id"]] + "\n" if contexts[msg["id"]] else ""
        )
        for model in candidates:
            tasks.append(one(model, msg, system))
    translations: dict[str, dict[int, dict]] = {m: {} for m in candidates}
    done = 0
    for coro in asyncio.as_completed(tasks):
        model, msg_id, result = await coro
        translations[model][msg_id] = result
        done += 1
        if done % 100 == 0:
            log.info("translated %d/%d", done, len(tasks))

    async def judge_one(msg: dict) -> tuple[int, dict | None]:
        cands = {m: translations[m][msg["id"]].get("text") for m in candidates}
        cands = {m: t for m, t in cands.items() if t}
        if len(cands) < 2:
            return msg["id"], None
        async with sem:
            try:
                return msg["id"], await judge(client, judge_model, msg, contexts[msg["id"]], cands)
            except Exception as exc:
                log.warning("judge failed on %s: %s", msg["id"], exc)
                return msg["id"], None

    verdicts: dict[int, dict] = {}
    for coro in asyncio.as_completed([judge_one(m) for m in messages]):
        msg_id, verdict = await coro
        if verdict:
            verdicts[msg_id] = verdict
    log.info("judged %d/%d", len(verdicts), len(messages))

    # ── Aggregate ──
    report: dict = {
        "date": datetime.now().isoformat(timespec="minutes"),
        "messages": len(messages), "judged": len(verdicts), "judge": judge_model,
        "candidates": {},
    }
    judge_in = sum(v["in"] for v in verdicts.values())
    judge_out = sum(v["out"] for v in verdicts.values())
    jp = PRICES.get(judge_model, (0, 0))
    report["judge_cost_usd"] = round((judge_in * jp[0] + judge_out * jp[1]) / 1e6, 3)

    for model in candidates:
        results = translations[model]
        ok = [r for r in results.values() if r.get("text")]
        errors = [r for r in results.values() if r.get("error")]
        lat = sorted(r["ms"] for r in ok)
        q = [verdicts[i]["scores"][model]["quality"] for i in verdicts if model in verdicts[i]["scores"]]
        acc = [verdicts[i]["scores"][model]["accuracy"] for i in verdicts if model in verdicts[i]["scores"]]
        nat = [verdicts[i]["scores"][model]["naturalness"] for i in verdicts if model in verdicts[i]["scores"]]
        wins = sum(1 for v in verdicts.values() if v["ranking"] and v["ranking"][0] == model)
        issues: dict[str, int] = {}
        for v in verdicts.values():
            for t in (v["scores"].get(model) or {}).get("issues") or []:
                issues[t] = issues.get(t, 0) + 1
        leftover_hebrew = sum(1 for r in ok if HEBREW_RE.search(r["text"]))
        tin = sum(r["in"] for r in ok)
        tout = sum(r["out"] for r in ok)
        p = PRICES.get(model, (0, 0))
        report["candidates"][model] = {
            "n": len(ok), "errors": len(errors),
            "error_sample": errors[0]["error"] if errors else None,
            "quality": round(statistics.mean(q), 2) if q else None,
            "accuracy": round(statistics.mean(acc), 2) if acc else None,
            "naturalness": round(statistics.mean(nat), 2) if nat else None,
            "bad_pct": round(100 * sum(s <= 3 for s in q) / len(q), 1) if q else None,
            "win_pct": round(100 * wins / len(verdicts), 1) if verdicts else None,
            "issues": dict(sorted(issues.items(), key=lambda kv: -kv[1])),
            "hebrew_left_pct": round(100 * leftover_hebrew / len(ok), 1) if ok else None,
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
            "cost_per_1k_msgs_usd": round((tin * p[0] + tout * p[1]) / 1e6 / max(len(ok), 1) * 1000, 3),
        }

    # Keep the raw material: every candidate's text per message, for reading by hand.
    report["samples"] = [
        {
            "id": m["id"], "pair": m["chat_pair_id"], "source": m["original_text"],
            "production": m["translated_text"],
            "candidates": {c: translations[c][m["id"]].get("text") for c in candidates},
            "verdict": verdicts.get(m["id"], {}).get("scores"),
            "ranking": verdicts.get(m["id"], {}).get("ranking"),
        }
        for m in messages
    ]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return report


def print_table(report: dict) -> None:
    cols = ["quality", "accuracy", "naturalness", "bad_pct", "win_pct", "hebrew_left_pct", "p50_ms", "p95_ms", "cost_per_1k_msgs_usd", "errors"]
    print(f"\nBake-off {report['date']}: {report['messages']} messages, {report['judged']} judged by {report['judge']} "
          f"(judge cost ${report['judge_cost_usd']})")
    print(f"{'model':14s} " + " ".join(f"{c:>12s}" for c in cols))
    for model, s in sorted(report["candidates"].items(), key=lambda kv: -(kv[1]["quality"] or 0)):
        print(f"{model:14s} " + " ".join(f"{str(s.get(c)):>12s}" for c in cols))
    for model, s in report["candidates"].items():
        if s["issues"]:
            print(f"  {model}: " + ", ".join(f"{k} {v}" for k, v in s["issues"].items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES))
    parser.add_argument("--judge", default=DEFAULT_JUDGE)
    parser.add_argument("--out", default=f"/tmp/bakeoff_{datetime.now():%Y%m%d_%H%M}.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    report = asyncio.run(run(args.limit, args.days, candidates, args.judge, args.out))
    print_table(report)
    print(f"\nfull report: {args.out}")


if __name__ == "__main__":
    main()
