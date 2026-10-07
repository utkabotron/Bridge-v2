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
| processor | Python 3.12, FastAPI, openai SDK, asyncpg | 8000 | published |
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
  └─ pipeline: validate → translate → format → deliver
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

### Shared (один источник для processor, analytics и bot)
- `shared/bridge_shared/` — stdlib-only пакет, копируется в три образа (`COPY shared/bridge_shared /app/bridge_shared`); build context этих сервисов — КОРЕНЬ репо (`context: .`, `dockerfile: <svc>/Dockerfile`, корневой `.dockerignore` — белый список). Правка в `shared/` = пересобрать и выкатить processor, bot, analytics вместе.
  - `llm.py` — `MODEL_PRICES`, `TRANSCRIBE_PRICES`, `is_reasoning`, `supports_flex`, `chat_request`, `token_cost`
  - `scripts.py` — регэкспы письменностей (`HEBREW_RE`, `SOURCE_SCRIPT_RE`, `CYRILLIC_RE`, `LATIN_RE`, `TARGET_SCRIPT_RE`)
  - `chat_context.py` — `format_chat_context` (единственная версия), `with_glossary`
  - `glossary_match.py` — `GlossaryIndex`, `key_of`, `words`, `covers`: поиск имён в иврите по словам
  - `glossary_review.py` — текст и кнопки сообщения «Имена на одобрение» (шлёт analytics, перестраивает бот)
  - `telegram_html.py` — `esc`; `env.py` — `parse_ids`, `admin_tg_ids`
  - `processor/tests/test_shared.py` — страж: падает, если копия цен/регэкспов/`format_chat_context`/`esc` появится в сервисе

### Config (все константы здесь, не хардкодить)
- `processor/src/config.py` — env vars processor (timeouts, TTLs, Redis, DB, Telegram, alerting); цены моделей — в `shared/bridge_shared/llm.py`
- `wa-service/src/config.js` — env vars wa-service (Redis, DB, WA client, media, cache)

### Pipeline
- `processor/src/main.py` — FastAPI + consume_loop + все API endpoints
- `processor/src/pipeline/graph.py` — `Pipeline`: validate → translate → format → deliver (обычный Python, стрим `{node: output}` для SSE)
- `processor/src/alerts.py` — `notify_admins()` (единственная отправка алертов админам) + `SlidingWindow` для порогов «N за окно»
- `processor/src/llm.py` — ЕДИНСТВЕННЫЙ способ звать OpenAI из processor (обёртка над `bridge_shared.llm`): таймауты, запись стоимости в `llm_usage`
- `processor/src/pipeline/nodes.py` — validate/translate/format/deliver
- `processor/src/pipeline/prompts.py` — A/B переводчика (`VARIANTS`: версия + промпт + модель, `choose_variant`), `register_prompt()` пишет смену версии в `analytics_changelog`; `docs/model-bakeoff-2026-10-06.md` — результаты bake-off
- `processor/src/pipeline/cache.py` — Redis translation/profile/media cache
- `processor/src/pipeline/glossary.py` — словарь имён в памяти, `lookup(lang, text, pair)`, `reload()`
- `processor/src/pipeline/events.py` — in-memory event bus (asyncio.Queue)
- `processor/src/telegram_sender.py` — raw httpx → Telegram API (sendMessage/Photo/Video/Audio/Document)
- `processor/src/media_analyzer.py` — OpenAI vision (`DIRECT_MODEL`) + `gpt-transcribe` + PyPDF
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
- `bot/src/handlers/translate.py` — DM: перевод текста (кнопки иврит/английский) + анализ медиа
- `bot/src/config.py` — константы бота (`DIRECT_LANGUAGES`, `DIRECT_LANG_DEFAULT`)
- `bot/src/handlers/analyze.py` — "Analyze" button callback
- `bot/src/handlers/chats.py` — /chats, /add, /pause, /resume, /done
- `bot/src/handlers/admin.py` — /users, /broadcast, /whitelist
- `bot/src/handlers/glossary.py` — кнопки ✅/✏️/❌ под «Имена на одобрение» + ответ с вариантом
- `bot/src/db.py` — asyncpg pool (command_timeout=10)

### Analytics
- `analytics/flows/` — 8 Prefect flows (local server mode, no Cloud)
- `analytics/flows/chat_context_builder.py` — daily glossary/members/tone → chat_profiles (VPS only)
- `analytics/flows/translation_quality.py` — nightly quality eval; `JEV_MODE` = off | shadow | primary
- `analytics/flows/jev_eval.py` — TypeSafe Jev scoring (`typesafe-sdk`), без Prefect/DB, тесты в `analytics/tests/`
- `analytics/flows/jev_benchmark.py` — read-only сверка Jev с LLM-оценками: `docker compose exec analytics python -m flows.jev_benchmark`
- `analytics/flows/glossary_resolver.py` — словарь имён: импорт кандидатов из профилей, резолвер, авто-принятие
- `analytics/flows/glossary.py` — гейты глоссария: LLM-валидатор новых записей, флаги от оценщика (3 → удаление в `glossary_removed`)
- `analytics/flows/quality_stats.py` — разбивка оценок по source/pair/language/type/prompt_version; отчёты считают только `source='bridge'`
- `docs/quality-loop-plan.md` — чеклист петли «аналитика → качество перевода», отмечать по факту выкатки

**Словарь имён сервиса** (`glossary` + `glossary_override`, миграция 025, план — `docs/glossary-plan.md`):
одно чтение имени на весь сервис, ключ — иврит/латиница как в сообщениях (`glossary_match.key_of`).
Статусы: candidate → proposed (ждёт одобрения) → verified / locked (ручная) / rejected; в промпт —
статусы `GLOSSARY_USED_STATUSES` (по умолчанию `verified,locked`), и только найденные в тексте.
Имя, которое пишется как обычное слово (`also_word`: עמוס «занят», אופק «горизонт»), — ТОЛЬКО в чатах
из `chat_pairs`; однозначные и locked — везде, включая DM. Без этого «אני עמוס היום» → «Я сегодня Амос».
Перед включением статуса: `docker compose exec processor python -m src.glossary_check --statuses verified,locked`
(фразы имя/слово, обе A/B-модели, мимо кэша; exit 1 при провале). Поиск — `bridge_shared.glossary_match.GlossaryIndex`:
по словам с отрезанием приставок ו/ה/ב/ל/מ/ש/כ, НЕ по подстроке (גיל ⊄ רגיל). processor держит
словарь в памяти (`pipeline/glossary.py`), перечитывает при смене count/max(updated_at) — любая
запись в таблицы ОБЯЗАНА трогать `updated_at`. Запись словаря перекрывает запись глоссария/участника
чата (`chat_context.with_glossary`); override чата перекрывает словарь. Действует и в DM `/translate`.
Строитель не предлагает verified/locked (`glossary.drop_global`). Люди — по словам, БЕЗ связей
(«ребёнок X» остаётся в профиле чата). Резолвер: `python -m flows.glossary_resolver --import | --resolve
[--limit N] [--contested] [--kind person|other] [--dry-run]` — веб-поиск латинского написания для мест/
организаций, пачки для людей; авто-`verified`, если совпал с единогласным вариантом чатов (conf ≥ 0.8);
`--classify` — `also_word` + подсказка ≤ 3 слов (длинное пояснение модель копировала в перевод).
НОВЫЕ ИМЕНА НЕ РАЗБИРАЕТ LLM СЕРВИСА (решение 06.10): ночной `chat_context_builder` раскладывает найденное
(`glossary_resolver.route_delta`): verified/locked — не в профиль; rejected («решает чат») — в профиль как раньше;
новое — `candidate` (`upsert_names`, заодно расширяет `chat_pairs`). Разбирает Claude по запросу «разбери новые
имена»: выгрузка candidate → решение → одна транзакция verified/rejected, `decided_by='admin'` → `glossary_check`.
Плохие оценки переводов: запись чата снимается после 3 флагов, запись словаря — только `flags` (миграция 028),
в дайджесте «под вопросом N». `--resolve`/`--arbitrate` в ночной поток НЕ включать.
Ручное одобрение — только для того, что арбитр не смог решить: после дайджеста analytics шлёт админам «Имена на одобрение» (10 штук, `bridge_shared.glossary_review`),
кнопки ✅/✏️/❌ и «Следующие» обрабатывает бот (`handlers/glossary.py`, callback `gl:*`); ✏️ — ответ на
вопрос бота (строка `ref g:` в вопросе). Решения → `verified`/`rejected`, `decided_by='admin'`; ✏️ НЕ `locked`
(locked = во всех чатах, сломает имена-слова). Автоматика (`decided_by='auto'`) решения админа не трогает.

**Глоссарий чатов:** только имена собственные. `chat_context_builder` видит ТОЛЬКО оригиналы;
новые записи проходят `glossary.validate_entries`; утром `apply_quality_feedback` читает
оценки за ночь и снимает записи с 3 флагами (`profile_data.glossary_removed` — строитель их
не предлагает). После любого изменения профиля — `invalidate_profile_cache` (Redis).
Ключ кэша переводов = sha(prompt_version + chat_context + text), версия — та, что выбрал A/B.
Ночные «добавьте правило в промпт» выключены (`PROMPT_SUGGESTIONS_ENABLED=false`); промпт
меняется только через A/B → `/weekly-improve` (продвижение B → A описано в скилле). Разовая чистка:
`docker compose exec analytics python -m flows.chat_context_builder --prune-glossaries`.

**Модели processor:** `OPENAI_MODEL` — вариант A моста (A/B); `DIRECT_MODEL` — личка бота
(`/translate`) и анализ медиа, по умолчанию = `OPENAI_MODEL`; `TRANSCRIBE_MODEL` — голосовые.

**Jev:** нужен `TYPESAFE_API_KEY` в `.env`, без него любой режим = off. `shadow` пишет строки
`translation_evaluations` с `shadow=true` — читатели таблицы ОБЯЗАНЫ фильтровать `NOT shadow`
(миграция 018). Любой сбой Jev → откат на LLM-выборку на эту ночь.

## REDIS KEYS

| Key | Type | TTL | Use |
|-----|------|-----|-----|
| `messages:in` | List | — | WA→processor queue |
| `messages:processing` | List | — | In-flight: взято из messages:in, ещё не завершено |
| `messages:dlq` | List | — | Dead-letter queue (авторазбор каждые 10 мин, до 5 попыток) |
| `messages:dlq:dead` | List | — | Сдались после DLQ_MAX_ATTEMPTS |
| `qr:token:{token}` | String | 15m | Одноразовый токен QR-страницы |
| `analytics:health:*` | String/Hash | 1h | Дедуп алертов + прошлые значения метрик |
| `dedup:msg:{wa_message_id}` | String | 5m | Message dedup (SET NX) |
| `onboarding:qr_scanned:{userId}` | Pub/Sub | — | WA connected event |
| `chat_pairs:user:{uid}:chat:{chatId}` | String | 1h / 60s если пар нет | Кэш пар — пишет processor (`pipeline/cache.py`); сбрасывает processor при паузе/миграции чата; wa-service/bot пока НЕ сбрасывают (C1 в плане) |
| `translation:{lang}:{pair_id}:{sha256}` | String | 24h | Translation cache (per-pair) |
| `translation_global:{lang}:{sha256}` | String | 24h | Translation cache (no profile) |
| `chat_profile:{pair_id}` | String | 1h | Chat profile cache |
| `ff:{flag_name}` | String | 60s | Feature flag cache |

## PROCESSOR API

| Method | Path | Feature flag | Purpose |
|--------|------|-------------|---------|
| GET | /health | — | Health check |
| GET | /metrics | — | Counters: processed/failed/skipped/dlq |
| GET | /events | — | SSE stream (pipeline events) |
| GET | /dashboard | — | HTML dashboard |
| GET | /api/stats | — | User stats за 30 дней, кэш 60 с |
| GET | /api/daily-stats | — | Today's counts |
| GET | /api/reports?date= | — | Nightly problems + quality |
| GET | /api/backlog | — | Open critical issues |
| PATCH | /api/backlog/{id} | — | Resolve/wontfix issue |
| GET | /api/dlq | — | DLQ messages (max 100); ретрай только автоматический (`_dlq_retry_loop`) |
| GET | /api/flags | — | Feature flags list |
| PATCH | /api/flags/{name} | — | Toggle flag `{"enabled": bool}` |
| GET | /api/costs?days= | — | LLM costs по дням и по назначению из `llm_usage` |
| GET | /api/profiles | — | Chat profiles with glossaries |
| GET | /api/glossary?status=&kind= | — | Словарь имён |
| PUT | /api/glossary | — | Закрепить имя (`locked`): `{source, translation, note?, kind?, target_language?}` |
| DELETE | /api/glossary/{id} | — | Удалить запись словаря |
| GET | /api/glossary/overrides | — | Переопределения по чатам |
| PUT | /api/glossary/override | — | `{chat_pair_id, source, translation, note?}` |
| DELETE | /api/glossary/override/{pair}?source= | — | Удалить переопределение |
| POST | /translate | translation_enabled | Text translation |
| POST | /analyze | media_analysis_enabled | Media analysis by event_id |
| POST | /analyze-direct | media_analysis_enabled | Media analysis (file upload) |

## WA-SERVICE API

**Все роуты кроме `/health` и `/miniapp` требуют авторизации** (`src/middleware/tg-auth.js`):
подписанный `X-Tg-Init-Data` из Mini App, либо `X-Internal-Token` + `X-Internal-User-Id`
(вызовы бота), либо `?t=<qr-токен>` для QR-страницы вне Telegram. `requireSelf` сверяет
личность с `:userId` в пути. Новый роут ниже `router.use(authenticate)` защищён автоматически.

| Method | Path | Purpose |
|--------|------|---------|
| GET | /health | activeClients + readyClients + lastMessageAt + redis (без авторизации) |
| GET | /chat-pairs/:userId | Pairs list + wa_connected |
| PATCH | /chat-pairs/:pairId | Pause/resume |
| DELETE | /chat-pairs/:pairId | Delete pair |
| GET | /tg-groups/:userId | TG groups from Redis |
| POST | /connect/:userId | Start WA client |
| GET | /status/:userId | WA status + groups (15s timeout) |
| POST | /disconnect/:userId | Destroy WA client |
| POST | /reconnect/:userId | Recreate WA client |
| POST | /chat-pairs | Создать мост (тело: wa_chat_id, wa_chat_name, tg_chat_id, tg_chat_title) |
| GET | /qr/image/:userId | PNG QR (202 if starting) |
| GET | /qr/page?t= | QR-страница по одноразовому токену (не по userId) |
| GET | /miniapp, /miniapp-assets/* | Mini App: оболочка + CSS/JS |

`GET /chat-pairs` отдаёт `target_language`, `language_inherited`, `summary_enabled`,
`created_at`; `wa_connected` — живое состояние клиента, а не только флаг в БД.
`PATCH /chat-pairs/:pairId` принимает `{status?, target_language?, summary_enabled?}`
(`target_language: null` = наследовать аккаунт) и возвращает обновлённую пару.

## FEATURE FLAGS

DB table `feature_flags` → Redis cache `ff:{name}` (60s) → env var fallback.
Module: `processor/src/feature_flags.py`. API: `GET/PATCH /api/flags/{name}`.

| Flag | Controls |
|------|----------|
| translation_enabled | POST /translate |
| media_analysis_enabled | POST /analyze, /analyze-direct |
| admin_alerts_enabled | 401 + failure rate alerts to admins |
| prompt_ab_enabled | A/B переводчика: нечётные пары → вариант B из `prompts.VARIANTS` (сейчас gpt-6-luna на промпте v2.10), чётные/DM → A; чаты `AB_ALWAYS_B_USERS` (по умолчанию админы) — всегда B; сравнение по `prompt_version` в оценках |

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
| 018 | translation_evaluations += evaluator, shadow, quality_expected, confidence (Jev) |
| 019 | message_events += prompt_version, cache_hit, translation_passthrough/failed/error; translation_evaluations += source (bridge/direct/fallback), chat_pair_id, target_language, message_type, prompt_version |
| 020 | feature_flags += prompt_ab_enabled |
| 021 | llm_usage — журнал каждого вызова модели из processor (purpose, model, tag, tokens, cost_usd, ms), 90 дней |
| 022 | feature_flags −= direct_chat_enabled (мёртвый) |
| 023 | users −= wa_session_id (никто не читал) |
| 024 | glossary_global (заменена в 025) |
| 025 | glossary, glossary_override — словарь имён сервиса; glossary_global → locked, удалена |
| 026 | glossary += also_word, chat_pairs — неоднозначные имена только в своих чатах |
| 027 | glossary += decided_by (auto / admin) |
| 028 | glossary += flags, flag_examples — «под вопросом» от плохих оценок |

## ANALYTICS FLOWS

| Flow | Cron | Model |
|------|------|-------|
| wa-health-check | */15 * * * * | — |
| daily-cleanup | 0 3 * * * | — |
| nightly-problems | 0 4 * * * | gpt-6-luna |
| translation-quality | 30 4 * * * | gpt-6.1-sol (судья, `EVAL_MODEL`) |
| chat-context-builder | 0 5 * * * | gpt-6.1-sol (+ web_search только при первом построении профиля) |
| weekly-report | 0 5 * * 1 | gpt-6.1-sol, reasoning high |
| daily-chat-summary | */30 * * * * | gpt-6-luna |
| nightly-backup | 30 2 * * * | — (pg_dump, 7 копий) |
| daily-digest | 20 5 * * * | — (единственное утреннее сообщение админам) |

Ночные flow (problems, quality, context, weekly) в Telegram НЕ пишут — только `daily-digest`
(`analytics/flows/daily_digest.py`) читает их результаты из БД и шлёт один дайджест на русском.
Алерты по событиям (health, DLQ, кредиты OpenAI, бэкап) — отдельно, как были.
Проверить текст без отправки: `docker compose exec -e DIGEST_DRY_RUN=1 analytics python -m flows.daily_digest`.

Все LLM-вызовы analytics — через `flows/llm.py`: `build_request` (форма под семейство модели),
`complete`/`respond` (Flex-тариф −50%, откат на стандарт при ошибке), `usage_cost` (цена по
таблице). Модели переопределяются env: `EVAL_MODEL`, `NIGHTLY_MODEL`, `SUMMARY_MODEL`,
`WEEKLY_MODEL`, `CONTEXT_MODEL`, `GLOSSARY_VALIDATION_MODEL`.

## ONBOARDING

`/start` — единственный вход, показывает кнопку Mini App. Дальше всё в приложении:
QR → выбор WA-чата → выбор TG-группы → `POST /chat-pairs`. Inline-визард
(`onboarding:*`, `/done`, `handle_webapp_data`) удалён — он был недостижим.

Список TG-групп — таблица `tg_groups` (одна строка на пару группа+админ), заполняется
`handlers/groups.py:sync_group` из трёх мест: событие `my_chat_member`, любое сообщение
в группе (троттлинг час на чат — единственный способ узнать о группах, где бот уже был),
и `/add`. Раньше это был Redis-хеш с TTL час, видимый только добавившему.

## MINI APP

`wa-service/public/miniapp.html` — только разметка экранов; логика в
`public/assets/miniapp.js`, стили в `public/assets/miniapp.css` (их же использует
браузерная `/qr/page`). Язык интерфейса — английский.

- Один автомат состояний: `navigate()` + стек истории, `BackButton`/`MainButton`
  регистрируются РОВНО ОДИН раз при старте (регистрация на каждом экране раньше давала
  N срабатываний на один тап). Экраны: loading → home → connect → pick-wa → pick-tg →
  done, плюс expired/denied и bottom-sheet настроек пары.
- Поллинг — `poll()` с номером поколения: смена экрана обесценивает ответы в полёте.
  Всегда ограничен числом попыток и заканчивается кнопкой Retry.
- Пара создаётся через `POST /chat-pairs`. **Не использовать `tg.sendData()`** — Telegram
  доставляет его только из reply-клавиатуры, а приложение открывается inline-кнопкой.
- Тема берётся у Telegram (`colorScheme`, `themeChanged`, `--tg-viewport-stable-height`,
  `safe-area`). Свои цвета только два: `--wa` и `--tg` — «берега моста».
- Проверки целостности — `wa-service/tests/miniapp.test.js` (id в разметке против
  `getElementById`, экран против обработчика, класс против CSS, экранирование).

## MESSAGE PRESENTATION

`format_node` собирает: `[➡️] Отправитель [✏️ изменено]` → цитата → контакты → оригинал →
перевод → пометки (`⚠️ перевод недоступен`, `📎 не удалось получить <тип>`).
Отправитель на иврите → в письменности читателя (`glossary.sender_display` → `glossary_match.render_name`),
только если КАЖДОЕ ивритское слово — имя человека в словаре (любой области, имена-слова тут безопасны);
частично известное, канал, имя с латиницей/кириллицей — как есть.
Тексты — в `processor/src/config.py` (`MEDIA_FAILED_NOTE`, `EDITED_MARK`, `REVOKE_NOTE`,
`OWN_MESSAGE_PREFIX`, `VOICE_TRANSCRIPT_TITLE`).

Перевод пропускается, если текст уже в письменности целевого языка
(`graph._already_in_target_script`) — смешанный текст всё равно переводится.

Правка в WA (`is_edited`) **редактирует доставленное TG-сообщение**
(`telegram_sender.edit_message` → `editMessageText` / `editMessageCaption` для медиа),
метку «edited» рисует сам Telegram. `format_node` отдаёт два текста: `formatted_text`
(с `✏️ изменено`) и `formatted_text_plain` — второй уходит в edit.

| Случай | Поведение |
|--------|-----------|
| Оригинал найден (`db.find_delivered_event`) | edit на месте, `tg_message_id` = оригинала |
| Telegram отказал (not found / can't be edited / текст > лимита) | fallback: новое сообщение reply + `✏️` |
| Оригинал не в БД (до фичи, failed, другой чат) | старое поведение |
| `message is not modified` | считается успехом — иначе вернётся дубль |
| Медиа | правится caption; `reply_markup` Analyze пересобирается по `event_id` оригинала |

Транскрипт голосового (отдельный reply) правка не трогает. Повторная одинаковая правка
отбрасывается дедупом wa-service (`<orig>:edit:<hash>`).

## DM-ПЕРЕВОД

Текст боту в личку → перевод на русский (русский текст → на иврит) + инлайн-кнопки выбора языка (Русский / עברית / English)
(`handlers/translate.py`). Перевод в `<code>` — тап копирует его целиком, служебная
строка `ms · язык` вне блока.

| Правило | Причина |
|---------|---------|
| Дефолт — `DIRECT_LANG_DEFAULT` (русский); текст уже на русском → `DIRECT_LANG_FALLBACK` (иврит), см. `config.default_direct_language` | русский в русский — пустой ответ |
| Активная кнопка → `callback_data="noop"` | нечего переводить заново, переиспользует `cb_noop` |
| Исходник берётся из `query.message.reply_to_message` | кнопка работает после рестарта бота, ничего не в памяти |
| Ответ шлётся с явным `ReplyParameters` | в приватном чате PTB не цитирует, и кнопке нечего переводить |
| Ошибка по кнопке → алерт, сообщение не трогаем | иначе теряется и перевод, и кнопки |
| Callback под whitelist | тап стоит вызова LLM |

Языки — `bot/src/config.py:DIRECT_LANGUAGES`, добавление языка = одна строка там
(pattern хендлера `^tr:[a-z]{2}$` менять не надо). Processor `/translate` уже принимает
`target_language`, менять его не требуется.

Язык — per-pair: `coalesce(cp.target_language, u.target_language)`, NULL = наследовать
аккаунт. Меняется кнопкой ⚙️ в `/chats`, там же переключатель сводки дня.

## RELIABILITY

- `wa-health-check` алертит в Telegram: клиент отвалился, тишина >3ч днём, очередь растёт,
  DLQ не разбирается, processor молчит, db_write_failed растёт, диск >85%, своп >75%.
  Дедуп алертов — Redis, час.
- Отказ OpenAI НЕ теряет сообщение: доставляется оригинал с пометкой (`TRANSLATION_UNAVAILABLE_NOTE`).
- Очередь: `CONSUMER_WORKERS` (4) воркеров, каждый `BLMOVE messages:in → messages:processing`,
  удаление после успеха, возврат зависших при старте. Сообщения ОДНОГО чата — строго по одному и
  в порядке очереди (lock по `user_id:wa_chat_id` в `main._chat_lock`), разные чаты — параллельно.
  DLQ разбирается автоматически. Худший случай на одно сообщение: 2 попытки LLM × `LLM_TIMEOUT`,
  Telegram retry_after ≤ `MAX_RETRY_AFTER` (20 с).
- Миграции: `./infra/migrate.sh` (журнал `schema_migrations`), запускать всегда.
- Бэкапы: `nightly-backup` → `/home/deploy/backups/pg`, 7 копий, проверка `pg_restore --list`.
- Python-зависимости: ставятся из `requirements.lock`, не из `.txt`.

## CONSTRAINTS

- wa-service: 1 replica only (whatsapp-web.js). Sessions in `.wwebjs_auth/` volume. System Chromium `/usr/bin/chromium`. SingletonLock cleanup needed on container recreate.
- Все сервисы работают под непривилегированным пользователем. Том `wa_sessions` принадлежит
  uid 1000 — при пересоздании тома нужен `chown -R 1000:1000` (см. wa-service/Dockerfile).
- `MINIO_ROOT_USER/PASSWORD` и `INTERNAL_API_TOKEN` обязательны в `.env` — compose падает без них.
- `REDIS_PASSWORD` в `.env` — пароль Redis для всех 4 клиентов (пусто = без auth). Смена требует `docker compose up -d` ВСЕХ сервисов разом. Ручной доступ: `docker compose exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli'`. Redis слушает только 127.0.0.1; в 02–07.2026 был открыт наружу без пароля (червь писал ключи `backup*`) — НЕ публиковать порт.
- Бакет медиа приватный; ссылки наружу только presigned (`processor/src/s3.py`), объекты живут 90 дней.
- wa-service port 3000: expose-only, NOT published. Access via nginx.
- Media format: `*Sender*\n\noriginal\n\ntranslated`. Media sent natively (sendPhoto/etc), NOT in formatted_text.
- Голосовые = тип `ptt` (не `voice`!) → `sendVoice` + авто-транскрипт `gpt-transcribe` (`TRANSCRIBE_MODEL`; whisper-1 отключают 26.02.2027) отдельным reply. На шуме возвращает пусто → «(empty audio)», не галлюцинирует.
- Локации → `sendLocation`, контакты (vcard) → разбор в имя+телефоны, опросы → вопрос+варианты.
  Всё это мимо LLM: раньше уходило в перевод как текст.
- Цитаты/правки → реальный reply в Telegram через `reply_parameters` (поиск `tg_message_id`
  в `message_events`). Удаления → пометка ответом на исходное сообщение.
- Свои сообщения (`fromMe`) бриджатся через `message_create`; id нормализуется
  (`normalizeMessageId` срезает префикс `true_/false_`), иначе своя и чужая копии = дубль.
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
- LLM-расходы: таблица `llm_usage` (processor) + `estimated_cost` в таблицах ночных flow (analytics). LangChain/LangGraph/LangSmith удалены 2026-10-06 — НЕ возвращать: тексты чатов уходили третьей стороне
