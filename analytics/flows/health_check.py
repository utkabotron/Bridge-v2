"""Prefect flow: bridge health check (every 15 min).

Answers the question every previous check dodged: are messages actually flowing?

The old version polled wa-service /health, compared the client count against an env var
that was never set (so it defaulted to 1 and a drop from four clients to one looked fine),
and on a mismatch wrote a warning into a Prefect log nobody reads — `notify_telegram` was
imported but only ever called for the failure-rate branch. Nothing watched the queues, the
dead-letter queue, the disk, or the silence itself.

Deploy:
  registered by serve_flows.py with cron */15 * * * *
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone

import httpx
import psycopg2
import psycopg2.extras
import redis
from prefect import flow, get_run_logger, task

from .shared import notify_telegram

WA_SERVICE_URL = os.getenv("WA_SERVICE_URL", "http://wa-service:3000")
PROCESSOR_URL = os.getenv("PROCESSOR_URL", "http://processor:8000")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
DB_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")

FAILURE_RATE_THRESHOLD = float(os.getenv("FAILURE_RATE_THRESHOLD", "0.05"))
FAILURE_RATE_MIN_MSGS = int(os.getenv("FAILURE_RATE_MIN_MSGS", "5"))

# Quiet hours are normal at night, so the dead-man switch only applies during the day.
SILENCE_HOURS = float(os.getenv("SILENCE_ALERT_HOURS", "3"))
ACTIVE_HOURS_START = int(os.getenv("ACTIVE_HOURS_START", "7"))
ACTIVE_HOURS_END = int(os.getenv("ACTIVE_HOURS_END", "23"))

QUEUE_DEPTH_THRESHOLD = int(os.getenv("QUEUE_DEPTH_THRESHOLD", "50"))
DISK_PCT_THRESHOLD = int(os.getenv("DISK_PCT_THRESHOLD", "85"))
SWAP_PCT_THRESHOLD = int(os.getenv("SWAP_PCT_THRESHOLD", "75"))

# Alert state lives in Redis so a restarted analytics container does not re-alert, and so
# "db_write_failed grew since last run" survives between flow runs.
_STATE_KEY = "analytics:health:state"
_ALERT_COOLDOWN = int(os.getenv("HEALTH_ALERT_COOLDOWN", "3600"))


def _redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                       decode_responses=True, socket_timeout=5)


def _alert_once(key: str, message: str) -> bool:
    """Send an alert unless the same key fired recently. Returns True if sent."""
    logger = get_run_logger()
    try:
        r = _redis()
        if not r.set(f"analytics:health:alert:{key}", "1", ex=_ALERT_COOLDOWN, nx=True):
            logger.info("Alert %s suppressed (cooldown)", key)
            return False
    except Exception as exc:
        logger.warning("Alert de-duplication unavailable (%s) — sending anyway", exc)

    notify_telegram(message)
    return True


@task(retries=2, retry_delay_seconds=10, name="check-wa-health")
def check_wa_health() -> dict:
    logger = get_run_logger()
    try:
        r = httpx.get(f"{WA_SERVICE_URL}/health", timeout=10)
        r.raise_for_status()
        data = r.json()
        logger.info("WA health: ready=%s active=%s redis=%s",
                    data.get("readyClients"), data.get("activeClients"), data.get("redis"))
        return data
    except Exception as exc:
        logger.error("WA health check failed: %s", exc)
        raise


@task(name="expected-clients")
def expected_clients() -> int:
    """How many WhatsApp clients should be up, according to the database."""
    logger = get_run_logger()
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute("select count(*) from public.users where wa_connected = true and is_active = true")
        n = cur.fetchone()[0]
        cur.close()
        conn.close()
        return int(n)
    except Exception as exc:
        logger.error("Could not read expected client count: %s", exc)
        return 0


@task(name="evaluate-clients")
def evaluate_clients(health: dict, expected: int) -> dict:
    """Alert when a user's WhatsApp client is gone, or wa-service lost Redis."""
    logger = get_run_logger()
    alerts = []

    if health.get("redis") != "connected":
        logger.error("wa-service lost Redis")
        if _alert_once("wa_redis", "🚨 <b>wa-service lost its Redis connection</b>\n\n"
                                   "Incoming WhatsApp messages are not being queued."):
            alerts.append("wa_redis")

    # readyClients, not activeClients: a client stuck initializing counts as active while
    # delivering nothing, which is exactly how a silent outage used to look healthy.
    ready = health.get("readyClients", health.get("activeClients", 0))

    if expected and ready < expected:
        missing = expected - ready
        detail = "\n".join(
            f"• {c.get('userId')}: {'ready' if c.get('isReady') else 'NOT ready'}"
            for c in health.get("clients", [])
        ) or "• (no clients at all)"
        logger.error("Expected %d WA clients, %d ready", expected, ready)
        if _alert_once("wa_clients_missing",
                       f"🚨 <b>{missing} WhatsApp client(s) offline</b>\n\n"
                       f"Expected {expected} (users marked connected), {ready} ready.\n\n{detail}\n\n"
                       "Their chats are not being bridged."):
            alerts.append("wa_clients_missing")

    return {"ready": ready, "expected": expected, "alerts": alerts}


@task(name="check-silence")
def check_silence(health: dict) -> dict:
    """Dead-man switch: clients look connected but nothing has arrived in hours.

    This is the shape of the failure that took the bridge down for 15 days — WhatsApp
    ships a build, the library's Store breaks, `message` stops firing, and getState()
    keeps answering CONNECTED.
    """
    logger = get_run_logger()
    now = datetime.now(timezone.utc)
    local_hour = int(os.getenv("_FORCE_HOUR", now.astimezone().hour))

    if not (ACTIVE_HOURS_START <= local_hour < ACTIVE_HOURS_END):
        logger.info("Outside active hours (%d) — skipping silence check", local_hour)
        return {"checked": False}

    last_ms = health.get("lastMessageAt")
    if not last_ms:
        logger.info("No message seen since wa-service started — nothing to compare yet")
        return {"checked": False}

    last = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc)
    quiet_for = now - last

    if quiet_for > timedelta(hours=SILENCE_HOURS):
        hours = round(quiet_for.total_seconds() / 3600, 1)
        logger.error("No WhatsApp message in %s hours", hours)
        _alert_once("silence",
                    f"🔇 <b>No WhatsApp messages for {hours}h</b>\n\n"
                    f"Clients report themselves connected, so this is most likely the "
                    f"library losing WhatsApp's Store rather than an idle day.\n"
                    f"Check: <code>docker compose logs wa-service --tail 100</code>")
        return {"checked": True, "quiet_hours": hours, "alerted": True}

    return {"checked": True, "quiet_hours": round(quiet_for.total_seconds() / 3600, 1), "alerted": False}


@task(name="check-queues")
def check_queues() -> dict:
    """Queue depth and dead letters — neither was watched by anything before."""
    logger = get_run_logger()
    try:
        r = _redis()
        depths = {
            "messages:in": r.llen("messages:in"),
            "messages:processing": r.llen("messages:processing"),
            "messages:dlq": r.llen("messages:dlq"),
            "messages:dlq:dead": r.llen("messages:dlq:dead"),
        }
    except Exception as exc:
        logger.error("Could not read queue depths: %s", exc)
        return {"error": str(exc)}

    logger.info("Queues: %s", depths)

    if depths["messages:in"] > QUEUE_DEPTH_THRESHOLD:
        _alert_once("queue_backlog",
                    f"🐌 <b>Message queue is backing up</b>\n\n"
                    f"Waiting: <b>{depths['messages:in']}</b>\n"
                    f"The processor is not keeping up, or it is stuck on one message.")

    if depths["messages:dlq:dead"] > 0:
        _alert_once("dlq_dead",
                    f"☠️ <b>{depths['messages:dlq:dead']} message(s) gave up retrying</b>\n\n"
                    "They are parked in <code>messages:dlq:dead</code> and will not be "
                    "delivered without intervention.")

    return depths


@task(name="check-processor-metrics")
def check_processor_metrics() -> dict:
    """Watch the processor's own counters — db_write_failed had no consumer at all."""
    logger = get_run_logger()
    try:
        r = httpx.get(f"{PROCESSOR_URL}/metrics", timeout=10)
        r.raise_for_status()
        metrics = r.json()
    except Exception as exc:
        logger.error("Processor metrics unreachable: %s", exc)
        _alert_once("processor_down",
                    "🚨 <b>Processor is not answering</b>\n\n"
                    "Nothing is translating or delivering right now.")
        return {"error": str(exc)}

    failures = int(metrics.get("db_write_failed", 0))
    try:
        rc = _redis()
        previous = int(rc.hget(_STATE_KEY, "db_write_failed") or 0)
        rc.hset(_STATE_KEY, "db_write_failed", failures)
    except Exception:
        previous = failures

    # The counter resets when the processor restarts, so only a rise means new damage.
    if failures > previous:
        _alert_once("db_writes",
                    f"💾 <b>Database writes are failing</b>\n\n"
                    f"{failures - previous} new failure(s) since the last check.\n"
                    "Messages may be delivered but not recorded — which also breaks dedup.")

    return {"db_write_failed": failures, "previous": previous}


@task(name="check-resources")
def check_resources() -> dict:
    """Disk and swap. The box has 3.8 GB and filled its disk with media once already."""
    logger = get_run_logger()
    out = {}

    try:
        usage = shutil.disk_usage("/")
        pct = round(usage.used / usage.total * 100)
        out["disk_pct"] = pct
        out["disk_free_gb"] = round(usage.free / 1024 ** 3, 1)
        if pct >= DISK_PCT_THRESHOLD:
            _alert_once("disk",
                        f"💽 <b>Disk {pct}% full</b>\n\n"
                        f"{out['disk_free_gb']} GB left. Postgres stops accepting writes "
                        f"when it runs out.")
    except Exception as exc:
        logger.warning("Disk check failed: %s", exc)

    try:
        meminfo = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                meminfo[key] = int(rest.strip().split()[0])
        swap_total = meminfo.get("SwapTotal", 0)
        if swap_total:
            swap_used = swap_total - meminfo.get("SwapFree", 0)
            pct = round(swap_used / swap_total * 100)
            out["swap_pct"] = pct
            if pct >= SWAP_PCT_THRESHOLD:
                _alert_once("swap",
                            f"🧠 <b>Swap {pct}% used</b>\n\n"
                            "The box is under memory pressure; this is how the OOM spiral "
                            "started last time.")
    except Exception as exc:
        logger.warning("Swap check failed: %s", exc)

    logger.info("Resources: %s", out)
    return out


@task(retries=1, name="check-processor-failures")
def check_processor_failures() -> dict:
    """Mapped failure rate over the last 15 minutes."""
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
            logger.error("ALERT: mapped failure rate %s%% (%d/%d) exceeds threshold",
                         rate_pct, mapped_failed, mapped_total)
            alerted = _alert_once(
                "failure_rate",
                f"🚨 <b>High failure rate alert</b>\n\n"
                f"Mapped failure rate: <b>{rate_pct}%</b> in last 15 min "
                f"({mapped_failed}/{mapped_total} messages)\n"
                f"Threshold: {int(FAILURE_RATE_THRESHOLD * 100)}%\n\n"
                f"Check logs: <code>docker compose logs processor</code>",
            )
        else:
            logger.info("Failure rate OK: %.1f%% (%d/%d mapped)", rate * 100, mapped_failed, mapped_total)
    else:
        logger.info("Not enough mapped messages in window (%d), skipping rate check", mapped_total)

    return {"mapped_total": mapped_total, "mapped_failed": mapped_failed, "alerted": alerted}


@flow(name="wa-health-check", log_prints=True)
def wa_health_check():
    """Check every layer and alert admins on anything that means messages are not moving."""
    health = check_wa_health()
    expected = expected_clients()

    clients = evaluate_clients(health, expected)
    silence = check_silence(health)
    queues = check_queues()
    metrics = check_processor_metrics()
    resources = check_resources()
    failures = check_processor_failures()

    return {
        "clients": clients,
        "silence": silence,
        "queues": queues,
        "metrics": metrics,
        "resources": resources,
        "failure_alert": failures["alerted"],
    }


if __name__ == "__main__":
    wa_health_check()
