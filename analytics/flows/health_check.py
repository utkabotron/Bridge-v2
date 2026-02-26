"""Prefect flow: WhatsApp connection health check (every 15 min).

Polls wa-service /health and alerts if clients drop.

Deploy:
  prefect deployment build flows/health_check.py:wa_health_check \
    --name wa-health-15min --cron "*/15 * * * *" --apply
"""
from __future__ import annotations

import os

import httpx
from prefect import flow, get_run_logger, task

WA_SERVICE_URL = os.getenv("WA_SERVICE_URL", "http://wa-service:3000")
EXPECTED_CLIENTS = int(os.getenv("EXPECTED_WA_CLIENTS", "1"))


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


@flow(name="wa-health-check", log_prints=True)
def wa_health_check():
    """Check wa-service health and log status to Prefect UI."""
    health = check_wa_health()
    ok = evaluate_health(health)
    return {"healthy": ok, "clients": health.get("activeClients", 0)}


if __name__ == "__main__":
    wa_health_check()
