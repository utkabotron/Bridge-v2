const Redis = require('ioredis');
const crypto = require('crypto');
const {
  REDIS_HOST, REDIS_PORT, REDIS_DB,
  REDIS_RETRY_LIMIT, REDIS_RETRY_DELAY_BASE, REDIS_RETRY_DELAY_MAX,
  DEDUP_TTL, CHAT_PAIRS_CACHE_TTL,
} = require('./config');

const redis = new Redis({
  host: REDIS_HOST,
  port: REDIS_PORT,
  db: REDIS_DB,
  retryStrategy: (times) => {
    if (times > REDIS_RETRY_LIMIT) {
      console.error(`Redis: failed after ${REDIS_RETRY_LIMIT} retries`);
      return null;
    }
    const delay = Math.min(times * REDIS_RETRY_DELAY_BASE, REDIS_RETRY_DELAY_MAX);
    console.warn(`Redis: retry attempt ${times}/${REDIS_RETRY_LIMIT} in ${delay}ms`);
    return delay;
  },
  lazyConnect: false,
});

redis.on('connect', () => console.log('Redis connected'));
redis.on('error', (err) => console.error('Redis error:', err.message));

/**
 * Build a content-based dedup id for messages whose wa_message_id is missing.
 * Uses user + chat + timestamp + body/media hash so genuine re-emits still collapse,
 * but distinct messages get distinct keys instead of a shared "undefined".
 */
function fallbackDedupId(payload) {
  const parts = [
    payload.user_id,
    payload.wa_chat_id,
    payload.timestamp,
    payload.body || '',
    payload.media_s3_url || '',
  ].join('|');
  const hash = crypto.createHash('sha256').update(parts).digest('hex').slice(0, 16);
  return `fallback:${hash}`;
}

/**
 * Push a WhatsApp message to the processor queue.
 * Processor does BRPOP on "messages:in".
 * Uses Redis SET NX to deduplicate — whatsapp-web.js can emit the same message twice.
 */
async function publishMessage(payload) {
  // whatsapp-web.js can emit messages with an undefined id._serialized when its
  // WhatsApp Web session drifts. Falling back to a constant "undefined" key would
  // collapse every message into one dedup bucket and drop all but the first in the
  // TTL window. Derive a stable per-message key from content instead.
  const dedupId = payload.wa_message_id || fallbackDedupId(payload);
  const dedupKey = `dedup:msg:${dedupId}`;
  const isNew = await redis.set(dedupKey, '1', 'EX', DEDUP_TTL, 'NX');
  if (!isNew) {
    console.log(`Dedup: skipping duplicate message ${dedupId}`);
    return;
  }
  // LPUSH first, then SET dedup key — if LPUSH fails, dedup key already set
  // but duplicate processing is safer than message loss (processor deduplicates by DB)
  await redis.lpush('messages:in', JSON.stringify(payload));
}

/**
 * Publish onboarding event when WhatsApp QR is scanned / client ready.
 * Bot subscribes to "onboarding:qr_scanned:*" pattern.
 */
async function publishQrScanned(userId, event = 'ready') {
  await redis.publish(
    `onboarding:qr_scanned:${userId}`,
    JSON.stringify({ userId, event, timestamp: new Date().toISOString() })
  );
  console.log(`Published qr_scanned (${event}) for user ${userId}`);
}

/**
 * Cache chat pairs: key = chat_pairs:user:{uid}:chat:{chatId}
 */
async function getChatPairsCache(userId, chatId) {
  const key = `chat_pairs:user:${userId}:chat:${chatId}`;
  try {
    const val = await redis.get(key);
    return val ? JSON.parse(val) : null;
  } catch {
    return null;
  }
}

async function setChatPairsCache(userId, chatId, data) {
  const key = `chat_pairs:user:${userId}:chat:${chatId}`;
  const ttl = CHAT_PAIRS_CACHE_TTL;
  try {
    await redis.setex(key, ttl, JSON.stringify(data));
  } catch {
    // non-critical
  }
}

module.exports = { redis, publishMessage, publishQrScanned, getChatPairsCache, setChatPairsCache };
