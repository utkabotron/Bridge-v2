"""Translation model bake-off on real messages, judged blind by stronger models.

No public benchmark covers Hebrew → Russian WhatsApp chat, so candidates are measured on
our own traffic: the same production prompt (variant A from prompt_registry) and the same
chat context (chat profile plus the service glossary of names, applied as the processor
applies it) that the pipeline would send, on recent delivered
messages. Each judge sees every candidate's rendering of one message side by side, in
shuffled anonymous order, and scores each on the nightly rubric plus a ranking.

Candidates and judges may be OpenAI (gpt-*, o*) or Anthropic (claude-*) models. With
judges from both families the report shows whether a verdict survives a change of judge —
the 2026-10-06 run had an OpenAI judge rank OpenAI models only.

Read-only against the database. Writes a JSON report next to this file (or --out).

  docker compose exec analytics python -m flows.model_bakeoff --limit 150 \\
      --candidates gpt-4.1-mini,gpt-6-luna,claude-haiku-5-5,claude-sonnet-5-5 \\
      --judges gpt-6.1-sol,claude-opus-5-5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import statistics
import time
from datetime import datetime

# The translator's own pieces, not copies of them: the request shape, the chat-context
# block and the price table are the ones the processor runs on (shared/bridge_shared).
from bridge_shared.chat_context import covered_by_global, format_chat_context, with_glossary
from bridge_shared.glossary_match import GlossaryIndex
from bridge_shared.llm import chat_request, token_cost
from bridge_shared.scripts import HEBREW_RE
from openai import AsyncOpenAI

from .shared import db_conn

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

DEFAULT_CANDIDATES = ["gpt-4.1-mini", "gpt-6-luna", "claude-haiku-5-5", "claude-haiku-4-5",
                      "claude-sonnet-5-5", "claude-opus-5-5"]
DEFAULT_JUDGES = ["gpt-6.1-sol", "claude-opus-5-5"]

CONCURRENCY = 6

# How each Claude candidate is asked. The translator wants the lowest latency and no
# thinking, like reasoning "none" on the OpenAI side; each model allows a different form:
# Haiku 5.5 and 4.5 can run without thinking, Sonnet 5.5 rejects "disabled" and takes
# "between_tools" instead (no tools here, so no thinking), Opus 5.5 always thinks — it gets
# the lowest effort.
CLAUDE_TRANSLATE = {
    "claude-haiku-5-5": {"thinking": {"type": "disabled"}},
    "claude-haiku-4-5": {},
    "claude-sonnet-5-5": {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}},
    "claude-opus-5-5": {"output_config": {"effort": "low"}},
}
# A judge is worth some thinking, as the OpenAI judge gets reasoning "low".
CLAUDE_JUDGE = {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}}


def is_claude(model: str) -> bool:
    return model.startswith("claude-")


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


class ServiceGlossary:
    """The service glossary of names, applied the way the processor applies it
    (processor/src/pipeline/glossary.py): verified/locked entries; names that are also
    everyday words only in their chats; a chat's override wins."""

    def __init__(self):
        with db_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT source, target_language, translation, note, kind, status, also_word, chat_pairs
                FROM glossary WHERE status IN ('verified', 'locked') AND translation IS NOT NULL
            """)
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT chat_pair_id, source, target_language, translation, note FROM glossary_override")
            overrides = [dict(r) for r in cur.fetchall()]

        def entry(r):
            note = r["note"] or ("имя" if r.get("kind") == "person" else None)
            return {"translation": r["translation"], **({"note": note} if note else {})}

        everywhere: dict[str, dict] = {}
        scoped: dict[tuple[int, str], dict] = {}
        for r in rows:
            if r["status"] == "locked" or r["also_word"] is False:
                everywhere.setdefault(r["target_language"], {})[r["source"]] = entry(r)
            else:
                for pid in r["chat_pairs"] or ():
                    scoped.setdefault((pid, r["target_language"]), {})[r["source"]] = entry(r)
        own: dict[tuple[int, str], dict] = {}
        for r in overrides:
            own.setdefault((r["chat_pair_id"], r["target_language"]), {})[r["source"]] = entry(r)
        self.everywhere = {k: GlossaryIndex(v) for k, v in everywhere.items()}
        self.scoped = {k: GlossaryIndex(v) for k, v in scoped.items()}
        self.own = {k: GlossaryIndex(v) for k, v in own.items()}

    def context(self, msg: dict) -> str:
        """The chat-context block production would send for this message."""
        lang, pair, text = msg["target_language"], msg["chat_pair_id"], msg["original_text"]
        hits = self.everywhere[lang].find(text) if lang in self.everywhere else {}
        if (pair, lang) in self.scoped:
            hits.update(self.scoped[(pair, lang)].find(text))
        if (pair, lang) in self.own:
            mine = self.own[(pair, lang)].find(text)
            hits = {k: v for k, v in hits.items() if not covered_by_global(k, mine)} | mine
        return format_chat_context(with_glossary(msg.get("profile_data") or {}, hits))


# ── Model calls ───────────────────────────────────────────

class Clients:
    """One client per provider, created only when a model of that provider is used."""

    def __init__(self, models: list[str]):
        self.openai = AsyncOpenAI(api_key=OPENAI_API_KEY) if any(not is_claude(m) for m in models) else None
        self.anthropic = None
        if any(is_claude(m) for m in models):
            from anthropic import AsyncAnthropic  # reads ANTHROPIC_API_KEY
            self.anthropic = AsyncAnthropic()


async def claude_call(client, model: str, system: str, user: str, extra: dict, max_tokens: int) -> dict:
    """One Messages API call → {"text", "in", "out"}; a refusal or a cut-off answer raises."""
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}], **extra,
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"refusal ({getattr(resp.stop_details, 'category', None)})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("max_tokens reached")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return {"text": text, "in": resp.usage.input_tokens, "out": resp.usage.output_tokens}


async def translate(clients: Clients, model: str, system: str, text: str) -> dict:
    t0 = time.monotonic()
    try:
        if is_claude(model):
            r = await claude_call(clients.anthropic, model, system, text,
                                  CLAUDE_TRANSLATE.get(model, {}), max_tokens=4000)
            return {**r, "ms": int((time.monotonic() - t0) * 1000)}

        # chat_request's defaults are the translator's request: reasoning "none" (chat-sized
        # messages need no thinking, the pipeline wants the lowest latency), temperature 0.
        kwargs = chat_request(model, [{"role": "system", "content": system}, {"role": "user", "content": text}])
        try:
            resp = await clients.openai.chat.completions.create(**kwargs)
        except Exception as exc:
            # Some reasoning candidates reject the "none" effort; measure them at "low"
            # rather than dropping them from the table.
            if "reasoning_effort" in kwargs and "reasoning" in str(exc).lower():
                kwargs["reasoning_effort"] = "low"
                resp = await clients.openai.chat.completions.create(**kwargs)
            else:
                raise
    except Exception as exc:
        return {"error": str(exc)[:200], "ms": int((time.monotonic() - t0) * 1000)}
    usage = resp.usage
    return {
        "text": (resp.choices[0].message.content or "").strip(),
        "ms": int((time.monotonic() - t0) * 1000),
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


def parse_json_object(content: str) -> dict:
    """The JSON object in a judge's answer, tolerating markdown fences or a stray line."""
    content = (content or "").strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in the answer")
    return json.loads(content[start:end + 1])


async def judge(clients: Clients, model: str, msg: dict, context: str, candidates: dict[str, str]) -> dict:
    order = list(candidates)
    random.shuffle(order)
    labels = {f"T{i + 1}": name for i, name in enumerate(order)}
    block = "\n\n".join(f"[{label}]\n{candidates[name]}" for label, name in labels.items())
    user = (
        f"{context}\n\nSource (Hebrew):\n{msg['original_text']}\n\nCandidates:\n{block}"
        if context else f"Source (Hebrew):\n{msg['original_text']}\n\nCandidates:\n{block}"
    )
    system = JUDGE_SYSTEM.format(target_language=msg["target_language"])
    if is_claude(model):
        r = await claude_call(clients.anthropic, model, system, user, CLAUDE_JUDGE, max_tokens=16000)
        data, tin, tout = parse_json_object(r["text"]), r["in"], r["out"]
    else:
        resp = await clients.openai.chat.completions.create(**chat_request(
            model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
            reasoning="low", json_mode=True,
        ))
        data = json.loads(resp.choices[0].message.content or "{}")
        tin = resp.usage.prompt_tokens if resp.usage else 0
        tout = resp.usage.completion_tokens if resp.usage else 0
    scores = {labels[k]: v for k, v in (data.get("scores") or {}).items() if k in labels}
    ranking = [labels[k] for k in (data.get("ranking") or []) if k in labels]
    return {"scores": scores, "ranking": ranking, "in": tin, "out": tout}


# ── Aggregation ───────────────────────────────────────────

def judge_summary(candidates: list[str], verdicts: dict[int, dict]) -> dict:
    """Per candidate: mean scores, share of bad (≤3) answers, wins and issues, by one judge."""
    out = {}
    for model in candidates:
        rows = [v["scores"][model] for v in verdicts.values() if model in v["scores"]]
        q = [r["quality"] for r in rows if isinstance(r.get("quality"), (int, float))]
        acc = [r["accuracy"] for r in rows if isinstance(r.get("accuracy"), (int, float))]
        nat = [r["naturalness"] for r in rows if isinstance(r.get("naturalness"), (int, float))]
        wins = sum(1 for v in verdicts.values() if v["ranking"] and v["ranking"][0] == model)
        issues: dict[str, int] = {}
        for r in rows:
            for t in r.get("issues") or []:
                issues[t] = issues.get(t, 0) + 1
        out[model] = {
            "quality": round(statistics.mean(q), 2) if q else None,
            "accuracy": round(statistics.mean(acc), 2) if acc else None,
            "naturalness": round(statistics.mean(nat), 2) if nat else None,
            "bad_pct": round(100 * sum(s <= 3 for s in q) / len(q), 1) if q else None,
            "win_pct": round(100 * wins / len(verdicts), 1) if verdicts else None,
            "issues": dict(sorted(issues.items(), key=lambda kv: -kv[1])),
        }
    return out


# ── Orchestration ─────────────────────────────────────────

async def run(limit: int, days: int, candidates: list[str], judges: list[str], out: str) -> dict:
    log = logging.getLogger("bakeoff")
    clients = Clients(candidates + judges)
    prompt = load_prompt()
    names = ServiceGlossary()
    messages = load_messages(limit, days)
    log.info("%d messages from %d pairs; candidates %s; judges %s",
             len(messages), len({m["chat_pair_id"] for m in messages}), candidates, judges)

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(model: str, msg: dict, system: str) -> tuple[str, int, dict]:
        async with sem:
            return model, msg["id"], await translate(clients, model, system, msg["original_text"])

    tasks = []
    contexts: dict[int, str] = {}
    for msg in messages:
        contexts[msg["id"]] = names.context(msg)
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

    async def judge_one(judge_model: str, msg: dict) -> tuple[str, int, dict | None]:
        cands = {m: translations[m][msg["id"]].get("text") for m in candidates}
        cands = {m: t for m, t in cands.items() if t}
        if len(cands) < 2:
            return judge_model, msg["id"], None
        async with sem:
            try:
                return judge_model, msg["id"], await judge(clients, judge_model, msg, contexts[msg["id"]], cands)
            except Exception as exc:
                log.warning("judge %s failed on %s: %s", judge_model, msg["id"], exc)
                return judge_model, msg["id"], None

    verdicts: dict[str, dict[int, dict]] = {j: {} for j in judges}
    for coro in asyncio.as_completed([judge_one(j, m) for j in judges for m in messages]):
        judge_model, msg_id, verdict = await coro
        if verdict:
            verdicts[judge_model][msg_id] = verdict
    for j in judges:
        log.info("%s judged %d/%d", j, len(verdicts[j]), len(messages))

    # ── Aggregate ──
    report: dict = {
        "date": datetime.now().isoformat(timespec="minutes"),
        "messages": len(messages), "candidates": {}, "judges": {},
    }
    for j in judges:
        jin = sum(v["in"] for v in verdicts[j].values())
        jout = sum(v["out"] for v in verdicts[j].values())
        report["judges"][j] = {
            "judged": len(verdicts[j]),
            "cost_usd": round(token_cost(j, jin, jout), 3),
            "scores": judge_summary(candidates, verdicts[j]),
        }

    for model in candidates:
        results = translations[model]
        ok = [r for r in results.values() if r.get("text")]
        errors = [r for r in results.values() if r.get("error")]
        lat = sorted(r["ms"] for r in ok)
        leftover_hebrew = sum(1 for r in ok if HEBREW_RE.search(r["text"]))
        tin = sum(r["in"] for r in ok)
        tout = sum(r["out"] for r in ok)
        per_judge = [report["judges"][j]["scores"][model]["quality"] for j in judges]
        per_judge = [q for q in per_judge if q is not None]
        report["candidates"][model] = {
            "n": len(ok), "errors": len(errors),
            "error_sample": errors[0]["error"] if errors else None,
            "quality_all_judges": round(statistics.mean(per_judge), 2) if per_judge else None,
            "hebrew_left_pct": round(100 * leftover_hebrew / len(ok), 1) if ok else None,
            "p50_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[int(len(lat) * 0.95)] if lat else None,
            "cost_per_1k_msgs_usd": round(token_cost(model, tin, tout) / max(len(ok), 1) * 1000, 3),
        }

    # Keep the raw material: every candidate's text per message, for reading by hand.
    report["samples"] = [
        {
            "id": m["id"], "pair": m["chat_pair_id"], "source": m["original_text"],
            "production": m["translated_text"],
            "candidates": {c: translations[c][m["id"]].get("text") for c in candidates},
            "verdicts": {j: {"scores": verdicts[j].get(m["id"], {}).get("scores"),
                             "ranking": verdicts[j].get(m["id"], {}).get("ranking")} for j in judges},
        }
        for m in messages
    ]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return report


def print_table(report: dict) -> None:
    print(f"\nBake-off {report['date']}: {report['messages']} messages")
    base = ["quality_all_judges", "hebrew_left_pct", "p50_ms", "p95_ms", "cost_per_1k_msgs_usd", "errors"]
    print(f"{'model':18s} " + " ".join(f"{c:>20s}" for c in base))
    for model, s in sorted(report["candidates"].items(), key=lambda kv: -(kv[1]["quality_all_judges"] or 0)):
        print(f"{model:18s} " + " ".join(f"{str(s.get(c)):>20s}" for c in base))
        if s["error_sample"]:
            print(f"    error: {s['error_sample']}")
    cols = ["quality", "accuracy", "naturalness", "bad_pct", "win_pct"]
    for j, jr in report["judges"].items():
        print(f"\njudge {j}: {jr['judged']} judged, cost ${jr['cost_usd']}")
        print(f"{'model':18s} " + " ".join(f"{c:>12s}" for c in cols))
        for model, s in sorted(jr["scores"].items(), key=lambda kv: -(kv[1]["quality"] or 0)):
            print(f"{model:18s} " + " ".join(f"{str(s.get(c)):>12s}" for c in cols))
        for model, s in jr["scores"].items():
            if s["issues"]:
                print(f"  {model}: " + ", ".join(f"{k} {v}" for k, v in s["issues"].items()))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--candidates", default=",".join(DEFAULT_CANDIDATES))
    parser.add_argument("--judges", default=",".join(DEFAULT_JUDGES))
    parser.add_argument("--out", default=f"/tmp/bakeoff_{datetime.now():%Y%m%d_%H%M}.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    report = asyncio.run(run(args.limit, args.days, candidates, judges, args.out))
    print_table(report)
    print(f"\nfull report: {args.out}")


if __name__ == "__main__":
    main()
