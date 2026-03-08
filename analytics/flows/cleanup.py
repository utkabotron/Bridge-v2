"""Prefect flow: daily cleanup of old data.

- Delete message_events older than 90 days
- Remove orphaned onboarding_sessions (done > 7 days ago)

Deploy:
  prefect deployment build flows/cleanup.py:daily_cleanup \
    --name daily-cleanup --cron "0 3 * * *" --apply
"""
from __future__ import annotations

import os

import psycopg2
from prefect import flow, get_run_logger, task

DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
RETAIN_DAYS = int(os.getenv("RETAIN_MESSAGE_DAYS", "90"))

# (label, SQL query, params)
_CLEANUP_TASKS = [
    (
        "message_events",
        "delete from public.message_events where created_at < now() - interval '%s days'",
        (RETAIN_DAYS,),
    ),
    (
        "onboarding_sessions",
        "delete from public.onboarding_sessions where state = 'done' and done_at < now() - interval '7 days'",
        None,
    ),
    (
        "nightly_analysis_runs",
        "delete from public.nightly_analysis_runs where created_at < now() - interval '%s days'",
        (RETAIN_DAYS,),
    ),
    (
        "direct_interactions",
        "delete from public.direct_interactions where created_at < now() - interval '%s days'",
        (RETAIN_DAYS,),
    ),
]


@task(retries=2, name="cleanup-table")
def cleanup_table(label: str, query: str, params: tuple | None) -> int:
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    if params:
        cur.execute(query, params)
    else:
        cur.execute(query)
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Deleted %d old %s", deleted, label)
    return deleted


@flow(name="daily-cleanup", log_prints=True)
def daily_cleanup():
    """Daily cleanup: old messages + stale onboarding sessions + old analysis + old direct interactions."""
    results = {}
    for label, query, params in _CLEANUP_TASKS:
        results[f"{label}_deleted"] = cleanup_table(label, query, params)
    return results


if __name__ == "__main__":
    daily_cleanup()
