"""Prefect flow: the one morning message.

Three nightly reports (problems, translation quality, chat context) and a fourth on
Mondays each sent their own Telegram message, in English, with LLM prose, full worst
translations and every new glossary entry. The admin needs one glance: is the system
up, what went through, how good was it, who is using it. Everything else stays in the
database and on the dashboard.

Deploy:
  cron "20 5 * * *" — after nightly-problems (04:00), translation-quality (04:30),
  chat-context-builder (05:00) and, on Mondays, weekly-report (05:00).
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime

import httpx
from bridge_shared import glossary_review
from prefect import flow, get_run_logger, task

from .shared import db_conn, esc, notify_telegram, redis_client

WA_SERVICE_URL = os.getenv("WA_SERVICE_URL", "http://wa-service:3000")
PROCESSOR_URL = os.getenv("PROCESSOR_URL", "http://processor:8000")
DISK_PCT_THRESHOLD = int(os.getenv("DISK_PCT_THRESHOLD", "85"))

MAX_USERS = 12

_DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


# ── Collection ────────────────────────────────────────────

def _system_state() -> dict:
    """WhatsApp clients, queues, disk, processor counters. Each part fails on its own."""
    out: dict = {}
    try:
        data = httpx.get(f"{WA_SERVICE_URL}/health", timeout=10).json()
        out["wa_ready"] = data.get("readyClients", 0)
        out["wa_active"] = data.get("activeClients", 0)
        out["wa_ready_ids"] = {c.get("userId") for c in data.get("clients", []) if c.get("isReady")}
    except Exception as exc:
        out["wa_error"] = str(exc)[:80]
    try:
        r = redis_client()
        out["queue"] = r.llen("messages:in") + r.llen("messages:processing")
        out["dlq"] = r.llen("messages:dlq")
        out["dlq_dead"] = r.llen("messages:dlq:dead")
    except Exception as exc:
        out["redis_error"] = str(exc)[:80]
    try:
        usage = shutil.disk_usage("/")
        out["disk_pct"] = round(usage.used / usage.total * 100)
    except Exception:
        pass
    try:
        out["processor"] = httpx.get(f"{PROCESSOR_URL}/metrics", timeout=5).json()
    except Exception as exc:
        out["processor_error"] = str(exc)[:80]
    return out


_ADDED_RE = re.compile(r"\+(\d+) glossary")
_REMOVED_RE = re.compile(r"(?:removed|dropped) (\d+)")


@task(retries=2, name="collect-digest")
def collect_digest() -> dict:
    """Yesterday's numbers, this morning's analyses, the users — one dict for the formatter."""
    logger = get_run_logger()
    with db_conn() as conn:
        cur = conn.cursor()
        data: dict = {"date": date.today(), "system": _system_state()}

        # Yesterday's traffic
        cur.execute("""
            SELECT
                count(*) FILTER (WHERE delivery_status = 'delivered') AS delivered,
                count(*) FILTER (WHERE delivery_status = 'skipped') AS skipped,
                count(*) FILTER (WHERE delivery_status = 'failed') AS failed,
                count(*) FILTER (WHERE delivery_status = 'delivered' AND translation_ms > 0
                                 AND NOT translation_failed) AS translated,
                count(*) FILTER (WHERE delivery_status = 'delivered' AND cache_hit) AS cached,
                count(*) FILTER (WHERE translation_failed) AS untranslated,
                count(*) FILTER (WHERE translation_passthrough) AS passthrough,
                count(*) FILTER (WHERE delivery_status = 'delivered'
                                 AND message_type NOT IN ('chat', 'text')) AS media
            FROM message_events
            WHERE created_at >= current_date - interval '1 day' AND created_at < current_date
        """)
        data["traffic"] = dict(cur.fetchone())

        # This morning's quality run
        cur.execute("""
            SELECT summary FROM nightly_analysis_runs
            WHERE flow_type = 'translation_quality' AND run_date = current_date
        """)
        row = cur.fetchone()
        summary = None
        if row:
            summary = row["summary"] if isinstance(row["summary"], dict) else json.loads(row["summary"] or "{}")
        data["quality"] = summary
        worst = (summary or {}).get("worst_pair")
        if worst:
            cur.execute("SELECT wa_chat_name FROM chat_pairs WHERE id = %s", (worst["key"],))
            r = cur.fetchone()
            data["worst_pair_name"] = (r["wa_chat_name"] if r else "") or ""
        cur.execute("SELECT count(*) AS n FROM prompt_suggestions WHERE status = 'pending'")
        data["pending_suggestions"] = cur.fetchone()["n"]

        # This morning's problems run: only what deserves attention
        cur.execute("""
            SELECT di.severity, di.title
            FROM detected_issues di
            JOIN nightly_analysis_runs nar ON nar.id = di.run_id
            WHERE nar.flow_type = 'problems' AND nar.run_date = current_date
              AND di.severity IN ('critical', 'warning')
            ORDER BY CASE di.severity WHEN 'critical' THEN 0 ELSE 1 END, di.id
        """)
        data["issues"] = [dict(r) for r in cur.fetchall()]

        # Glossary movement this morning (the context builder writes a history row per change)
        cur.execute("""
            SELECT change_summary FROM chat_profile_history
            WHERE created_at >= current_date
        """)
        added = removed = 0
        for r in cur.fetchall():
            s = r["change_summary"] or ""
            added += sum(int(m) for m in _ADDED_RE.findall(s))
            removed += sum(int(m) for m in _REMOVED_RE.findall(s))
        data["glossary"] = {"added": added, "removed": removed}

        # The service glossary of names: what the resolver settled by itself, what waits
        cur.execute("""
            SELECT count(*) FILTER (WHERE decided_by = 'auto' AND status = 'verified'
                                    AND decided_at >= current_date - interval '1 day') AS auto_accepted,
                   count(*) FILTER (WHERE status = 'proposed') AS proposed
            FROM glossary
        """)
        data["names"] = dict(cur.fetchone())

        # Users: who is connected, how many bridges, what went through for them yesterday
        cur.execute("""
            SELECT u.tg_user_id, u.tg_username, u.wa_connected,
                   (SELECT count(*) FROM chat_pairs cp WHERE cp.user_id = u.id AND cp.status = 'active') AS pairs,
                   (SELECT count(*) FROM message_events me JOIN chat_pairs cp ON cp.id = me.chat_pair_id
                    WHERE cp.user_id = u.id AND me.delivery_status = 'delivered'
                      AND me.created_at >= current_date - interval '1 day' AND me.created_at < current_date) AS delivered,
                   (SELECT max(me.created_at) FROM message_events me JOIN chat_pairs cp ON cp.id = me.chat_pair_id
                    WHERE cp.user_id = u.id AND me.delivery_status = 'delivered') AS last_delivered
            FROM users u
            WHERE u.is_active
            ORDER BY delivered DESC, pairs DESC
        """)
        data["users"] = [dict(r) for r in cur.fetchall()]

        # Monday: the weekly report, if it has finished
        cur.execute("""
            SELECT executive_summary, recommendations FROM weekly_insights
            WHERE created_at >= current_date ORDER BY created_at DESC LIMIT 1
        """)
        row = cur.fetchone()
        if row:
            recs = row["recommendations"]
            if isinstance(recs, str):
                recs = json.loads(recs or "[]")
            data["weekly"] = {"summary": row["executive_summary"], "recommendations": recs or []}
        elif date.today().weekday() == 0:
            data["weekly"] = {"pending": True}

    logger.info("Digest collected: %s", {k: v for k, v in data["traffic"].items()})
    return data


# ── Formatting ────────────────────────────────────────────

def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _when(ts, today: date) -> str:
    if not ts:
        return "—"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    if ts.date() == today:
        return "сегодня " + ts.strftime("%H:%M")
    delta = (today - ts.date()).days
    if delta == 1:
        return "вчера " + ts.strftime("%H:%M")
    return ts.strftime("%d.%m")


def _system_line(system: dict) -> tuple[str, list[str]]:
    """One status line, and the problems that should go under 'Внимание'."""
    attention: list[str] = []
    parts: list[str] = []
    ok = True

    if "wa_error" in system:
        parts.append("WA ❌")
        attention.append(f"wa-service не отвечает: {esc(system['wa_error'])}")
        ok = False
    else:
        ready, active = system.get("wa_ready", 0), system.get("wa_active", 0)
        parts.append(f"WA {ready}/{active}")
        if ready < active or active == 0:
            ok = False
            attention.append(f"WhatsApp: готовы {ready} из {active} клиентов")

    if "redis_error" in system:
        parts.append("очереди ?")
        ok = False
    else:
        parts.append(f"очереди {system.get('queue', 0)}")
        dlq, dead = system.get("dlq", 0), system.get("dlq_dead", 0)
        parts.append(f"DLQ {dlq}")
        if dlq:
            attention.append(f"DLQ: {dlq} в повторе")
        if dead:
            ok = False
            attention.append(f"DLQ: {dead} сдались (messages:dlq:dead)")

    if "disk_pct" in system:
        parts.append(f"диск {system['disk_pct']}%")
        if system["disk_pct"] >= DISK_PCT_THRESHOLD:
            ok = False
            attention.append(f"диск заполнен на {system['disk_pct']}%")

    proc = system.get("processor")
    if proc is None:
        parts.append("processor ❌")
        ok = False
        attention.append("processor не отвечает на /metrics")
    elif proc.get("db_write_failed"):
        ok = False
        attention.append(f"processor: {proc['db_write_failed']} записей в БД не прошли")

    status = "✅" if ok else "⚠️"
    return f"<b>Система:</b> {status} " + " · ".join(parts), attention


def format_digest(data: dict) -> str:
    today: date = data["date"]
    title = f"{_DAYS[today.weekday()]} {today.day} {_MONTHS[today.month - 1]}"
    lines = [f"☀️ <b>Bridge — {title}</b>", ""]

    system_line, attention = _system_line(data.get("system", {}))
    t = data.get("traffic", {})
    untranslated = t.get("untranslated", 0) or 0
    if untranslated:
        system_line += " · OpenAI ⚠️"
        attention.append(f"{untranslated} сообщ. ушли без перевода (OpenAI)")
    else:
        system_line += " · OpenAI ок"
    lines.append(system_line)

    lines.append(
        f"<b>За сутки:</b> {t.get('delivered', 0)} доставлено · {t.get('skipped', 0)} без моста"
        f" · {t.get('failed', 0)} ошибок"
    )
    extra = f"  переводов {t.get('translated', 0)} · кэш {t.get('cached', 0)} · медиа {t.get('media', 0)}"
    if t.get("passthrough"):
        extra += f" · эхо {t['passthrough']}"
    lines.append(extra)
    if t.get("failed"):
        attention.append(f"{t['failed']} сообщ. не доставлены в Telegram")

    q = data.get("quality")
    if q:
        bridge = ((q.get("breakdown") or {}).get("by_source") or {}).get("bridge") or {}
        avg = (q.get("avg_scores") or {}).get("quality")
        qline = f"<b>Качество:</b> {avg if avg is not None else '—'}"
        if bridge.get("n"):
            qline += f" · плохих {bridge.get('bad_pct') or 0}% (n={bridge['n']})"
        worst = q.get("worst_pair")
        if worst:
            name = data.get("worst_pair_name") or ""
            qline += f" · худшая пара #{worst['key']}"
            if name:
                qline += f" {esc(name[:24])}"
            qline += f" ({worst['bad']} из {worst['n']})"
        lines.append(qline)
        versions = [r for r in ((q.get("breakdown") or {}).get("by_prompt_version") or []) if r.get("key")]
        if len(versions) >= 2:
            lines.append("<b>A/B:</b> " + " vs ".join(
                f"{esc(str(r['key']))} {r['quality']} (n={r['n']}, плохих {r['bad_pct'] or 0}%)"
                for r in sorted(versions, key=lambda r: str(r["key"]))
            ))
        jev = q.get("jev") or {}
        if jev.get("fallback"):
            attention.append("Jev недоступен, оценка только LLM")
    else:
        lines.append("<b>Качество:</b> ночная оценка не отработала")
        attention.append("translation-quality не записал результат за сегодня")

    g = data.get("glossary") or {}
    if g.get("added") or g.get("removed"):
        lines.append(f"<b>Глоссарий:</b> +{g.get('added', 0)} · снято {g.get('removed', 0)}")
    names = data.get("names") or {}
    if names.get("auto_accepted") or names.get("proposed"):
        lines.append(f"<b>Словарь имён:</b> принято автоматически {names.get('auto_accepted') or 0}"
                     f" · ждут одобрения {names.get('proposed') or 0}")

    # Users
    users = data.get("users") or []
    ready_ids = data.get("system", {}).get("wa_ready_ids")
    def connected(u: dict) -> bool:
        if ready_ids is not None:
            return u["tg_user_id"] in ready_ids
        return bool(u.get("wa_connected"))
    online = [u for u in users if connected(u)]
    offline = [u for u in users if not connected(u)]
    lines.append("")
    lines.append(f"<b>Пользователи</b> ({len(online)} из {len(users)} подключены):")
    for u in online[:MAX_USERS]:
        name = u.get("tg_username") or str(u["tg_user_id"])
        pairs = u.get("pairs") or 0
        lines.append(
            f"<code>{esc(name[:14]):<14}</code> {pairs} {_plural(pairs, 'пара', 'пары', 'пар')}"
            f" · {u.get('delivered') or 0} · {_when(u.get('last_delivered'), today)}"
        )
    if offline:
        names = ", ".join(esc(u.get("tg_username") or str(u["tg_user_id"])) for u in offline[:MAX_USERS])
        lines.append(f"не подключены: {names}")

    # Attention
    for issue in data.get("issues") or []:
        mark = "🚨" if issue["severity"] == "critical" else "⚠️"
        attention.append(f"{mark} {esc(issue['title'][:90])}")
    pending = data.get("pending_suggestions") or 0
    if pending:
        attention.append(f"{pending} {_plural(pending, 'предложение', 'предложения', 'предложений')} к промпту ждут")
    if attention:
        lines.append("")
        lines.append("⚠️ <b>Внимание:</b>")
        lines.extend(f"• {a}" for a in attention)

    # Monday
    weekly = data.get("weekly")
    if weekly:
        lines.append("")
        if weekly.get("pending"):
            lines.append("📈 <b>Неделя:</b> отчёт ещё считается, будет на дашборде")
        else:
            lines.append(f"📈 <b>Неделя:</b> {esc(weekly.get('summary') or '')}")
            for i, rec in enumerate(weekly.get("recommendations") or [][:3], 1):
                if i > 3:
                    break
                lines.append(f"{i}. {esc(rec.get('action', ''))}")

    return "\n".join(lines)


@task(retries=1, name="send-digest")
def send_digest(text: str) -> int:
    return notify_telegram(text, timeout=15)


def review_message() -> tuple[str, dict] | None:
    """The first batch of proposed names with ✅ / ✏️ / ❌ buttons; None when nothing waits."""
    with db_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT {glossary_review.REVIEW_COLUMNS} FROM glossary
            WHERE status = 'proposed' ORDER BY chats_seen DESC, id LIMIT %s
        """, (glossary_review.BATCH,))
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return None
        cur.execute("SELECT count(*) AS n FROM glossary WHERE status = 'proposed'")
        total = cur.fetchone()["n"]
    text, keyboard = glossary_review.build(rows, total - len(rows))
    markup = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row]
                                  for row in keyboard]}
    return text, markup


@task(retries=1, name="send-name-review")
def send_name_review() -> int:
    message = review_message()
    if message is None:
        return 0
    return notify_telegram(message[0], timeout=15, reply_markup=message[1])


@flow(name="daily-digest", log_prints=True)
def daily_digest():
    """Collect → format → one Telegram message to the admins, then the names to review."""
    data = collect_digest()
    text = format_digest(data)
    sent = send_digest(text)
    review = send_name_review()
    return {"sent": sent, "chars": len(text), "name_review": review}


if __name__ == "__main__":
    if os.getenv("DIGEST_DRY_RUN"):
        # Outside a Prefect run there is no run logger; print the message instead of sending it.
        import logging

        globals()["get_run_logger"] = lambda: logging.getLogger("digest")
        print(format_digest(collect_digest.fn()))
        review = review_message()
        if review:
            print("\n" + review[0] + "\n" + "\n".join(" ".join(b["text"] for b in row)
                                                      for row in review[1]["inline_keyboard"]))
    else:
        daily_digest()
