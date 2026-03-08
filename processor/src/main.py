"""Processor service entry point.

Starts two concurrent tasks:
1. FastAPI HTTP server (health + metrics + dashboard endpoints)
2. Redis BRPOP consumer loop — pops messages from "messages:in" and
   runs them through the LangGraph pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, File, Form, Query, Path, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .pipeline.events import emit, subscribe, unsubscribe
from .pipeline.graph import pipeline
from .media_analyzer import analyze_image, transcribe_audio, analyze_document
from .db import get_pool, insert_direct_translation, insert_direct_media_analysis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .db import get_pool
    from .pipeline.prompts import register_prompt
    pool = await get_pool()
    await register_prompt(pool)
    logger.info("Translation prompt registered in DB")
    task = asyncio.create_task(consume_loop())
    logger.info("Processor started")
    yield
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
    return {"processed": _counter["processed"], "failed": _counter["failed"], "skipped": _counter["skipped"]}


_counter = {"processed": 0, "failed": 0, "skipped": 0}

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


@app.get("/api/stats")
async def api_stats():
    """User stats from DB for the dashboard."""
    from .db import get_pool
    pool = await get_pool()
    rows = await pool.fetch("""
        select
            u.tg_username,
            u.tg_user_id,
            u.wa_connected,
            u.target_language,
            (select count(*) from chat_pairs cp where cp.user_id = u.id and cp.status = 'active') as pairs,
            (select count(*) from message_events me
             join chat_pairs cp2 on cp2.id = me.chat_pair_id
             where cp2.user_id = u.id and me.delivery_status = 'delivered') as delivered,
            (select count(*) from message_events me
             join chat_pairs cp2 on cp2.id = me.chat_pair_id
             where cp2.user_id = u.id and me.delivery_status = 'failed') as failed,
            (select round(avg(me.translation_ms)) from message_events me
             join chat_pairs cp2 on cp2.id = me.chat_pair_id
             where cp2.user_id = u.id and me.translation_ms is not null) as avg_ms,
            (select max(me.created_at) from message_events me
             join chat_pairs cp2 on cp2.id = me.chat_pair_id
             where cp2.user_id = u.id) as last_msg,
            (select count(*) from direct_interactions di
             where di.user_id = u.id and di.interaction_type = 'translation') as dir_tl,
            (select count(*) from direct_interactions di
             where di.user_id = u.id and di.interaction_type = 'media_analysis') as dir_ma
        from users u
        where u.is_active = true
        order by delivered desc
    """)
    totals = await pool.fetchrow("""
        select
            count(*) filter (where delivery_status = 'skipped') as total_skipped,
            round(avg(translation_ms) filter (where translation_ms is not null)) as total_avg_ms
        from message_events
    """)
    return {
        "users": [dict(r) for r in rows],
        "total_skipped": totals["total_skipped"],
        "total_avg_ms": totals["total_avg_ms"],
    }


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
            SELECT count(*) FROM translation_evaluations WHERE run_id = $1
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


# ── Costs API (LangSmith) ────────────────────────────────

# Fallback costs per token (USD) when LangSmith doesn't provide cost
_FALLBACK_COSTS = {
    "gpt-4.1-mini": {"input": 0.40 / 1_000_000, "output": 1.60 / 1_000_000},
    "gpt-4o-mini": {"input": 0.15 / 1_000_000, "output": 0.60 / 1_000_000},
}
_DEFAULT_FALLBACK = {"input": 0.40 / 1_000_000, "output": 1.60 / 1_000_000}

# In-memory cache: {days: (timestamp, data)}
_costs_cache: dict[int, tuple[float, dict]] = {}
_COSTS_CACHE_TTL = 900  # 15 min


def _fetch_costs_sync(days: int) -> dict:
    """Synchronous LangSmith query — runs in thread."""
    from langsmith import Client

    client = Client()
    project = os.getenv("LANGCHAIN_PROJECT", "bridge-v2")
    start = datetime.now(timezone.utc) - timedelta(days=days)

    by_day: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "tokens": 0, "runs": 0})
    total_cost = 0.0
    total_tokens = 0
    total_runs = 0

    for run in client.list_runs(
        project_name=project,
        run_type="llm",
        is_root=False,
        start_time=start,
    ):
        day_key = run.start_time.strftime("%Y-%m-%d") if run.start_time else "unknown"
        tokens = (run.total_tokens or 0)
        cost = run.total_cost
        if cost is None or cost == 0:
            model = (run.extra or {}).get("metadata", {}).get("ls_model_name", "")
            fb = _FALLBACK_COSTS.get(model, _DEFAULT_FALLBACK)
            cost = (run.prompt_tokens or 0) * fb["input"] + (run.completion_tokens or 0) * fb["output"]
        cost = float(cost)

        total_cost += cost
        total_tokens += tokens
        total_runs += 1
        by_day[day_key]["cost"] += cost
        by_day[day_key]["tokens"] += tokens
        by_day[day_key]["runs"] += 1

    by_day_list = sorted(
        [{"date": k, "cost": round(v["cost"], 4), "tokens": v["tokens"], "runs": v["runs"]} for k, v in by_day.items()],
        key=lambda x: x["date"],
        reverse=True,
    )

    return {
        "period_days": days,
        "total_cost": round(total_cost, 4),
        "total_tokens": total_tokens,
        "total_runs": total_runs,
        "by_day": by_day_list,
    }


@app.get("/api/costs")
async def api_costs(days: int = Query(default=7, ge=1, le=90)):
    """LangSmith LLM cost data, cached for 15 min."""
    now = time.monotonic()
    if days in _costs_cache:
        ts, data = _costs_cache[days]
        if now - ts < _COSTS_CACHE_TTL:
            return data

    try:
        data = await asyncio.to_thread(_fetch_costs_sync, days)
        _costs_cache[days] = (now, data)
        return data
    except Exception as exc:
        logger.error("LangSmith costs fetch error: %s", exc)
        # Return stale cache if available
        if days in _costs_cache:
            return _costs_cache[days][1]
        return JSONResponse({"error": str(exc)}, status_code=502)


# ── Translation API ───────────────────────────────────────

class TranslateRequest(BaseModel):
    text: str
    target_language: str = ""
    user_id: int = 0


@app.post("/translate")
async def translate_text(body: TranslateRequest):
    """Translate text using the pipeline's LLM + cache."""
    from .pipeline.nodes import get_llm
    from .pipeline.cache import get_cached, set_cached
    from .pipeline.prompts import get_translate_prompt

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
        lang = os.getenv("TARGET_LANGUAGE", "Hebrew")

    # Cache check
    cached = await get_cached(text, lang)
    if cached:
        if body.user_id:
            await insert_direct_translation(body.user_id, text, cached, lang, 0, True)
        return {"original": text, "translated": cached, "target_language": lang, "cache_hit": True}

    # LLM translation
    from langchain_core.messages import HumanMessage, SystemMessage
    t0 = time.monotonic()
    messages = [
        SystemMessage(content=get_translate_prompt(lang)),
        HumanMessage(content=text),
    ]
    response = await get_llm().ainvoke(messages)
    translation_ms = int((time.monotonic() - t0) * 1000)
    translated = response.content.strip()

    await set_cached(text, lang, translated)

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

    t0 = time.monotonic()
    analysis_type = "unknown"
    try:
        if msg_type in ("image", "photo"):
            analysis_type = "image"
            result_text = await analyze_image(content_bytes, content_type, target_lang)
        elif msg_type in ("audio", "voice"):
            analysis_type = "audio"
            result_text = await transcribe_audio(content_bytes, filename, target_lang)
        elif msg_type == "document":
            analysis_type = "document"
            result_text = await analyze_document(content_bytes, filename, content_type, target_lang)
        else:
            return JSONResponse({"error": f"unsupported media type: {msg_type}"}, status_code=400)

        processing_ms = int((time.monotonic() - t0) * 1000)
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
    # Resolve target language
    from .db import get_pool
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT target_language FROM users WHERE tg_user_id = $1", user_id,
    )
    target_lang = (row["target_language"] if row and row["target_language"] else "") or os.getenv("TARGET_LANGUAGE", "Hebrew")

    content_bytes = await file.read()

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


async def consume_loop():
    r = aioredis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
    )

    logger.info("Consumer loop started — waiting for messages:in")

    while True:
        try:
            result = await r.brpop("messages:in", timeout=5)
            if result is None:
                continue  # timeout — loop again

            _, raw = result
            payload = json.loads(raw)

            # Build initial state from the wa-service payload
            state = {
                "wa_message_id": payload.get("wa_message_id", ""),
                "wa_chat_id": payload.get("wa_chat_id", ""),
                "wa_chat_name": payload.get("wa_chat_name", ""),
                "user_id": int(payload.get("user_id", 0)),
                "sender_name": payload.get("sender_name", ""),
                "original_text": payload.get("body", ""),
                "message_type": payload.get("message_type", "text"),
                "media_s3_url": payload.get("media_s3_url"),
                "media_mime": payload.get("media_mime"),
                "media_filename": payload.get("media_filename"),
                "timestamp": payload.get("timestamp", 0),
                "from_me": payload.get("from_me", False),
                "is_edited": payload.get("is_edited", False),
                # Will be resolved by validate node
                "chat_pair_id": None,
                "tg_chat_id": None,
                "target_language": os.getenv("TARGET_LANGUAGE", "Hebrew"),
                "translated_text": None,
                "translation_ms": None,
                "cache_hit": False,
                "formatted_text": None,
                "delivery_status": "pending",
                "error": None,
            }

            # ── DB dedup: skip if already delivered ──
            wa_msg_id = state["wa_message_id"]
            if wa_msg_id:
                pool = await get_pool()
                existing = await pool.fetchval(
                    "SELECT delivery_status FROM message_events WHERE wa_message_id = $1",
                    wa_msg_id,
                )
                if existing == "delivered":
                    _counter["skipped"] += 1
                    logger.info("Dedup skip: %s already delivered", wa_msg_id)
                    continue

            msg_id = state["wa_message_id"][:12]
            sender = state["sender_name"] or "unknown"
            text_preview = (state["original_text"] or "")[:60]

            emit("message_received", {
                "msg_id": msg_id,
                "sender": sender,
                "text": text_preview,
                "chat": state["wa_chat_name"],
            })

            try:
                final_state = None
                t0 = time.monotonic()

                async for chunk in pipeline.astream(state, stream_mode="updates"):
                    for node_name, node_output in chunk.items():
                        elapsed = int((time.monotonic() - t0) * 1000)
                        evt = {
                            "msg_id": msg_id,
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
                        "total_ms": total_ms,
                        "cache_hit": final_state.get("cache_hit"),
                    })
                    logger.info(
                        "Delivered %s (lang=%s, cache=%s, ms=%s)",
                        state["wa_message_id"],
                        final_state.get("target_language"),
                        final_state.get("cache_hit"),
                        final_state.get("translation_ms"),
                    )
                elif final_state and final_state.get("delivery_status") == "skipped":
                    _counter["skipped"] += 1
                    emit("message_skipped", {
                        "msg_id": msg_id,
                        "error": final_state.get("error", "no_chat_pair"),
                    })
                    logger.debug(
                        "Skipped %s: %s",
                        state["wa_message_id"],
                        final_state.get("error"),
                    )
                else:
                    _counter["failed"] += 1
                    emit("message_failed", {
                        "msg_id": msg_id,
                        "error": final_state.get("error") if final_state else "no_output",
                    })
                    logger.warning(
                        "Failed %s: %s",
                        state["wa_message_id"],
                        final_state.get("error") if final_state else "no_output",
                    )
            except Exception as exc:
                _counter["failed"] += 1
                emit("message_failed", {"msg_id": msg_id, "error": str(exc)})
                logger.error("Pipeline error for %s: %s", state.get("wa_message_id"), exc)

        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON in messages:in: %s", exc)
        except Exception as exc:
            logger.error("Consumer loop error: %s", exc)
            await asyncio.sleep(2)


# ── Dashboard HTML (loaded from external file) ──────────

_DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")
with open(_DASHBOARD_PATH) as _f:
    _dashboard_html = _f.read()


# ── Startup ───────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PROCESSOR_PORT", 8000))
    uvicorn.run("src.main:app", host="0.0.0.0", port=port, reload=False)
