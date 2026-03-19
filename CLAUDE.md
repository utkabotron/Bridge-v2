# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Рабочие правила

### Перед изменениями — исследуй
Перед тем как менять код, используй task agent для исследования всех затронутых файлов. Составь карту функций и вызовов, потом предложи план. Не пиши код вслепую.

### После бэкенд-изменений — рестарт
После изменений серверных файлов всегда рестартуй сервис. Не предполагай, что старый процесс подхватит изменения.

### После рестарта processor/bot — рестартуй nginx
При `docker compose up -d` контейнер получает новый IP. Nginx кеширует старый upstream IP → 502. Всегда `docker compose restart nginx` после пересоздания processor/bot.

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

Bridge v2 — WhatsApp→Telegram мост с AI-переводом. Монорепозиторий из 4 сервисов, оркестрированных через Docker Compose. Стек: JavaScript (wa-service), Python (bot, processor, analytics). TypeScript НЕ используется. Дефолтный язык перевода — Hebrew.

| Сервис | Стек | Порт |
|--------|------|------|
| `wa-service` | Node.js 20, whatsapp-web.js, Express, ioredis, pg | 3000 (только внутри docker network) |
| `processor` | Python 3.12, FastAPI, LangGraph, asyncpg | 8000 |
| `bot` | Python 3.12, python-telegram-bot, asyncpg | 8001 |
| `analytics` | Python 3.12, Prefect, OpenAI | 4200 |

## Команды

```bash
# Локальная разработка
make up            # docker compose up -d (все 4 сервиса + redis + postgres + minio)
make down          # остановка
make restart       # docker compose restart (все сервисы)
make logs          # логи всех сервисов
make logs-bot      # логи конкретного сервиса (logs-wa, logs-processor, logs-bot, logs-analytics)
make health        # curl /health всех сервисов

# Тесты и линтинг
make test          # Jest (wa-service) + pytest (processor + bot, asyncio_mode=auto)
make lint          # ruff check processor/src/ bot/src/ analytics/flows/
make format        # ruff format

# Запуск одного теста
cd wa-service && npm test                                              # все Jest-тесты
cd processor && python -m pytest tests/test_pipeline.py::test_validate_node -v
cd bot && python -m pytest tests/test_onboarding.py -v

# wa-service разработка
cd wa-service && npm run dev   # node --watch src/index.js (hot reload)

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
- `POST /disconnect/:userId` — уничтожить WA-клиент
- `POST /reconnect/:userId` — пересоздать WA-клиент (полезно при зависших сессиях)
- `GET /qr/image/:userId` — PNG QR-код (202 если клиент ещё стартует)

Mini App: `wa-service/public/miniapp.html` (Vanilla JS, Telegram WebApp SDK, требует HTTPS).
wa-service DB: `wa-service/src/db.js` (pg Pool → Postgres, chat-pairs CRUD).

### Межсервисное взаимодействие

```
wa-service → Redis LPUSH "messages:in"                → processor (BRPOP)
wa-service → Redis PUBLISH "onboarding:qr_scanned:*"  → bot (PSUBSCRIBE, daemon thread)
bot        → HTTP GET/POST wa-service /connect/, /status/  (onboarding)
processor  → HTTP POST Telegram API send*                (доставка, напрямую без bot)
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
whatsapp-web.js не поддерживает кластеризацию. Всегда `replicas: 1`. WA-сессии хранятся в `.wwebjs_auth/` (Docker volume `wa_sessions`). Chromium берётся системный (`/usr/bin/chromium`), не из npm. При пересоздании контейнера может остаться `SingletonLock` — удалить через volume:
```bash
docker compose stop wa-service
docker run --rm -v bridge-v2_wa_sessions:/data alpine find /data -name SingletonLock -delete
docker compose start wa-service
```

wa-service порт 3000 **не опубликован наружу** — только `expose` внутри docker network. Доступ снаружи через nginx.

### Медиа-пайплайн
wa-service загружает медиа в S3/MinIO (`uploadMedia()`), кладёт `media_s3_url`, `media_mime`, `media_filename` в payload. При ошибке загрузки — сообщение всё равно отправляется (без медиа, с текстом). Processor передаёт медиа-поля в state (`MessageState` в `models/message.py`). `telegram_sender.py` отправляет медиа нативно через Telegram API (sendPhoto/sendVideo/sendAudio/sendDocument) с fallback на текстовую ссылку. Медиа-ссылка **не** включается в `formatted_text` — формат текста всегда `*Sender*\n\noriginal\n\ntranslated`.

Локально S3 заменён на **MinIO** (порт 9000, консоль 9001). Бакет `bridge-media` создаётся автоматически через `minio-init` сервис в docker-compose (`infra/minio-init.sh`).

### Media Analyzer
`processor/src/media_analyzer.py` — анализ медиа через OpenAI:
- `analyze_image()` — GPT-4.1-mini vision для описания изображений
- `analyze_audio()` — Whisper для транскрипции аудио
- `analyze_document()` — PyPDF для извлечения текста из PDF

Результат возвращается на `target_language` пользователя. Endpoint: `POST /analyze` (event_id, analysis_type, requested_by).

### Bot — дополнительные хэндлеры
- `handlers/translate.py` — прямой перевод текста и анализ медиа в личке бота:
  - `handle_direct_text()` — текст → reply(⏳) → POST /translate → edit(только перевод). Оригинал юзера остаётся в чате (не удаляется). Reply содержит только перевод + metadata, без оригинала — чтобы избежать `Message_too_long` при длинных текстах.
  - `handle_direct_media()` — фото/документ/аудио/голосовое/видео-кружок → reply(⏳) → download file → POST /analyze-direct (multipart) → edit(результат) → delete(user msg). При ошибке — НЕ удаляет.
- `handlers/analyze.py` — callback handler для кнопки "Analyze" на медиа. Парсит `analyze:{event_id}`, вызывает `POST /analyze`.

### Processor — LangGraph pipeline
Граф в `processor/src/pipeline/graph.py`. Узлы в `nodes.py`. Условная маршрутизация: нет пары → сразу deliver(failed), нет текста → skip translate. Промпт версионирован: `PROMPT_VERSION` в `prompts.py`. Fallback to admins при отсутствии пары работает только для admin users (ADMIN_TG_IDS).

Pipeline использует `astream(state, stream_mode="updates")` — каждый узел эмитит события через `pipeline/events.py` (in-memory event bus с asyncio.Queue, deque history 50 events). SSE endpoint `/events` и ASCII-дашборд `/dashboard`.

Дашборд processor'а (`/dashboard`): realtime pipeline + users + analytics reports + critical backlog. API:
- `GET /api/stats` — статистика по пользователям (delivered/failed/avg_ms)
- `GET /api/reports?date=YYYY-MM-DD` — nightly problems + translation quality из БД (default: today)
- `GET /api/backlog` — открытые critical issues из persistent бэклога
- `PATCH /api/backlog/{id}` — обновить статус (resolved/wontfix)
- `GET /api/costs` — LangSmith cost data (кэш 15 мин), fallback pricing для gpt-4.1-mini
- `GET /api/daily-stats` — дневная статистика
- `POST /translate` — перевод текста (text, user_id) → original, translated, target_language, translation_ms
- `POST /analyze` — анализ медиа по event_id из pipeline (event_id, analysis_type, requested_by)
- `POST /analyze-direct` — прямой анализ медиа из лички бота (multipart: file, user_id, mime_type, filename) → result_text, analysis_type, processing_ms

### Bot — смешанный sync/async
python-telegram-bot в asyncio. Redis subscriber (`redis_sub.py`) — отдельный daemon thread. Коммуникация: `asyncio.run_coroutine_threadsafe(coro, bot_loop)`, где `bot_loop` захватывается в `main.py:post_init`.

### Онбординг-состояния
`idle → qr_pending → wa_connected → linking → done`
Хранятся в `onboarding_sessions`. Мигрированные из v1 получают `done`. Bot /start всегда показывает описание + кнопку Mini App (независимо от состояния). При добавлении бота как admin в TG группу — автосообщение с кнопкой /add.

### Chat Context Builder
`analytics/flows/chat_context_builder.py` (только на VPS) — ежедневно строит профили чатов для улучшения перевода:
- **collect**: для чатов без профиля — берёт 90 дней, для остальных — 24ч (инкрементально)
- **extract**: LLM (gpt-4.1 + web_search для верификации имён) извлекает delta: glossary (транслитерация Hebrew→target_lang), members (имена участников), mentioned_people (упоминаемые люди не из группы), recurring_topics, tone
- **merge**: delta мержится в существующий профиль с сохранением истории в `chat_profile_history`
- Профили из `chat_profiles` инжектируются в translation prompt — не переводить, а транслитерировать термины из глоссария

### Analytics — Prefect flows
7 flow в `analytics/flows/`. Запускается в **локальном режиме** (без Prefect Cloud) — `entrypoint.sh` стартует локальный Prefect Server на 4200, затем `serve_flows.py`. Dual-mode: без `PREFECT_API_URL` — локальный server, с ним — Prefect Cloud worker. Оба store_results делают UPSERT для `nightly_analysis_runs`, но дочерние таблицы (`detected_issues`, `translation_evaluations`, `prompt_suggestions`) — DELETE + INSERT при повторном запуске за тот же день. `nightly-problems` дополнительно копирует critical issues в `issues_backlog` (persistent, не перезаписывается).

| Flow | Cron | Модель | Назначение |
|------|------|--------|------------|
| `wa-health-check` | `*/15 * * * *` | — | Проверка wa-service |
| `daily-cleanup` | `0 3 * * *` | — | Очистка старых данных (message_events > 90д, sessions > 7д, direct_interactions > 90д) |
| `nightly-problems` | `0 4 * * *` | gpt-4.1-mini | Анализ проблем за 24h |
| `translation-quality` | `30 4 * * *` | gpt-4.1-mini | Оценка качества переводов |
| `chat-context-builder` | `0 5 * * *` | gpt-4.1 (+ web_search) | Глоссарий, имена участников, тон чата → `chat_profiles` |
| `weekly-report` | `0 5 * * 1` | o3 | Еженедельный отчёт (с persistent memory в `weekly_insights`) |
| `chat-context-builder` | `0 5 * * *` | gpt-4.1 | Построение per-chat профилей (глоссарии, участники, тон) |
| `daily-chat-summary` | `*/30 * * * *` | gpt-4.1-mini | Ежедневные сводки по чатам в TG-группы (оптимальный час отправки per-chat) |
| `export-to-bq` | ручной | — | Экспорт в BigQuery (требует GCP_SA_PATH) |

## База данных

PostgreSQL 16. Все операции через `asyncpg` (processor, bot) и `psycopg2` (analytics) без ORM.

**001_initial_schema.sql** (4 таблицы):
- `users` — tg_user_id unique, wa_connected, target_language, is_admin, is_active
- `chat_pairs` — unique(user_id, wa_chat_id, tg_chat_id), status: active/paused
- `message_events` — wa_message_id unique, ON CONFLICT DO UPDATE, delivery_status
- `onboarding_sessions` — user_id PK, state FSM

**002_llm_analysis.sql** (4 таблицы для analytics):
- `nightly_analysis_runs` — unique(run_date, flow_type), jsonb summary
- `detected_issues` — severity: critical/warning/info, acknowledged flag
- `translation_evaluations` — quality/accuracy/naturalness scores 1-5
- `prompt_suggestions` — status: pending/applied/rejected

**003_prompt_registry.sql**:
- `prompt_registry` — key PK, version, content. Processor регистрирует текущий промпт при старте (`register_prompt()` в `pipeline/prompts.py`)

**004_issues_backlog.sql**:
- `issues_backlog` — persistent бэклог critical issues. status: open/resolved/wontfix. Не перезаписывается при повторных nightly runs

**005_weekly_insights.sql**:
- `weekly_insights` — persistent memory для weekly-report (week_start UNIQUE, executive_summary, deep_analysis JSONB, recommendations JSONB)
- `analytics_changelog` — лог изменений аналитики

**006_delivery_status_skipped.sql**:
- Добавляет статус `skipped` в `delivery_status` enum. Сообщения без chat_pair теперь `skipped` (ранее `failed`). Разделяет реальные ошибки от ожидаемых skip'ов в метриках.

**007_media_analysis.sql**:
- `media_analysis` — результаты анализа медиа (image/audio/document), связь с message_events
- Добавляет `tg_message_id` в `message_events`

**008_direct_interactions.sql**:
- `direct_interactions` — трекинг прямых переводов и анализа медиа из личного чата бота. interaction_type: translation/media_analysis, status: completed/failed. Записывается из `/translate` и `/analyze-direct` endpoints processor'а. Включён в nightly/weekly отчёты и дашборд.

**009_chat_profiles.sql** (только на VPS, отсутствует в локальном репо):
- `chat_profiles` — per-chat профили: glossary, members, mentioned_people, recurring_topics, tone, chat_type. UNIQUE(chat_pair_id). Заполняется `chat-context-builder` flow, инжектируется в translation prompt.
- `chat_profile_history` — история версий профилей (version, change_summary)

**010_daily_chat_summaries.sql**:
- `daily_chat_summaries` — ежедневные сводки по чатам (chat_pair_id + summary_date UNIQUE, summary_text, plans_extracted JSONB, sent, tg_message_id)
- `chat_summary_schedule` — кэш оптимальных часов отправки (chat_pair_id PK, optimal_hour, mean_hour, std_hour, sample_size)

## Production (VPS)

- **Домен:** brdg.tools
- **Сервер:** Ubuntu 24.04, 3.8GB RAM — `ssh bridge` (deploy@83.217.222.126)
- **Деплой (на VPS нет git repo).** ВАЖНО: рабочий каталог `/home/deploy/bridge-v2/`, НЕ `~/bridge-v2/` (root home). `.env` лежит только там:
  ```bash
  rsync -avz --exclude '.git' --exclude 'node_modules' --exclude '__pycache__' --exclude '.wwebjs_auth' --exclude '.env' --exclude '.venv' ./ bridge:/home/deploy/bridge-v2/
  ssh bridge "cd /home/deploy/bridge-v2 && docker compose build <сервис> && docker compose up -d <сервис>"
  ssh bridge "cd /home/deploy/bridge-v2 && docker compose restart nginx"  # обязательно после up -d
  ssh bridge "curl -s http://localhost:3000/health"  # wa-service
  ssh bridge "curl -s http://localhost:8000/health"  # processor
  ```
- **CI/CD:** GitHub Actions — `ci.yml` (lint/test/build на push/PR). `deploy.yml` устарел (AWS снесён 2026-02-27).
- **Безопасность:** SSH по ключу, UFW (22/80/443), fail2ban
- **LangSmith:** `LANGCHAIN_TRACING_V2=true`, проект `bridge-v2-prod`
- **Формат Telegram-сообщений:** `*Sender*\n\noriginal\n\ntranslated` (медиа отправляется нативно как caption)
- **Nginx:** reverse proxy в docker-compose. Basic auth (`.htpasswd`) для `/dashboard`, `/api/*`, `/events`. SSE `/events` настроен с `proxy_buffering off` и `proxy_read_timeout 86400s`. SSL через certbot (auto-renewal cron `0 3 * * *`).
- **Логи:** json-file driver с ротацией `max-size: 10m, max-file: 3` на всех сервисах.
