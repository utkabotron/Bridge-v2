/**
 * Centralized configuration for wa-service.
 * All env vars with typed defaults in one place.
 */

module.exports = {
  // Redis
  REDIS_HOST: process.env.REDIS_HOST || 'localhost',
  REDIS_PORT: parseInt(process.env.REDIS_PORT) || 6379,
  REDIS_DB: parseInt(process.env.REDIS_DB) || 0,
  REDIS_RETRY_LIMIT: parseInt(process.env.REDIS_RETRY_LIMIT) || 10,
  REDIS_RETRY_DELAY_BASE: parseInt(process.env.REDIS_RETRY_DELAY_BASE) || 500,
  REDIS_RETRY_DELAY_MAX: parseInt(process.env.REDIS_RETRY_DELAY_MAX) || 5000,

  // Auth
  // Mini App requests are signed with the bot token; server-to-server calls from the bot
  // carry INTERNAL_API_TOKEN. Both are required — without them every route is anonymous.
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || '',
  INTERNAL_API_TOKEN: process.env.INTERNAL_API_TOKEN || '',
  INIT_DATA_MAX_AGE: parseInt(process.env.INIT_DATA_MAX_AGE) || 86400,
  QR_TOKEN_TTL: parseInt(process.env.QR_TOKEN_TTL) || 900,

  // Database
  DATABASE_URL: process.env.DATABASE_URL || 'postgresql://bridge:bridge@localhost:5432/bridge',
  DB_POOL_MAX: parseInt(process.env.DB_POOL_MAX) || 5,
  DB_STATEMENT_TIMEOUT: parseInt(process.env.DB_STATEMENT_TIMEOUT) || 10000,

  // WhatsApp client
  MAX_CONCURRENT_CLIENTS: parseInt(process.env.MAX_CONCURRENT_CLIENTS) || 4,
  MAX_PARALLEL_INIT: parseInt(process.env.MAX_PARALLEL_INIT) || 1,
  QR_TIMEOUT_MS: parseInt(process.env.QR_TIMEOUT_MS) || 60 * 60 * 1000,
  RECONNECT_DELAYS: [5000, 15000, 45000],
  HEALTH_CHECK_INTERVAL: parseInt(process.env.HEALTH_CHECK_INTERVAL) || 30000,
  HEALTH_CHECK_TIMEOUT: parseInt(process.env.HEALTH_CHECK_TIMEOUT) || 10000,
  MAX_MESSAGE_ERRORS: parseInt(process.env.MAX_MESSAGE_ERRORS) || 5,
  GET_CHATS_TIMEOUT: parseInt(process.env.GET_CHATS_TIMEOUT) || 15000,
  // Per-message Store calls in the delivery path. Must stay well under
  // PUPPETEER_PROTOCOL_TIMEOUT so a degraded Store degrades delivery instead of stalling it.
  GET_CHAT_TIMEOUT: parseInt(process.env.GET_CHAT_TIMEOUT) || 15000,
  // A client that never produced a QR and never went ready within this window is stuck
  // in initialize() and will never recover on its own.
  INIT_STUCK_TIMEOUT: parseInt(process.env.INIT_STUCK_TIMEOUT) || 5 * 60 * 1000,
  // A client that authenticated is syncing chat history, which is slow for a busy
  // account. Restarting it on the INIT_STUCK_TIMEOUT clock would restart the sync too,
  // forever; this is the point past which the sync is genuinely wedged.
  SYNC_STUCK_TIMEOUT: parseInt(process.env.SYNC_STUCK_TIMEOUT) || 20 * 60 * 1000,
  // Upper bound per client during shutdown; Docker's default stop grace is 10s total.
  DESTROY_TIMEOUT: parseInt(process.env.DESTROY_TIMEOUT) || 8000,
  OLD_MESSAGE_THRESHOLD: parseInt(process.env.OLD_MESSAGE_THRESHOLD) || 120,
  PUPPETEER_PROTOCOL_TIMEOUT: parseInt(process.env.PUPPETEER_PROTOCOL_TIMEOUT) || 120000,
  SESSION_RESTORE_BATCH_DELAY: parseInt(process.env.SESSION_RESTORE_BATCH_DELAY) || 1000,

  // Pin the WhatsApp Web build. whatsapp-web.js reaches into WA's minified Store,
  // so a WA release can break getChats()/getChat() (they start throwing 'r') while
  // the library still lags months behind. Setting WA_WEB_VERSION to a known-good
  // build from wppconnect-team/wa-version freezes WA at that build; empty means
  // "whatever WA serves today" (type: 'local'), which is what broke us.
  WA_WEB_VERSION: process.env.WA_WEB_VERSION || '',
  WA_WEB_VERSION_BASE_URL:
    process.env.WA_WEB_VERSION_BASE_URL ||
    'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html',

  // Message dedup
  DEDUP_TTL: parseInt(process.env.DEDUP_TTL) || 300,

  // Cache
  CHAT_PAIRS_CACHE_TTL: parseInt(process.env.CACHE_TTL) || 3600,

  // Media
  MAX_FILE_SIZE: parseInt(process.env.MAX_FILE_SIZE) || 50 * 1024 * 1024,
  MAX_CONCURRENT_MEDIA: parseInt(process.env.MAX_CONCURRENT_MEDIA) || 2,
  // downloadMedia fails transiently while WA's Store is degrading; a single attempt was
  // dropping ~43 media a day silently.
  MEDIA_DOWNLOAD_ATTEMPTS: parseInt(process.env.MEDIA_DOWNLOAD_ATTEMPTS) || 2,
  MEDIA_RETRY_DELAY: parseInt(process.env.MEDIA_RETRY_DELAY) || 1500,

  // Bridge the user's own outgoing WhatsApp messages too, so the Telegram copy reads as a
  // conversation rather than one side of it.
  BRIDGE_OWN_MESSAGES: (process.env.BRIDGE_OWN_MESSAGES || 'true') === 'true',
  // Post a note when a message is deleted for everyone in WhatsApp.
  REVOKE_NOTICES: (process.env.REVOKE_NOTICES || 'true') === 'true',

  // S3/MinIO
  S3_BUCKET: process.env.S3_BUCKET || 'bridge-media',
  S3_PUBLIC_URL: process.env.S3_PUBLIC_URL || 'http://localhost:9000',
};
