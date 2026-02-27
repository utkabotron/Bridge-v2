"""Prefect flow: weekly aggregated report with LLM summary.

Collects 7-day data from all nightly analysis runs, generates an LLM summary
with trends, and sends a formatted report to admins via Telegram.

Deploy:
  prefect deployment build flows/weekly_report.py:weekly_report \
    --name weekly-report --cron "0 5 * * 1" --apply
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import httpx
import psycopg2
import psycopg2.extras
from openai import OpenAI
from prefect import flow, get_run_logger, task

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TG_IDS = [int(x) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()]

SUMMARY_MODEL = "gpt-4o-mini"


@task(retries=2, name="collect-weekly-data")
def collect_weekly_data() -> dict:
    """Collect 7-day aggregates from all analytics tables."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    today = date.today()
    week_ago = today - timedelta(days=7)
    data: dict = {"period_start": str(week_ago), "period_end": str(today)}

    # --- Message events overview ---
    cur.execute("""
        SELECT
            count(*) AS total_messages,
            count(*) FILTER (WHERE delivery_status = 'delivered') AS delivered,
            count(*) FILTER (WHERE delivery_status = 'failed') AS failed,
            count(*) FILTER (WHERE delivery_status = 'pending') AS pending,
            round(avg(translation_ms) FILTER (WHERE translation_ms IS NOT NULL)::numeric, 0) AS avg_translation_ms,
            max(translation_ms) AS max_translation_ms,
            count(*) FILTER (WHERE translation_ms > 3000) AS slow_translations
        FROM message_events
        WHERE created_at >= %s AND created_at < %s
    """, (week_ago, today))
    data["messages"] = dict(cur.fetchone())

    # --- Nightly problems: daily summaries ---
    cur.execute("""
        SELECT run_date, summary
        FROM nightly_analysis_runs
        WHERE flow_type = 'problems'
          AND run_date >= %s AND run_date < %s
        ORDER BY run_date
    """, (week_ago, today))
    data["problem_runs"] = [
        {"date": str(r["run_date"]), "summary": r["summary"]}
        for r in cur.fetchall()
    ]

    # --- Detected issues ---
    cur.execute("""
        SELECT di.severity, di.category, di.title
        FROM detected_issues di
        JOIN nightly_analysis_runs nar ON nar.id = di.run_id
        WHERE nar.run_date >= %s AND nar.run_date < %s
          AND nar.flow_type = 'problems'
        ORDER BY di.severity, di.category
    """, (week_ago, today))
    data["issues"] = [dict(r) for r in cur.fetchall()]

    # --- Translation quality: daily summaries ---
    cur.execute("""
        SELECT run_date, summary
        FROM nightly_analysis_runs
        WHERE flow_type = 'translation_quality'
          AND run_date >= %s AND run_date < %s
        ORDER BY run_date
    """, (week_ago, today))
    data["quality_runs"] = [
        {"date": str(r["run_date"]), "summary": r["summary"]}
        for r in cur.fetchall()
    ]

    # --- Translation evaluations: avg scores by day ---
    cur.execute("""
        SELECT
            nar.run_date,
            round(avg(te.quality_score)::numeric, 2) AS avg_quality,
            round(avg(te.accuracy_score)::numeric, 2) AS avg_accuracy,
            round(avg(te.naturalness_score)::numeric, 2) AS avg_naturalness,
            count(*) AS sample_count
        FROM translation_evaluations te
        JOIN nightly_analysis_runs nar ON nar.id = te.run_id
        WHERE nar.run_date >= %s AND nar.run_date < %s
        GROUP BY nar.run_date
        ORDER BY nar.run_date
    """, (week_ago, today))
    data["daily_scores"] = [
        {
            "date": str(r["run_date"]),
            "quality": float(r["avg_quality"]) if r["avg_quality"] else None,
            "accuracy": float(r["avg_accuracy"]) if r["avg_accuracy"] else None,
            "naturalness": float(r["avg_naturalness"]) if r["avg_naturalness"] else None,
            "samples": r["sample_count"],
        }
        for r in cur.fetchall()
    ]

    # --- Prompt suggestions ---
    cur.execute("""
        SELECT ps.suggestion, ps.rationale
        FROM prompt_suggestions ps
        JOIN nightly_analysis_runs nar ON nar.id = ps.run_id
        WHERE nar.run_date >= %s AND nar.run_date < %s
        ORDER BY ps.created_at DESC
    """, (week_ago, today))
    data["suggestions"] = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()

    logger.info(
        "Collected weekly data: %d messages, %d issues, %d quality runs, %d suggestions",
        data["messages"]["total_messages"],
        len(data["issues"]),
        len(data["quality_runs"]),
        len(data["suggestions"]),
    )
    return data


@task(retries=1, name="summarize-with-llm")
def summarize_with_llm(data: dict) -> dict:
    """Generate LLM summary with trends and recommendations."""
    logger = get_run_logger()

    if data["messages"]["total_messages"] == 0:
        logger.info("No messages this week, skipping LLM summary")
        return {"summary": "No messages processed this week.", "tokens_used": 0}

    client = OpenAI(api_key=OPENAI_API_KEY)

    system_prompt = """You are a DevOps/SRE analyst for a WhatsApp→Telegram message bridge.
Analyze the weekly aggregated data and produce a concise summary with trends.

Focus on:
- Message delivery trends (improving/degrading?)
- Translation quality trends (scores going up/down?)
- Recurring problems and their patterns
- Most impactful prompt suggestions
- Actionable recommendations for the coming week

Write in plain text, no markdown. Keep it under 500 characters.
Be specific with numbers and comparisons when possible."""

    user_prompt = f"Weekly data ({data['period_start']} to {data['period_end']}):\n\n{json.dumps(data, indent=2, default=str)}"

    response = client.chat.completions.create(
        model=SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=1000,
    )

    summary = response.choices[0].message.content.strip()
    tokens_used = response.usage.total_tokens if response.usage else 0

    logger.info("Generated weekly summary (%d chars, %d tokens)", len(summary), tokens_used)
    return {"summary": summary, "tokens_used": tokens_used}


@task(retries=1, name="notify-weekly-report")
def notify_weekly_report(data: dict, llm_result: dict) -> int:
    """Format and send weekly report via Telegram."""
    logger = get_run_logger()

    if not TELEGRAM_BOT_TOKEN or not ADMIN_TG_IDS:
        logger.warning("Telegram not configured, cannot send weekly report")
        return 0

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    msgs = data["messages"]
    total = msgs["total_messages"]
    delivered = msgs["delivered"]
    pct = round(delivered / total * 100) if total else 0
    avg_ms = msgs["avg_translation_ms"]
    avg_s = round(float(avg_ms) / 1000, 1) if avg_ms else "—"

    lines = [
        f"📋 <b>Weekly Report ({data['period_start']} – {data['period_end']})</b>\n",
        f"<b>Messages:</b> {total} total, {delivered} delivered ({pct}%)",
        f"Avg translation: {avg_s}s\n",
    ]

    # Translation quality trends
    daily_scores = data.get("daily_scores", [])
    if len(daily_scores) >= 2:
        first = daily_scores[0]
        last = daily_scores[-1]

        def _arrow(old, new):
            if old is None or new is None:
                return "—"
            diff = new - old
            arrow = "↑" if diff > 0 else "↓" if diff < 0 else "→"
            return f"{old} → {new} {arrow}"

        lines.append("<b>Translation Quality:</b>")
        lines.append(f"  Quality: {_arrow(first.get('quality'), last.get('quality'))}")
        lines.append(f"  Accuracy: {_arrow(first.get('accuracy'), last.get('accuracy'))}")
        lines.append(f"  Naturalness: {_arrow(first.get('naturalness'), last.get('naturalness'))}\n")
    elif daily_scores:
        s = daily_scores[0]
        lines.append("<b>Translation Quality:</b>")
        lines.append(f"  Quality: {s.get('quality', '—')}")
        lines.append(f"  Accuracy: {s.get('accuracy', '—')}")
        lines.append(f"  Naturalness: {s.get('naturalness', '—')}\n")

    # Issues summary
    issues = data.get("issues", [])
    if issues:
        critical = sum(1 for i in issues if i["severity"] == "critical")
        warning = sum(1 for i in issues if i["severity"] == "warning")

        # Count by category
        cat_counts: dict[str, int] = {}
        for i in issues:
            cat_counts[i["category"]] = cat_counts.get(i["category"], 0) + 1
        top_cats = sorted(cat_counts.items(), key=lambda x: -x[1])[:3]
        top_str = ", ".join(f"{_esc(c)} ({n})" for c, n in top_cats)

        lines.append(f"<b>Issues this week:</b> {len(issues)} ({critical} critical, {warning} warning)")
        lines.append(f"Top: {top_str}\n")

    # LLM summary
    summary = llm_result.get("summary", "")
    if summary:
        lines.append(f"<b>LLM Summary:</b>\n{_esc(summary)}\n")

    # Top prompt suggestions
    suggestions = data.get("suggestions", [])
    if suggestions:
        # Deduplicate by suggestion text, keep first occurrence
        seen: set[str] = set()
        unique: list[dict] = []
        for s in suggestions:
            if s["suggestion"] not in seen:
                seen.add(s["suggestion"])
                unique.append(s)

        top = unique[:3]
        lines.append("<b>Top prompt suggestions:</b>")
        for i, s in enumerate(top, 1):
            lines.append(f"{i}. {_esc(s['suggestion'])}")

    text = "\n".join(lines)

    # Telegram max message length is 4096
    if len(text) > 4096:
        text = text[:4090] + "\n…"

    sent = 0
    for chat_id in ADMIN_TG_IDS:
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            sent += 1
        except Exception as e:
            logger.error("Failed to notify admin %d: %s", chat_id, e)

    logger.info("Sent weekly report to %d/%d admins", sent, len(ADMIN_TG_IDS))
    return sent


@flow(name="weekly-report", log_prints=True)
def weekly_report():
    """Weekly report: collect 7-day data → LLM summary → notify admins."""
    data = collect_weekly_data()
    llm_result = summarize_with_llm(data)
    notified = notify_weekly_report(data, llm_result)
    return {
        "period": f"{data['period_start']} – {data['period_end']}",
        "total_messages": data["messages"]["total_messages"],
        "issues": len(data.get("issues", [])),
        "suggestions": len(data.get("suggestions", [])),
        "tokens_used": llm_result.get("tokens_used", 0),
        "admins_notified": notified,
    }


if __name__ == "__main__":
    weekly_report()
