# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## RULES

- ИССЛЕДУЙ перед изменениями: agent для чтения затронутых файлов → карта функций → план. Не пиши код вслепую.
- РЕСТАРТУЙ сервис после бэкенд-изменений. Старый процесс НЕ подхватит.
- РЕСТАРТУЙ nginx после `docker compose up -d` — контейнер получает новый IP, nginx кэширует старый → 502.
- UI: подтверди у пользователя ЧТО/КАК/ГДЕ меняется, жди OK. НЕ добавляй "на проекты в сайдбаре" если сказано "на продукты в гриде".
- CSS: проблема = специфичность, не логика. Проверяй конфликты.
- Дебаг 404/500: (1) сервер запущен? (2) миграции применены? (3) пути к статике? — только потом теории.
- Дебаг multi-service: трейс `wa-service → Redis → processor → Telegram API`, `bot → PostgreSQL`. Правишь конфиг в одном сервисе — проверь все остальные.
- `>5` правок в файле → `Write` целиком, не `Edit` по одной.
- `/test` перед каждым коммитом. Исключение: только docs/CLAUDE.md.
- "деплой"/"задеплой" → `/deploy` немедленно, без plan mode.
- Новые константы → в `config.py`/`config.js`, НЕ хардкодить.
- HTTP в bot handlers → `bot/src/utils/http_client.py`, НЕ создавать `httpx.AsyncClient` напрямую.

## SKILLS

| Команда | Действие |
|---------|----------|
| `/test` | Jest (wa-service) + pytest (processor + bot) |
| `/deploy [сервис...]` | test → rsync → migrate → build → up -d → restart nginx → health |
| `/commit [msg]` | add -A → commit → push (msg генерируется если не передан) |
| `/weekly-improve` | weekly_insights из БД → classify → plan → code → test → deploy → broadcast |

## PROJECT

Bridge v2 — WhatsApp→Telegram мост с AI-переводом. Монорепо, 4 сервиса, Docker Compose. JS (wa-service), Python (bot, processor, analytics). НЕ TypeScript. Default lang: Hebrew.

| Сервис | Стек | Порт | Примечание |
|--------|------|------|------------|
| wa-service | Node 20, whatsapp-web.js, Express, ioredis, pg | 3000 | expose-only, не published |
| processor | Python 3.12, FastAPI, LangGraph, asyncpg | 8000 | published |
| bot | Python 3.12, python-telegram-bot, asyncpg | 8001 | polling, нет health endpoint |
| analytics | Python 3.12, Prefect, OpenAI | 4200 | — |

## COMMANDS

```bash
make up / down / restart / logs / logs-bot / health
make test          # Jest + pytest (asyncio_mode=auto)
make lint          # ruff check
make format        # ruff format
make db-shell      # psql -U bridge -d bridge

# Один тест
cd wa-service && npm test
cd processor && python -m pytest tests/test_pipeline.py::test_validate_node -v
cd bot && python -m pytest tests/test_onboarding.py -v

cd wa-service && npm run dev   # node --watch (hot reload)
```

## DATA FLOW

```
WhatsApp msg → wa-service handleIncomingMessage()
  ├─ dedup: SET NX "dedup:msg:{id}" EX 300
  ├─ uploadMedia() → S3/MinIO
  └─ LPUSH "messages:in"
       ↓
processor consume_loop (BRPOP)
  └─ LangGraph: validate → translate → format → deliver
       ├─ deliver OK → message_events (delivered)
       ├─ no pair → message_events (skipped)
       ├─ no text → skip translate → format → deliver
       └─ exception → LPUSH "messages:dlq"
```

```
wa-service → Redis LPUSH "messages:in"               → processor (BRPOP)
wa-service → Redis PUBLISH "onboarding:qr_scanned:*" → bot (PSUBSCRIBE, daemon thread)
bot        → HTTP wa-service /connect/, /status/        (via http_client.py with retry)
processor  → HTTP Telegram API send*                    (напрямую, без bot)
analytics  → HTTP wa-service /health                    (мониторинг)
```

processor и bot НЕ общаются — оба независимо → PostgreSQL + Telegram API.

## KEY FILES

### Config (все константы здесь, не хардкодить)
- `processor/src/config.py` — env vars processor (timeouts, TTLs, Redis, DB, Telegram, alerting)
- `wa-service/src/config.js` — env vars wa-service (Redis, DB, WA client, media, cache)

### Pipeline
- `processor/src/main.py` — FastAPI + consume_loop + все API endpoints
- `processor/src/pipeline/graph.py` — LangGraph StateGraph
- `processor/src/pipeline/nodes.py` — validate/translate/format/deliver
- `processor/src/pipeline/prompts.py` — prompt + PROMPT_VERSION + register_prompt()
- `processor/src/pipeline/cache.py` — Redis translation/profile/media cache
- `processor/src/pipeline/events.py` — in-memory event bus (asyncio.Queue)
- `processor/src/telegram_sender.py` — raw httpx → Telegram API (sendMessage/Photo/Video/Audio/Document)
- `processor/src/media_analyzer.py` — OpenAI GPT-4.1-mini vision + Whisper + PyPDF
- `processor/src/feature_flags.py` — DB → Redis cache 60s → env fallback
- `processor/src/db.py` — asyncpg pool (command_timeout=10)

### wa-service
- `wa-service/src/whatsapp-client.js` — WA client manager (connect/disconnect/reconnect/health)
- `wa-service/src/redis-publisher.js` — LPUSH messages:in + dedup + pub/sub
- `wa-service/src/media-handler.js` — S3 upload
- `wa-service/src/routes/index.js` — Express routes (Mini App API)
- `wa-service/src/db.js` — pg Pool (statement_timeout=10000)
- `wa-service/public/miniapp.html` — Vanilla JS Mini App (Telegram WebApp SDK, HTTPS)

### Bot
- `bot/src/main.py` — python-telegram-bot setup + post_init
- `bot/src/redis_sub.py` — daemon thread pub/sub (buffers events until bot ready)
- `bot/src/utils/http_client.py` — shared httpx.AsyncClient + retry (1x, 2s delay)
- `bot/src/onboarding/wizard.py` — /start + 5-step onboarding
- `bot/src/handlers/translate.py` — direct text translation + media analysis in DM
- `bot/src/handlers/analyze.py` — "Analyze" button callback
- `bot/src/handlers/chats.py` — /chats, /add, /pause, /resume, /done
- `bot/src/handlers/admin.py` — /users, /broadcast, /whitelist
- `bot/src/db.py` — asyncpg pool (command_timeout=10)

### Analytics
- `analytics/flows/` — 8 Prefect flows (local server mode, no Cloud)
- `analytics/flows/chat_context_builder.py` — daily glossary/members/tone → chat_profiles (VPS only)

## REDIS KEYS

| Key | Type | TTL | Use |
|-----|------|-----|-----|
| `messages:in` | List | — | WA→processor queue |
| `messages:dlq` | List | — | Dead-letter queue |
| `dedup:msg:{wa_message_id}` | String | 5m | Message dedup (SET NX) |
| `onboarding:qr_scanned:{userId}` | Pub/Sub | — | WA connected event |
| `chat_pairs:user:{uid}:chat:{chatId}` | String | 1h | Chat pairs cache |
| `translation:{lang}:{pair_id}:{sha256}` | String | 24h | Translation cache (per-pair) |
| `translation_global:{lang}:{sha256}` | String | 24h | Translation cache (no profile) |
| `chat_profile:{pair_id}` | String | 1h | Chat profile cache |
| `bot:user_groups:{userId}` | Hash | 1h | TG groups for Mini App |
| `ff:{flag_name}` | String | 60s | Feature flag cache |

## PROCESSOR API

| Method | Path | Feature flag | Purpose |
|--------|------|-------------|---------|
| GET | /health | — | Health check |
| GET | /metrics | — | Counters: processed/failed/skipped/dlq |
| GET | /events | — | SSE stream (pipeline events) |
| GET | /dashboard | — | HTML dashboard |
| GET | /api/config | — | Current config (no secrets) |
| GET | /api/stats | — | User stats |
| GET | /api/daily-stats | — | Today's counts |
| GET | /api/reports?date= | — | Nightly problems + quality |
| GET | /api/backlog | — | Open critical issues |
| PATCH | /api/backlog/{id} | — | Resolve/wontfix issue |
| GET | /api/dlq | — | DLQ messages (max 100) |
| POST | /api/dlq/retry | — | Retry all DLQ → messages:in |
| GET | /api/flags | — | Feature flags list |
| PATCH | /api/flags/{name} | — | Toggle flag `{"enabled": bool}` |
| GET | /api/costs?days= | — | LangSmith costs (15m cache) |
| GET | /api/profiles | — | Chat profiles with glossaries |
| POST | /translate | translation_enabled | Text translation |
| POST | /analyze | media_analysis_enabled | Media analysis by event_id |
| POST | /analyze-direct | media_analysis_enabled | Media analysis (file upload) |

## WA-SERVICE API

| Method | Path | Purpose |
|--------|------|---------|
| GET | /health | Health + activeClients + redis status |
| GET | /chat-pairs/:userId | Pairs list + wa_connected |
| PATCH | /chat-pairs/:pairId | Pause/resume |
| DELETE | /chat-pairs/:pairId | Delete pair |
| GET | /tg-groups/:userId | TG groups from Redis |
| POST | /connect/:userId | Start WA client |
| GET | /status/:userId | WA status + groups (15s timeout) |
| POST | /disconnect/:userId | Destroy WA client |
| POST | /reconnect/:userId | Recreate WA client |
| GET | /qr/image/:userId | PNG QR (202 if starting) |

## FEATURE FLAGS

DB table `feature_flags` → Redis cache `ff:{name}` (60s) → env var fallback.
Module: `processor/src/feature_flags.py`. API: `GET/PATCH /api/flags/{name}`.

| Flag | Controls |
|------|----------|
| translation_enabled | POST /translate |
| media_analysis_enabled | POST /analyze, /analyze-direct |
| direct_chat_enabled | (reserved) |
| admin_alerts_enabled | 401 + failure rate alerts to admins |

## DATABASE

PostgreSQL 16. asyncpg (processor, bot), psycopg2 (analytics). No ORM.

| Migration | Tables |
|-----------|--------|
| 001 | users, chat_pairs, message_events, onboarding_sessions |
| 002 | nightly_analysis_runs, detected_issues, translation_evaluations, prompt_suggestions |
| 003 | prompt_registry |
| 004 | issues_backlog |
| 005 | weekly_insights, analytics_changelog |
| 006 | delivery_status += 'skipped' |
| 007 | media_analysis, message_events += tg_message_id |
| 008 | direct_interactions |
| 009 | chat_profiles, chat_profile_history (VPS only) |
| 010 | daily_chat_summaries, chat_summary_schedule |
| 011 | feature_flags |

## ANALYTICS FLOWS

| Flow | Cron | Model |
|------|------|-------|
| wa-health-check | */15 * * * * | — |
| daily-cleanup | 0 3 * * * | — |
| nightly-problems | 0 4 * * * | gpt-4.1-mini |
| translation-quality | 30 4 * * * | gpt-4.1-mini |
| chat-context-builder | 0 5 * * * | gpt-4.1 + web_search |
| weekly-report | 0 5 * * 1 | o3 |
| daily-chat-summary | */30 * * * * | gpt-4.1-mini |

## ONBOARDING FSM

`idle → qr_pending → wa_connected → linking → done`
Table: onboarding_sessions. /start always shows Mini App button.

## CONSTRAINTS

- wa-service: 1 replica only (whatsapp-web.js). Sessions in `.wwebjs_auth/` volume. System Chromium `/usr/bin/chromium`. SingletonLock cleanup needed on container recreate.
- wa-service port 3000: expose-only, NOT published. Access via nginx.
- Media format: `*Sender*\n\noriginal\n\ntranslated`. Media sent natively (sendPhoto/etc), NOT in formatted_text.
- MinIO locally (9000/9001), bucket `bridge-media` auto-created via `infra/minio-init.sh`.
- Bot: polling-based, NO health endpoint. Mixed sync/async — Redis sub in daemon thread, `asyncio.run_coroutine_threadsafe` for cross-thread.
- QR events buffered in `redis_sub.py._pending_events` until bot ready, drained on `set_bot_app()`.

## PRODUCTION

- Domain: brdg.tools
- Server: Ubuntu 24.04, 3.8GB RAM, `ssh bridge` (deploy@83.217.222.126)
- Deploy dir: `/home/deploy/bridge-v2/` (NOT ~/bridge-v2/). `.env` only there.
- Deploy: rsync → migrate SQL → docker compose build+up → restart nginx → health check
- Health checks: processor `curl localhost:8000/health`, wa-service from inside container, bot via logs
- CI: GitHub Actions `ci.yml` (lint/test/build)
- Nginx: reverse proxy, basic auth for /dashboard /api/* /events, SSE proxy_buffering off
- SSL: certbot, auto-renewal cron 0 3 * * *
- Logs: json-file, max-size 10m, max-file 3
- LangSmith: LANGCHAIN_TRACING_V2=true, project bridge-v2-prod
