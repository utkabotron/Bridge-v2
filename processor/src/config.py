"""Centralized configuration for the processor service.

All env vars with typed defaults in one place. Model prices live in
shared/bridge_shared/llm.py — analytics bills by the same table.
"""
import os

from bridge_shared.env import admin_tg_ids, parse_ids

# ── Redis ────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))
# Empty/unset = no AUTH, matching a Redis started with an empty requirepass: a missing .env
# line must leave the queue reachable rather than lock every client out.
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
BRPOP_TIMEOUT = int(os.getenv("BRPOP_TIMEOUT", 5))
# Workers pulling from messages:in. One worker meant a slow OpenAI or Telegram reply (up to
# minutes with retries) held every chat of every user; chats are independent, so several
# run side by side while messages of one chat still go one at a time, in order.
CONSUMER_WORKERS = int(os.getenv("CONSUMER_WORKERS", 4))
# MUST stay strictly greater than BRPOP_TIMEOUT. redis-py 8.x applies socket_timeout to
# blocking commands too, so an equal (or unset — it then defaults to something shorter than
# the server-side block) value makes every idle brpop die with "Timeout reading from redis"
# instead of returning None: 1395 bogus ERROR lines a day and a reconnect every minute.
REDIS_SOCKET_TIMEOUT = int(os.getenv("REDIS_SOCKET_TIMEOUT", BRPOP_TIMEOUT + 5))
REDIS_CONNECT_TIMEOUT = int(os.getenv("REDIS_CONNECT_TIMEOUT", 5))


def redis_kwargs(**overrides) -> dict:
    """Connection kwargs shared by every Redis client in the processor."""
    kwargs = {
        "host": REDIS_HOST,
        "port": REDIS_PORT,
        "db": REDIS_DB,
        "password": REDIS_PASSWORD,
        "decode_responses": True,
        "socket_timeout": REDIS_SOCKET_TIMEOUT,
        "socket_connect_timeout": REDIS_CONNECT_TIMEOUT,
        "health_check_interval": 30,
    }
    kwargs.update(overrides)
    return kwargs

# ── Database ─────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://bridge:bridge@postgres:5432/bridge")
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", 2))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", 10))
DB_COMMAND_TIMEOUT = int(os.getenv("DB_COMMAND_TIMEOUT", 10))

# ── Telegram ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_SEND_TIMEOUT = int(os.getenv("TELEGRAM_SEND_TIMEOUT", 30))
# Longest Telegram retry_after we honour before giving up on this attempt: a worker stuck
# for a minute on one chat is a minute lost for that chat, not for the others any more.
MAX_RETRY_AFTER = int(os.getenv("MAX_RETRY_AFTER", 20))
TARGET_LANGUAGE = os.getenv("TARGET_LANGUAGE", "Hebrew")

# ── Alerting ─────────────────────────────────────────────
ADMIN_TG_IDS = admin_tg_ids()
# Bridge translation model — variant A of the prompt/model A/B (prompts.VARIANTS).
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
# Model for everything outside the bridge A/B: bot DM translation and media analysis.
# OPENAI_MODEL stays variant A of the A/B, so promoting a model here does not touch it.
DIRECT_MODEL = os.getenv("DIRECT_MODEL") or OPENAI_MODEL
# whisper-1 is deprecated (shutdown 2027-02-26). gpt-transcribe is OpenAI's replacement,
# 25% cheaper, and on short clips it does not misdetect the language the way whisper did.
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-transcribe")
# Users whose chats always get A/B variant B while the flag is on — the admin dogfoods the
# candidate in every chat instead of half of them. Defaults to the admins.
AB_ALWAYS_B_USERS = parse_ids(os.getenv("AB_ALWAYS_B_USERS")) or ADMIN_TG_IDS
UNAUTH_WINDOW = int(os.getenv("UNAUTH_WINDOW", 900))
UNAUTH_THRESHOLD = int(os.getenv("UNAUTH_THRESHOLD", 3))
FAILURE_RATE_WINDOW = int(os.getenv("FAILURE_RATE_WINDOW", 900))
FAILURE_RATE_THRESHOLD = float(os.getenv("FAILURE_RATE_THRESHOLD", 0.05))
FAILURE_RATE_MIN_MSGS = int(os.getenv("FAILURE_RATE_MIN_MSGS", 5))
# Untranslated deliveries in a window before admins hear about it. An exhausted OpenAI
# balance alerts on the first one — it never heals by itself.
TRANSLATION_FAIL_WINDOW = int(os.getenv("TRANSLATION_FAIL_WINDOW", 900))
TRANSLATION_FAIL_THRESHOLD = int(os.getenv("TRANSLATION_FAIL_THRESHOLD", 3))
TRANSLATION_ALERT_COOLDOWN = int(os.getenv("TRANSLATION_ALERT_COOLDOWN", 3600))
OPENAI_BILLING_URL = "https://platform.openai.com/settings/organization/billing"

# ── Unpaired chats ──────────────────────────────────────
# A WhatsApp message from a chat with no active pair used to fall back to the admin's own
# Telegram chat. Off: every unpaired chat of the admin's number became a firehose into the
# bot. Set to true to restore the fallback.
ADMIN_NO_PAIR_FALLBACK = os.getenv("ADMIN_NO_PAIR_FALLBACK", "false").lower() == "true"

# ── Dead-letter queue ───────────────────────────────────
# Nothing drained the DLQ before; messages that failed during a brief OpenAI or Telegram
# outage stayed there until someone clicked retry in the dashboard.
DLQ_RETRY_INTERVAL = int(os.getenv("DLQ_RETRY_INTERVAL", 600))
DLQ_RETRY_BATCH = int(os.getenv("DLQ_RETRY_BATCH", 50))
DLQ_MAX_ATTEMPTS = int(os.getenv("DLQ_MAX_ATTEMPTS", 5))
DLQ_ALERT_THRESHOLD = int(os.getenv("DLQ_ALERT_THRESHOLD", 20))
DLQ_ALERT_COOLDOWN = int(os.getenv("DLQ_ALERT_COOLDOWN", 3600))
# Messages taken off messages:in but not yet finished. BRPOP deleted them outright, so a
# process killed mid-message lost it; the consumer returns anything stranded here on start.
PROCESSING_QUEUE = os.getenv("PROCESSING_QUEUE", "messages:processing")

# ── LLM ─────────────────────────────────────────────────
# The consumer processes one message at a time, so a slow LLM call is head-of-line
# blocking for every user. Fail fast and deliver the original instead.
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", 30))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", 1))  # 2 attempts x LLM_TIMEOUT worst case
TRANSLATION_UNAVAILABLE_NOTE = os.getenv(
    "TRANSLATION_UNAVAILABLE_NOTE", "⚠️ Перевод временно недоступен",
)

# ── Voice notes ─────────────────────────────────────────
# A voice note is opaque to someone who does not speak the language — transcribe and
# translate it automatically rather than hiding it behind a button. Runtime toggle lives
# in the feature_flags table as voice_transcribe_enabled.
VOICE_AUTO_TRANSCRIBE = os.getenv("VOICE_AUTO_TRANSCRIBE", "true").lower() == "true"
VOICE_TRANSCRIPT_TITLE = os.getenv("VOICE_TRANSCRIPT_TITLE", "Расшифровка")

# ── Message presentation ────────────────────────────────
MEDIA_FAILED_NOTE = os.getenv("MEDIA_FAILED_NOTE", "📎 Не удалось получить {kind}")
EDITED_MARK = os.getenv("EDITED_MARK", "✏️ изменено")
OWN_MESSAGE_PREFIX = os.getenv("OWN_MESSAGE_PREFIX", "➡️")
REVOKE_NOTE = os.getenv("REVOKE_NOTE", "🗑 Сообщение удалено в WhatsApp")

# ── Cache TTLs ───────────────────────────────────────────
TRANSLATION_CACHE_TTL = int(os.getenv("TRANSLATION_CACHE_TTL", 86400))
PROFILE_CACHE_TTL = int(os.getenv("PROFILE_CACHE_TTL", 3600))
# Service glossary (pipeline/glossary.py) lives in memory; this is how often a message may
# check whether the tables changed. /api/glossary edits apply at once regardless.
GLOSSARY_REFRESH_SECONDS = int(os.getenv("GLOSSARY_REFRESH_SECONDS", 60))
# Statuses that reach the prompt. `verified` is off until names that are also everyday words
# are handled: with them on, "אני עמוס היום" came out "Я сегодня Амос" (docs/glossary-plan.md).
GLOSSARY_USED_STATUSES = tuple(
    s.strip() for s in os.getenv("GLOSSARY_USED_STATUSES", "locked").split(",") if s.strip())
MEDIA_CACHE_TTL = int(os.getenv("MEDIA_CACHE_TTL", 86400))
# Active pairs of a WhatsApp chat, looked up for every message (pipeline/cache.py). Pair
# changes made outside the processor (wa-service, bot) do not invalidate the key yet, so
# "no pair" — the common answer, every unbridged chat gets it — is kept only briefly: a
# freshly created pair must not wait out an hour of cached "none".
PAIRS_CACHE_TTL = int(os.getenv("PAIRS_CACHE_TTL", 3600))
PAIRS_NEGATIVE_CACHE_TTL = int(os.getenv("PAIRS_NEGATIVE_CACHE_TTL", 60))
# Per-process copy of a feature flag, in front of its Redis read (feature_flags.is_enabled
# runs several times per message). set_flag clears it, so a toggle is not delayed.
FLAG_MEMORY_TTL = int(os.getenv("FLAG_MEMORY_TTL", 5))

# ── Dashboard stats ──────────────────────────────────────
# /api/stats is polled every 15s by every open dashboard tab. Counting message_events
# since the beginning of time per user per poll grew with the table; a bounded window plus
# a short in-process cache keeps it flat.
STATS_WINDOW_DAYS = int(os.getenv("STATS_WINDOW_DAYS", 30))
STATS_CACHE_TTL = int(os.getenv("STATS_CACHE_TTL", 60))

# ── Media analysis ───────────────────────────────────────
IMAGE_ANALYSIS_TIMEOUT = int(os.getenv("IMAGE_ANALYSIS_TIMEOUT", 60))
AUDIO_ANALYSIS_TIMEOUT = int(os.getenv("AUDIO_ANALYSIS_TIMEOUT", 120))
DOCUMENT_ANALYSIS_TIMEOUT = int(os.getenv("DOCUMENT_ANALYSIS_TIMEOUT", 60))

# ── S3/MinIO ────────────────────────────────────────────
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
# Host Telegram (and the user) reach media on. The bucket is no longer world-readable,
# so links built on this endpoint must be presigned — see src/s3.py.
S3_PUBLIC_URL = os.getenv("S3_PUBLIC_URL", "http://localhost:9000")
