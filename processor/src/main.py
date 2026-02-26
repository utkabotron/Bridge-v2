"""Processor service entry point.

Starts two concurrent tasks:
1. FastAPI HTTP server (health + metrics endpoints)
2. Redis BRPOP consumer loop — pops messages from "messages:in" and
   runs them through the LangGraph pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI

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

# ── Redis consumer ────────────────────────────────────────

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

            try:
                final_state = await pipeline.ainvoke(state)
                if final_state.get("delivery_status") == "delivered":
                    _counter["processed"] += 1
                    logger.info(
                        "Delivered %s (lang=%s, cache=%s, ms=%s)",
                        state["wa_message_id"],
                        final_state.get("target_language"),
                        final_state.get("cache_hit"),
                        final_state.get("translation_ms"),
                    )
                else:
                    _counter["failed"] += 1
                    logger.warning(
                        "Failed %s: %s",
                        state["wa_message_id"],
                        final_state.get("error"),
                    )
            except Exception as exc:
                _counter["failed"] += 1
                logger.error("Pipeline error for %s: %s", state.get("wa_message_id"), exc)

        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON in messages:in: %s", exc)
        except Exception as exc:
            logger.error("Consumer loop error: %s", exc)
            await asyncio.sleep(2)


# ── Startup ───────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PROCESSOR_PORT", 8000))
    uvicorn.run("src.main:app", host="0.0.0.0", port=port, reload=False)
