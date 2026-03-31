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

  // Database
  DATABASE_URL: process.env.DATABASE_URL || 'postgresql://bridge:bridge@localhost:5432/bridge',
  DB_POOL_MAX: parseInt(process.env.DB_POOL_MAX) || 5,
  DB_STATEMENT_TIMEOUT: parseInt(process.env.DB_STATEMENT_TIMEOUT) || 10000,

  // WhatsApp client
  MAX_CONCURRENT_CLIENTS: parseInt(process.env.MAX_CONCURRENT_CLIENTS) || 10,
  MAX_PARALLEL_INIT: parseInt(process.env.MAX_PARALLEL_INIT) || 3,
  QR_TIMEOUT_MS: parseInt(process.env.QR_TIMEOUT_MS) || 60 * 60 * 1000,
  RECONNECT_DELAYS: [5000, 15000, 45000],
  HEALTH_CHECK_INTERVAL: parseInt(process.env.HEALTH_CHECK_INTERVAL) || 30000,
  HEALTH_CHECK_TIMEOUT: parseInt(process.env.HEALTH_CHECK_TIMEOUT) || 10000,
  MAX_MESSAGE_ERRORS: parseInt(process.env.MAX_MESSAGE_ERRORS) || 5,
  GET_CHATS_TIMEOUT: parseInt(process.env.GET_CHATS_TIMEOUT) || 15000,
  OLD_MESSAGE_THRESHOLD: parseInt(process.env.OLD_MESSAGE_THRESHOLD) || 120,
  PUPPETEER_PROTOCOL_TIMEOUT: parseInt(process.env.PUPPETEER_PROTOCOL_TIMEOUT) || 30000,
  SESSION_RESTORE_BATCH_DELAY: parseInt(process.env.SESSION_RESTORE_BATCH_DELAY) || 1000,

  // Message dedup
  DEDUP_TTL: parseInt(process.env.DEDUP_TTL) || 300,

  // Cache
  CHAT_PAIRS_CACHE_TTL: parseInt(process.env.CACHE_TTL) || 3600,

  // Media
  MAX_FILE_SIZE: parseInt(process.env.MAX_FILE_SIZE) || 50 * 1024 * 1024,

  // S3/MinIO
  S3_BUCKET: process.env.S3_BUCKET || 'bridge-v2-media',
  S3_PUBLIC_URL: process.env.S3_PUBLIC_URL || 'http://localhost:9000',
};
