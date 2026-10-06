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
          AND NOT te.shadow AND te.source = 'bridge'
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

    # --- Quality breakdowns (4 weeks): the overall average hid the one chat that was
    # a third bad, and mixed DM/fallback rows into the bridge numbers ---
    def _slice(sql: str, params: tuple) -> list[dict]:
        cur.execute(sql, params)
        return [
            {k: (float(v) if hasattr(v, "quantize") else v) for k, v in dict(r).items()}
            for r in cur.fetchall()
        ]

    slice_where = """
        FROM translation_evaluations te
        JOIN nightly_analysis_runs nar ON nar.id = te.run_id
        WHERE nar.run_date >= %s AND nar.run_date < %s
          AND NOT te.shadow AND te.quality_score IS NOT NULL
    """
    stats = """
            count(*) AS n,
            round(avg(te.quality_score)::numeric, 2) AS quality,
            round(100.0 * avg((te.quality_score <= 3)::int), 1) AS bad_pct
    """
    data["quality_by_source_4w"] = _slice(
        f"SELECT te.source AS key, {stats} {slice_where} GROUP BY te.source ORDER BY n DESC",
        (four_weeks_ago, today),
    )
    data["quality_by_pair_4w"] = _slice(
        f"""SELECT te.chat_pair_id AS pair, cp.wa_chat_name AS name, {stats}
            FROM translation_evaluations te
            JOIN nightly_analysis_runs nar ON nar.id = te.run_id
            LEFT JOIN chat_pairs cp ON cp.id = te.chat_pair_id
            WHERE nar.run_date >= %s AND nar.run_date < %s
              AND NOT te.shadow AND te.quality_score IS NOT NULL AND te.source = 'bridge'
            GROUP BY te.chat_pair_id, cp.wa_chat_name
            HAVING count(*) >= 5
            ORDER BY bad_pct DESC, n DESC
            LIMIT 15""",
        (four_weeks_ago, today),
    )
    data["quality_by_language_4w"] = _slice(
        f"SELECT te.target_language AS key, {stats} {slice_where} AND te.source = 'bridge' "
        "GROUP BY te.target_language ORDER BY n DESC",
        (four_weeks_ago, today),
    )
    data["quality_by_type_4w"] = _slice(
        f"SELECT te.message_type AS key, {stats} {slice_where} AND te.source = 'bridge' "
        "GROUP BY te.message_type ORDER BY n DESC",
        (four_weeks_ago, today),
    )
    data["quality_by_prompt_version_4w"] = _slice(
        f"SELECT te.prompt_version AS key, {stats} {slice_where} AND te.source = 'bridge' "
        "GROUP BY te.prompt_version ORDER BY n DESC",
        (four_weeks_ago, today),
    )

    # Issue types this week, summed over the nightly quality runs
    cur.execute("""
        SELECT summary FROM nightly_analysis_runs
        WHERE flow_type = 'translation_quality' AND run_date >= %s AND run_date < %s
    """, (week_ago, today))
    issue_totals: dict[str, int] = {}
    for r in cur.fetchall():
        summary = r["summary"] if isinstance(r["summary"], dict) else json.loads(r["summary"] or "{}")
        for k, v in (summary.get("issue_counts") or {}).items():
            issue_totals[k] = issue_totals.get(k, 0) + int(v)
    data["issue_counts_week"] = dict(sorted(issue_totals.items(), key=lambda kv: -kv[1]))

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
- 4 weeks of quality score trends (bridge chats only)
- Quality broken down by chat pair (with names), target language, message type, prompt
  version and source (bridge vs bot DM vs unpaired fallback), plus this week's issue types.
  Name the worst pairs by id; never generalise from the overall average alone.
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
    "worst_chats": ["pair #<id> <name>: <what is going wrong, from the data>"],
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


@flow(name="weekly-report", log_prints=True)
def weekly_report():
    """Weekly intelligence: collect data → load history → o3 analysis → store."""
    data = collect_weekly_data()
    previous_insights = load_previous_insights()
    o3_result = analyze_with_o3(data, previous_insights)
    insight_id = store_weekly_insights(data, o3_result)
    # No Telegram message of its own: Monday's digest carries the summary and top three.

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
    }


if __name__ == "__main__":
    weekly_report()
