# Bridge v2 — Bug Hunt Triage (2026-07-25)

Источник: 5 параллельных Fable-5 агентов (wa-service, processor-pipeline, processor-core, bot, cross-cutting).
Правки — отдельным проходом Opus. Каждый пункт Opus обязан **перепроверить** перед фиксом.

Легенда статуса:
- ✅ **PROD-CONFIRMED** — механизм подтверждён на проде (БД/логи).
- 🔁 **MULTI-AGENT** — независимо найдено ≥2 агентами.
- 📄 **CODE-CITED** — обосновано чтением кода, требует проверки Opus.

---

## P0 — ГРУППА 1: Потеря `wa_message_id` в payload → коллапс (корневая причина инцидента 15.07)

**Статус: ✅ PROD-CONFIRMED + 🔁 (A, B, C, E — все четверо)**

Механизм (подтверждён строкой в проде `wa_message_id='' | delivered | document | 2026-07-15`):
`whatsapp-web.js` отдаёт `message.id._serialized=undefined` → `redis-publisher.js:55` считает `fallbackDedupId`, **но кладёт его только в Redis dedup-ключ, а НЕ в `payload.wa_message_id`** → JS `JSON.stringify` выбрасывает `undefined`-ключ → processor `main.py:841` `.get("wa_message_id","")` = `""` → `db.py:104` `ON CONFLICT (wa_message_id) DO UPDATE ... WHERE delivery_status!='delivered'` → после первой `delivered` `""`-строки все последующие upsert'ы = **тихий no-op**.

Симптомы (все следствия одного корня):
- ❌ **Медиа не доставляется с 15.07** (`nodes.py:245-250`): two-phase delivery — `insert_message_event(return_id=True)` → `event_id=None` → `failed`, `send_message` **не вызывается**. Юзер не получает фото/войс/док. `B#2`, `C#2`.
- ❌ `message_events` заморожена → аналитика/nightly-flows/dedup-история слепы. `C#1`, `E-C1`.
- ❌ DB-dedup `main.py:867` (`if wa_msg_id:`) полностью пропускается для пустого id → `/api/dlq/retry` дублирует такие сообщения. `C#7`.
- ❌ Мой фикс `cc564a6` (22.07) НЕ закрыл это — починил только Redis dedup-ключ.

**Фикс для Opus:**
1. wa-service `redis-publisher.js` `publishMessage`: `payload.wa_message_id = dedupId` (единый источник истины — и Redis dedup, и DB unique key используют один стабильный id).
2. processor `main.py` consume_loop: guard — пустой id → суррогат `fallback:{sha256(user|chat|ts|body)[:16]}`.
3. `nodes.py._deliver_media_with_button`: при `event_id=None` **всё равно отправлять** (без кнопки Analyze, через `_deliver_simple`), а не отказывать.
4. `db.py:129`: проверять статус-тег `pool.execute` (`INSERT 0 0` → warning + счётчик `db_write_failed` в `/metrics`).
5. Разово почистить прод: удалить/переименовать `""`-строку в `message_events`.

---

## P0 — ГРУППА 2: Потеря сообщений в конвейере (нет at-least-once гарантий)

- **CRIT ✅🔁 (B#1, C#3)** `main.py:840-888` — «мёртвая зона» consume_loop. BRPOP уже снял сообщение; исключение **до** внутреннего try (`pool.fetchval` dedup упал по БД-таймауту `command_timeout=10`; `int(payload.get("user_id",0))` при `user_id:null` → TypeError; `state["wa_message_id"][:12]` при явном `null`) → внешний `except` (`main.py:984`) только логирует+спит → **сообщение исчезло мимо DLQ**. Пик недоступности БД = массовый тихий дроп. Фикс: весь блок после `json.loads` в try с LPUSH в DLQ; нормализовать `None`-поля; dedup-сбой ≠ дроп.
- **CRIT ✅🔁 (A-CRIT, E-H3)** `redis-publisher.js:57-64` — `SET dedup` **до** `LPUSH`, хотя комментарий говорит «LPUSH first». Падение между ними → dedup-ключ стоит 5 мин, сообщение не в очереди → потеряно навсегда. Плюс `whatsapp-client.js:419` catch `[DLQ] Failed to publish` **только логирует**, никуда не пушит; `errorCount` не растёт. Фикс: LPUSH первым, SET dedup после успеха; или Lua/MULTI атомарно.
- **HIGH 🔁 (E-H2, C)** — BRPOP без ACK: краш/редеплой/OOM между pop и терминальным исходом = потеря. Фикс: `BRPOPLPUSH messages:in → messages:processing` (+ дренаж processing на старте) или Redis Streams + consumer group + XACK.
- **HIGH 🔁 (C#4, E-H2)** `main.py:77` — graceful shutdown `task.cancel()`: `CancelledError` (BaseException в 3.12) не ловится ни одним `except Exception` → in-flight сообщение при каждом деплое теряется мимо DLQ. Фикс: drain-фаза (`shield` вокруг обработки одного сообщения) или reliable queue из H2.

---

## P0 — ГРУППА 3: wa-service session/Chromium lifecycle (OOM + reconnect-штормы, single replica)

- **CRIT 📄 (A)** `redis-publisher.js:13-21` — `retryStrategy` возвращает `null` после 10 попыток → ioredis `end`, **больше никогда не реконнектится**. Redis недоступен ~1 мин → каждое сообщение всех юзеров теряется до ручного рестарта. Фикс: бесконечный capped retry (никогда null), либо `process.exit(1)` на `end` → Docker поднимет.
- **CRIT 📄 (A)** `whatsapp-client.js:270-280` — хендлер `disconnected` делает `clients.delete()` + reconnect, **но не `destroy()`** → Chromium (~300-400MB) утекает + держит SingletonLock → все reconnect-попытки бьются о лок → «Session lost» + осиротевший браузер ест память → OOM. Фикс: `await client.destroy()` перед reconnect + точечная чистка SingletonLock.
- **CRIT 📄 (A)** `whatsapp-client.js:334-340` + dedup — `message_edit` дропается в 100%: тот же `id` (dedup глушит в пределах 5 мин) ИЛИ `timestamp` оригинала → age>120с. `is_edited` — мёртвый код. Фикс: отдельный ключ `dedup:msg:{id}:edit:{hash}` + ослабить age для edits.
- **HIGH 📄 (A)** `whatsapp-client.js:62-73,105-124,282-298` — гонка параллельных reconnect (health check + `disconnected`): `clients.delete()` сносит клиент в процессе `initialize()` → два Chromium на одной сессии. Фикс: per-user lock (`Map<userId,Promise>`) + флаг `initializing`.
- **HIGH 📄 (A)** `index.js:9-12` — `process.exit(1)` на любой `unhandledRejection`; puppeteer регулярно их кидает (`Protocol error: Target closed` при destroy) → падение единственной реплики, ре-инит всех сессий (минуты), потеря трафика. Фикс: логировать+метрика для rejections, exit только для `uncaughtException`.
- **HIGH 📄 (A)** `whatsapp-client.js:232-241` — `isReady=true` на `authenticated` (до `ready`) → health check убивает клиента во время синхронизации чатов → reconnect-чурн. Фикс: `isReady` только в `ready` / grace-период.
- **HIGH 📄 (A)** `whatsapp-client.js:197-216,250-261` — `rmSync(sessionDir)` при не-awaited `destroy()`: живой Chromium → ENOTEMPTY / пересоздание лока → «profile in use» до рестарта. Фикс: `await destroy()` перед rmSync + ретрай.
- **HIGH 📄 (A)** `media-handler.js:55-62` — 50MB медиа: base64 (~67MB)+Buffer(50MB), проверка размера **после** аллокации, без семафора → OOM при конкурентных видео. Фикс: семафор 1-2 на `handleMedia` + ранний отсев по `message._data.size`.
- **HIGH 📄 (A)** `whatsapp-client.js:336-340` — `message.timestamp || 0`: при `timestamp=undefined` (тот же дрейф сессии) → age≈1.7млрд → каждое живое сообщение дропается как «старое». Фикс: `!timestamp` → не дропать, warn-лог.

---

## P1 — ГРУППА 4: Корректность доставки в Telegram (тихие отказы, статус=delivered)

- **HIGH 📄 (B)** `telegram_sender.py:176-196` — нет разбиения по лимиту 4096. Длинное WA-сообщение (оригинал+перевод ≈2×, +HTML-escape) → 400 «too long» → `failed`, без ретрая, мимо DLQ → не доедет никогда. Промпт v2.6 специально требует полный перевод длинных. Фикс: чанки ≤4096 по границам строк.
- **HIGH 📄 (B)** `telegram_sender.py:215-262` — caption >1024 → медиа деградирует до голой MinIO-ссылки, но статус `delivered` (нарушение констрейнта «медиа нативно»); кнопка Analyze теряется. Фикс: медиа с усечённым caption, полный текст отдельным reply.
- **HIGH 📄 (B)** `nodes.py:338-341` — миграция supergroup: `str(new_tg_chat_id)` в `bigint`-колонку → asyncpg DataError, проглочен → `chat_pairs` не обновлён навсегда → двойной round-trip на каждое сообщение вечно. Фикс: `int(new_tg_chat_id)`.
- **HIGH 📄 (B)** `nodes.py:190-192` — ветка missing `tg_chat_id` возвращает `failed` **без** `_persist_event` → потеря без следа в БД. Фикс: добавить `await _persist_event(result)`.
- **HIGH 📄 (B)** `nodes.py:138-143` + `cache.py:55-80` — пустой/огрызок перевода кэшируется на 24ч (в т.ч. глобально) → все пары без профиля получают непереведённый текст сутки, статус `delivered`. Фикс: не кэшировать если `not translated` или `len<0.3*len(text)`; пустой перевод = ошибка translate.

---

## P1 — ГРУППА 5: Bot — конкурентность и авторизация

- **CRIT 📄 (D)** `groups.py:21` + `docker-compose.yml:151-166` — `_get_redis()` берёт `REDIS_URL` (default `localhost:6379`), но bot-сервису задан только `REDIS_HOST=redis` → ConnectionRefused в контейнере → весь `my_chat_member` тракт мёртв, Mini App-линковка групп не работает. Фикс: перевести `groups.py` на `REDIS_HOST/PORT/DB` (как `redis_sub.py`) или добавить `REDIS_URL` в compose; try/except вокруг Redis.
- **CRIT 📄 (D)** `main.py:51,94` — нет `.concurrent_updates(True)` + блокирующие хендлеры до ~244с (analyze/translate timeout=120 + retry) → бот замерзает для ВСЕХ при одном тяжёлом апдейте. Фикс: `.concurrent_updates(True)` / `block=False` + снизить таймауты. ⚠️ После включения станет реальной гонка «два первых /start».
- **HIGH 📄 (D)** `analyze.py:54` + `main.py:674-695` — подделка callback `analyze:<чужой event_id>`: ни бот, ни processor не сверяют владельца → анализ чужого медиа + расход денег. Фикс: сверять принадлежность события паре `requested_by`.
- **HIGH 📄 (D)** `chats.py:136-147` — подделка `chat:pause:<чужой pair_id>` → стоп чужого моста. Фикс: `UPDATE ... WHERE id=$2 AND user_id=(...)`.
- **HIGH 📄 (D)** `translate.py:18,65` — нет `is_whitelisted` в ЛС-переводах/медиа → бесплатный LLM (Whisper/vision до 20MB) любому. Фикс: whitelist в начале обоих хендлеров.
- **HIGH 🔁 (D, E-H4)** `redis_sub.py:47-48,106,113-125` — потеря QR-событий: `socket_timeout=5` на блокирующем pub/sub → disconnect каждые 5с → событие в окно реконнекта теряется; + `run_coroutine_threadsafe` future отброшен → исключения не логируются → юзер застревает в `qr_pending` навсегда. Фикс: убрать socket_timeout / `health_check_interval`; `add_done_callback`; поллинг `/status` для qr_pending; реконсиляция из `users.wa_connected` на старте.
- **HIGH 📄 (D)** `http_client.py:47-50` — retry на `TimeoutException` для неидемпотентных POST → двойные LLM-вызовы/записи. Фикс: ретраить POST только на `ConnectError`/`ConnectTimeout`.
- **HIGH 📄 (D)** `groups.py:68,82` — мёртвая кнопка `cmd:add` (нет CallbackQueryHandler) → воронка обрывается. Фикс: зарегистрировать `^cmd:add$`.
- **HIGH 📄 (D)** `chats.py:86-88,105-127` — гонка `user_data` при `/add` в двух группах → линковка не той пары молча. Фикс: токен сессии в callback_data.
- **HIGH 📄 (D)** `wizard.py:169-192` + `db.py:152-169` — `handle_webapp_data` без whitelist (деактивированный юзер реактивируется через ON CONFLICT) + `dict(None)` TypeError. Фикс: `is_whitelisted` + обработать `row is None`.

---

## P1/P2 — ГРУППА 6: Безопасность и инфраструктура

- **HIGH 📄 (E-H1)** `docker-compose.yml:12-13,32-33,48-50` — Redis (без `requirepass`), Postgres (`bridge:bridge`), MinIO — опубликованы на `0.0.0.0` публичного VPS. Нет firewall-конфига в репо. Внешний `FLUSHALL`/дамп БД возможен, если host UFW не закрывает. Фикс: bind `127.0.0.1:` в prod-override, Redis-пароль, сильные creds, UFW.
- **MED 🔁 (E-M1, A)** `docker-compose.yml` — нет `mem_limit` ни у кого на 3.8 GiB → OOM-killer бьёт по wa-service (single replica). `messages:in` растёт без предела при OOM processor. Фикс: per-service `mem_limit` + cap длины очереди/алерт.
- **MED 📄 (E-M2)** миграции только через `docker-entrypoint-initdb.d` (лишь при пустом data-dir) → новые `NNN_*.sql` не применяются автоматом, нет `schema_migrations` → schema drift без детекта. Фикс: migration runner + tracking table / version-check в health.
- **MED 📄 (E-M3)** `007_media_analysis.sql:8` — FK `media_analysis.message_event_id integer` ссылается на `message_events.id bigint` → сломается при id>2^31. Фикс: `ALTER COLUMN ... TYPE bigint`.
- **HIGH 🔁 (C#5, E-L3)** `feature_flags.py:33-64` — fail-**open**: при недоступной БД отключённые флаги (напр. `translation_enabled`, выключенный из-за расходов) молча включаются обратно (env default `true`); ошибки Redis глотаются `except: pass`. Фикс: last-known-value в памяти как fallback-2; конфигурируемый env-default для «опасных» флагов; rate-limited лог.
- **HIGH 🔁 (C#6, D, B)** повсеместное глотание ошибок персистенса (`db.py:131` `except → logger.error → return None`) без метрик → отказы невидимы (именно это скрыло заморозку `message_events` на 7 дней). Фикс: счётчик `db_write_failed` в `/metrics` + SSE `persist_failed`.
- **LOW 📄 (E-L1)** nginx не проксирует `/api/dlq`, `/api/dlq/retry`, `/api/flags`, `/api/config`, `/metrics` → падают в `location /` на wa-service:3000 (404). Админ-API недоступны через домен. Фикс: добавить auth'd `location` блоки (подтвердить намерение).

---

## Сквозные корневые причины (для стратегии Opus)

1. **Один id — два потребителя, но пропагируется в один.** `fallbackDedupId` → только Redis-ключ, не payload. Единый фикс закрывает Группу 1 целиком.
2. **At-most-once очередь.** BRPOP без ACK + SET-до-LPUSH + cancel на shutdown → потеря на каждом сбое/деплое. Reliable-queue (`BRPOPLPUSH`/Streams) закрывает Группу 2.
3. **«Удалить из Map» ≠ «уничтожить клиента».** Утечки Chromium, гонки reconnect, штормы (Группа 3).
4. **Тихое глотание ошибок без телеметрии.** Скрыло 7-дневную заморозку; добавить `db_write_failed`/алерты — предохранитель для всех групп.
