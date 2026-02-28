/**
 * WhatsApp client manager for Bridge v2.
 * Ported from services/whatsapp-manager.js — Supabase and BullMQ removed.
 * Uses Redis pub/sub for events and LPUSH for message queue.
 */
const { Client, LocalAuth } = require('whatsapp-web.js');
const fs = require('fs');
const path = require('path');
const { publishMessage, publishQrScanned, getChatPairsCache, setChatPairsCache } = require('./redis-publisher');
const { handleMedia } = require('./media-handler');

// Map<userId:number, ClientData>
const clients = new Map();

const MAX_CONCURRENT_CLIENTS = parseInt(process.env.MAX_CONCURRENT_CLIENTS) || 10;
const MAX_PARALLEL_INIT = parseInt(process.env.MAX_PARALLEL_INIT) || 3;

// Reconnect settings
const RECONNECT_DELAYS = [5000, 15000, 45000]; // 5s, 15s, 45s
const HEALTH_CHECK_INTERVAL = 60000; // 60s
const HEALTH_CHECK_TIMEOUT = 10000; // 10s

function getClientId(userId) {
  return `user-${userId}`;
}

// ── SingletonLock cleanup ─────────────────────────────────

function cleanupSingletonLocks() {
  const authDir = '.wwebjs_auth';
  if (!fs.existsSync(authDir)) return;

  let cleaned = 0;
  const entries = fs.readdirSync(authDir, { recursive: true });
  for (const entry of entries) {
    const entryStr = typeof entry === 'string' ? entry : entry.toString();
    if (path.basename(entryStr) === 'SingletonLock') {
      const fullPath = path.join(authDir, entryStr);
      try {
        fs.unlinkSync(fullPath);
        cleaned++;
      } catch (err) {
        console.warn(`Failed to remove SingletonLock ${fullPath}: ${err.message}`);
      }
    }
  }
  if (cleaned > 0) {
    console.log(`Cleaned up ${cleaned} SingletonLock file(s)`);
  }
}

// ── Reconnect with exponential backoff ────────────────────

async function reconnectClient(userId, reason) {
  for (let attempt = 0; attempt < RECONNECT_DELAYS.length; attempt++) {
    const delay = RECONNECT_DELAYS[attempt];
    console.log(`Reconnect attempt ${attempt + 1}/${RECONNECT_DELAYS.length} for user ${userId} in ${delay / 1000}s (reason: ${reason})`);
    await new Promise((r) => setTimeout(r, delay));

    // If someone else already reconnected this user, stop
    const existing = clients.get(userId);
    if (existing && existing.isReady) {
      console.log(`User ${userId} already reconnected, skipping`);
      return;
    }

    // Clean up old client if still in map
    clients.delete(userId);

    try {
      await createWhatsAppClient(userId);
      console.log(`Reconnect successful for user ${userId} on attempt ${attempt + 1}`);
      return;
    } catch (err) {
      console.error(`Reconnect attempt ${attempt + 1} failed for user ${userId}: ${err.message}`);
    }
  }

  console.error(`CRITICAL: All ${RECONNECT_DELAYS.length} reconnect attempts exhausted for user ${userId}. Session lost.`);
}

// ── Periodic health check ─────────────────────────────────

let healthCheckTimer = null;

async function checkClientHealth() {
  for (const [userId, clientData] of clients) {
    if (!clientData.isReady) continue;

    try {
      const statePromise = clientData.client.getState();
      const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error('getState timeout')), HEALTH_CHECK_TIMEOUT)
      );
      const state = await Promise.race([statePromise, timeoutPromise]);

      if (state !== 'CONNECTED') {
        console.warn(`Health check: user ${userId} state=${state}, triggering reconnect`);
        clientData.isReady = false;
        try { await clientData.client.destroy(); } catch { /* ignore */ }
        clients.delete(userId);
        reconnectClient(userId, `health_check_state_${state}`).catch(console.error);
      }
    } catch (err) {
      console.warn(`Health check: user ${userId} failed (${err.message}), triggering reconnect`);
      clientData.isReady = false;
      try { await clientData.client.destroy(); } catch { /* ignore */ }
      clients.delete(userId);
      reconnectClient(userId, 'health_check_error').catch(console.error);
    }
  }
}

function startHealthCheck() {
  if (healthCheckTimer) return;
  healthCheckTimer = setInterval(() => {
    checkClientHealth().catch((err) =>
      console.error('Health check loop error:', err.message)
    );
  }, HEALTH_CHECK_INTERVAL);
  console.log(`Session health check started (every ${HEALTH_CHECK_INTERVAL / 1000}s)`);
}

function stopHealthCheck() {
  if (healthCheckTimer) {
    clearInterval(healthCheckTimer);
    healthCheckTimer = null;
  }
}

// ── Create / manage a single client ──────────────────────

async function createWhatsAppClient(userId) {
  userId = parseInt(userId, 10);

  if (clients.has(userId)) {
    console.log(`Client for user ${userId} already exists`);
    return clients.get(userId);
  }

  if (clients.size >= MAX_CONCURRENT_CLIENTS) {
    throw new Error(`Max clients reached (${MAX_CONCURRENT_CLIENTS})`);
  }

  const clientData = { client: null, qr: null, isReady: false, userId };

  const client = new Client({
    authStrategy: new LocalAuth({ clientId: getClientId(userId) }),
    webVersionCache: {
      type: 'remote',
      remotePath: 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/{version}.html',
    },
    puppeteer: {
      headless: true,
      executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
      args: [
        '--no-sandbox',
        '--disable-setuid-sandbox',
        '--disable-dev-shm-usage',
        '--disable-accelerated-2d-canvas',
        '--no-first-run',
        '--no-zygote',
        '--disable-gpu',
        '--disable-images',
        '--mute-audio',
        '--disable-extensions',
        '--disable-background-networking',
        '--disable-default-apps',
        '--disable-sync',
        '--disable-translate',
        '--disk-cache-size=1',
        '--media-cache-size=1',
      ],
    },
  });

  client.on('qr', (qr) => {
    console.log(`QR received for user ${userId}`);
    clientData.qr = qr;
    clientData.isReady = false;
  });

  client.on('ready', async () => {
    console.log(`WhatsApp ready for user ${userId}`);
    clientData.isReady = true;
    clientData.qr = null;
    await publishQrScanned(userId, 'ready').catch(console.error);
  });

  client.on('authenticated', async () => {
    console.log(`WhatsApp authenticated for user ${userId}`);
    clientData.isReady = true;
    clientData.qr = null;
    await publishQrScanned(userId, 'authenticated').catch(console.error);
  });

  client.on('auth_failure', (msg) => {
    console.error(`Auth failed for user ${userId}:`, msg);
    clientData.isReady = false;

    // Destroy zombie client and clean up broken session
    client.destroy().catch(() => {});
    clients.delete(userId);

    const sessionDir = path.join('.wwebjs_auth', `session-${getClientId(userId)}`);
    if (fs.existsSync(sessionDir)) {
      try {
        fs.rmSync(sessionDir, { recursive: true, force: true });
        console.log(`Removed broken session dir: ${sessionDir}`);
      } catch (err) {
        console.error(`Failed to remove session dir ${sessionDir}: ${err.message}`);
      }
    }

    console.error(`User ${userId}: session lost after auth_failure, new QR scan required`);
  });

  client.on('disconnected', (reason) => {
    console.log(`WhatsApp disconnected for user ${userId}: ${reason}`);
    clientData.isReady = false;
    clientData.qr = null;
    clients.delete(userId);

    // Auto-reconnect with exponential backoff
    reconnectClient(userId, reason).catch(console.error);
  });

  client.on('message', async (message) => {
    await handleIncomingMessage(userId, message, false).catch((err) =>
      console.error(`Message handler error for user ${userId}:`, err.message)
    );
  });

  client.on('message_edit', async (message) => {
    await handleIncomingMessage(userId, message, true).catch((err) =>
      console.error(`Edit handler error for user ${userId}:`, err.message)
    );
  });

  clientData.client = client;
  clients.set(userId, clientData);

  try {
    await client.initialize();
  } catch (error) {
    console.error(`Failed to init client for user ${userId}:`, error.message);
    clients.delete(userId);
    throw error;
  }

  return clientData;
}

// ── Incoming message handler ──────────────────────────────

async function handleIncomingMessage(userId, message, isEdited) {
  // Skip old messages (e.g. after session restore) — 2 min threshold
  const ageSeconds = Math.floor(Date.now() / 1000) - (message.timestamp || 0);
  if (ageSeconds > 120) {
    console.log(`Skipping old message ${message.id._serialized} (age=${ageSeconds}s)`);
    return;
  }

  const chat = await message.getChat();
  const chatId = chat.id._serialized;

  // Try DB-backed chat pair lookup (with Redis cache)
  let chatPairs = await getChatPairsCache(userId, chatId);

  if (!chatPairs) {
    // Processor/bot will resolve active pairs; we just push the event.
    // For v2 (10 users), we push everything and let the processor filter.
    chatPairs = [{}]; // non-null sentinel so we always forward
  }

  // Sender info
  let senderName = 'Unknown';
  try {
    const contact = await message.getContact();
    senderName = contact.pushname || contact.name || contact.number || 'Unknown';
  } catch {
    const d = message._data || {};
    senderName = d.notifyName || d.pushname || message.author?.split('@')[0] || chat.name || 'Unknown';
  }

  // Handle special types
  if (message.type === 'poll_creation') {
    message.body = '[Poll — open WhatsApp to view]';
  }

  // Media upload to S3
  let mediaInfo = null;
  let mediaFailed = false;

  if (message.hasMedia && message.type !== 'poll_creation') {
    try {
      mediaInfo = await handleMedia(message, userId);
      if (!mediaInfo && message.type === 'sticker') {
        message.body = '[Sticker]';
      }
    } catch (err) {
      console.error(`Media error for user ${userId}:`, err.message);
      mediaFailed = true;
    }
  }

  if (mediaFailed) {
    console.warn(`Media failed for ${message.id._serialized} — sending without media`);
  }

  const payload = {
    wa_message_id: message.id._serialized,
    wa_chat_id: chatId,
    wa_chat_name: chat.name,
    user_id: userId,
    sender_name: senderName,
    body: message.body || '',
    message_type: message.type,
    timestamp: message.timestamp,
    from_me: message.fromMe,
    is_edited: isEdited,
    media_s3_url: mediaInfo?.s3Url || null,
    media_mime: mediaInfo?.mimeType || null,
    media_filename: mediaInfo?.filename || null,
  };

  await publishMessage(payload);
  console.log(`Queued message ${message.id._serialized} from chat ${chat.name}`);
}

// ── Restore persisted sessions on startup ─────────────────

async function restoreExistingSessions() {
  // Clean up stale SingletonLock files before restoring
  cleanupSingletonLocks();

  const authDir = '.wwebjs_auth';
  if (!fs.existsSync(authDir)) {
    console.log('No .wwebjs_auth directory — skipping session restore');
    return;
  }

  const sessions = fs.readdirSync(authDir).filter(
    (d) => d.startsWith('user-') || d.startsWith('session-user-')
  );

  if (sessions.length === 0) {
    console.log('No existing sessions found');
    return;
  }

  console.log(`Found ${sessions.length} session(s) — restoring up to ${MAX_PARALLEL_INIT} in parallel`);

  for (let i = 0; i < sessions.length; i += MAX_PARALLEL_INIT) {
    const batch = sessions.slice(i, i + MAX_PARALLEL_INIT);
    await Promise.allSettled(
      batch.map(async (dir) => {
        const uid = parseInt(dir.replace('session-user-', '').replace('user-', ''));
        if (isNaN(uid)) return;
        try {
          await createWhatsAppClient(uid);
          console.log(`Session restored for user ${uid}`);
        } catch (err) {
          console.error(`Failed to restore session for user ${uid}:`, err.message);
        }
      })
    );
    if (i + MAX_PARALLEL_INIT < sessions.length) {
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  // Start periodic health checks after all sessions restored
  startHealthCheck();
}

// ── Graceful shutdown ─────────────────────────────────────

async function destroyAllClients() {
  stopHealthCheck();
  const destroyPromises = [];
  for (const [userId, clientData] of clients) {
    console.log(`Destroying client for user ${userId}...`);
    destroyPromises.push(
      clientData.client.destroy().catch((err) =>
        console.error(`Error destroying client for user ${userId}: ${err.message}`)
      )
    );
  }
  await Promise.allSettled(destroyPromises);
  clients.clear();
  console.log('All WhatsApp clients destroyed');
}

module.exports = { clients, createWhatsAppClient, restoreExistingSessions, destroyAllClients, stopHealthCheck };
