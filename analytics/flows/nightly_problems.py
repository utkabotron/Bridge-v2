"""Prefect flow: nightly problem detection via LLM analysis.

Collects 24h stats from message_events, sends aggregates to gpt-4.1-mini
for analysis, stores detected issues, and notifies admins on critical issues.

Deploy:
  prefect deployment build flows/nightly_problems.py:nightly_problems \
    --name nightly-problems --cron "0 4 * * *" --apply
"""
from __future__ import annotations

import json
import os
from datetime import date

import psycopg2
import psycopg2.extras
from openai import OpenAI
from prefect import flow, get_run_logger, task

from .shared import esc, notify_telegram

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

ANALYSIS_MODEL = "gpt-4.1-mini"


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
            count(*) FILTER (WHERE delivery_status = 'skipped') AS skipped,
            avg(translation_ms) FILTER (WHERE translation_ms IS NOT NULL) AS avg_translation_ms,
            max(translation_ms) AS max_translation_ms,
            count(*) FILTER (WHERE translation_ms > 3000) AS slow_translations,
            count(*) FILTER (WHERE chat_pair_id IS NOT NULL) AS mapped_total,
            count(*) FILTER (WHERE chat_pair_id IS NOT NULL AND delivery_status = 'delivered') AS mapped_delivered,
            count(*) FILTER (WHERE chat_pair_id IS NOT NULL AND delivery_status = 'failed') AS mapped_failed
        FROM message_events
        WHERE created_at >= current_date - interval '1 day'
          AND created_at < current_date
    """)
    stats["overview"] = dict(cur.fetchone())

    # Failed deliveries by error type
    cur.execute("""
        SELECT
            error_message,
            count(*) AS count
        FROM message_events
        WHERE created_at >= current_date - interval '1 day'
          AND created_at < current_date
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
        WHERE created_at >= current_date - interval '1 day'
          AND created_at < current_date
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
        WHERE created_at >= current_date - interval '1 day'
          AND created_at < current_date
        GROUP BY message_type
        ORDER BY count DESC
    """)
    stats["message_types"] = [dict(r) for r in cur.fetchall()]

    # Direct interactions (bot private chat)
    cur.execute("""
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE interaction_type = 'translation') AS translations,
            count(*) FILTER (WHERE interaction_type = 'media_analysis') AS analyses,
            count(*) FILTER (WHERE status = 'failed') AS failed
        FROM direct_interactions
        WHERE created_at >= current_date - interval '1 day'
          AND created_at < current_date
    """)
    stats["direct_interactions"] = dict(cur.fetchone())

    cur.close()
    conn.close()

    logger.info(
        "Collected stats: %d total, %d failed, %d slow, %d direct",
        stats["overview"]["total_messages"],
        stats["overview"]["failed"],
        stats["overview"]["slow_translations"],
        stats["direct_interactions"]["total"],
    )
    return stats


@task(retries=1, name="fetch-open-issues")
def fetch_open_issues() -> list[dict]:
    """Fetch open issues from issues_backlog (last 7 days) to avoid LLM duplicates."""
    logger = get_run_logger()
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT title, description
            FROM issues_backlog
            WHERE status = 'open'
              AND created_at >= now() - interval '7 days'
            ORDER BY created_at DESC
        """)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        logger.info("Fetched %d open issues for dedup context", len(rows))
        return rows
    except Exception as exc:
        logger.warning("Could not fetch open issues for dedup: %s", exc)
        return []


@task(retries=1, name="analyze-with-llm")
def analyze_with_llm(stats: dict, open_issues: list[dict] | None = None) -> dict:
    """Send aggregated stats to LLM for problem analysis."""
    logger = get_run_logger()

    if stats["overview"]["total_messages"] == 0:
        logger.info("No messages in last 24h, skipping LLM analysis")
        return {"issues": [], "tokens_used": 0}

    client = OpenAI(api_key=OPENAI_API_KEY)

    existing_block = ""
    if open_issues:
        items = "\n".join(f"- [{i.get('category', 'other')}] {i['title']}: {i.get('description', '')[:120]}" for i in open_issues)
        existing_block = f"\n\nAlready tracked open issues (do NOT generate duplicate issues for these — skip any issue substantively identical to one below, even if rephrased):\n{items}"

    system_prompt = f"""You are a DevOps/SRE analyst for a WhatsApp→Telegram message bridge.
Analyze the provided 24h statistics and identify issues that need attention.

Stats include mapped_total/mapped_delivered/mapped_failed — use these for failure rate.
Focus only on mapped message failures (chats with a configured pair).

direct_interactions contains stats about direct translations and media analysis from the bot's private chat (not pipeline messages). Report issues if failure rate is high.

Return a JSON array of issues. Each issue must have:
- severity: "critical" | "warning" | "info"
- category: one of "delivery_failure", "performance", "volume_anomaly", "error_pattern", "other"
- title: short title (max 100 chars)
- description: detailed description of the problem
- suggested_fix: actionable suggestion to fix/investigate

Rules:
- critical: >10% mapped failure rate, complete outage, data loss risk
- warning: 3-10% mapped failure rate, degraded performance (avg translation >2s), unusual patterns
- info: minor anomalies, optimization opportunities
- If no issues found, return an empty array []
- Return ONLY the JSON array, no markdown fences or extra text.{existing_block}"""

    user_prompt = f"24h stats for {date.today().isoformat()}:\n\n{json.dumps(stats, indent=2, default=str)}"

    response = client.chat.completions.create(
        model=ANALYSIS_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4000,
        temperature=0,
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
    # gpt-4.1-mini: ~$0.40/1M input + $1.60/1M output, rough estimate
    cost = tokens * 0.001 / 1000
    issues = analysis.get("issues", [])

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
        (
            date.today(),
            json.dumps(
                {
                    "stats": stats["overview"],
                    "issues_total": len(issues),
                    "issues_critical": len([i for i in issues if i.get("severity") == "critical"]),
                    "issues_warning": len([i for i in issues if i.get("severity") == "warning"]),
                    "issues_info": len([i for i in issues if i.get("severity") == "info"]),
                    "mapped_failure_rate": round(
                        stats["overview"]["mapped_failed"] / stats["overview"]["mapped_total"] * 100, 2
                    ) if stats["overview"].get("mapped_total") else 0,
                },
                default=str,
            ),
            tokens,
            cost,
        ),
    )
    run_id = cur.fetchone()[0]

    # Remove old issues before inserting fresh ones (UPSERT only updates the run row)
    cur.execute("DELETE FROM detected_issues WHERE run_id = %s", (run_id,))

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

    # Copy critical issues to persistent backlog (skip duplicates by category+title or fuzzy title match)
    for issue in issues:
        if issue["severity"] == "critical":
            # Extract first 10 words for fuzzy matching
            first_10_words = " ".join(issue["title"].split()[:10])
            cur.execute(
                """SELECT 1 FROM issues_backlog
                   WHERE status = 'open'
                     AND lower(category) = lower(%s)
                     AND (lower(title) = lower(%s)
                          OR lower(title) LIKE '%%' || lower(%s) || '%%')
                   LIMIT 1""",
                (issue["category"], issue["title"], first_10_words),
            )
            if cur.fetchone():
                logger.info("Skipping duplicate backlog issue: %s", issue["title"])
                continue
            cur.execute(
                """
                INSERT INTO issues_backlog
                    (source_run_date, severity, category, title, description, suggested_fix)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    date.today(),
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

    critical_count = len([i for i in issues if i["severity"] == "critical"])
    logger.info("Stored run_id=%d with %d issues (%d critical → backlog)", run_id, len(issues), critical_count)
    return run_id


@task(retries=1, name="notify-admin")
def notify_admin(stats: dict, analysis: dict) -> int:
    """Send full nightly problems report to admins."""
    logger = get_run_logger()

    issues = analysis.get("issues", [])

    today = date.today().isoformat()
    lines = [f"🔍 <b>Nightly Problems Report</b> ({today})\n"]

    # Stats overview
    overview = stats.get("overview", {})
    if overview:
        total = overview.get("total_messages", 0)
        delivered = overview.get("delivered", 0)
        failed = overview.get("failed", 0)
        avg_ms = overview.get("avg_translation_ms")
        avg_str = f"{avg_ms:.0f}ms" if avg_ms else "—"
        slow = overview.get("slow_translations", 0)
        mapped_total = overview.get("mapped_total", 0)
        mapped_failed = overview.get("mapped_failed", 0)
        fail_rate = round(mapped_failed / mapped_total * 100, 1) if mapped_total else 0
        lines.append("<b>Stats (24h):</b>")
        lines.append(f"  Total: {total}  Delivered: {delivered}  Failed: {failed}")
        lines.append(f"  Mapped: {mapped_total} (failed: {mapped_failed}, rate: {fail_rate}%)")
        lines.append(f"  Avg translation: {avg_str}  Slow (>3s): {slow}")
        di = stats.get("direct_interactions", {})
        if di.get("total", 0) > 0:
            lines.append(f"  Direct: {di['translations']} translations, {di['analyses']} analyses ({di['failed']} failed)")
        lines.append("")

    # Issues by severity
    critical = [i for i in issues if i["severity"] == "critical"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    infos = [i for i in issues if i["severity"] == "info"]

    if not issues:
        lines.append("✅ No issues detected")
    else:
        lines.append(f"<b>Issues ({len(issues)}):</b> 🚨{len(critical)} ⚠️{len(warnings)} ℹ️{len(infos)}\n")

        for severity_label, severity_issues, emoji in [
            ("Critical", critical, "🚨"),
            ("Warning", warnings, "⚠️"),
            ("Info", infos, "ℹ️"),
        ]:
            for issue in severity_issues:
                lines.append(f"{emoji} <b>{esc(issue['title'])}</b>")
                lines.append(f"  {esc(issue.get('description', ''))}")
                if issue.get("suggested_fix"):
                    lines.append(f"  Fix: <i>{esc(issue['suggested_fix'])}</i>")
                lines.append("")

    text = "\n".join(lines)
    sent = notify_telegram(text)

    logger.info("Sent nightly report to %d admins (%d issues)", sent, len(issues))
    return sent


@flow(name="nightly-problems", log_prints=True)
def nightly_problems():
    """Nightly problem detection: collect stats → LLM analysis → store → notify."""
    stats = collect_stats()
    open_issues = fetch_open_issues()
    analysis = analyze_with_llm(stats, open_issues)
    run_id = store_results(stats, analysis)
    notified = notify_admin(stats, analysis)
    return {
        "run_id": run_id,
        "issues_found": len(analysis.get("issues", [])),
        "critical": len([i for i in analysis.get("issues", []) if i["severity"] == "critical"]),
        "admins_notified": notified,
    }


if __name__ == "__main__":
    nightly_problems()
