"""Prefect flow: WhatsApp connection health check (every 15 min).

Polls wa-service /health and alerts if clients drop.

Deploy:
  prefect deployment build flows/health_check.py:wa_health_check \
    --name wa-health-15min --cron "*/15 * * * *" --apply
"""
from __future__ import annotations

import os

import httpx
import psycopg2
import psycopg2.extras
from prefect import flow, get_run_logger, task

from .shared import notify_telegram

WA_SERVICE_URL = os.getenv("WA_SERVICE_URL", "http://wa-service:3000")
EXPECTED_CLIENTS = int(os.getenv("EXPECTED_WA_CLIENTS", "1"))
DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
FAILURE_RATE_THRESHOLD = float(os.getenv("FAILURE_RATE_THRESHOLD", "0.05"))  # 5%
FAILURE_RATE_MIN_MSGS = int(os.getenv("FAILURE_RATE_MIN_MSGS", "5"))  # min mapped msgs to trigger


@task(retries=2, retry_delay_seconds=10, name="check-wa-health")
def check_wa_health() -> dict:
    logger = get_run_logger()
    try:
        r = httpx.get(f"{WA_SERVICE_URL}/health", timeout=10)
        r.raise_for_status()
        data = r.json()
        logger.info("WA health: %s", data)
        return data
    except Exception as exc:
        logger.error("WA health check failed: %s", exc)
        raise


@task(name="evaluate-health")
def evaluate_health(health: dict) -> bool:
    logger = get_run_logger()

    active = health.get("activeClients", 0)
    redis_ok = health.get("redis") == "connected"

    if not redis_ok:
        logger.error("ALERT: Redis disconnected in wa-service!")

    if active < EXPECTED_CLIENTS:
        logger.warning(
            "ALERT: Expected %d WA clients, got %d",
            EXPECTED_CLIENTS,
            active,
        )
        return False

    logger.info("Health OK: %d clients active, redis=%s", active, health.get("redis"))
    return True


@task(retries=1, name="check-processor-failures")
def check_processor_failures() -> dict:
    """Query DB for mapped failure rate in the last 15 minutes and alert if >5%."""
    logger = get_run_logger()
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT
                count(*) FILTER (WHERE chat_pair_id IS NOT NULL) AS mapped_total,
                count(*) FILTER (WHERE chat_pair_id IS NOT NULL AND delivery_status = 'failed') AS mapped_failed
            FROM message_events
            WHERE created_at >= now() - interval '15 minutes'
        """)
        row = dict(cur.fetchone())
        cur.close()
        conn.close()
    except Exception as exc:
        logger.error("DB query for failure rate failed: %s", exc)
        return {"mapped_total": 0, "mapped_failed": 0, "alerted": False}

    mapped_total = row["mapped_total"] or 0
    mapped_failed = row["mapped_failed"] or 0
    alerted = False

    if mapped_total >= FAILURE_RATE_MIN_MSGS:
        rate = mapped_failed / mapped_total
        if rate > FAILURE_RATE_THRESHOLD:
            rate_pct = round(rate * 100, 1)
            msg = (
                f"🚨 <b>High failure rate alert</b>\n\n"
                f"Mapped failure rate: <b>{rate_pct}%</b> in last 15 min "
                f"({mapped_failed}/{mapped_total} messages)\n"
                f"Threshold: {int(FAILURE_RATE_THRESHOLD * 100)}%\n\n"
                f"Check logs: <code>docker compose logs processor</code>"
            )
            logger.error("ALERT: mapped failure rate %s%% (%d/%d) exceeds threshold", rate_pct, mapped_failed, mapped_total)
            notify_telegram(msg)
            alerted = True
        else:
            logger.info("Failure rate OK: %.1f%% (%d/%d mapped)", rate * 100, mapped_failed, mapped_total)
    else:
        logger.info("Not enough mapped messages in window (%d), skipping rate check", mapped_total)

    return {"mapped_total": mapped_total, "mapped_failed": mapped_failed, "alerted": alerted}


@flow(name="wa-health-check", log_prints=True)
def wa_health_check():
    """Check wa-service health and log status to Prefect UI."""
    health = check_wa_health()
    ok = evaluate_health(health)
    failures = check_processor_failures()
    return {"healthy": ok, "clients": health.get("activeClients", 0), "failure_alert": failures["alerted"]}


if __name__ == "__main__":
    wa_health_check()
