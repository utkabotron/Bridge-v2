"""Prefect flow: weekly system intelligence report via o3.

Collects 4 weeks of data, loads previous weekly insights for continuity,
performs deep analysis with o3, stores insights in weekly_insights table,
and sends executive summary to admins via Telegram.

Deploy:
  prefect deployment build flows/weekly_report.py:weekly_report \
    --name weekly-report --cron "0 5 * * 1" --apply
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import psycopg2
import psycopg2.extras
from openai import OpenAI
from prefect import flow, get_run_logger, task

from .shared import esc, notify_telegram

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

ANALYSIS_MODEL = "o3"


@task(retries=2, name="collect-weekly-data")
def collect_weekly_data() -> dict:
    """Collect extended data: current week + 4 weeks of trends, prompt, backlog, changelog."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    today = date.today()
    week_ago = today - timedelta(days=7)
    four_weeks_ago = today - timedelta(days=28)
    month_ago = today - timedelta(days=30)
    data: dict = {"period_start": str(week_ago), "period_end": str(today)}

    # --- Current week message overview ---
    cur.execute("""
        SELECT
            count(*) AS total_messages,
            count(*) FILTER (WHERE delivery_status = 'delivered') AS delivered,
            count(*) FILTER (WHERE delivery_status = 'failed') AS failed,
            count(*) FILTER (WHERE delivery_status = 'pending') AS pending,
            count(*) FILTER (WHERE delivery_status = 'skipped') AS skipped,
            round(avg(translation_ms) FILTER (WHERE translation_ms IS NOT NULL)::numeric, 0) AS avg_translation_ms,
            max(translation_ms) AS max_translation_ms,
            count(*) FILTER (WHERE translation_ms > 3000) AS slow_translations,
            count(*) FILTER (WHERE chat_pair_id IS NOT NULL) AS mapped_total,
            count(*) FILTER (WHERE chat_pair_id IS NOT NULL AND delivery_status = 'delivered') AS mapped_delivered,
            count(*) FILTER (WHERE chat_pair_id IS NOT NULL AND delivery_status = 'failed') AS mapped_failed
        FROM message_events
        WHERE created_at >= %s AND created_at < %s
    """, (week_ago, today))
    data["messages"] = dict(cur.fetchone())

    # --- Nightly problems: daily summaries (this week) ---
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

    # --- Detected issues (this week) ---
    cur.execute("""
        SELECT di.severity, di.category, di.title, di.description
        FROM detected_issues di
        JOIN nightly_analysis_runs nar ON nar.id = di.run_id
        WHERE nar.run_date >= %s AND nar.run_date < %s
          AND nar.flow_type = 'problems'
        ORDER BY di.severity, di.category
    """, (week_ago, today))
    data["issues"] = [dict(r) for r in cur.fetchall()]

    # --- Translation quality scores: 4 weeks for trends ---
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
    """, (four_weeks_ago, today))
    data["daily_scores_4w"] = [
        {
            "date": str(r["run_date"]),
            "quality": float(r["avg_quality"]) if r["avg_quality"] else None,
            "accuracy": float(r["avg_accuracy"]) if r["avg_accuracy"] else None,
            "naturalness": float(r["avg_naturalness"]) if r["avg_naturalness"] else None,
            "samples": r["sample_count"],
        }
        for r in cur.fetchall()
    ]

    # --- Direct interactions (bot private chat) ---
    cur.execute("""
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE interaction_type = 'translation') AS translations,
            count(*) FILTER (WHERE interaction_type = 'media_analysis') AS analyses,
            count(*) FILTER (WHERE status = 'failed') AS failed,
            round(avg(translation_ms) FILTER (WHERE translation_ms IS NOT NULL)::numeric, 0) AS avg_translation_ms,
            round(avg(processing_ms) FILTER (WHERE processing_ms IS NOT NULL)::numeric, 0) AS avg_processing_ms
        FROM direct_interactions
        WHERE created_at >= %s AND created_at < %s
    """, (week_ago, today))
    data["direct_interactions"] = dict(cur.fetchone())

    # --- Current translation prompt ---
    cur.execute("SELECT key, version, content FROM prompt_registry WHERE key = 'translate'")
    row = cur.fetchone()
    if row:
        data["current_prompt"] = {"version": row["version"], "content": row["content"]}
    else:
        data["current_prompt"] = None

    # --- ALL pending prompt suggestions (not just this week) ---
    cur.execute("""
        SELECT ps.id, ps.suggestion, ps.rationale, nar.run_date
        FROM prompt_suggestions ps
        JOIN nightly_analysis_runs nar ON nar.id = ps.run_id
        WHERE ps.status = 'pending'
        ORDER BY ps.created_at DESC
    """)
    data["pending_suggestions"] = [
        {
            "id": r["id"],
            "suggestion": r["suggestion"],
            "rationale": r["rationale"],
            "from_date": str(r["run_date"]),
        }
        for r in cur.fetchall()
    ]

    # --- Open issues from backlog ---
    cur.execute("""
        SELECT id, source_run_date, severity, category, title, description, suggested_fix
        FROM issues_backlog
        WHERE status = 'open'
        ORDER BY source_run_date DESC
    """)
    data["open_backlog"] = [dict(r) for r in cur.fetchall()]
    # Serialize dates
    for item in data["open_backlog"]:
        item["source_run_date"] = str(item["source_run_date"])

    # --- Analytics changelog (last month) ---
    cur.execute("""
        SELECT change_date, change_type, description, impact_notes
        FROM analytics_changelog
        WHERE change_date >= %s
        ORDER BY change_date DESC
    """, (month_ago,))
    data["changelog"] = [
        {
            "date": str(r["change_date"]),
            "type": r["change_type"],
            "description": r["description"],
            "impact": r["impact_notes"],
        }
        for r in cur.fetchall()
    ]

    cur.close()
    conn.close()

    logger.info(
        "Collected weekly data: %d messages, %d direct, %d issues, %d quality days (4w), "
        "%d pending suggestions, %d open backlog, %d changelog entries",
        data["messages"]["total_messages"],
        data["direct_interactions"]["total"],
        len(data["issues"]),
        len(data["daily_scores_4w"]),
        len(data["pending_suggestions"]),
        len(data["open_backlog"]),
        len(data["changelog"]),
    )
    return data


@task(retries=2, name="load-previous-insights")
def load_previous_insights() -> list[dict]:
    """Load last 4 weekly_insights for continuity and recommendation tracking."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT week_start, week_end, executive_summary,
               deep_analysis, recommendations, prompt_draft,
               analytics_meta, previous_recommendations_review
        FROM weekly_insights
        ORDER BY week_start DESC
        LIMIT 4
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    insights = []
    for r in rows:
        insights.append({
            "week_start": str(r["week_start"]),
            "week_end": str(r["week_end"]),
            "executive_summary": r["executive_summary"],
            "deep_analysis": r["deep_analysis"],
            "recommendations": r["recommendations"],
            "prompt_draft": r["prompt_draft"],
            "analytics_meta": r["analytics_meta"],
            "previous_recommendations_review": r["previous_recommendations_review"],
        })

    logger.info("Loaded %d previous weekly insights", len(insights))
    return insights


@task(retries=1, name="analyze-with-o3")
def analyze_with_o3(data: dict, previous_insights: list[dict]) -> dict:
    """Deep analysis with o3 thinking model — the system intelligence brain."""
    logger = get_run_logger()

    if data["messages"]["total_messages"] == 0:
        logger.info("No messages this week, skipping o3 analysis")
        return {
            "analysis": {
                "executive_summary": "No messages processed this week.",
                "delivery_health": {},
                "translation_quality": {},
                "prompt_evaluation": {},
                "recommendations": [],
                "previous_recommendations_review": [],
                "analytics_meta": {},
            },
            "tokens_used": 0,
        }

    client = OpenAI(api_key=OPENAI_API_KEY)

    system_prompt = """You are the system intelligence brain for a WhatsApp→Telegram message bridge.
You perform deep weekly analysis with full historical context and memory of your previous recommendations.

You receive:
- Current week's operational data (messages, failures, issues, quality scores)
- Direct interactions stats (translations and media analysis from bot private chat)
- 4 weeks of quality score trends
- The current translation prompt
- ALL pending prompt improvement suggestions from daily flows
- Open issues from the persistent backlog
- Recent analytics changelog (model switches, config changes)
- Your previous 4 weekly insights (for continuity and recommendation tracking)

Return a single JSON object with these exact sections:

{
  "executive_summary": "TL;DR of the week in ≤300 characters",

  "delivery_health": {
    "failure_rate_pct": <number>,
    "trend": "improving|stable|degrading",
    "root_causes": ["..."],
    "progress_on_past_recommendations": ["..."]
  },

  "translation_quality": {
    "avg_scores": {"quality": <n>, "accuracy": <n>, "naturalness": <n>},
    "trend": "improving|stable|degrading",
    "per_language_notes": ["..."],
    "recurring_issues": ["..."],
    "impact_of_recent_changes": "..."
  },

  "prompt_evaluation": {
    "current_prompt_assessment": "...",
    "suggestion_reviews": [
      {
        "suggestion_id": <int>,
        "verdict": "apply|reject|defer",
        "reasoning": "..."
      }
    ],
    "new_prompt_draft": null or "full new prompt text if changes recommended"
  },

  "recommendations": [
    {
      "priority": <1-7>,
      "area": "delivery|translation|prompt|infrastructure|monitoring",
      "action": "specific action to take",
      "rationale": "why this matters",
      "metric_to_track": "how to measure success"
    }
  ],

  "previous_recommendations_review": [
    {
      "recommendation": "original text",
      "status": "implemented|in_progress|not_started|no_longer_relevant",
      "evidence": "what data shows about this"
    }
  ],

  "analytics_meta": {
    "daily_flow_assessment": "are the daily flows producing signal or noise?",
    "score_calibration": "are quality scores well-calibrated or inflated/deflated?",
    "threshold_suggestions": "any threshold adjustments needed?",
    "self_improvement": "what should change about the analytics system itself?"
  }
}

Rules:
- Be specific and data-driven. Reference actual numbers from the data.
- For prompt_evaluation: review EVERY pending suggestion. If recommending a new prompt, write it out COMPLETELY.
- For recommendations: prioritize by impact. 3-7 items max.
- For previous_recommendations_review: track each recommendation from your last report.
- Be honest about what's working and what isn't. No false optimism.
- Return ONLY the JSON object, no markdown fences or extra text."""

    # Build context with previous insights
    context_parts = [
        f"=== CURRENT WEEK DATA ({data['period_start']} to {data['period_end']}) ===",
        json.dumps(data, indent=2, default=str),
    ]

    if previous_insights:
        context_parts.append("\n=== PREVIOUS WEEKLY INSIGHTS (most recent first) ===")
        for insight in previous_insights:
            context_parts.append(f"\n--- Week {insight['week_start']} to {insight['week_end']} ---")
            context_parts.append(f"Summary: {insight.get('executive_summary', 'N/A')}")
            if insight.get("recommendations"):
                context_parts.append(f"Recommendations: {json.dumps(insight['recommendations'], default=str)}")
            if insight.get("analytics_meta"):
                context_parts.append(f"Analytics meta: {json.dumps(insight['analytics_meta'], default=str)}")
    else:
        context_parts.append("\n=== NO PREVIOUS WEEKLY INSIGHTS (first run) ===")

    user_prompt = "\n".join(context_parts)

    response = client.chat.completions.create(
        model=ANALYSIS_MODEL,
        messages=[
            {"role": "developer", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_completion_tokens=16000,
    )

    content = response.choices[0].message.content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    analysis = json.loads(content)
    tokens_used = response.usage.total_tokens if response.usage else 0

    logger.info(
        "o3 analysis complete: %d tokens, %d recommendations, %d suggestion reviews",
        tokens_used,
        len(analysis.get("recommendations", [])),
        len(analysis.get("prompt_evaluation", {}).get("suggestion_reviews", [])),
    )
    return {"analysis": analysis, "tokens_used": tokens_used}


@task(retries=2, name="store-weekly-insights")
def store_weekly_insights(data: dict, o3_result: dict) -> int:
    """Store analysis in weekly_insights and update prompt_suggestions based on review."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    analysis = o3_result["analysis"]
    tokens = o3_result.get("tokens_used", 0)
    # o3: ~$2/1M input + $8/1M output (thinking tokens billed as output)
    cost = tokens * 0.010 / 1000

    today = date.today()
    week_start = today - timedelta(days=7)

    cur.execute(
        """
        INSERT INTO weekly_insights
            (week_start, week_end, executive_summary, deep_analysis,
             recommendations, prompt_draft, analytics_meta,
             previous_recommendations_review, tokens_used, estimated_cost)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (week_start) DO UPDATE SET
            week_end = EXCLUDED.week_end,
            executive_summary = EXCLUDED.executive_summary,
            deep_analysis = EXCLUDED.deep_analysis,
            recommendations = EXCLUDED.recommendations,
            prompt_draft = EXCLUDED.prompt_draft,
            analytics_meta = EXCLUDED.analytics_meta,
            previous_recommendations_review = EXCLUDED.previous_recommendations_review,
            tokens_used = EXCLUDED.tokens_used,
            estimated_cost = EXCLUDED.estimated_cost
        RETURNING id
        """,
        (
            week_start,
            today,
            analysis.get("executive_summary", ""),
            json.dumps({
                "delivery_health": analysis.get("delivery_health", {}),
                "translation_quality": analysis.get("translation_quality", {}),
            }),
            json.dumps(analysis.get("recommendations", [])),
            analysis.get("prompt_evaluation", {}).get("new_prompt_draft"),
            json.dumps(analysis.get("analytics_meta", {})),
            json.dumps(analysis.get("previous_recommendations_review", [])),
            tokens,
            cost,
        ),
    )
    insight_id = cur.fetchone()[0]

    # Update prompt_suggestions based on o3 review
    reviews = analysis.get("prompt_evaluation", {}).get("suggestion_reviews", [])
    for review in reviews:
        suggestion_id = review.get("suggestion_id")
        verdict = review.get("verdict", "defer")
        if suggestion_id and verdict in ("apply", "reject"):
            status = "applied" if verdict == "apply" else "rejected"
            cur.execute(
                "UPDATE prompt_suggestions SET status = %s WHERE id = %s AND status = 'pending'",
                (status, suggestion_id),
            )

    conn.commit()
    cur.close()
    conn.close()

    logger.info(
        "Stored weekly insight id=%d, reviewed %d suggestions, cost=$%.4f",
        insight_id, len(reviews), cost,
    )
    return insight_id


@task(retries=1, name="notify-weekly-report")
def notify_weekly_report(data: dict, o3_result: dict) -> int:
    """Send executive summary + key metrics + recommendations via Telegram."""
    logger = get_run_logger()

    analysis = o3_result["analysis"]
    msgs = data["messages"]
    total = msgs["total_messages"]
    delivered = msgs["delivered"]
    failed = msgs["failed"]
    mapped_total = msgs.get("mapped_total", 0)
    mapped_failed = msgs.get("mapped_failed", 0)
    fail_rate = round(mapped_failed / mapped_total * 100, 1) if mapped_total else 0
    avg_ms = msgs.get("avg_translation_ms")
    avg_str = f"{float(avg_ms):.0f}ms" if avg_ms else "—"

    lines = [
        "🧠 <b>Weekly Intelligence Report</b>",
        f"📅 {data['period_start']} → {data['period_end']}\n",
    ]

    # Executive summary
    summary = analysis.get("executive_summary", "")
    if summary:
        lines.append(f"<b>TL;DR:</b> {esc(summary)}\n")

    # Key metrics
    lines.append("<b>📊 Metrics:</b>")
    lines.append(f"  Messages: {total} (delivered: {delivered}, failed: {failed})")
    lines.append(f"  Failure rate: {fail_rate}% | Avg translation: {avg_str}")

    # Direct interactions
    di = data.get("direct_interactions", {})
    if di.get("total", 0) > 0:
        lines.append(f"  Direct: {di['translations']} translations, {di['analyses']} analyses ({di['failed']} failed)")

    # Quality trend
    tq = analysis.get("translation_quality", {})
    scores = tq.get("avg_scores", {})
    trend = tq.get("trend", "unknown")
    trend_emoji = {"improving": "📈", "stable": "➡️", "degrading": "📉"}.get(trend, "❓")
    if scores:
        lines.append(
            f"  Quality: {scores.get('quality', '—')} | "
            f"Accuracy: {scores.get('accuracy', '—')} | "
            f"Naturalness: {scores.get('naturalness', '—')} {trend_emoji}"
        )
    lines.append("")

    # Top recommendations
    recommendations = analysis.get("recommendations", [])
    if recommendations:
        lines.append(f"<b>🎯 Recommendations ({len(recommendations)}):</b>")
        for rec in recommendations[:3]:
            area = rec.get("area", "").upper()
            action = esc(rec.get("action", ""))
            lines.append(f"  {rec.get('priority', '•')}. [{area}] {action}")
        if len(recommendations) > 3:
            lines.append(f"  ... +{len(recommendations) - 3} more in DB")
        lines.append("")

    # Prompt status
    prompt_eval = analysis.get("prompt_evaluation", {})
    reviews = prompt_eval.get("suggestion_reviews", [])
    new_draft = prompt_eval.get("new_prompt_draft")
    if reviews or new_draft:
        applied = sum(1 for r in reviews if r.get("verdict") == "apply")
        rejected = sum(1 for r in reviews if r.get("verdict") == "reject")
        lines.append("<b>📝 Prompt:</b>")
        if reviews:
            lines.append(f"  Reviewed {len(reviews)} suggestions: ✅{applied} ❌{rejected}")
        if new_draft:
            lines.append("  ⚡ New prompt draft available in DB!")
        lines.append("")

    # Backlog count
    open_backlog = len(data.get("open_backlog", []))
    if open_backlog:
        lines.append(f"🔴 Open backlog issues: {open_backlog}")

    # Cost
    tokens = o3_result.get("tokens_used", 0)
    cost = tokens * 0.010 / 1000
    lines.append(f"\n💰 o3: {tokens:,} tokens (~${cost:.2f})")

    text = "\n".join(lines)
    sent = notify_telegram(text, timeout=15)

    logger.info("Sent weekly intelligence report to %d admins", sent)
    return sent


@flow(name="weekly-report", log_prints=True)
def weekly_report():
    """Weekly intelligence: collect data → load history → o3 analysis → store → notify."""
    data = collect_weekly_data()
    previous_insights = load_previous_insights()
    o3_result = analyze_with_o3(data, previous_insights)
    insight_id = store_weekly_insights(data, o3_result)
    notified = notify_weekly_report(data, o3_result)

    analysis = o3_result.get("analysis", {})
    return {
        "period": f"{data['period_start']} – {data['period_end']}",
        "total_messages": data["messages"]["total_messages"],
        "issues": len(data.get("issues", [])),
        "recommendations": len(analysis.get("recommendations", [])),
        "suggestions_reviewed": len(
            analysis.get("prompt_evaluation", {}).get("suggestion_reviews", [])
        ),
        "new_prompt_draft": bool(
            analysis.get("prompt_evaluation", {}).get("new_prompt_draft")
        ),
        "tokens_used": o3_result.get("tokens_used", 0),
        "insight_id": insight_id,
        "admins_notified": notified,
    }


if __name__ == "__main__":
    weekly_report()
