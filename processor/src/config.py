"""Centralized configuration for the processor service.

All env vars with typed defaults in one place.
"""
import os

# ── Redis ────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))
BRPOP_TIMEOUT = int(os.getenv("BRPOP_TIMEOUT", 5))

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

# ── LangSmith ───────────────────────────────────────────
LANGCHAIN_PROJECT = os.getenv("LANGCHAIN_PROJECT", "bridge-v2")
