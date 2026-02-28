# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Рабочие правила

### Перед изменениями — исследуй
Перед тем как менять код, используй task agent для исследования всех затронутых файлов. Составь карту функций и вызовов, потом предложи план. Не пиши код вслепую.

### После бэкенд-изменений — рестарт
После изменений серверных файлов всегда рестартуй сервис. Не предполагай, что старый процесс подхватит изменения.

### CSS — проверяй специфичность
При CSS-изменениях всегда проверяй конфликты специфичности с существующими стилями. Если CSS-фикс не работает — скорее всего проблема в специфичности, а не в логике.

### UI — подтверждай перед кодом
Перед любыми UI-изменениями сначала подтверди у пользователя:
1. Какой именно элемент/компонент будет изменён
2. Какое будет поведение
3. Как это будет выглядеть
Жди OK перед написанием кода. НЕ добавляй на проекты в сайдбаре, если сказано "на продукты в гриде".

### Дебаг 404/500
1. Проверь что сервер запущен и был рестартнут после изменений
2. Проверь что миграции БД применены
3. Проверь пути к статическим файлам
НЕ гоняй теории про URL resolution пока не проверил эти базовые вещи.

### Массовые правки — Write вместо Edit
Если нужно больше 5 правок в одном файле — используй Write для перезаписи целиком вместо последовательных Edit вызовов. Это быстрее и избегает промежуточных сломанных состояний.

## Обзор проекта

Bridge v2 — WhatsApp→Telegram мост с AI-переводом. Монорепозиторий из 4 сервисов, оркестрированных через Docker Compose. Стек: JavaScript (wa-service), Python (bot, processor, analytics). TypeScript НЕ используется.

| Сервис | Стек | Порт |
|--------|------|------|
| `wa-service` | Node.js 20, whatsapp-web.js, Express, ioredis, pg | 3000 |
| `processor` | Python 3.12, FastAPI, LangGraph, asyncpg | 8000 |
| `bot` | Python 3.12, python-telegram-bot, asyncpg | 8001 |
| `analytics` | Python 3.12, Prefect, BigQuery, OpenAI | 4200 |

## Команды

```bash
# Локальная разработка
make up            # docker compose up -d (все 4 сервиса + redis + postgres + minio)
make down          # остановка
make logs          # логи всех сервисов
make logs-bot      # логи конкретного сервиса (logs-wa, logs-processor, logs-bot, logs-analytics)
make health        # curl /health всех сервисов

# Тесты и линтинг
make test          # pytest processor/tests/ + bot/tests/ (asyncio_mode=auto)
make lint          # ruff check processor/src/ bot/src/ analytics/flows/
make format        # ruff format

# Запуск одного теста
cd processor && python -m pytest tests/test_pipeline.py::test_validate_node -v
cd bot && python -m pytest tests/test_onboarding.py -v

# База данных
make db-shell      # psql -U bridge -d bridge
make migrate       # применить migrations (обычно не нужно — initdb.d автоматически)
```

## Архитектура

### Поток сообщений

```
WhatsApp (wa-service)
  └─ handleIncomingMessage()
       ├─ dedup: Redis SET NX "dedup:msg:{wa_message_id}" EX 300
       ├─ uploadMedia() → S3/MinIO
       └─ redis.LPUSH("messages:in", payload)
                 ↓
Processor consumer loop (BRPOP "messages:in")
  └─ LangGraph StateGraph:
       validate → [conditional] → translate → format → deliver
                                                          └─ httpx → Telegram API
                                                          └─ asyncpg → message_events
       validate (no pair) → deliver (failed, записать в БД)
       validate (no text) → format → deliver (skip translation)
```

### Mini App — хаб управления

```
/start → кнопка "Open Mini App" (всегда одинаково)
  ↓
Mini App GET /chat-pairs/:userId
  ├─ wa_connected=false → QR-экран → после подключения → Home
  ├─ Нет пар → QR → WA группы → Add bot → TG группа → sendData → Home
  └─ Есть пары → Home:
      ├─ Карточки пар (pause/resume/delete)
      ├─ "Add new pair" → WA группы → Add bot → TG группа
      └─ Инструкция-подсказка
```

wa-service endpoints для Mini App:
- `GET /chat-pairs/:userId` — список пар + wa_connected
- `PATCH /chat-pairs/:pairId` — pause/resume
- `DELETE /chat-pairs/:pairId` — удаление
- `GET /tg-groups/:userId` — TG группы из Redis (polling)
- `POST /connect/:userId`, `GET /status/:userId` — WA подключение

Mini App: `wa-service/public/miniapp.html` (Vanilla JS, Telegram WebApp SDK, требует HTTPS).
wa-service DB: `wa-service/src/db.js` (pg Pool → Postgres, chat-pairs CRUD).

### Межсервисное взаимодействие

```
wa-service → Redis LPUSH "messages:in"                → processor (BRPOP)
wa-service → Redis PUBLISH "onboarding:qr_scanned:*"  → bot (PSUBSCRIBE, daemon thread)
bot        → HTTP GET/POST wa-service /connect/, /status/  (onboarding)
processor  → HTTP POST Telegram API sendMessage        (доставка, напрямую без bot)
analytics  → HTTP GET wa-service /health               (мониторинг)
analytics  → Telegram API sendMessage                  (алерты админам)
```

Processor и bot НЕ общаются напрямую — оба независимо ходят в PostgreSQL и Telegram API.

### Ключевые Redis-ключи

| Ключ | Тип | TTL | Назначение |
|------|-----|-----|------------|
| `messages:in` | List | — | Очередь сообщений wa→processor |
| `dedup:msg:{wa_message_id}` | String | 5m | Дедупликация (SET NX) |
| `onboarding:qr_scanned:{userId}` | Pub/Sub | — | WA подключён |
| `chat_pairs:user:{uid}:chat:{chatId}` | String | 1h | Кэш пар чатов |
| `translation:{lang}:{sha256(text)}` | String | 24h | Кэш переводов |
| `bot:user_groups:{userId}` | Hash | 1h | TG группы пользователя (для Mini App polling) |

## Критические особенности

### wa-service — только 1 инстанс
whatsapp-web.js не поддерживает кластеризацию. Всегда `replicas: 1`. WA-сессии хранятся в `.wwebjs_auth/` (Docker volume `wa_sessions`). При пересоздании контейнера может остаться Chromium `SingletonLock` — удалить через volume:
```bash
docker compose stop wa-service
docker run --rm -v bridge-v2_wa_sessions:/data alpine find /data -name SingletonLock -delete
docker compose start wa-service
```

### Processor — LangGraph pipeline
Граф в `processor/src/pipeline/graph.py`. Узлы в `nodes.py`. Условная маршрутизация: нет пары → сразу deliver(failed), нет текста → skip translate. Промпт версионирован: `PROMPT_VERSION` в `prompts.py`. Fallback to admins при отсутствии пары работает только для admin users (ADMIN_TG_IDS).

Pipeline использует `astream(state, stream_mode="updates")` — каждый узел эмитит события через `pipeline/events.py` (in-memory event bus с asyncio.Queue, deque history 50 events). SSE endpoint `/events` и ASCII-дашборд `/dashboard`.

Дашборд processor'а (`/dashboard`): realtime pipeline + users + analytics reports. API:
- `GET /api/stats` — статистика по пользователям (delivered/failed/avg_ms)
- `GET /api/reports?date=YYYY-MM-DD` — nightly problems + translation quality из БД (default: today)

### Bot — смешанный sync/async
python-telegram-bot в asyncio. Redis subscriber (`redis_sub.py`) — отдельный daemon thread. Коммуникация: `asyncio.run_coroutine_threadsafe(coro, bot_loop)`, где `bot_loop` захватывается в `main.py:post_init`.

### Онбординг-состояния
`idle → qr_pending → wa_connected → linking → done`
Хранятся в `onboarding_sessions`. Мигрированные из v1 получают `done`. Bot /start всегда показывает описание + кнопку Mini App (независимо от состояния). При добавлении бота как admin в TG группу — автосообщение с кнопкой /add.

### Analytics — Prefect flows
5 flow с cron-расписанием в `analytics/flows/`. Оба store_results делают UPSERT для `nightly_analysis_runs`, но дочерние таблицы (`detected_issues`, `translation_evaluations`, `prompt_suggestions`) — DELETE + INSERT при повторном запуске за тот же день.

| Flow | Cron | Назначение |
|------|------|------------|
| `wa-health-check` | `*/15 * * * *` | Проверка wa-service |
| `daily-cleanup` | `0 3 * * *` | Очистка старых данных |
| `nightly-problems` | `0 4 * * *` | o3-mini анализ проблем за 24h |
| `translation-quality` | `30 4 * * *` | o3-mini оценка качества переводов |
| `weekly-report` | `0 5 * * 1` | Еженедельный отчёт |

## База данных

PostgreSQL 16. Все операции через `asyncpg` без ORM. Пулы соединений создаются при старте.

**001_initial_schema.sql** (4 таблицы):
- `users` — tg_user_id unique, wa_connected, target_language, is_admin
- `chat_pairs` — unique(user_id, wa_chat_id, tg_chat_id), status: active/paused
- `message_events` — wa_message_id unique, ON CONFLICT DO UPDATE, delivery_status
- `onboarding_sessions` — user_id PK, state FSM

**002_llm_analysis.sql** (4 таблицы для analytics):
- `nightly_analysis_runs` — unique(run_date, flow_type), jsonb summary
- `detected_issues` — severity: critical/warning/info, acknowledged flag
- `translation_evaluations` — quality/accuracy/naturalness scores 1-5
- `prompt_suggestions` — status: pending/applied/rejected

## Production (VPS)

- **Сервер:** Ubuntu 24.04, 3.8GB RAM — `ssh bridge` (deploy@83.217.222.126)
- **Деплой (на VPS нет git repo):**
  ```bash
  rsync -avz --exclude '.git' --exclude 'node_modules' --exclude '__pycache__' --exclude '.wwebjs_auth' --exclude '.env' ./ bridge:~/bridge-v2/
  ssh bridge "cd ~/bridge-v2 && docker compose build <сервис> && docker compose up -d <сервис>"
  ssh bridge "curl -s http://localhost:3000/health"  # wa-service
  ssh bridge "curl -s http://localhost:8000/health"  # processor
  ```
- **CI/CD:** GitHub Actions — `ci.yml` (lint/test/build на push/PR)
- **Безопасность:** SSH по ключу, UFW (22/80/443), fail2ban
- **LangSmith:** `LANGCHAIN_TRACING_V2=true`, проект `bridge-v2-prod`
- **Формат Telegram-сообщений:** `*Sender*\n\noriginal\n\ntranslated`
