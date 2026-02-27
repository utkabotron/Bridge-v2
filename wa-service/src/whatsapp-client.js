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

function getClientId(userId) {
  return `user-${userId}`;
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
  });

  client.on('disconnected', (reason) => {
    console.log(`WhatsApp disconnected for user ${userId}:`, reason);
    clientData.isReady = false;
    clientData.qr = null;
    clients.delete(userId);
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
    console.warn(`Skipping message ${message.id._serialized} — media failed`);
    return;
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
}

module.exports = { clients, createWhatsAppClient, restoreExistingSessions };
