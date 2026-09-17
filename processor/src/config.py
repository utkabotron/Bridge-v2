"""Centralized configuration for the processor service.

All env vars with typed defaults in one place.
"""
import os

# ── Redis ────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))
BRPOP_TIMEOUT = int(os.getenv("BRPOP_TIMEOUT", 5))
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
MAX_RETRY_AFTER = int(os.getenv("MAX_RETRY_AFTER", 60))
TARGET_LANGUAGE = os.getenv("TARGET_LANGUAGE", "Hebrew")

# ── Alerting ─────────────────────────────────────────────
ADMIN_TG_IDS = [int(x) for x in os.getenv("ADMIN_TG_IDS", "").split(",") if x.strip()]
UNAUTH_WINDOW = int(os.getenv("UNAUTH_WINDOW", 900))
UNAUTH_THRESHOLD = int(os.getenv("UNAUTH_THRESHOLD", 3))
FAILURE_RATE_WINDOW = int(os.getenv("FAILURE_RATE_WINDOW", 900))
FAILURE_RATE_THRESHOLD = float(os.getenv("FAILURE_RATE_THRESHOLD", 0.05))
FAILURE_RATE_MIN_MSGS = int(os.getenv("FAILURE_RATE_MIN_MSGS", 5))

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
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", 2))
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
MEDIA_CACHE_TTL = int(os.getenv("MEDIA_CACHE_TTL", 86400))
COSTS_CACHE_TTL = int(os.getenv("COSTS_CACHE_TTL", 900))

# ── Media analysis ───────────────────────────────────────
IMAGE_ANALYSIS_TIMEOUT = int(os.getenv("IMAGE_ANALYSIS_TIMEOUT", 60))
AUDIO_ANALYSIS_TIMEOUT = int(os.getenv("AUDIO_ANALYSIS_TIMEOUT", 120))
DOCUMENT_ANALYSIS_TIMEOUT = int(os.getenv("DOCUMENT_ANALYSIS_TIMEOUT", 60))

# ── S3/MinIO ────────────────────────────────────────────
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
# Host Telegram (and the user) reach media on. The bucket is no longer world-readable,
# so links built on this endpoint must be presigned — see src/s3.py.
S3_PUBLIC_URL = os.getenv("S3_PUBLIC_URL", "http://localhost:9000")

# ── LangSmith ───────────────────────────────────────────
LANGCHAIN_PROJECT = os.getenv("LANGCHAIN_PROJECT", "bridge-v2")
