---
description: Читает последний Weekly Intelligence Report из БД, применяет кодовые рекомендации через план→реализацию→тесты, деплоит и рассылает changelog пользователям в Telegram
---

# Weekly Improve Skill

Полный цикл применения еженедельных рекомендаций: DB → анализ → план → код → тесты → деплой → broadcast.

## Steps

### 1. Читаем последний weekly report из production DB

```bash
ssh bridge "docker compose -f /home/deploy/bridge-v2/docker-compose.yml exec -T postgres psql -U bridge -d bridge -c \"SELECT week_start, executive_summary, deep_analysis::text, recommendations::text FROM weekly_insights ORDER BY week_start DESC LIMIT 2;\""
```

Выводим пользователю:
- `week_start` и `executive_summary` последнего отчёта
- Таблицу рекомендаций: **priority | area | action | metric_to_track**
- Тренд качества: последние 2 записи `deep_analysis.translation_quality.avg_scores`

### 2. Классифицируем рекомендации

Делим на два списка:

**Кодовые** (реализуем): delivery, prompt, infrastructure, monitoring, analytics
**Операционные** (сообщаем пользователю, не кодируем):
- "Rotate token" / "rotate bot token" → попросить пользователя вручную сменить TELEGRAM_BOT_TOKEN
- "Audit chat pairs" / "manual audit" → сообщить какие пары нужно проверить
- Любое "manually" или "in dashboard" действие

Операционные рекомендации — вывести списком, объяснить что нужно сделать руками, и продолжить с кодовыми.

### 3. Применяем кодовые рекомендации через план

Для каждой кодовой рекомендации (по порядку priority):

1. Исследовать затронутые файлы через Explore agent
2. Войти в plan mode (`EnterPlanMode`) — составить план реализации
3. Дождаться одобрения пользователя
4. Реализовать изменения

**Ключевые файлы проекта для ориентира:**
- Промпт перевода: `processor/src/pipeline/prompts.py` (PROMPT_VERSION, SYSTEM_TRANSLATE)
- Доставка/ошибки: `processor/src/telegram_sender.py`, `processor/src/pipeline/nodes.py`
- Мониторинг: `analytics/flows/health_check.py`, `analytics/flows/nightly_problems.py`
- Кэш: `processor/src/pipeline/cache.py`, `processor/src/pipeline/nodes.py`
- Аналитика: `analytics/flows/translation_quality.py`, `analytics/flows/weekly_report.py`
- Bot команды: `bot/src/handlers/admin.py`, `bot/src/main.py`

### 4. Тесты

После всех изменений — запустить тесты через `/test` или вручную:

```bash
cd wa-service && npx jest --forceExit 2>&1
cd processor && python3 -m pytest tests/ -v 2>&1
cd bot && python3 -m pytest tests/ -v 2>&1
```

Если тесты падают — исправить до коммита. НЕ деплоить с упавшими тестами.

### 5. Коммит

```bash
git add -A
git commit -m "feat: apply weekly report recommendations $(date +%Y-%m-%d)"
```

### 6. Деплой

Вызвать `/deploy` с именами изменённых сервисов. Обычно: `processor bot analytics`.

### 7. Составляем changelog для пользователей

Сформировать сообщение в HTML-формате для Telegram:

**Структура "проблема → решение":**
```
<b>Bridge — обновление</b>

За эту неделю исправили:

<b>[Версия/Дата]</b>
<i>Проблема:</i> [что не работало — понятно пользователю]
<i>Решение:</i> [что сделали — без технических деталей]

...

Качество перевода (авто-оценка 1–5):
прошлая неделя — <b>X.XX</b> → сейчас — <b>X.XX</b>
```

**Правила для текста:**
- Писать на русском
- Проблемы — с точки зрения пользователя (что он замечал), не технически
- Не упоминать внутренние названия (LangGraph, asyncpg, Redis)
- Качество брать из `deep_analysis.translation_quality.avg_scores` двух последних отчётов
- Показать текст пользователю и подождать подтверждения перед отправкой

### 8. Broadcast через processor container

После подтверждения — создать Python-скрипт и запустить через processor:

```python
# /tmp/broadcast_weekly.py
import asyncio, os, asyncpg, httpx, json

MSG = "... HTML текст ..."  # только ASCII + unicode escapes, БЕЗ сырых эмодзи в строковых литералах

async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    db_url = os.getenv("DATABASE_URL")
    pool = await asyncpg.create_pool(db_url)
    users = await pool.fetch("SELECT tg_user_id FROM users WHERE is_active = true")
    await pool.close()
    sent, failed = 0, 0
    async with httpx.AsyncClient(timeout=10) as client:
        for u in users:
            # ВАЖНО: ensure_ascii=False + content= вместо json= чтобы избежать UnicodeEncodeError
            payload = json.dumps(
                {"chat_id": u["tg_user_id"], "text": MSG, "parse_mode": "HTML"},
                ensure_ascii=False,
            ).encode("utf-8")
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                content=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            if r.status_code == 200:
                sent += 1
            else:
                failed += 1
                print(f"FAIL {u['tg_user_id']}: {r.text[:100]}")
    print(f"{sent} отправлено, {failed} ошибок")

asyncio.run(main())
```

Запуск:
```bash
scp /tmp/broadcast_weekly.py bridge:/tmp/broadcast_weekly.py
ssh bridge "docker compose -f /home/deploy/bridge-v2/docker-compose.yml cp /tmp/broadcast_weekly.py processor:/tmp/broadcast_weekly.py"
ssh bridge "docker compose -f /home/deploy/bridge-v2/docker-compose.yml exec -T processor python3 /tmp/broadcast_weekly.py"
```

### 9. Итоговый отчёт

Вывести пользователю:
- Список применённых рекомендаций с кратким описанием
- Список операционных рекомендаций (что нужно сделать вручную)
- Результат рассылки (N отправлено, M ошибок)
- Commit hash

## Важные ограничения

- **wa-service не трогаем** если нет явных рекомендаций по нему — он работает 24/7 с WA-сессиями
- **Не создавать новые таблицы** без миграции — схема в `postgres/initdb.d/`
- **После `docker compose up -d`** — всегда `docker compose restart nginx`
- **Broadcast эмодзи**: использовать unicode escapes (`\U0001F4CA`) а не сырые символы в Python-строках внутри heredoc/scp, или строить MSG из списка строк через `"\n".join(lines)`
