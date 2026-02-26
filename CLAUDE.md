# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Обзор проекта

Bridge v2 — портфолио-rebuild WhatsApp→Telegram моста с переводом. Монорепозиторий из 4 сервисов, оркестрированных через Docker Compose.

| Сервис | Стек | Порт |
|--------|------|------|
| `wa-service` | Node.js, whatsapp-web.js, Express, ioredis | 3000 |
| `processor` | Python, FastAPI, LangGraph, asyncpg | 8000 |
| `bot` | Python, python-telegram-bot, asyncpg | 8001 |
| `analytics` | Python, Prefect, BigQuery | 4200 |

## Команды

```bash
# Локальная разработка
make up            # docker compose up -d (все 4 сервиса + redis + postgres)
make down          # остановка
make logs          # логи всех сервисов
make logs-bot      # логи конкретного сервиса (wa, processor, bot, analytics)
make health        # curl /health всех сервисов

# Тесты и линтинг
make test          # pytest processor/tests/ + bot/tests/
make lint          # ruff check processor/src/ bot/src/ analytics/flows/
make format        # ruff format

# База данных
make db-shell      # psql -U bridge -d bridge
make migrate       # применить migrations (обычно не нужно — initdb.d делает это автоматически)

# Деплой в production (AWS ECS)
make deploy-processor
make deploy-bot
make deploy-analytics
# wa-service деплоится вручную (EFS-сессии требуют stop→start, не rolling update)
```

**Запуск одного теста:**
```bash
cd processor && python -m pytest tests/test_pipeline.py::test_validate_node -v
cd bot && python -m pytest tests/test_onboarding.py -v
```

## Архитектура

### Поток сообщений

```
WhatsApp (wa-service)
  └─ handleIncomingMessage()
       ├─ uploadMedia() → S3
       └─ redis.LPUSH("messages:in", payload)
                 ↓
Processor consumer loop (BRPOP "messages:in")
  └─ LangGraph StateGraph:
       validate → translate → format → deliver
                                           └─ httpx → Telegram API
                                           └─ asyncpg → message_events
```

### Онбординг пользователя

```
/start → POST /connect/:userId (wa-service)
       → QR page → пользователь сканирует
       → Redis PUB "onboarding:qr_scanned:{userId}"
       → bot/redis_sub.py (daemon thread) ловит событие
       → пользователь создаёт TG группу, добавляет бота
       → /add в группе → выбор WA группы → INSERT chat_pairs
```

### Ключевые Redis-ключи

| Ключ | Тип | TTL | Назначение |
|------|-----|-----|------------|
| `messages:in` | List | — | Очередь сообщений wa→processor |
| `onboarding:qr_scanned:{userId}` | Pub/Sub | — | WA подключён |
| `chat_pairs:user:{uid}:chat:{chatId}` | String | 1h | Кэш пар чатов |
| `translation:{lang}:{sha256(text)}` | String | 24h | Кэш переводов |

## Критические особенности

### wa-service — только 1 инстанс
whatsapp-web.js не поддерживает кластеризацию. В docker-compose и ECS всегда `replicas: 1`. WA-сессии хранятся в `.wwebjs_auth/` (локально) или EFS (production). Деплой wa-service через force-new-deployment **ломает активные сессии** — нужен stop→start вручную.

### Processor — LangGraph pipeline
Граф в `processor/src/pipeline/graph.py`. Узлы в `nodes.py`. Если у сообщения нет активной пары чатов — узел `validate` устанавливает `delivery_status=failed` и граф пропускает translate/format, переходя сразу к `deliver` (который записывает failed в БД). Промпт версионирован: `PROMPT_VERSION` в `prompts.py`.

### Bot — смешанный sync/async
python-telegram-bot работает в asyncio. Redis subscriber (`redis_sub.py`) — отдельный daemon thread. Коммуникация между ними через `asyncio.run_coroutine_threadsafe(coro, bot_loop)`, где `bot_loop` захватывается при старте в `main.py`.

### Онбординг-состояния
`idle → qr_pending → wa_connected → linking → done`
Хранятся в таблице `onboarding_sessions`. Мигрированные пользователи из v1 получают state=`done` и при `/start` видят "welcome back".

## База данных

4 таблицы в `infra/migrations/001_initial_schema.sql`:
- `users` — Telegram пользователи (bigserial PK, tg_user_id unique)
- `chat_pairs` — пары WA↔TG (unique: user_id + wa_chat_id + tg_chat_id)
- `message_events` — лог сообщений (wa_message_id unique)
- `onboarding_sessions` — состояние онбординга (user_id PK)

Все операции с БД через `asyncpg` без ORM. Пулы соединений создаются при старте сервиса и переиспользуются.

## Production (AWS)

- **Оркестрация:** ECS Fargate
- **БД:** RDS PostgreSQL t3.micro
- **Кэш:** ElastiCache Redis t3.micro
- **Хранилище сессий:** EFS (смонтирован в wa-service)
- **Медиа:** S3
- **CI/CD:** GitHub Actions → ECR → ECS (`.github/workflows/`)
- **Аналитика:** Prefect Cloud → BigQuery (GCP free tier 10GB)

**wa-service деплоится вручную** — остальные сервисы через `make deploy-{service}` или автоматически через CI при пуше в `main`.
