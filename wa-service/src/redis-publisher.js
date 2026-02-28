const Redis = require('ioredis');

const redis = new Redis({
  host: process.env.REDIS_HOST || 'localhost',
  port: parseInt(process.env.REDIS_PORT) || 6379,
  db: parseInt(process.env.REDIS_DB) || 0,
  retryStrategy: (times) => {
    if (times > 10) {
      console.error('Redis: failed after 10 retries');
      return null;
    }
    const delay = Math.min(times * 500, 5000);
    console.warn(`Redis: retry attempt ${times}/10 in ${delay}ms`);
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
  const isNew = await redis.set(dedupKey, '1', 'EX', 300, 'NX');
  if (!isNew) {
    console.log(`Dedup: skipping duplicate message ${payload.wa_message_id}`);
    return;
  }
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
  const ttl = parseInt(process.env.CACHE_TTL) || 3600;
  try {
    await redis.setex(key, ttl, JSON.stringify(data));
  } catch {
    // non-critical
  }
}

module.exports = { redis, publishMessage, publishQrScanned, getChatPairsCache, setChatPairsCache };
