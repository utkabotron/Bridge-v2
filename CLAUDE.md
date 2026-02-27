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

### Деплой Bridge-v2 на VPS
1. `rsync` файлов на сервер (на VPS нет git repo)
2. `docker compose build` нужных сервисов
3. `docker compose up -d` для рестарта
4. Проверка health endpoints
5. Проверка логов на ошибки

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

# Деплой в production (VPS)
ssh bridge              # подключиться к серверу
# Docker Compose деплой — настраивается
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

Pipeline использует `astream(state, stream_mode="updates")` для потоковой обработки — каждый узел эмитит события через `pipeline/events.py` (in-memory event bus с asyncio.Queue subscribers).

### Processor — Real-time Dashboard
`/dashboard` — ASCII-терминальный дашборд в реальном времени (SSE endpoint `/events`):
- Два столбца по 600px: слева pipeline visualization, справа user statistics
- Pipeline показывает детали каждого узла (chat_pair_id, translation preview, delivery_status)
- Events bus: `processor/src/pipeline/events.py` — emit/subscribe, deque history (50 events)
- `TELEGRAM_BOT_TOKEN` обязателен в env processor (для deliver node)

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

## Production (VPS)

- **Сервер:** Ubuntu 24.04, 3.8GB RAM, 48GB диск — `ssh bridge` (deploy@83.217.222.126)
- **Деплой:** Docker Compose на VPS
- **CI/CD:** GitHub Actions (`.github/workflows/`) — ci.yml (lint/test/build), deploy.yml (push to server)
- **Безопасность:** SSH по ключу (`~/.ssh/id_bridge_vps`), UFW (22/80/443), fail2ban
- **AWS инфраструктура удалена** — Terraform configs в `infra/terraform/` сохранены для справки
- **LangSmith:** трассировка включена (`LANGCHAIN_TRACING_V2=true`), проект `bridge-v2-prod`
- **Формат Telegram-сообщений:** `*Sender*\n\noriginal\n\ntranslated` (bold имя, пустая строка, оригинал, пустая строка, перевод)
