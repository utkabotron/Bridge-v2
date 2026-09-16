const Redis = require('ioredis');
const crypto = require('crypto');
const {
  REDIS_HOST, REDIS_PORT, REDIS_DB,
  REDIS_RETRY_DELAY_BASE, REDIS_RETRY_DELAY_MAX,
  DEDUP_TTL, CHAT_PAIRS_CACHE_TTL,
} = require('./config');

const redis = new Redis({
  host: REDIS_HOST,
  port: REDIS_PORT,
  db: REDIS_DB,
  // NEVER give up: returning null puts ioredis into a terminal "end" state and it
  // never reconnects — the single wa-service replica would then silently drop every
  // message until a manual restart. Retry forever with a capped backoff instead.
  retryStrategy: (times) => {
    const delay = Math.min(times * REDIS_RETRY_DELAY_BASE, REDIS_RETRY_DELAY_MAX);
    if (times % 20 === 1) {
      console.warn(`Redis: reconnect attempt ${times}, next in ${delay}ms`);
    }
    return delay;
  },
  maxRetriesPerRequest: null, // don't fail in-flight commands during a reconnect
  lazyConnect: false,
});

redis.on('connect', () => console.log('Redis connected'));
redis.on('error', (err) => console.error('Redis error:', err.message));

// Atomic dedup + enqueue: SET NX and LPUSH run server-side in one script, so either
// both happen or neither does. This closes the crash-between-two-commands window that
// could otherwise leave a dedup marker set while the message never reached the queue
// (permanent loss), and it never double-pushes a genuine duplicate.
// Returns 1 if enqueued, 0 if it was a duplicate.
const DEDUP_ENQUEUE_LUA = `
if redis.call('SET', KEYS[1], '1', 'EX', tonumber(ARGV[1]), 'NX') then
  redis.call('LPUSH', KEYS[2], ARGV[2])
  return 1
else
  return 0
end`;

/**
 * Build a content-based dedup id for messages whose wa_message_id is missing.
 * Uses chat + timestamp + body hash so genuine re-emits still collapse, but distinct
 * messages get distinct keys instead of a shared "undefined".
 *
 * Deliberately NOT keyed by user_id: every WhatsApp client sitting in a group receives
 * the same message, so hashing the receiving client turned one group message into one
 * queue entry PER CLIENT — four clients meant four translations and four Telegram
 * messages. All fields below are identical across clients for the same message;
 * sender_name is not (pushname vs. contact name), which is why it stays out.
 * The processor fans the single surviving copy out to every active pair of the chat.
 *
 * Edits get their own namespace so an edit is never dropped as a duplicate of
 * the original message it revises.
 */
function fallbackDedupId(payload) {
  const parts = [
    payload.wa_chat_id,
    payload.timestamp,
    payload.body || '',
    payload.media_mime || '',
  ].join('|');
  const hash = crypto.createHash('sha256').update(parts).digest('hex').slice(0, 16);
  return `fallback:${hash}`;
}

/**
 * Push a WhatsApp message to the processor queue.
 * Processor does BRPOP on "messages:in".
 *
 * The dedup id is written back into payload.wa_message_id so it is the SINGLE source
 * of truth: the Redis dedup key, the processor's DB dedup, and the message_events
 * UNIQUE key all use the same stable per-message id. Before this, a missing
 * id._serialized (whatsapp-web.js session drift) reached the processor as "" and
 * collapsed every such message into one message_events row (media silently undelivered).
 */
async function publishMessage(payload) {
  let dedupId = payload.wa_message_id || fallbackDedupId(payload);
  // Edits share the original message id — give them a distinct id so they are neither
  // dropped by dedup nor mistaken for the original in the DB.
  if (payload.is_edited) {
    const bodyHash = crypto.createHash('sha256')
      .update(payload.body || '').digest('hex').slice(0, 12);
    dedupId = `${dedupId}:edit:${bodyHash}`;
  }
  payload.wa_message_id = dedupId;

  const dedupKey = `dedup:msg:${dedupId}`;
  const enqueued = await redis.eval(
    DEDUP_ENQUEUE_LUA, 2, dedupKey, 'messages:in',
    DEDUP_TTL, JSON.stringify(payload),
  );
  if (enqueued === 0) {
    console.log(`Dedup: skipping duplicate message ${dedupId}`);
  }
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

module.exports = { redis, publishMessage, publishQrScanned, getChatPairsCache, setChatPairsCache, fallbackDedupId };
