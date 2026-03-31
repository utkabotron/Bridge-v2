const Redis = require('ioredis');
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
 * Push a WhatsApp message to the processor queue.
 * Processor does BRPOP on "messages:in".
 * Uses Redis SET NX to deduplicate — whatsapp-web.js can emit the same message twice.
 */
async function publishMessage(payload) {
  const dedupKey = `dedup:msg:${payload.wa_message_id}`;
  const isNew = await redis.set(dedupKey, '1', 'EX', DEDUP_TTL, 'NX');
  if (!isNew) {
    console.log(`Dedup: skipping duplicate message ${payload.wa_message_id}`);
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
