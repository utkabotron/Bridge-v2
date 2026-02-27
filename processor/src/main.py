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
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from .pipeline.events import emit, subscribe, unsubscribe
from .pipeline.graph import pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
</style>
</head>
<body>
<div class="wrap">
  <pre id="left"></pre>
  <pre id="right"></pre>
</div>
<script>
const NN = ['validate','translate','format','deliver'];
const W = 10;
let ns={}, stats={ok:0,fail:0,lat:[]}, conn=false, logs=[], msg=null, users=[];

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

  document.getElementById('right').innerHTML=o;
}

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

window.addEventListener('resize',()=>{measured=false;render();});
// ensure DOM is ready before measuring
requestAnimationFrame(()=>{
  measure();
  connect();
  loadUsers();
  setInterval(loadUsers,15000);
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
