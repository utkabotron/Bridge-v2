"""Prefect flow: nightly problem detection via LLM analysis.

Collects 24h stats from message_events, sends aggregates to GPT-4o-mini
for analysis, stores detected issues, and notifies admins on critical issues.

Deploy:
  prefect deployment build flows/nightly_problems.py:nightly_problems \
    --name nightly-problems --cron "0 4 * * *" --apply
"""
from __future__ import annotations

import json
import os
from datetime import date

import httpx
import psycopg2
import psycopg2.extras
from openai import OpenAI
from prefect import flow, get_run_logger, task

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_TG_IDS = [int(x) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()]

ANALYSIS_MODEL = "gpt-4o-mini"


@task(retries=2, name="collect-stats")
def collect_stats() -> dict:
    """Collect 24h aggregates from message_events."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    stats = {}

    # Overall counts
    cur.execute("""
        SELECT
            count(*) AS total_messages,
            count(*) FILTER (WHERE delivery_status = 'delivered') AS delivered,
            count(*) FILTER (WHERE delivery_status = 'failed') AS failed,
            count(*) FILTER (WHERE delivery_status = 'pending') AS pending,
            avg(translation_ms) FILTER (WHERE translation_ms IS NOT NULL) AS avg_translation_ms,
            max(translation_ms) AS max_translation_ms,
            count(*) FILTER (WHERE translation_ms > 3000) AS slow_translations
        FROM message_events
        WHERE created_at >= now() - interval '24 hours'
    """)
    stats["overview"] = dict(cur.fetchone())

    # Failed deliveries by error type
    cur.execute("""
        SELECT
            error_message,
            count(*) AS count
        FROM message_events
        WHERE created_at >= now() - interval '24 hours'
          AND delivery_status = 'failed'
          AND error_message IS NOT NULL
        GROUP BY error_message
        ORDER BY count DESC
        LIMIT 20
    """)
    stats["errors_by_type"] = [dict(r) for r in cur.fetchall()]

    # Hourly volume
    cur.execute("""
        SELECT
            date_trunc('hour', created_at) AS hour,
            count(*) AS total,
            count(*) FILTER (WHERE delivery_status = 'failed') AS failed
        FROM message_events
        WHERE created_at >= now() - interval '24 hours'
        GROUP BY 1
        ORDER BY 1
    """)
    rows = cur.fetchall()
    stats["hourly_volume"] = [
        {"hour": str(r["hour"]), "total": r["total"], "failed": r["failed"]}
        for r in rows
    ]

    # Message types distribution
    cur.execute("""
        SELECT message_type, count(*) AS count
        FROM message_events
        WHERE created_at >= now() - interval '24 hours'
        GROUP BY message_type
        ORDER BY count DESC
    """)
    stats["message_types"] = [dict(r) for r in cur.fetchall()]

    cur.close()
    conn.close()

    logger.info(
        "Collected stats: %d total, %d failed, %d slow",
        stats["overview"]["total_messages"],
        stats["overview"]["failed"],
        stats["overview"]["slow_translations"],
    )
    return stats


@task(retries=1, name="analyze-with-llm")
def analyze_with_llm(stats: dict) -> dict:
    """Send aggregated stats to LLM for problem analysis."""
    logger = get_run_logger()

    if stats["overview"]["total_messages"] == 0:
        logger.info("No messages in last 24h, skipping LLM analysis")
        return {"issues": [], "tokens_used": 0}

    client = OpenAI(api_key=OPENAI_API_KEY)

    system_prompt = """You are a DevOps/SRE analyst for a WhatsApp→Telegram message bridge.
Analyze the provided 24h statistics and identify issues that need attention.

Return a JSON array of issues. Each issue must have:
- severity: "critical" | "warning" | "info"
- category: one of "delivery_failure", "performance", "volume_anomaly", "error_pattern", "other"
- title: short title (max 100 chars)
- description: detailed description of the problem
- suggested_fix: actionable suggestion to fix/investigate

Rules:
- critical: >10% failure rate, complete outage, data loss risk
- warning: 3-10% failure rate, degraded performance (avg translation >2s), unusual patterns
- info: minor anomalies, optimization opportunities
- If no issues found, return an empty array []
- Return ONLY the JSON array, no markdown fences or extra text."""

    user_prompt = f"24h stats for {date.today().isoformat()}:\n\n{json.dumps(stats, indent=2, default=str)}"

    response = client.chat.completions.create(
        model=ANALYSIS_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=2000,
    )

    content = response.choices[0].message.content.strip()
    # Handle possible markdown fences
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    issues = json.loads(content)
    tokens_used = response.usage.total_tokens if response.usage else 0

    logger.info("LLM found %d issues (tokens: %d)", len(issues), tokens_used)
    return {"issues": issues, "tokens_used": tokens_used}


@task(retries=2, name="store-results")
def store_results(stats: dict, analysis: dict) -> int:
    """Store analysis run and detected issues in DB."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    tokens = analysis.get("tokens_used", 0)
    # gpt-4o-mini: ~$0.15/1M input + $0.60/1M output, rough estimate
    cost = tokens * 0.0003 / 1000

    cur.execute(
        """
        INSERT INTO nightly_analysis_runs (run_date, flow_type, summary, tokens_used, estimated_cost)
        VALUES (%s, 'problems', %s, %s, %s)
        ON CONFLICT (run_date, flow_type) DO UPDATE
            SET summary = EXCLUDED.summary,
                tokens_used = EXCLUDED.tokens_used,
                estimated_cost = EXCLUDED.estimated_cost
        RETURNING id
        """,
        (date.today(), json.dumps(stats["overview"], default=str), tokens, cost),
    )
    run_id = cur.fetchone()[0]

    issues = analysis.get("issues", [])
    for issue in issues:
        cur.execute(
            """
            INSERT INTO detected_issues (run_id, severity, category, title, description, suggested_fix)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                issue["severity"],
                issue["category"],
                issue["title"],
                issue.get("description", ""),
                issue.get("suggested_fix", ""),
            ),
        )

    conn.commit()
    cur.close()
    conn.close()

    logger.info("Stored run_id=%d with %d issues", run_id, len(issues))
    return run_id


@task(retries=1, name="notify-admin")
def notify_admin(analysis: dict) -> int:
    """Send Telegram notification if critical issues found."""
    logger = get_run_logger()

    critical = [i for i in analysis.get("issues", []) if i["severity"] == "critical"]
    if not critical:
        logger.info("No critical issues, skipping notification")
        return 0

    if not TELEGRAM_BOT_TOKEN or not ADMIN_TG_IDS:
        logger.warning("Telegram not configured, cannot notify about %d critical issues", len(critical))
        return 0

    def _esc(s: str) -> str:
        """Escape HTML special chars in LLM-generated text."""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = [f"🚨 <b>Nightly Analysis: {len(critical)} critical issue(s)</b>\n"]
    for issue in critical:
        lines.append(f"<b>{_esc(issue['title'])}</b>")
        lines.append(f"Category: {_esc(issue['category'])}")
        lines.append(f"{_esc(issue['description'])}")
        lines.append(f"Fix: <i>{_esc(issue.get('suggested_fix', 'N/A'))}</i>\n")

    text = "\n".join(lines)
    sent = 0

    for chat_id in ADMIN_TG_IDS:
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            resp.raise_for_status()
            sent += 1
        except Exception as e:
            logger.error("Failed to notify admin %d: %s", chat_id, e)

    logger.info("Notified %d/%d admins about %d critical issues", sent, len(ADMIN_TG_IDS), len(critical))
    return sent


@flow(name="nightly-problems", log_prints=True)
def nightly_problems():
    """Nightly problem detection: collect stats → LLM analysis → store → notify."""
    stats = collect_stats()
    analysis = analyze_with_llm(stats)
    run_id = store_results(stats, analysis)
    notified = notify_admin(analysis)
    return {
        "run_id": run_id,
        "issues_found": len(analysis.get("issues", [])),
        "critical": len([i for i in analysis.get("issues", []) if i["severity"] == "critical"]),
        "admins_notified": notified,
    }


if __name__ == "__main__":
    nightly_problems()
