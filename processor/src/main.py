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
from fastapi import FastAPI, Query, Path
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from .pipeline.events import emit, subscribe, unsubscribe
from .pipeline.graph import pipeline

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
    return {"processed": _counter["processed"], "failed": _counter["failed"]}


_counter = {"processed": 0, "failed": 0}

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
    return DASHBOARD_HTML


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
             where cp2.user_id = u.id) as last_msg
        from users u
        where u.is_active = true
        order by delivered desc
    """)
    return [dict(r) for r in rows]


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


# ── Dashboard HTML ───────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Bridge v2 Pipeline</title>
<style>
  body { background:#bbb; color:#111; font:18px/1.5 monospace; margin:0; padding:20px; }
  pre { margin:0; overflow:hidden; }
  .wrap { display:flex; gap:0; height:calc(100vh - 40px); }
  .left, .right { width:600px; min-width:600px; overflow:hidden; }
  .left { border-right:2px solid #888; padding-right:16px; }
  .right { padding-left:16px; }
  pre { overflow:hidden; }
  .g { color:#060; } .r { color:#900; } .y { color:#860; }
  .c { color:#068; } .w { color:#000; } .d { color:#666; }
  .reports { margin-top:20px; padding:20px; }
  .reports pre { white-space:pre-wrap; }
  .date-nav { cursor:pointer; user-select:none; }
  .date-nav:hover { color:#068; }
  .bl-btn { cursor:pointer; padding:0 4px; user-select:none; }
  .bl-btn:hover { text-decoration:underline; }
</style>
</head>
<body>
<div class="wrap">
  <pre id="left"></pre>
  <pre id="right"></pre>
</div>
<div class="reports">
  <pre id="reports"></pre>
</div>
<div class="reports">
  <pre id="backlog"></pre>
</div>
<script>
const NN = ['validate','translate','format','deliver'];
const W = 10;
let ns={}, stats={ok:0,fail:0,lat:[]}, conn=false, logs=[], msg=null, users=[], costsData=null;

function reset() { ns={}; NN.forEach(n=>ns[n]={s:'idle',d:'',info:[]}); msg=null; }
reset();

function pad(s,w) { s=String(s); return s.length>=w ? s.substring(0,w) : s+' '.repeat(w-s.length); }
function c(cl,t) { return '<span class="'+cl+'">'+t+'</span>'; }

function colWidth(id) {
  const el=document.getElementById(id);
  if(!el) return 55;
  // measure char width using body font
  const m=document.createElement('pre');
  m.style.cssText='position:absolute;visibility:hidden;white-space:pre;font:inherit';
  m.textContent='X'.repeat(200);
  document.body.appendChild(m);
  const cw=m.offsetWidth/200;
  document.body.removeChild(m);
  // use parent container width (the .left/.right div)
  const pw = el.parentElement ? el.parentElement.clientWidth : el.clientWidth;
  const padL = parseFloat(getComputedStyle(el.parentElement||el).paddingLeft)||0;
  const padR = parseFloat(getComputedStyle(el.parentElement||el).paddingRight)||0;
  return Math.floor((pw - padL - padR) / cw);
}

let LW=0, RW=0, measured=false;
function measure() {
  if(measured) return;
  LW=colWidth('left'); RW=colWidth('right');
  const mx=Math.max(LW,RW);
  LW=mx; RW=mx;
  if(mx>10) measured=true;
}

function renderLeft() {
  let o = '';
  o += c('c','Bridge v2 Pipeline') + '\n';
  o += c('d','='.repeat(LW)) + '\n';

  const dot = conn ? c('g','[connected]') : c('r','[disconnected]');
  const avg = stats.lat.length ? Math.round(stats.lat.reduce((a,b)=>a+b,0)/stats.lat.length)+'ms' : '--';
  const total = stats.ok + stats.fail;
  o += ' '+dot+'  ok:'+c('g',stats.ok)+'  fail:'+c('r',stats.fail)+'  avg:'+c('c',avg)+'\n';
  if (total > 0) {
    const barW = LW - 2;
    const okW = Math.round(stats.ok / total * barW);
    const failW = barW - okW;
    o += ' ' + c('g','\u2588'.repeat(okW)) + c('r','\u2588'.repeat(failW)) + '\n';
  }
  o += c('d','-'.repeat(LW)) + '\n';

  if (msg) {
    o += ' msg: '+c('w',msg.sender)+' '+c('d','"'+msg.text+'"')+'\n';
  } else {
    o += ' '+c('d','waiting for messages...')+'\n';
  }
  o += '\n';

  // vertical pipeline with details
  const BW = 14; // box inner width
  NN.forEach((n,idx) => {
    const st=ns[n]; let cl='d',mk=' ';
    if(st.s==='active'){cl='y';mk='*';}
    if(st.s==='done'){cl='g';mk='+';}
    if(st.s==='failed'){cl='r';mk='x';}
    if(st.s==='skipped'){cl='d';mk='-';}

    const bar = c(cl, ' +'+'-'.repeat(BW+2)+'+');
    const nm  = c(cl, ' | '+mk+' '+pad(n,BW-2)+' |');
    const dt  = c(cl, ' |   '+pad(st.d||'',BW-2)+' |');

    // info lines next to box
    const info = st.info || [];
    const lines = [bar, nm, dt, bar];
    for(let i=0;i<lines.length;i++){
      const detail = info[i] ? '  '+info[i] : '';
      o += lines[i] + detail + '\n';
    }

    // extra info lines beyond box height
    for(let i=lines.length;i<info.length;i++){
      o += ' '.repeat(BW+5) + '  ' + info[i] + '\n';
    }

    // arrow down
    if(idx < NN.length-1){
      const acl = st.s==='done'?'g':st.s==='failed'?'r':'d';
      o += c(acl,'        v') + '\n';
    }
  });
  document.getElementById('left').innerHTML=o;
}

function renderRight() {
  let o='';
  o+=c('c','Users')+'\n';
  o+=c('d','='.repeat(RW))+'\n';
  o+=c('d',' '+pad('user',16)+pad('wa',5)+pad('lang',8)+pad('pairs',6)+pad('ok',6)+pad('fail',6)+'last')+'\n';
  o+=c('d',' '+'-'.repeat(RW-2))+'\n';

  if(users.length===0){
    o+=' '+c('d','loading...')+'\n';
  } else {
    users.forEach(u=>{
      const nm=pad(u.tg_username||String(u.tg_user_id),16);
      const wa=u.wa_connected?c('g','on  '):c('d','off ');
      const lang=pad(u.target_language||'--',8);
      const pairs=pad(String(u.pairs),6);
      const ok=u.delivered>0?c('g',pad(String(u.delivered),6)):pad('0',6);
      const fail=u.failed>0?c('r',pad(String(u.failed),6)):pad('0',6);
      const last=u.last_msg?timeAgo(u.last_msg):c('d','never');
      o+=' '+nm+wa+' '+lang+pairs+ok+fail+last+'\n';
    });
  }

  o+='\n'+c('d','='.repeat(RW))+'\n';
  o+=c('c','Chat Pairs')+'\n';
  o+=c('d',' '+'-'.repeat(RW-2))+'\n';
  let totalPairs=0, activePairs=0;
  users.forEach(u=>{totalPairs+=u.pairs; if(u.wa_connected)activePairs+=u.pairs;});
  o+=' total pairs:  '+c('w',totalPairs)+'\n';
  o+=' active users: '+c('w',users.filter(u=>u.wa_connected).length)+'/'+users.length+'\n';
  const totalOk=users.reduce((a,u)=>a+u.delivered,0);
  const totalFail=users.reduce((a,u)=>a+u.failed,0);
  o+=' total msgs:   '+c('g',totalOk)+' ok  '+c('r',totalFail)+' fail\n';

  // ── Costs section ──
  o+='\n'+c('d','='.repeat(RW))+'\n';
  o+=c('c','Costs (7d)')+'\n';
  o+=c('d',' '+'-'.repeat(RW-2))+'\n';
  if(costsData){
    const d=costsData;
    o+=' total: '+c('w','$'+d.total_cost.toFixed(2))+'  ('+c('w',fmtK(d.total_tokens))+' tok, '+c('w',d.total_runs)+' calls)\n';
    const today=new Date().toISOString().slice(0,10);
    const yesterday=new Date(Date.now()-86400000).toISOString().slice(0,10);
    const todayD=d.by_day.find(x=>x.date===today);
    const yesterD=d.by_day.find(x=>x.date===yesterday);
    o+=' today: '+c('w','$'+(todayD?todayD.cost.toFixed(2):'0.00'));
    o+='  yesterday: '+c('w','$'+(yesterD?yesterD.cost.toFixed(2):'0.00'))+'\n';
    const avg=d.period_days>0?(d.total_cost/d.period_days):0;
    o+=' avg/day: '+c('w','$'+avg.toFixed(2))+'\n';
  } else {
    o+=' '+c('d','loading...')+'\n';
  }

  document.getElementById('right').innerHTML=o;
}

function fmtK(n){return n>=1000?Math.round(n/1000)+'K':String(n);}

function render() { measure(); renderLeft(); renderRight(); }

function log(text) {
  const h=new Date().toLocaleTimeString('en-GB',{hour12:false});
  logs.push(' '+c('d',h)+' '+text);
  if(logs.length>200) logs.shift();
}

function handle(e) {
  switch(e.type) {
    case 'message_received':
      reset();
      msg={sender:e.sender, text:(e.text||'(media)').substring(0,30)};
      ns.validate.s='active'; ns.validate.d='...';
      log(c('c','[IN]')+' '+c('w',e.sender)+' "'+msg.text+'"');
      break;
    case 'node_done': {
      const n=e.node;
      let d=e.elapsed_ms+'ms';
      if(n==='translate'&&e.cache_hit)d='cached';
      else if(n==='translate'&&e.translation_ms)d=e.translation_ms+'ms';
      if(e.error)d=e.error.substring(0,12);
      ns[n].s=e.error?'failed':'done'; ns[n].d=d;

      // rich info per node
      if(n==='validate'){
        ns[n].info=[
          c('d','pair: ')+c('w',e.chat_pair_id||'none'),
          c('d','tg:   ')+c('w',e.tg_chat_id||'--'),
          c('d','lang: ')+c('c',e.target_lang||'--'),
          c('d','type: ')+c('w',e.msg_type||'text'),
        ];
      } else if(n==='translate'){
        const src=(e.original||'').substring(0,LW-22);
        const dst=(e.translated||'').substring(0,LW-22);
        ns[n].info=[
          e.cache_hit?c('g','CACHE HIT'):c('y','LLM call ')+c('w',(e.translation_ms||0)+'ms'),
          c('d','src: ')+c('w',src),
          c('d','dst: ')+c('c',dst),
          '',
        ];
      } else if(n==='format'){
        ns[n].info=[
          c('d','len: ')+c('w',(e.text_len||0)+' chars'),
          c('d','preview:'),
          c('w',(e.preview||'').substring(0,LW-22)),
          '',
        ];
      } else if(n==='deliver'){
        const ok=e.delivery_status==='delivered';
        ns[n].info=[
          c('d','tg:     ')+c('w',e.tg_chat_id||'--'),
          c('d','status: ')+(ok?c('g','delivered'):c('r',e.delivery_status||'--')),
          c('d','total:  ')+c('w',e.elapsed_ms+'ms'),
          '',
        ];
      }

      const idx=NN.indexOf(n);
      if(e.status==='failed'&&n==='validate'){
        ns.translate.s='skipped';ns.translate.d='skip';
        ns.format.s='skipped';ns.format.d='skip';
        ns.deliver.s='active';ns.deliver.d='...';
      } else if(idx<NN.length-1&&!e.error){
        ns[NN[idx+1]].s='active';ns[NN[idx+1]].d='...';
      }
      const cl=e.error?'r':'y';
      log(c(cl,'['+pad(n.toUpperCase(),9)+']')+' '+d+(e.cache_hit?' (cache)':''));
      break;
    }
    case 'message_delivered':
      stats.ok++; stats.lat.push(e.total_ms);
      if(stats.lat.length>100) stats.lat.shift();
      log(c('g','[OK]       ')+' '+e.total_ms+'ms'+(e.cache_hit?' (cached)':''));
      setTimeout(()=>{reset();render();},4000);
      loadUsers();
      break;
    case 'message_failed':
      stats.fail++;
      log(c('r','[FAIL]     ')+' '+(e.error||'unknown'));
      setTimeout(()=>{reset();render();},4000);
      loadUsers();
      break;
  }
  render();
}

function connect() {
  const es=new EventSource('/events');
  es.onopen=()=>{conn=true;render();};
  es.onmessage=(e)=>{try{handle(JSON.parse(e.data))}catch(x){}};
  es.onerror=()=>{conn=false;render();es.close();setTimeout(connect,2000);};
}

function timeAgo(iso) {
  const s=Math.floor((Date.now()-new Date(iso).getTime())/1000);
  if(s<60)return s+'s ago';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}

function loadUsers() {
  fetch('/api/stats').then(r=>r.json()).then(d=>{users=d;render();}).catch(()=>{});
}

function loadCosts() {
  fetch('/api/costs?days=7').then(r=>r.json()).then(d=>{costsData=d;render();}).catch(()=>{});
}

// ── Reports section ──────────────────────────────────────
let reportData=null, reportDate='', reportDates=[];

function loadReports(dt) {
  const q=dt?'?date='+dt:'';
  fetch('/api/reports'+q).then(r=>r.json()).then(d=>{
    reportData=d; reportDate=d.date; reportDates=d.dates||[];
    renderReports();
  }).catch(()=>{});
}

function renderReports() {
  if(!reportData){document.getElementById('reports').innerHTML=c('d','Loading reports...');return;}
  let o='';
  const W=120;
  o+=c('d','='.repeat(W))+'\n';
  o+=c('c','Analytics Reports')+'\n';
  o+=c('d','='.repeat(W))+'\n\n';

  // Date navigation
  const idx=reportDates.indexOf(reportDate);
  const prev=idx<reportDates.length-1?reportDates[idx+1]:'';
  const next=idx>0?reportDates[idx-1]:'';
  const prevBtn=prev?'<span class="date-nav" onclick="loadReports(\''+prev+'\')">\u25C0 '+prev+'</span>':'          ';
  const nextBtn=next?'<span class="date-nav" onclick="loadReports(\''+next+'\')">'+next+' \u25B6</span>':'';
  o+=' '+prevBtn+' | '+c('w',reportDate)+' | '+nextBtn+'\n\n';

  // Nightly Problems
  o+=c('c','\uD83D\uDD0D Nightly Problems')+'\n';
  o+=c('d','-'.repeat(W))+'\n';
  const p=reportData.problems;
  if(p.summary){
    const s=p.summary.stats||p.summary;
    o+=' total: '+c('w',s.total_messages??'—');
    o+='  delivered: '+c('g',s.delivered??'—');
    o+='  failed: '+c('r',s.failed??'—');
    o+='  pending: '+c('y',s.pending??'—');
    const avg=s.avg_translation_ms;
    o+='  avg_ms: '+c('c',avg?Math.round(avg):'—');
    o+='  slow(>3s): '+c('y',s.slow_translations??'—');
    if(s.mapped_total!=null) o+='  mapped: '+c('w',s.mapped_total)+' (fail:'+c('r',s.mapped_failed??0)+')';
    if(p.summary.mapped_failure_rate!=null) o+='  fail%: '+c(p.summary.mapped_failure_rate>10?'r':'y',p.summary.mapped_failure_rate+'%');
    o+='\n';
    if(p.summary.issues_total!=null) o+=' issues: '+c('r',p.summary.issues_critical??0)+' crit  '+c('y',p.summary.issues_warning??0)+' warn  '+c('d',p.summary.issues_info??0)+' info\n';
    o+='\n';
  } else {
    o+=' '+c('d','No problems data for this date')+'\n\n';
  }

  if(p.issues&&p.issues.length>0){
    const crit=p.issues.filter(i=>i.severity==='critical');
    const warn=p.issues.filter(i=>i.severity==='warning');
    const info=p.issues.filter(i=>i.severity==='info');
    function renderIssues(list,label,cl){
      if(!list.length)return;
      o+=' '+c(cl,label+' ('+list.length+')')+'\n';
      list.forEach(i=>{
        const ack=i.acknowledged?' [ack]':'';
        o+='   '+c(cl,'\u2022')+' '+c('w',esc(i.title))+c('d',ack)+'\n';
        if(i.description) o+='     '+c('d',esc(i.description).substring(0,W-6))+'\n';
        if(i.suggested_fix) o+='     fix: '+c('c',esc(i.suggested_fix).substring(0,W-10))+'\n';
      });
      o+='\n';
    }
    renderIssues(crit,'CRITICAL','r');
    renderIssues(warn,'WARNING','y');
    renderIssues(info,'INFO','d');
  } else if(p.summary) {
    o+=' '+c('g','No issues detected')+'\n\n';
  }

  // Translation Quality
  o+=c('c','\uD83D\uDCCA Translation Quality')+'\n';
  o+=c('d','-'.repeat(W))+'\n';
  const q=reportData.quality;
  if(q.summary){
    const qs=q.summary;
    const avg=qs.avg_scores||{};
    o+=' samples: '+c('w',qs.samples_evaluated??'—');
    o+='  quality: '+c('c',avg.quality??'—');
    o+='  accuracy: '+c('c',avg.accuracy??'—');
    o+='  naturalness: '+c('c',avg.naturalness??'—')+'\n';

    if(qs.issue_counts&&Object.keys(qs.issue_counts).length>0){
      o+=' issues: ';
      const entries=Object.entries(qs.issue_counts).sort((a,b)=>b[1]-a[1]);
      o+=entries.map(([k,v])=>c('y',k)+':'+v).join('  ')+'\n';
    }
    o+='\n';
  } else {
    o+=' '+c('d','No quality data for this date')+'\n\n';
  }

  if(q.suggestions&&q.suggestions.length>0){
    o+=' '+c('w','Prompt suggestions ('+q.suggestions.length+')')+'\n';
    q.suggestions.forEach((s,i)=>{
      const st=s.status||'pending';
      const stc=st==='applied'?'g':st==='rejected'?'r':'y';
      o+='   '+(i+1)+'. '+c('w',esc(s.suggestion))+' '+c(stc,'['+st+']')+'\n';
      if(s.rationale) o+='      '+c('d',esc(s.rationale))+'\n';
    });
  }

  document.getElementById('reports').innerHTML=o;
}

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// ── Backlog section ─────────────────────────────────────
let backlogData=[];

function loadBacklog() {
  fetch('/api/backlog').then(r=>r.json()).then(d=>{backlogData=d;renderBacklog();}).catch(()=>{});
}

function updateBacklog(id,status) {
  fetch('/api/backlog/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:status})})
    .then(r=>r.json()).then(()=>{loadBacklog();}).catch(()=>{});
}

function renderBacklog() {
  const el=document.getElementById('backlog');
  if(!el) return;
  const W=120;
  let o='';
  o+=c('d','='.repeat(W))+'\n';
  o+=c('r','\uD83D\uDEA8 Critical Backlog')+'\n';
  o+=c('d','='.repeat(W))+'\n\n';

  if(backlogData.length===0){
    o+=' '+c('g','No open critical issues')+'\n';
  } else {
    o+=' '+c('w','Open issues: '+backlogData.length)+'\n\n';
    backlogData.forEach(i=>{
      const dt=i.source_run_date||'—';
      o+='  '+c('r','\u2022')+' '+c('w','#'+i.id)+' '+c('d','['+dt+']')+' '+c('w',esc(i.title))+'\n';
      if(i.description) o+='    '+c('d',esc(i.description).substring(0,W-6))+'\n';
      if(i.suggested_fix) o+='    fix: '+c('c',esc(i.suggested_fix).substring(0,W-10))+'\n';
      o+='    '+c('d','cat: '+esc(i.category));
      o+='  <span class="bl-btn g" onclick="updateBacklog('+i.id+',\'resolved\')">[resolve]</span>';
      o+='  <span class="bl-btn y" onclick="updateBacklog('+i.id+',\'wontfix\')">[wontfix]</span>\n\n';
    });
  }
  el.innerHTML=o;
}

window.addEventListener('resize',()=>{measured=false;render();});
// ensure DOM is ready before measuring
requestAnimationFrame(()=>{
  measure();
  connect();
  loadUsers();
  loadCosts();
  loadReports();
  loadBacklog();
  setInterval(loadUsers,15000);
  setInterval(loadCosts,300000);
  setInterval(loadBacklog,30000);
  setInterval(()=>{if(reportDate===new Date().toISOString().slice(0,10))loadReports();},60000);
  render();
});
</script>
</body>
</html>
"""


# ── Startup ───────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PROCESSOR_PORT", 8000))
    uvicorn.run("src.main:app", host="0.0.0.0", port=port, reload=False)
