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


@task(retries=2, name="cleanup-old-messages")
def cleanup_old_messages() -> int:
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        "delete from public.message_events where created_at < now() - interval '%s days'",
        (RETAIN_DAYS,),
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Deleted %d old message_events", deleted)
    return deleted


@task(retries=2, name="cleanup-onboarding-sessions")
def cleanup_onboarding_sessions() -> int:
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        """
        delete from public.onboarding_sessions
        where state = 'done' and done_at < now() - interval '7 days'
        """
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Deleted %d stale onboarding sessions", deleted)
    return deleted


@task(retries=2, name="cleanup-old-analysis")
def cleanup_old_analysis() -> int:
    """Delete nightly analysis data older than 90 days (cascades to issues/evaluations/suggestions)."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        "delete from public.nightly_analysis_runs where created_at < now() - interval '%s days'",
        (RETAIN_DAYS,),
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Deleted %d old analysis runs (cascaded to issues/evaluations/suggestions)", deleted)
    return deleted


@task(retries=2, name="cleanup-old-direct-interactions")
def cleanup_old_direct_interactions() -> int:
    """Delete direct_interactions older than 90 days."""
    logger = get_run_logger()
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()
    cur.execute(
        "delete from public.direct_interactions where created_at < now() - interval '%s days'",
        (RETAIN_DAYS,),
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    logger.info("Deleted %d old direct_interactions", deleted)
    return deleted


@flow(name="daily-cleanup", log_prints=True)
def daily_cleanup():
    """Daily cleanup: old messages + stale onboarding sessions + old analysis + old direct interactions."""
    msgs = cleanup_old_messages()
    sessions = cleanup_onboarding_sessions()
    analysis = cleanup_old_analysis()
    direct = cleanup_old_direct_interactions()
    return {"messages_deleted": msgs, "sessions_deleted": sessions, "analysis_deleted": analysis, "direct_deleted": direct}


if __name__ == "__main__":
    daily_cleanup()
