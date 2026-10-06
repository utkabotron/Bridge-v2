"""Processor service entry point.

Starts two concurrent tasks:
1. FastAPI HTTP server (health + metrics + dashboard endpoints)
2. Redis BRPOP consumer loop — pops messages from "messages:in" and
   runs them through the pipeline (pipeline/graph.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, File, Form, Query, Path, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import (
    BRPOP_TIMEOUT, redis_kwargs,
    DLQ_RETRY_INTERVAL, DLQ_RETRY_BATCH, DLQ_MAX_ATTEMPTS, PROCESSING_QUEUE,
    DLQ_ALERT_THRESHOLD, DLQ_ALERT_COOLDOWN,
    UNAUTH_WINDOW, UNAUTH_THRESHOLD,
    FAILURE_RATE_WINDOW, FAILURE_RATE_THRESHOLD, FAILURE_RATE_MIN_MSGS,
    TRANSLATION_FAIL_WINDOW, TRANSLATION_FAIL_THRESHOLD, TRANSLATION_ALERT_COOLDOWN,
    OPENAI_BILLING_URL,
    TARGET_LANGUAGE, REVOKE_NOTE, DIRECT_MODEL,
    STATS_WINDOW_DAYS, STATS_CACHE_TTL,
)
from .alerts import SlidingWindow, notify_admins
from .pipeline.events import emit, subscribe, unsubscribe
from .pipeline.graph import pipeline
from .media_analyzer import analyze_image, transcribe_audio, analyze_document
from .pipeline.cache import lookup_chat_pairs
from .db import get_pool, fetch_delivered_pair_ids, insert_direct_translation, insert_direct_media_analysis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
# httpx logs every request at INFO, and every Telegram call carries the bot token in its
# URL — which put a working token into docker logs, log shippers and any backup of them.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)



async def _validate_bot_token() -> None:
    """Check that the Telegram bot token is valid on startup."""
    from .telegram_sender import BASE_URL, BOT_TOKEN, get_client
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN is not set")
        return
    try:
        r = await get_client().get(f"{BASE_URL}/getMe")
        if r.status_code == 200:
            data = r.json()
            logger.info("Bot token valid: @%s (id=%s)", data["result"].get("username"), data["result"].get("id"))
        elif r.status_code == 401:
            logger.critical("TELEGRAM_BOT_TOKEN is invalid (401 Unauthorized) — deliveries will fail!")
        else:
            logger.warning("getMe returned unexpected status %s: %s", r.status_code, r.text)
    except Exception as exc:
        logger.warning("Could not validate bot token on startup: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .db import get_pool
    from .pipeline.prompts import register_prompt
    pool = await get_pool()
    await register_prompt(pool)
    logger.info("Translation prompt registered in DB")
    await _validate_bot_token()
    task = asyncio.create_task(consume_loop())
    dlq_task = asyncio.create_task(_dlq_retry_loop())
    logger.info("Processor started")
    yield
    # Graceful drain: ask the loop to stop after the current message, and give it time to
    # finish an in-flight one (which runs under asyncio.shield). Only hard-cancel if it
    # overruns, so a redeploy doesn't drop a message that was mid-pipeline.
    _shutting_down.set()
    dlq_task.cancel()
    try:
        await asyncio.wait_for(task, timeout=BRPOP_TIMEOUT + 30)
    except asyncio.TimeoutError:
        logger.warning("Consumer loop did not drain in time — cancelling")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Bridge v2 — Processor", version="2.0.0", lifespan=lifespan)

# ── Health ────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "processor"}


@app.get("/metrics")
async def metrics():
    """Simple counters — replace with Prometheus if needed later."""
    from .db import get_db_write_failures
    return {
        "processed": _counter["processed"],
        "failed": _counter["failed"],
        "skipped": _counter["skipped"],
        "dlq": _counter["dlq"],
        "db_write_failed": get_db_write_failures(),
    }


_counter = {"processed": 0, "failed": 0, "skipped": 0, "dlq": 0}

# Set by lifespan on shutdown so consume_loop stops after the current message instead of
# being cancelled mid-pipeline.
_shutting_down = asyncio.Event()

# ── Admin alerts ─────────────────────────────────────────
# Each tracker counts events in a sliding window and alerts once per incident (see
# alerts.SlidingWindow); the _alert_admins_* functions only compose the text, and
# notify_admins does the sending.
_unauth_window = SlidingWindow(UNAUTH_WINDOW)
_delivery_window = SlidingWindow(FAILURE_RATE_WINDOW)  # hit = a failed delivery
# An untranslated message still counts as delivered, so the failure-rate alert never saw
# it: on 30.09 the OpenAI balance ran out and every message went untranslated for 12h
# without a word to the admins.
_translation_window = SlidingWindow(TRANSLATION_FAIL_WINDOW, cooldown=TRANSLATION_ALERT_COOLDOWN)

# Suppress repeat DLQ alerts: the loop runs every few minutes and a backlog clears slowly.
_last_dlq_alert = 0.0


async def _alert_admins_unauthorized() -> None:
    """Send a Telegram alert to admins when repeated 401 errors are detected."""
    await notify_admins(
        "🚨 <b>401 Unauthorized spike detected</b>\n\n"
        f"≥{UNAUTH_THRESHOLD} Telegram API 401 errors in the last 15 min.\n"
        "Possible causes: bot removed from chats, or token invalid.\n"
        "Check logs: <code>docker compose logs processor | grep 401</code>"
    )


def _track_unauth_error() -> None:
    """Record a 401 error and trigger alert if threshold exceeded."""
    _unauth_window.record()
    if _unauth_window.total == 1:
        _unauth_window.rearm()  # alone in the window: a new burst, free to alert again
    if _unauth_window.total >= UNAUTH_THRESHOLD and _unauth_window.fire():
        logger.critical("401 threshold reached (%d errors in 15 min) — alerting admins", _unauth_window.total)
        asyncio.create_task(_alert_admins_unauthorized())


async def _alert_admins_failure_rate(rate: float, failed: int, total: int) -> None:
    """Send a Telegram alert to admins when mapped failure rate exceeds threshold."""
    await notify_admins(
        "\U0001F6A8 <b>High failure rate detected</b>\n\n"
        f"Mapped failure rate: <b>{rate:.1%}</b> ({failed}/{total} messages)\n"
        f"Threshold: {FAILURE_RATE_THRESHOLD:.0%} over {FAILURE_RATE_WINDOW // 60} min window.\n"
        "Check logs: <code>docker compose logs processor --tail 100</code>"
    )


def _track_delivery(failed: bool) -> None:
    """Record a mapped delivery result and alert if failure rate exceeds threshold."""
    _delivery_window.record(hit=failed)
    total = _delivery_window.total
    if total < FAILURE_RATE_MIN_MSGS:
        return
    failed_count = _delivery_window.hits
    rate = failed_count / total
    if rate >= FAILURE_RATE_THRESHOLD and _delivery_window.fire():
        logger.critical(
            "Failure rate %.1f%% (%d/%d) exceeds threshold — alerting admins",
            rate * 100, failed_count, total,
        )
        asyncio.create_task(_alert_admins_failure_rate(rate, failed_count, total))
    # Failure-free window of enough messages: the incident is over, the next may alert.
    if failed_count == 0:
        _delivery_window.rearm()


async def _alert_admins_dlq(depth: int) -> None:
    """Warn admins that dead-lettered messages are piling up."""
    global _last_dlq_alert
    if time.time() - _last_dlq_alert < DLQ_ALERT_COOLDOWN:
        return
    sent = await notify_admins(
        "\U0001F4EC <b>Dead-letter queue is filling up</b>\n\n"
        f"Messages waiting: <b>{depth}</b>\n"
        "They are retried automatically; this means the retries keep failing.\n"
        "Check: <code>docker compose logs processor --tail 100</code>"
    )
    # Stamped on delivery: with alerts off, or Telegram unreachable, the next pass retries
    # instead of staying silent for the whole cooldown.
    if sent:
        _last_dlq_alert = time.time()


def _is_quota_error(error: str) -> bool:
    e = error.lower()
    return "insufficient_quota" in e or "no credits" in e or "exceeded your current quota" in e


async def _alert_admins_translation(failed: int, error: str) -> None:
    """Tell admins messages are going out untranslated, and why."""
    from html import escape
    if _is_quota_error(error):
        text = (
            "\U0001F4B3 <b>OpenAI credits ran out</b>\n\n"
            "Messages are delivered untranslated; media analysis, voice transcripts "
            "and DM translation are down too.\n"
            f"Top up: {OPENAI_BILLING_URL}"
        )
    else:
        text = (
            "⚠️ <b>Translation is failing</b>\n\n"
            f"{failed} messages in the last {TRANSLATION_FAIL_WINDOW // 60} min "
            "were delivered untranslated.\n"
            f"<code>{escape(error[:300])}</code>"
        )
    await notify_admins(text)


def _track_translation_failure(error: str) -> None:
    """Record an untranslated delivery and alert admins once per cooldown."""
    _translation_window.record()
    if not _is_quota_error(error) and _translation_window.total < TRANSLATION_FAIL_THRESHOLD:
        return
    if not _translation_window.fire():
        return
    logger.critical(
        "Translation failing (%d in window): %s — alerting admins",
        _translation_window.total, error[:200],
    )
    asyncio.create_task(_alert_admins_translation(_translation_window.total, error))

# ── SSE stream ───────────────────────────────────────────

@app.get("/events")
async def sse_events():
    """Server-Sent Events stream for real-time pipeline visualization."""
    q = await subscribe()

    async def generate():
        try:
            while True:
                event = await q.get()
                data = json.dumps(event, ensure_ascii=False, default=str)
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe(q)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

# ── Dashboard ────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return _dashboard_html


# (monotonic time computed, result) — see STATS_CACHE_TTL.
_stats_cache: tuple[float, dict] | None = None


@app.get("/api/stats")
async def api_stats():
    """User stats from DB for the dashboard, over the last STATS_WINDOW_DAYS days."""
    global _stats_cache
    now = time.monotonic()
    if _stats_cache is not None and now - _stats_cache[0] < STATS_CACHE_TTL:
        return _stats_cache[1]

    from .db import get_pool
    pool = await get_pool()
    # Each source is aggregated once and joined to users, instead of seven correlated
    # subqueries per user that rescanned message_events every time.
    rows = await pool.fetch("""
        with ev as (
            select cp.user_id,
                   count(*) filter (where me.delivery_status = 'delivered') as delivered,
                   count(*) filter (where me.delivery_status = 'failed') as failed,
                   round(avg(me.translation_ms)) as avg_ms,
                   max(me.created_at) as last_msg
            from message_events me
            join chat_pairs cp on cp.id = me.chat_pair_id
            where me.created_at >= now() - make_interval(days => $1)
            group by cp.user_id
        ),
        di as (
            select user_id,
                   count(*) filter (where interaction_type = 'translation') as dir_tl,
                   count(*) filter (where interaction_type = 'media_analysis') as dir_ma
            from direct_interactions
            where created_at >= now() - make_interval(days => $1)
            group by user_id
        ),
        pr as (
            select user_id, count(*) as pairs
            from chat_pairs where status = 'active'
            group by user_id
        )
        select
            u.tg_username,
            u.tg_user_id,
            u.wa_connected,
            u.target_language,
            coalesce(pr.pairs, 0) as pairs,
            coalesce(ev.delivered, 0) as delivered,
            coalesce(ev.failed, 0) as failed,
            ev.avg_ms,
            ev.last_msg,
            coalesce(di.dir_tl, 0) as dir_tl,
            coalesce(di.dir_ma, 0) as dir_ma
        from users u
        left join ev on ev.user_id = u.id
        left join di on di.user_id = u.id
        left join pr on pr.user_id = u.id
        where u.is_active = true
        order by delivered desc
    """, STATS_WINDOW_DAYS)
    # Not derived from the per-user rows: skipped events often have no pair (that is why
    # they were skipped), so they never reach the join above.
    totals = await pool.fetchrow("""
        select
            count(*) filter (where delivery_status = 'skipped') as total_skipped,
            round(avg(translation_ms)) as total_avg_ms
        from message_events
        where created_at >= now() - make_interval(days => $1)
    """, STATS_WINDOW_DAYS)
    result = {
        "users": [dict(r) for r in rows],
        "total_skipped": totals["total_skipped"],
        "total_avg_ms": totals["total_avg_ms"],
    }
    _stats_cache = (now, result)
    return result


@app.get("/api/daily-stats")
async def api_daily_stats():
    """Today's message counts for the dashboard header."""
    from .db import get_pool
    pool = await get_pool()
    row = await pool.fetchrow("""
        select
            count(*) filter (where delivery_status = 'delivered') as delivered,
            count(*) filter (where delivery_status = 'failed') as failed,
            count(*) filter (where delivery_status = 'skipped') as skipped,
            round(avg(translation_ms) filter (where translation_ms is not null)) as avg_ms
        from message_events
        where created_at >= current_date
    """)
    result = dict(row)
    direct = await pool.fetchrow("""
        select
            count(*) filter (where interaction_type = 'translation') as direct_translations,
            count(*) filter (where interaction_type = 'media_analysis') as direct_analyses
        from direct_interactions
        where created_at >= current_date
    """)
    result.update(dict(direct))
    return result


@app.get("/api/reports")
async def api_reports(date: str = Query(default="")):
    """Analytics reports: nightly problems + translation quality by date."""
    from datetime import date as date_type

    from .db import get_pool

    pool = await get_pool()
    try:
        report_date = date_type.fromisoformat(date) if date else date_type.today()
    except ValueError:
        report_date = date_type.today()

    # Available dates (last 30 with data)
    date_rows = await pool.fetch("""
        SELECT DISTINCT run_date FROM nightly_analysis_runs
        ORDER BY run_date DESC LIMIT 30
    """)
    dates = [str(r["run_date"]) for r in date_rows]

    # Problems run
    problems_run = await pool.fetchrow("""
        SELECT id, summary FROM nightly_analysis_runs
        WHERE run_date = $1 AND flow_type = 'problems'
    """, report_date)

    problems = {"summary": None, "issues": []}
    if problems_run:
        problems["summary"] = json.loads(problems_run["summary"]) if problems_run["summary"] else None
        issue_rows = await pool.fetch("""
            SELECT severity, category, title, description, suggested_fix, acknowledged
            FROM detected_issues WHERE run_id = $1
            ORDER BY
                CASE severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                id
        """, problems_run["id"])
        problems["issues"] = [dict(r) for r in issue_rows]

    # Quality run
    quality_run = await pool.fetchrow("""
        SELECT id, summary FROM nightly_analysis_runs
        WHERE run_date = $1 AND flow_type = 'translation_quality'
    """, report_date)

    quality = {"summary": None, "evaluations_count": 0, "suggestions": []}
    if quality_run:
        quality["summary"] = json.loads(quality_run["summary"]) if quality_run["summary"] else None
        eval_count = await pool.fetchval("""
            SELECT count(*) FROM translation_evaluations WHERE run_id = $1 AND NOT shadow
        """, quality_run["id"])
        quality["evaluations_count"] = eval_count
        sug_rows = await pool.fetch("""
            SELECT suggestion, rationale, status
            FROM prompt_suggestions WHERE run_id = $1
            ORDER BY id
        """, quality_run["id"])
        quality["suggestions"] = [dict(r) for r in sug_rows]

    return {
        "date": report_date.isoformat(),
        "dates": dates,
        "problems": problems,
        "quality": quality,
    }


# ── Backlog API ──────────────────────────────────────────

class BacklogUpdate(BaseModel):
    status: str


@app.get("/api/backlog")
async def api_backlog():
    """Open critical issues from the persistent backlog."""
    from .db import get_pool
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT id, source_run_date, severity, category, title, description,
               suggested_fix, status, resolved_at, created_at
        FROM issues_backlog
        WHERE status = 'open'
        ORDER BY created_at DESC
    """)
    return [dict(r) for r in rows]


@app.patch("/api/backlog/{issue_id}")
async def api_backlog_update(issue_id: int = Path(...), body: BacklogUpdate = ...):
    """Update backlog issue status (resolved / wontfix)."""
    from .db import get_pool
    if body.status not in ("resolved", "wontfix"):
        return JSONResponse({"error": "status must be 'resolved' or 'wontfix'"}, status_code=400)
    pool = await get_pool()
    row = await pool.fetchrow("""
        UPDATE issues_backlog
        SET status = $1, resolved_at = CASE WHEN $1 = 'resolved' THEN now() ELSE resolved_at END
        WHERE id = $2
        RETURNING id, status
    """, body.status, issue_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return dict(row)


# ── Dead-Letter Queue API ────────────────────────────────

@app.get("/api/dlq")
async def api_dlq():
    """List messages in the dead-letter queue."""
    r = aioredis.Redis(**redis_kwargs())
    try:
        items = await r.lrange("messages:dlq", 0, 99)
        return [json.loads(item) for item in items]
    finally:
        await r.aclose()


# ── Feature Flags API ────────────────────────────────────

@app.get("/api/flags")
async def api_flags():
    """List all feature flags."""
    from .feature_flags import get_all_flags
    return await get_all_flags()


class FlagUpdate(BaseModel):
    enabled: bool


@app.patch("/api/flags/{flag_name}")
async def api_flag_update(flag_name: str = Path(...), body: FlagUpdate = ...):
    """Toggle a feature flag."""
    from .feature_flags import set_flag
    updated = await set_flag(flag_name, body.enabled)
    if not updated:
        return JSONResponse({"error": "flag not found"}, status_code=404)
    return {"name": flag_name, "enabled": body.enabled}


# ── Chat Profiles API ────────────────────────────────────

@app.get("/api/profiles")
async def api_profiles():
    """Chat profiles with glossaries and member names."""
    from .db import get_pool
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT cp_tbl.id AS chat_pair_id,
               cp_tbl.wa_chat_id,
               prof.profile_data,
               prof.version,
               prof.updated_at
        FROM chat_profiles prof
        JOIN chat_pairs cp_tbl ON cp_tbl.id = prof.chat_pair_id
        WHERE cp_tbl.status = 'active'
        ORDER BY prof.updated_at DESC
    """)
    result = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("profile_data"), str):
            d["profile_data"] = json.loads(d["profile_data"])
        result.append(d)
    return result


# ── Costs API (LangSmith) ────────────────────────────────

# Fallback costs per token (USD) when LangSmith doesn't provide cost
@app.get("/api/costs")
async def api_costs(days: int = Query(default=7, ge=1, le=90)):
    """Processor LLM spend per day, from the llm_usage ledger (src/llm.py writes it).

    Same shape LangSmith used to give the dashboard, plus a split by purpose and model.
    """
    from .db import get_pool
    pool = await get_pool()
    rows = await pool.fetch("""
        SELECT (created_at AT TIME ZONE 'Asia/Jerusalem')::date AS day,
               sum(cost_usd) AS cost, sum(tokens_in + tokens_out) AS tokens, count(*) AS runs
        FROM llm_usage
        WHERE created_at >= now() - make_interval(days => $1)
        GROUP BY 1 ORDER BY 1 DESC
    """, days)
    split = await pool.fetch("""
        SELECT purpose, model, sum(cost_usd) AS cost, count(*) AS runs
        FROM llm_usage
        WHERE created_at >= now() - make_interval(days => $1)
        GROUP BY 1, 2 ORDER BY 3 DESC
    """, days)
    by_day = [
        {"date": r["day"].isoformat(), "cost": round(float(r["cost"]), 4),
         "tokens": int(r["tokens"] or 0), "runs": r["runs"]}
        for r in rows
    ]
    return {
        "period_days": days,
        "total_cost": round(sum(d["cost"] for d in by_day), 4),
        "total_tokens": sum(d["tokens"] for d in by_day),
        "total_runs": sum(d["runs"] for d in by_day),
        "by_day": by_day,
        "by_purpose": [
            {"purpose": r["purpose"], "model": r["model"], "cost": round(float(r["cost"]), 4), "runs": r["runs"]}
            for r in split
        ],
    }


# ── Translation API ───────────────────────────────────────

class TranslateRequest(BaseModel):
    text: str
    target_language: str = ""
    user_id: int = 0


@app.post("/translate")
async def translate_text(body: TranslateRequest):
    """Translate text using the pipeline's LLM + cache."""
    from .feature_flags import is_enabled
    if not await is_enabled("translation_enabled"):
        return JSONResponse({"error": "Translation is temporarily disabled"}, status_code=503)
    from .llm import chat as llm_chat
    from .pipeline.cache import get_cached, set_cached
    from .pipeline.prompts import PROMPT_VERSION, get_translate_prompt

    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)

    # Resolve target language from user profile if not provided
    lang = body.target_language
    if not lang and body.user_id:
        from .db import get_pool
        pool = await get_pool()
        row = await pool.fetchrow(
            "SELECT target_language FROM users WHERE tg_user_id = $1", body.user_id,
        )
        lang = row["target_language"] if row and row["target_language"] else ""
    if not lang:
        lang = TARGET_LANGUAGE

    # Cache check
    cached = await get_cached(text, lang, version=f"{PROMPT_VERSION}@{DIRECT_MODEL}")
    if cached:
        if body.user_id:
            await insert_direct_translation(body.user_id, text, cached, lang, 0, True)
        return {"original": text, "translated": cached, "target_language": lang, "cache_hit": True}

    # LLM translation. DM is outside the bridge A/B: DIRECT_MODEL, cached under its own version.
    t0 = time.monotonic()
    messages = [
        {"role": "system", "content": get_translate_prompt(lang)},
        {"role": "user", "content": text},
    ]
    translated = (await llm_chat(messages, model=DIRECT_MODEL, purpose="direct_translate")).text
    translation_ms = int((time.monotonic() - t0) * 1000)

    await set_cached(text, lang, translated, version=f"{PROMPT_VERSION}@{DIRECT_MODEL}")

    if body.user_id:
        await insert_direct_translation(body.user_id, text, translated, lang, translation_ms, False)

    return {
        "original": text,
        "translated": translated,
        "target_language": lang,
        "translation_ms": translation_ms,
        "cache_hit": False,
    }


# ── Media Analysis API ────────────────────────────────────

class AnalyzeRequest(BaseModel):
    message_event_id: int
    requested_by: int


@app.post("/analyze")
async def analyze_media(body: AnalyzeRequest):
    """Analyze media content (image/audio/document) by message_event_id."""
    from .feature_flags import is_enabled
    if not await is_enabled("media_analysis_enabled"):
        return JSONResponse({"error": "Media analysis is temporarily disabled"}, status_code=503)
    from .db import get_event_for_analysis, get_existing_analysis, insert_media_analysis
    from .telegram_sender import download_media

    # Check for existing analysis
    existing = await get_existing_analysis(body.message_event_id)
    if existing:
        return {"result_text": existing["result_text"], "analysis_type": existing["analysis_type"]}

    # Fetch event
    event = await get_event_for_analysis(body.message_event_id)
    if not event:
        return JSONResponse({"error": "event not found"}, status_code=404)

    media_url = event["media_s3_key"]
    if not media_url:
        return JSONResponse({"error": "no media attached"}, status_code=400)

    # Download media from MinIO
    downloaded = await download_media(media_url)
    if not downloaded:
        return JSONResponse({"error": "failed to download media"}, status_code=502)

    content_bytes, filename, content_type = downloaded
    target_lang = event.get("target_language") or "Russian"
    msg_type = event.get("message_type", "")

    # Check media analysis cache by file content hash
    import hashlib as _hashlib
    from .pipeline.cache import get_cached_media, set_cached_media
    file_hash = _hashlib.sha256(content_bytes).hexdigest()
    cached = await get_cached_media(file_hash, target_lang)
    if cached:
        logger.info("Media analysis cache hit for event %s (hash=%s…)", body.message_event_id, file_hash[:12])
        await insert_media_analysis(
            body.message_event_id, "cached", cached,
            "completed", 0, body.requested_by,
        )
        return {"result_text": cached, "analysis_type": "cached", "processing_ms": 0}

    t0 = time.monotonic()
    analysis_type = "unknown"
    try:
        if msg_type in ("image", "photo"):
            analysis_type = "image"
            result_text = await analyze_image(content_bytes, content_type, target_lang)
        elif msg_type in ("audio", "voice", "ptt"):
            analysis_type = "audio"
            result_text = await transcribe_audio(content_bytes, filename, target_lang)
        elif msg_type == "document":
            analysis_type = "document"
            result_text = await analyze_document(content_bytes, filename, content_type, target_lang)
        else:
            return JSONResponse({"error": f"unsupported media type: {msg_type}"}, status_code=400)

        processing_ms = int((time.monotonic() - t0) * 1000)
        await set_cached_media(file_hash, target_lang, result_text)
        await insert_media_analysis(
            body.message_event_id, analysis_type, result_text,
            "completed", processing_ms, body.requested_by,
        )
        return {"result_text": result_text, "analysis_type": analysis_type, "processing_ms": processing_ms}

    except Exception as exc:
        processing_ms = int((time.monotonic() - t0) * 1000)
        error_msg = str(exc)
        logger.error("Media analysis failed for event %s: %s", body.message_event_id, error_msg)
        await insert_media_analysis(
            body.message_event_id, analysis_type, error_msg,
            "failed", processing_ms, body.requested_by,
        )
        return JSONResponse({"error": error_msg}, status_code=500)


# ── Direct media analysis (from bot private chat) ─────────

@app.post("/analyze-direct")
async def analyze_direct(
    file: UploadFile = File(...),
    user_id: int = Form(...),
    mime_type: str = Form(...),
    filename: str = Form("file"),
):
    """Analyze media directly from binary upload (bot private chat)."""
    from .feature_flags import is_enabled
    if not await is_enabled("media_analysis_enabled"):
        return JSONResponse({"error": "Media analysis is temporarily disabled"}, status_code=503)
    # Resolve target language
    from .db import get_pool
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT target_language FROM users WHERE tg_user_id = $1", user_id,
    )
    target_lang = (row["target_language"] if row and row["target_language"] else "") or TARGET_LANGUAGE

    content_bytes = await file.read()

    # Check media analysis cache by file content hash
    import hashlib as _hashlib
    from .pipeline.cache import get_cached_media, set_cached_media
    file_hash = _hashlib.sha256(content_bytes).hexdigest()
    cached = await get_cached_media(file_hash, target_lang)
    if cached:
        logger.info("Media analysis cache hit for %s (hash=%s…)", filename, file_hash[:12])
        return {"result_text": cached, "analysis_type": "cached", "processing_ms": 0}

    t0 = time.monotonic()
    try:
        if mime_type.startswith("image/"):
            analysis_type = "image"
            result_text = await analyze_image(content_bytes, mime_type, target_lang)
        elif mime_type.startswith("audio/") or mime_type == "application/ogg":
            analysis_type = "audio"
            result_text = await transcribe_audio(content_bytes, filename, target_lang)
        else:
            analysis_type = "document"
            result_text = await analyze_document(content_bytes, filename, mime_type, target_lang)

        processing_ms = int((time.monotonic() - t0) * 1000)
        await set_cached_media(file_hash, target_lang, result_text)
        await insert_direct_media_analysis(
            user_id, analysis_type, mime_type, filename, result_text, processing_ms,
        )
        return {"result_text": result_text, "analysis_type": analysis_type, "processing_ms": processing_ms}

    except Exception as exc:
        processing_ms = int((time.monotonic() - t0) * 1000)
        error_msg = str(exc)
        logger.error("Direct media analysis failed: %s", error_msg)
        await insert_direct_media_analysis(
            user_id, analysis_type, mime_type, filename, error_msg, processing_ms,
            status="failed", error_message=error_msg,
        )
        return JSONResponse({"error": error_msg}, status_code=500)


# ── Redis consumer ────────────────────────────────────────

NODES = ["validate", "translate", "format", "deliver"]


def _surrogate_id(payload: dict) -> str:
    """Stable id for a message whose wa_message_id is missing — mirrors the wa-service
    content fallback so dedup and the message_events unique key stay coherent."""
    raw = "|".join(str(payload.get(k, "")) for k in ("user_id", "wa_chat_id", "timestamp", "body"))
    return "noid:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


async def _dlq_push(r, payload, error: str, attempts: int = 0) -> None:
    """Envelope a message into the DLQ ({"payload": ...}), the shape _dlq_retry_loop reads.

    `attempts` rides along so the auto-retry task can give up on a message that keeps
    failing instead of cycling it between the queues forever.
    """
    try:
        entry = json.dumps({
            "payload": payload,
            "error": error,
            "attempts": attempts,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, default=str)
        await r.lpush("messages:dlq", entry)
        _counter["dlq"] += 1
    except Exception as dlq_exc:
        logger.error("Failed to push to DLQ: %s", dlq_exc)


async def _dlq_retry_loop():
    """Re-queue dead-lettered messages with a bounded number of attempts.

    Nothing drained the DLQ before: a message that failed — most often because OpenAI or
    Telegram was briefly unavailable — sat there until someone noticed and clicked retry
    in the dashboard. In practice nobody did.
    """
    r = aioredis.Redis(**redis_kwargs())
    while not _shutting_down.is_set():
        try:
            await asyncio.wait_for(_shutting_down.wait(), timeout=DLQ_RETRY_INTERVAL)
            break  # shutting down
        except asyncio.TimeoutError:
            pass

        try:
            depth = await r.llen("messages:dlq")
            if depth == 0:
                continue

            if depth >= DLQ_ALERT_THRESHOLD:
                await _alert_admins_dlq(depth)

            # One pass over what is there now; anything re-queued that fails again comes
            # back with a higher attempt count on the next pass.
            for _ in range(min(depth, DLQ_RETRY_BATCH)):
                raw = await r.rpop("messages:dlq")
                if raw is None:
                    break
                try:
                    entry = json.loads(raw)
                    payload = entry.get("payload")
                    attempts = int(entry.get("attempts", 0)) + 1
                except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                    await r.lpush("messages:dlq:dead", raw)
                    continue

                if payload is None:
                    await r.lpush("messages:dlq:dead", raw)
                    continue

                if attempts > DLQ_MAX_ATTEMPTS:
                    logger.warning("DLQ: giving up on %s after %d attempts",
                                   payload.get("wa_message_id"), attempts - 1)
                    entry["attempts"] = attempts - 1
                    await r.lpush("messages:dlq:dead", json.dumps(entry, default=str))
                    continue

                payload["_dlq_attempts"] = attempts
                await r.lpush("messages:in", json.dumps(payload, default=str))
                logger.info("DLQ: re-queued %s (attempt %d)", payload.get("wa_message_id"), attempts)
        except Exception as exc:
            logger.error("DLQ retry loop error: %s", exc)


async def _process_message(r, raw: str) -> None:
    """Handle one popped message end-to-end. Any failure routes the message to the DLQ so
    that BRPOP's destructive read can never silently lose it."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Unparseable — can't envelope as payload; store the raw string for inspection.
        logger.error("Invalid JSON in messages:in: %s", exc)
        try:
            await r.lpush("messages:dlq", json.dumps({"raw": raw, "error": str(exc)}))
        except Exception as dlq_exc:
            logger.error("Failed to push malformed message to DLQ: %s", dlq_exc)
        return

    if payload.get("kind") == "revoke":
        await _handle_revoke(payload)
        return

    try:
        try:
            user_id = int(payload.get("user_id") or 0)
        except (TypeError, ValueError):
            user_id = 0

        wa_message_id = payload.get("wa_message_id") or _surrogate_id(payload)

        state = {
            "wa_message_id": wa_message_id,
            "wa_chat_id": payload.get("wa_chat_id", ""),
            "wa_chat_name": payload.get("wa_chat_name", ""),
            "user_id": user_id,
            "sender_name": payload.get("sender_name", ""),
            "original_text": payload.get("body", ""),
            "message_type": payload.get("message_type", "text"),
            "media_s3_url": payload.get("media_s3_url"),
            "media_mime": payload.get("media_mime"),
            "media_filename": payload.get("media_filename"),
            "timestamp": payload.get("timestamp", 0),
            "from_me": payload.get("from_me", False),
            "is_edited": payload.get("is_edited", False),
            "media_failed": payload.get("media_failed", False),
            "quoted": payload.get("quoted"),
            "location": payload.get("location"),
            "contacts": payload.get("contacts"),
            # Will be resolved by validate node
            "chat_pair_id": None,
            "tg_chat_id": None,
            "target_language": TARGET_LANGUAGE,
            "translated_text": None,
            "translation_ms": None,
            "cache_hit": False,
            "formatted_text": None,
            "delivery_status": "pending",
            "error": None,
        }

        msg_id = wa_message_id[:12]
        sender = state["sender_name"] or "unknown"
        text_preview = (state["original_text"] or "")[:60]

        emit("message_received", {
            "msg_id": msg_id,
            "sender": sender,
            "text": text_preview,
            "chat": state["wa_chat_name"],
        })
        # Every active pair of this chat gets its own run: a WhatsApp group message is
        # seen by several clients but dedup keeps only one copy, so fanning out here is
        # what stops the pairs that did not win the dedup race from starving.
        pairs = await lookup_chat_pairs(user_id, state["wa_chat_id"])
    except Exception as exc:
        # Failure in parse/build/lookup/emit — the popped message would otherwise vanish.
        _counter["failed"] += 1
        logger.error("Pre-pipeline error for %s: %s", payload.get("wa_message_id"), exc)
        await _dlq_push(r, payload, str(exc), attempts=payload.get("_dlq_attempts", 0))
        return

    # No pair → a single pair-less run, so validate_node still decides between the
    # admin fallback and "skipped" (it must not look the pairs up a second time).
    branches = [
        {
            **state,
            "chat_pair_id": pair["id"],
            "tg_chat_id": pair["tg_chat_id"],
            "target_language": pair.get("target_language") or "Russian",
        }
        for pair in pairs
    ] or [{**state, "pairs_resolved": True}]

    # A message that comes back through the DLQ or the in-flight list may already have
    # reached some of its pairs (a fan-out that failed halfway). The upsert in
    # insert_message_event only guards the row, and runs after Telegram was called, so
    # without this check those pairs would get a second copy. Fresh messages skip the
    # query: wa-service dedup has them covered.
    delivered = await fetch_delivered_pair_ids(wa_message_id) if _is_requeued(payload) else set()

    for branch in branches:
        chat_pair_id = branch.get("chat_pair_id")
        if chat_pair_id in delivered:
            _counter["skipped"] += 1
            logger.info("Dedup skip: %s already delivered to pair %s", wa_message_id, chat_pair_id)
            continue
        await _run_pipeline(r, payload, branch, msg_id, wa_message_id)


async def _handle_revoke(payload: dict) -> None:
    """Note in Telegram that a message was deleted for everyone in WhatsApp.

    Leaving the original standing misrepresents the conversation — the reader has no way
    to know the sender took it back.
    """
    from .telegram_sender import send_message

    wa_message_id = payload.get("wa_message_id")
    if not wa_message_id:
        return

    try:
        pool = await get_pool()
        rows = await pool.fetch(
            """
            select me.tg_message_id, cp.tg_chat_id
            from public.message_events me
            join public.chat_pairs cp on cp.id = me.chat_pair_id
            where me.wa_message_id = $1
              and me.delivery_status = 'delivered'
              and me.tg_message_id is not null
            """,
            wa_message_id,
        )
    except Exception as exc:
        logger.warning("Revoke lookup failed for %s: %s", wa_message_id, exc)
        return

    if not rows:
        logger.debug("Revoke: no delivered copy of %s to annotate", wa_message_id)
        return

    for row in rows:
        try:
            await send_message(
                chat_id=row["tg_chat_id"],
                text=REVOKE_NOTE,
                reply_to_message_id=row["tg_message_id"],
            )
        except Exception as exc:
            logger.warning("Revoke note failed for %s: %s", wa_message_id, exc)


def _is_requeued(payload: dict) -> bool:
    """True for a message that already went through the pipeline once: a DLQ retry, or one
    recovered from the in-flight list after a crash."""
    return bool(payload.get("_dlq_attempts") or payload.get("_requeued"))


def _mark_requeued(raw: str) -> str:
    """Flag a recovered in-flight message so _process_message checks what it already reached."""
    try:
        payload = json.loads(raw)
        payload["_requeued"] = True
        return json.dumps(payload, default=str)
    except (json.JSONDecodeError, TypeError):
        return raw


async def _run_pipeline(r, payload: dict, state: dict, msg_id: str, wa_message_id: str) -> None:
    """Run the pipeline for one (message, chat pair) branch."""

    try:
        final_state = None
        t0 = time.monotonic()

        async for chunk in pipeline.astream(state, stream_mode="updates"):
            for node_name, node_output in chunk.items():
                elapsed = int((time.monotonic() - t0) * 1000)
                evt = {
                    "msg_id": msg_id,
                    # One message can run once per chat pair — the dashboard needs the pair
                    # to keep those branches apart.
                    "chat_pair_id": state.get("chat_pair_id"),
                    "node": node_name,
                    "elapsed_ms": elapsed,
                    "status": node_output.get("delivery_status", "ok"),
                    "error": node_output.get("error"),
                }
                if node_name == "validate":
                    evt["chat_pair_id"] = node_output.get("chat_pair_id")
                    evt["tg_chat_id"] = node_output.get("tg_chat_id")
                    evt["target_lang"] = node_output.get("target_language")
                    evt["msg_type"] = node_output.get("message_type", "text")
                elif node_name == "translate":
                    evt["cache_hit"] = node_output.get("cache_hit")
                    evt["translation_ms"] = node_output.get("translation_ms")
                    orig = (node_output.get("original_text") or "")[:40]
                    trans = (node_output.get("translated_text") or "")[:40]
                    evt["original"] = orig
                    evt["translated"] = trans
                elif node_name == "format":
                    fmt = (node_output.get("formatted_text") or "")[:80]
                    evt["preview"] = fmt
                    evt["text_len"] = len(node_output.get("formatted_text") or "")
                elif node_name == "deliver":
                    evt["tg_chat_id"] = node_output.get("tg_chat_id")
                    evt["delivery_status"] = node_output.get("delivery_status")
                emit("node_done", evt)
                final_state = node_output

        if final_state and final_state.get("delivery_status") == "delivered":
            _counter["processed"] += 1
            total_ms = int((time.monotonic() - t0) * 1000)
            emit("message_delivered", {
                "msg_id": msg_id,
                "chat_pair_id": state.get("chat_pair_id"),
                "total_ms": total_ms,
                "cache_hit": final_state.get("cache_hit"),
            })
            _track_delivery(failed=False)
            if final_state.get("translation_failed"):
                _track_translation_failure(final_state.get("translation_error") or "")
            logger.info(
                "Delivered %s (lang=%s, cache=%s, ms=%s)",
                wa_message_id,
                final_state.get("target_language"),
                final_state.get("cache_hit"),
                final_state.get("translation_ms"),
            )
        elif final_state and final_state.get("delivery_status") == "skipped":
            _counter["skipped"] += 1
            emit("message_skipped", {
                "msg_id": msg_id,
                "chat_pair_id": state.get("chat_pair_id"),
                "error": final_state.get("error", "no_chat_pair"),
            })
            logger.debug("Skipped %s: %s", wa_message_id, final_state.get("error"))
        else:
            _counter["failed"] += 1
            err = final_state.get("error") if final_state else "no_output"
            _track_delivery(failed=True)
            if err == "401_UNAUTHORIZED":
                _track_unauth_error()
            emit("message_failed", {"msg_id": msg_id, "error": err})
            logger.warning("Failed %s: %s", wa_message_id, err)
    except Exception as exc:
        _counter["failed"] += 1
        emit("message_failed", {"msg_id": msg_id, "error": str(exc)})
        logger.error("Pipeline error for %s: %s", wa_message_id, exc)
        # Count it as a delivery failure: an OpenAI or Telegram outage lands here, and
        # without this the failure-rate alert stayed silent through the whole incident.
        _track_delivery(failed=True)
        await _dlq_push(r, payload, str(exc), attempts=payload.get("_dlq_attempts", 0))


async def _requeue_inflight(r) -> None:
    """Return messages a previous process never finished to the head of the queue."""
    try:
        stranded = await r.lrange(PROCESSING_QUEUE, 0, -1)
        if not stranded:
            return
        for raw in stranded:
            await r.rpush("messages:in", _mark_requeued(raw))
            await r.lrem(PROCESSING_QUEUE, 1, raw)
        logger.warning("Recovered %d in-flight message(s) from a previous run", len(stranded))
    except Exception as exc:
        logger.error("Failed to recover in-flight messages: %s", exc)


async def consume_loop():
    r = aioredis.Redis(**redis_kwargs())

    # Anything left in the in-flight list belongs to a previous process that died between
    # taking a message and finishing it (OOM kill, SIGKILL past the stop grace period).
    # BRPOP alone deleted the message the instant it was read, so those were simply lost.
    await _requeue_inflight(r)

    logger.info("Consumer loop started — waiting for messages:in")

    while not _shutting_down.is_set():
        raw = None
        try:
            # Atomically move to an in-flight list instead of popping into thin air, and
            # delete it only once the message is done.
            raw = await r.blmove("messages:in", PROCESSING_QUEUE, BRPOP_TIMEOUT, "RIGHT", "LEFT")
            if raw is None:
                continue  # timeout — loop again
            # Shield so a shutdown cancel can't interrupt a message we've already popped
            # from Redis — it finishes (or DLQs) before the loop exits.
            await asyncio.shield(_process_message(r, raw))
            await r.lrem(PROCESSING_QUEUE, 1, raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Consumer loop error: %s", exc)
            # Backstop: if we popped a message but blew up outside _process_message's own
            # handling, DLQ the raw item rather than lose it.
            if raw is not None:
                try:
                    await r.lpush("messages:dlq", json.dumps({"raw": raw, "error": str(exc)}))
                    await r.lrem(PROCESSING_QUEUE, 1, raw)
                except Exception:
                    pass
            await asyncio.sleep(1)

    try:
        await r.aclose()
    except Exception:
        pass
    logger.info("Consumer loop stopped (graceful)")


# ── Dashboard HTML (loaded from external file) ──────────

_DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")
with open(_DASHBOARD_PATH) as _f:
    _dashboard_html = _f.read()


# ── Startup ───────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PROCESSOR_PORT", 8000))
    uvicorn.run("src.main:app", host="0.0.0.0", port=port, reload=False)
