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
const { setWaConnected, setWaDisconnected } = require('./db');

/** Reject with a labelled error if `promise` outlives `ms`. */
function withTimeout(promise, label, ms) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`${label} timeout (${ms / 1000}s)`)), ms)
    ),
  ]);
}

// When a message last arrived, globally and per client. Every health check until now
// asked "is the socket connected?"; none asked "are messages still flowing?" — and the
// failure that took the bridge down for 15 days looks healthy by the first question and
// dead by the second (WA breaks the Store, `message` stops firing, getState() still says
// CONNECTED). The analytics dead-man switch reads this.
let lastMessageAt = null;

function getLastMessageAt() {
  return lastMessageAt;
}
const config = require('./config');

// Map<userId:number, ClientData>
const clients = new Map();

// Per-user lock: userIds with a create/reconnect currently in flight. A synchronous
// guard (checked/added before the first await) prevents two concurrent reconnect loops
// (e.g. health check + 'disconnected') from racing on the same session and spawning a
// second Chromium on the same auth dir.
const connecting = new Set();

const MAX_CONCURRENT_CLIENTS = config.MAX_CONCURRENT_CLIENTS;
const MAX_PARALLEL_INIT = config.MAX_PARALLEL_INIT;
const QR_TIMEOUT_MS = config.QR_TIMEOUT_MS;
const RECONNECT_DELAYS = config.RECONNECT_DELAYS;
const HEALTH_CHECK_INTERVAL = config.HEALTH_CHECK_INTERVAL;
const HEALTH_CHECK_TIMEOUT = config.HEALTH_CHECK_TIMEOUT;
const MAX_MESSAGE_ERRORS = config.MAX_MESSAGE_ERRORS;
const OLD_MESSAGE_THRESHOLD = config.OLD_MESSAGE_THRESHOLD;
const INIT_STUCK_TIMEOUT = config.INIT_STUCK_TIMEOUT;
const DESTROY_TIMEOUT = config.DESTROY_TIMEOUT;

// Persistent recovery: capped backoff that NEVER gives up (unlike RECONNECT_DELAYS,
// which exhausts after 3 tries). A transient DNS/network glitch at startup or
// mid-session must not become a multi-week silent outage.
const RECOVERY_BACKOFF = [30000, 60000, 120000, 300000]; // 30s, 1m, 2m, then 5m cap
const recoveryState = new Map(); // uid -> { attempts, nextAttempt } for lost authenticated sessions
const inProgress = new Set();    // uids with an in-flight (re)connect — prevents double Chromium

function getClientId(userId) {
  return `user-${userId}`;
}

function safeMessageId(message) {
  return message?.id?._serialized || '(no-id)';
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

// Remove the SingletonLock of a single session dir (after its Chromium has exited).
// Pin WA to a known-good build when WA_WEB_VERSION is set. Left empty, whatsapp-web.js
// takes whatever WA serves today — and a WA release can break the minified Store calls
// behind getChats()/getChat() (they throw a bare 'r'), which silently kills the Mini App's
// group list while message delivery keeps limping along on fallbacks.
function buildWebVersionCache() {
  const version = config.WA_WEB_VERSION;
  if (!version) return { type: 'local' };
  const remotePath = `${config.WA_WEB_VERSION_BASE_URL}/${version}.html`;
  console.log(`Pinning WhatsApp Web to ${version} (${remotePath})`);
  return { type: 'remote', remotePath };
}

function cleanupSessionLock(userId) {
  const lockPath = path.join('.wwebjs_auth', `session-${getClientId(userId)}`, 'SingletonLock');
  try {
    // SingletonLock is a symlink to "<hostname>-<pid>". existsSync follows the link, so a
    // lock left by a dead browser answers false and never gets cleaned — which is exactly
    // the case this function exists for. Unlink unconditionally; ENOENT means it is gone.
    fs.unlinkSync(lockPath);
  } catch (err) {
    if (err.code !== 'ENOENT') {
      console.warn(`Failed to remove SingletonLock for user ${userId}: ${err.message}`);
    }
  }
}

// Destroy a client and wait for Chromium to actually exit (bounded), so callers can
// safely recreate on / delete the same auth dir afterwards.
async function destroyClient(client, userId) {
  if (!client) return;
  let timer;
  try {
    await Promise.race([
      client.destroy(),
      new Promise((r) => { timer = setTimeout(r, 10000); }),
    ]);
  } catch (err) {
    console.warn(`destroy() for user ${userId} errored: ${err.message}`);
  } finally {
    clearTimeout(timer);
  }
}

// ── Reconnect with exponential backoff ────────────────────

async function reconnectClient(userId, reason) {
  // Don't run two (re)connects for the same user at once — the recovery loop and a
  // disconnect handler can both fire. Whoever holds inProgress wins; the other skips.
  if (inProgress.has(userId)) {
    console.log(`Reconnect for user ${userId} skipped — connect already in progress`);
    return;
  }
  inProgress.add(userId);
  try {
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

      // Another create/reconnect is already bringing this user up — don't race it.
      if (connecting.has(userId)) {
        console.log(`User ${userId} reconnect already in progress, skipping`);
        return;
      }

      // Destroy any stale (non-ready) client before recreating, so its Chromium exits
      // and releases the SingletonLock instead of colliding with the new instance.
      if (existing?.client) {
        await destroyClient(existing.client, userId);
      }
      clients.delete(userId);
      cleanupSessionLock(userId);

      try {
        const created = await createWhatsAppClient(userId);
        if (created) {
          console.log(`Reconnect successful for user ${userId} on attempt ${attempt + 1}`);
        }
        return;
      } catch (err) {
        console.error(`Reconnect attempt ${attempt + 1} failed for user ${userId}: ${err.message}`);
      }
    }

    // Fast retries exhausted — but the session is still authenticated on disk, so this
    // is almost certainly transient. Mark the flag honest (disconnected) and hand off
    // to the persistent recovery loop, which keeps retrying on a capped backoff forever.
    console.error(`All ${RECONNECT_DELAYS.length} fast reconnect attempts exhausted for user ${userId} — handing off to recovery loop.`);
    await setWaDisconnected(userId).catch((err) =>
      console.error(`Failed to set wa_connected=false for user ${userId}: ${err.message}`)
    );
  } finally {
    inProgress.delete(userId);
  }
}

// ── Group listing ─────────────────────────────────────────
// client.getChats() maps every chat through WWebJS.getChatModel, which for groups calls
// GroupMetadata.update() and reaches into participants._models. That path breaks (throws
// a bare 'r') whenever WhatsApp reshuffles its minified internals — and it took the Mini
// App's group picker down with it, even though the session was perfectly healthy.
//
// Listing groups needs an id and a name, nothing else. Read them straight off the chat
// collection and skip the metadata machinery entirely.
async function getGroupsLite(client) {
  return client.pupPage.evaluate(() => {
    const tryRequire = (name) => {
      try {
        return window.require(name);
      } catch {
        return null;
      }
    };

    // window.Store is whatsapp-web.js's own alias for require('WAWebCollections'). When
    // WhatsApp renames that module the alias never gets built, which is why every Store
    // call starts throwing a bare 'r'. Go to the collection directly, trying the module
    // names WhatsApp has used, before giving up.
    const collections = window.Store || tryRequire('WAWebCollections');
    let Chat = collections?.Chat;
    if (!Chat) {
      for (const name of ['WAWebChatCollection', 'WAWebChatStorage', 'ChatCollection']) {
        const mod = tryRequire(name);
        Chat = mod?.ChatCollection || mod?.Chat || mod?.default;
        if (Chat?.getModelsArray) break;
        Chat = null;
      }
    }

    if (!Chat?.getModelsArray) {
      const diag = [
        `store=${typeof window.Store}`,
        `require=${typeof window.require}`,
        `wwebjs=${typeof window.WWebJS}`,
        `collections=${collections ? Object.keys(collections).slice(0, 8).join('/') : 'none'}`,
      ].join(' ');
      throw new Error(`chat collection unavailable (${diag})`);
    }

    return Chat.getModelsArray()
      .filter((c) => c.id?._serialized?.endsWith('@g.us'))
      .map((c) => ({
        id: c.id._serialized,
        name: c.name || c.formattedTitle || c.id.user,
        participants: c.groupMetadata?.participants?._models?.length || 0,
      }));
  });
}

// Full model first (it carries participant counts); fall back to the lightweight read
// when WhatsApp has moved on from what the library expects.
async function getGroups(client, timeoutMs) {
  const withTimeout = (promise, label) =>
    Promise.race([
      promise,
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error(`${label} timeout (${timeoutMs / 1000}s)`)), timeoutMs)
      ),
    ]);

  try {
    const chats = await withTimeout(client.getChats(), 'getChats');
    return chats.filter((c) => c.isGroup).map((c) => ({
      id: c.id._serialized,
      name: c.name,
      participants: c.participants?.length || 0,
    }));
  } catch (err) {
    console.warn(`getChats failed (${err.message}) — falling back to lightweight group read`);
    return withTimeout(getGroupsLite(client), 'getGroupsLite');
  }
}

// ── Persistent session recovery ───────────────────────────
// A session with an `.authenticated` marker on disk *should* be connected. If it has
// no live client (failed restore at startup, exhausted fast reconnects, …), keep
// retrying on a capped backoff — forever — so a transient glitch can't strand it.
// Terminal losses (LOGOUT, auth_failure) remove the session dir, so they are excluded.

function getAuthenticatedSessionUids() {
  const authDir = '.wwebjs_auth';
  if (!fs.existsSync(authDir)) return [];
  const uids = [];
  for (const dir of fs.readdirSync(authDir)) {
    if (!dir.startsWith('user-') && !dir.startsWith('session-user-')) continue;
    if (!fs.existsSync(path.join(authDir, dir, '.authenticated'))) continue;
    const uid = parseInt(dir.replace('session-user-', '').replace('user-', ''));
    if (!isNaN(uid)) uids.push(uid);
  }
  return uids;
}

function removeSessionDir(userId) {
  const sessionDir = path.join('.wwebjs_auth', `session-${getClientId(userId)}`);
  if (!fs.existsSync(sessionDir)) return;
  try {
    fs.rmSync(sessionDir, { recursive: true, force: true });
    console.log(`Removed session dir: ${sessionDir}`);
  } catch (err) {
    console.error(`Failed to remove session dir ${sessionDir}: ${err.message}`);
  }
}

async function recoverLostSessions() {
  const now = Date.now();
  for (const uid of getAuthenticatedSessionUids()) {
    const existing = clients.get(uid);
    if (existing && existing.isReady) { recoveryState.delete(uid); continue; } // healthy
    if (existing) continue;            // initializing / QR-waiting — leave to health check
    if (inProgress.has(uid)) continue; // a reconnect or recovery attempt already running

    const state = recoveryState.get(uid) || { attempts: 0, nextAttempt: 0 };
    if (now < state.nextAttempt) continue; // still backing off

    inProgress.add(uid);
    try {
      console.warn(`Recovery: restoring lost session for user ${uid} (attempt ${state.attempts + 1})`);
      await createWhatsAppClient(uid);
      console.log(`Recovery: session restored for user ${uid}`);
      recoveryState.delete(uid);
    } catch (err) {
      const attempts = state.attempts + 1;
      const delay = RECOVERY_BACKOFF[Math.min(attempts - 1, RECOVERY_BACKOFF.length - 1)];
      recoveryState.set(uid, { attempts, nextAttempt: Date.now() + delay });
      console.error(`Recovery: failed for user ${uid} (attempt ${attempts}), retrying in ${delay / 1000}s: ${err.message}`);
    } finally {
      inProgress.delete(uid);
    }
  }
}

// ── Periodic health check ─────────────────────────────────

let healthCheckTimer = null;

async function checkClientHealth() {
  for (const [userId, clientData] of clients) {
    // Non-ready (QR-waiting) clients: just destroy, no reconnect — they're useless
    if (!clientData.isReady) {
      // Only clean up if client has been sitting idle (not freshly created)
      if (clientData.qrTimer === null && clientData.qr !== null) {
        console.warn(`Health check: user ${userId} not ready and no QR timer — destroying idle client`);
        await destroyClient(clientData.client, userId);
        clients.delete(userId);
        continue;
      }

      // Stuck in initialize(): no QR ever arrived and it never went ready, so the branch
      // above (which needs a QR) never fires and the recovery loop skips it because the
      // map still holds an entry. It sat there forever, counted as an active client,
      // holding a slot and ~400 MB, delivering nothing. Drop it and let recovery retry.
      const stuckFor = Date.now() - (clientData.initStartedAt || 0);
      if (!clientData.qr && stuckFor > INIT_STUCK_TIMEOUT) {
        console.warn(`Health check: user ${userId} stuck initializing for ${Math.round(stuckFor / 1000)}s — destroying`);
        clientData.intentionalDestroy = true;
        await destroyClient(clientData.client, userId);
        clients.delete(userId);
        cleanupSessionLock(userId);
      }
      continue;
    }

    try {
      const statePromise = clientData.client.getState();
      const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error('getState timeout')), HEALTH_CHECK_TIMEOUT)
      );
      const state = await Promise.race([statePromise, timeoutPromise]);

      if (state !== 'CONNECTED') {
        console.warn(`Health check: user ${userId} state=${state}, triggering reconnect`);
        clientData.isReady = false;
        await destroyClient(clientData.client, userId);
        clients.delete(userId);
        reconnectClient(userId, `health_check_state_${state}`).catch(console.error);
      }
    } catch (err) {
      console.warn(`Health check: user ${userId} failed (${err.message}), triggering reconnect`);
      clientData.isReady = false;
      await destroyClient(clientData.client, userId);
      clients.delete(userId);
      reconnectClient(userId, 'health_check_error').catch(console.error);
    }
  }
}

let healthCheckRunning = false;

function startHealthCheck() {
  if (healthCheckTimer) return;
  healthCheckTimer = setInterval(async () => {
    // Each pass can take longer than the interval when clients are hanging (getState and
    // destroy are bounded at 10s each, per client), and two passes racing could both
    // decide to destroy the same client.
    if (healthCheckRunning) return;
    healthCheckRunning = true;
    try {
      await checkClientHealth();
      await recoverLostSessions();
    } catch (err) {
      console.error('Health check loop error:', err.message);
    } finally {
      healthCheckRunning = false;
    }
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

  // Synchronous lock (before any await) — blocks a concurrent create/reconnect for the
  // same user from spawning a second Chromium on the same auth dir.
  if (connecting.has(userId)) {
    console.log(`Client for user ${userId} is already initializing — skipping`);
    return null;
  }

  if (clients.size >= MAX_CONCURRENT_CLIENTS) {
    throw new Error(`Max clients reached (${MAX_CONCURRENT_CLIENTS})`);
  }

  connecting.add(userId);
  try {
    return await buildClient(userId);
  } finally {
    // Not just around initialize(): anything thrown while constructing the Client or
    // wiring its handlers left `connecting` set, and from then on createWhatsAppClient
    // returned null forever while the recovery loop logged "session restored".
    connecting.delete(userId);
  }
}

async function buildClient(userId) {
  const clientData = {
    client: null, qr: null, isReady: false, userId, qrTimer: null,
    errorCount: 0, intentionalDestroy: false,
    initStartedAt: Date.now(), lastMessageAt: null,
  };

  const client = new Client({
    authStrategy: new LocalAuth({ clientId: getClientId(userId) }),
    webVersionCache: buildWebVersionCache(),
    puppeteer: {
      headless: true,
      executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
      protocolTimeout: config.PUPPETEER_PROTOCOL_TIMEOUT,
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

    // Start QR timeout on first QR event — destroy client if user never scans
    if (clientData.qrTimer === null) {
      console.log(`QR timeout started for user ${userId} (${QR_TIMEOUT_MS / 1000}s)`);
      clientData.qrTimer = setTimeout(async () => {
        console.warn(`QR timeout expired for user ${userId} — destroying idle client`);
        clientData.qrTimer = null;
        clientData.intentionalDestroy = true;
        await destroyClient(client, userId);
        clients.delete(userId);

        recoveryState.delete(userId);

        const sessionDir = path.join('.wwebjs_auth', `session-${getClientId(userId)}`);
        if (fs.existsSync(sessionDir)) {
          try {
            fs.rmSync(sessionDir, { recursive: true, force: true });
            console.log(`Removed unused session dir: ${sessionDir}`);
          } catch (err) {
            console.error(`Failed to remove session dir ${sessionDir}: ${err.message}`);
          }
        }

        setWaDisconnected(userId).catch((err) =>
          console.error(`Failed to set wa_connected=false for user ${userId}: ${err.message}`)
        );
      }, QR_TIMEOUT_MS);
    }
  });

  client.on('ready', async () => {
    console.log(`WhatsApp ready for user ${userId}`);
    clearTimeout(clientData.qrTimer);
    clientData.qrTimer = null;
    clientData.isReady = true;
    clientData.qr = null;
    clientData.errorCount = 0;
    // Mark session as authenticated — used by restoreExistingSessions to skip dead sessions
    writeAuthMarker(userId);
    // Write the flag here rather than leaving it to the bot's pub/sub listener. Pub/sub has
    // no replay, so a bot that was restarting (every full deploy) missed the event and left
    // users at wa_connected=false with a live WhatsApp — which the Mini App reads as "not
    // connected" and bounces back to the QR screen forever.
    setWaConnected(userId, true).catch((err) =>
      console.error(`Failed to set wa_connected=true for user ${userId}: ${err.message}`)
    );
    await publishQrScanned(userId, 'ready').catch(console.error);
  });

  client.on('authenticated', async () => {
    console.log(`WhatsApp authenticated for user ${userId}`);
    clearTimeout(clientData.qrTimer);
    clientData.qrTimer = null;
    // NOTE: do NOT set isReady here. 'authenticated' fires before chat sync completes;
    // marking ready now makes the health check call getState() mid-sync, see a
    // non-CONNECTED state, and destroy+reconnect in a loop. isReady is set only on 'ready'.
    clientData.qr = null;
    clientData.errorCount = 0;
    writeAuthMarker(userId);
    await publishQrScanned(userId, 'authenticated').catch(console.error);
  });

  client.on('auth_failure', async (msg) => {
    console.error(`Auth failed for user ${userId}:`, msg);
    clearTimeout(clientData.qrTimer);
    clientData.qrTimer = null;
    clientData.isReady = false;
    clientData.intentionalDestroy = true;

    // Destroy zombie client and WAIT for Chromium to exit before removing the session
    // dir — otherwise the live browser recreates the dir / SingletonLock we just deleted.
    await destroyClient(client, userId);
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
    recoveryState.delete(userId);

    setWaDisconnected(userId).catch((err) =>
      console.error(`Failed to set wa_connected=false for user ${userId}: ${err.message}`)
    );
  });

  client.on('disconnected', async (reason) => {
    console.log(`WhatsApp disconnected for user ${userId}: ${reason}`);
    clearTimeout(clientData.qrTimer);
    clientData.qrTimer = null;
    clientData.isReady = false;
    clientData.qr = null;

    // Destroy the dead client so Chromium exits and releases its SingletonLock before we
    // recreate on the same auth dir; a leaked browser would block every reconnect attempt.
    await destroyClient(client, userId);
    clients.delete(userId);

    if (clientData.intentionalDestroy) {
      console.log(`User ${userId}: intentional disconnect — not auto-reconnecting`);
      return;
    }

    // LOGOUT is terminal — the session is dead and requires a fresh QR scan. Reset the DB
    // flag and drop the session dir so the recovery loop won't keep retrying a doomed session.
    if (reason === 'LOGOUT') {
      setWaDisconnected(userId).catch((err) =>
        console.error(`Failed to set wa_connected=false for user ${userId}: ${err.message}`)
      );
      removeSessionDir(userId);
      recoveryState.delete(userId);
      return;
    }

    // Other reasons (NAVIGATION, network blips) — auto-reconnect with exponential backoff
    reconnectClient(userId, reason).catch(console.error);
  });

  client.on('message', async (message) => {
    lastMessageAt = Date.now();
    clientData.lastMessageAt = lastMessageAt;
    try {
      await handleIncomingMessage(userId, message, false);
      clientData.errorCount = 0;
    } catch (err) {
      clientData.errorCount++;
      console.error(`Message handler error for user ${userId} (${clientData.errorCount}/${MAX_MESSAGE_ERRORS}):`, err.message);
      if (clientData.errorCount >= MAX_MESSAGE_ERRORS) {
        console.error(`Too many message errors (${MAX_MESSAGE_ERRORS}), triggering reconnect for user ${userId}`);
        clientData.isReady = false;
        clientData.errorCount = 0;
        await destroyClient(client, userId);
        clients.delete(userId);
        reconnectClient(userId, 'message_errors').catch(console.error);
      }
    }
  });

  client.on('message_edit', async (message) => {
    lastMessageAt = Date.now();
    clientData.lastMessageAt = lastMessageAt;
    try {
      await handleIncomingMessage(userId, message, true);
      clientData.errorCount = 0;
    } catch (err) {
      clientData.errorCount++;
      console.error(`Edit handler error for user ${userId} (${clientData.errorCount}/${MAX_MESSAGE_ERRORS}):`, err.message);
      if (clientData.errorCount >= MAX_MESSAGE_ERRORS) {
        console.error(`Too many message errors (${MAX_MESSAGE_ERRORS}), triggering reconnect for user ${userId}`);
        clientData.isReady = false;
        clientData.errorCount = 0;
        await destroyClient(client, userId);
        clients.delete(userId);
        reconnectClient(userId, 'message_errors').catch(console.error);
      }
    }
  });

  clientData.client = client;
  clients.set(userId, clientData);

  try {
    await client.initialize();
  } catch (error) {
    console.error(`Failed to init client for user ${userId}:`, error.message);
    clients.delete(userId);
    // A failed initialize() still leaves its Chromium running, holding the profile's
    // SingletonLock — so every later attempt dies with "browser is already running" and
    // the orphan keeps its ~500 MB. That is how 30 stray browsers piled up and pushed a
    // 3.8 GB box into swap death. Tear it down before giving up on this attempt.
    await destroyClient(client, userId);
    cleanupSessionLock(userId);
    throw error;
  }

  return clientData;
}

// ── Incoming message handler ──────────────────────────────

async function handleIncomingMessage(userId, message, isEdited) {
  const safeId = safeMessageId(message);

  // Skip old messages (e.g. after session restore). Edits carry the ORIGINAL timestamp,
  // so never age-filter them. A missing timestamp (same session-drift mode that drops
  // id._serialized) must NOT be treated as "infinitely old" — rely on dedup instead of
  // silently discarding a live message.
  const ts = message.timestamp;
  if (!isEdited && ts) {
    const ageSeconds = Math.floor(Date.now() / 1000) - ts;
    if (ageSeconds > OLD_MESSAGE_THRESHOLD) {
      console.log(`Skipping old message ${safeId} (age=${ageSeconds}s)`);
      return;
    }
  } else if (!ts) {
    console.warn(`Message ${safeId} has no timestamp — forwarding (relying on dedup)`);
  }

  let chatId, chatName;
  if (message.from?.includes('@newsletter')) {
    // Newsletter chats break getChat() in whatsapp-web.js — skip the call
    chatId = message.from;
    chatName = message._data?.subject || message._data?.notifyName || '';
  } else {
    try {
      // Bounded: these reach into WA's Store, and when it degrades they hang until the
      // 120s protocol timeout. Unbounded, parallel hangs pile up holding whole messages
      // in memory while delivery stalls; the fallback below is cheap and correct.
      const chat = await withTimeout(message.getChat(), 'getChat', config.GET_CHAT_TIMEOUT);
      chatId = chat.id._serialized;
      chatName = chat.name;
    } catch (err) {
      console.warn(`getChat() failed for message ${safeId}: ${err.message} — using fallback`);
      chatId = message.from;
      chatName = message._data?.subject || message._data?.notifyName || '';
    }
  }

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
    const contact = await withTimeout(message.getContact(), 'getContact', config.GET_CHAT_TIMEOUT);
    senderName = contact.pushname || contact.name || contact.number || 'Unknown';
  } catch {
    const d = message._data || {};
    senderName = d.notifyName || d.pushname || message.author?.split('@')[0] || chatName || 'Unknown';
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
        message.body = '[Sticker]'; // fallback when sticker media download fails
      }
    } catch (err) {
      console.error(`Media error for user ${userId}:`, err.message);
      mediaFailed = true;
    }
  }

  if (mediaFailed) {
    console.warn(`Media failed for ${safeId} — sending without media`);
  }

  const payload = {
    wa_message_id: message.id?._serialized, // may be undefined → redis-publisher assigns a stable fallback id
    wa_chat_id: chatId,
    wa_chat_name: chatName,
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

  // publishMessage assigns payload.wa_message_id (real or content-fallback) and enqueues
  // atomically. On Redis failure it throws — the message handler counts the error and,
  // past the threshold, reconnects; the message is not silently marked handled.
  await publishMessage(payload);
  console.log(`Queued message ${payload.wa_message_id} from chat ${chatName}`);
}

// ── Auth marker — only restore sessions that were actually authenticated ──

function writeAuthMarker(userId) {
  const sessionDir = path.join('.wwebjs_auth', `session-${getClientId(userId)}`);
  const markerPath = path.join(sessionDir, '.authenticated');
  try {
    if (fs.existsSync(sessionDir) && !fs.existsSync(markerPath)) {
      fs.writeFileSync(markerPath, new Date().toISOString());
      console.log(`Auth marker written for user ${userId}`);
    }
  } catch (err) {
    console.warn(`Failed to write auth marker for user ${userId}: ${err.message}`);
  }
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

  // Filter: only restore sessions with .authenticated marker
  const authenticatedSessions = [];
  for (const dir of sessions) {
    const markerPath = path.join(authDir, dir, '.authenticated');
    if (fs.existsSync(markerPath)) {
      authenticatedSessions.push(dir);
    } else {
      // Remove unauthenticated session dir — it was never successfully connected
      const sessionPath = path.join(authDir, dir);
      try {
        fs.rmSync(sessionPath, { recursive: true, force: true });
        console.log(`Removed unauthenticated session dir: ${sessionPath}`);
      } catch (err) {
        console.warn(`Failed to remove session dir ${sessionPath}: ${err.message}`);
      }
    }
  }

  if (authenticatedSessions.length === 0) {
    console.log(`Found ${sessions.length} session(s) but none authenticated — skipping restore`);
    return;
  }

  console.log(`Found ${sessions.length} session(s), ${authenticatedSessions.length} authenticated — restoring up to ${MAX_PARALLEL_INIT} in parallel`);

  for (let i = 0; i < authenticatedSessions.length; i += MAX_PARALLEL_INIT) {
    const batch = authenticatedSessions.slice(i, i + MAX_PARALLEL_INIT);
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
    if (i + MAX_PARALLEL_INIT < authenticatedSessions.length) {
      await new Promise((r) => setTimeout(r, config.SESSION_RESTORE_BATCH_DELAY));
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
    clearTimeout(clientData.qrTimer);
    clientData.intentionalDestroy = true;
    console.log(`Destroying client for user ${userId}...`);
    destroyPromises.push(
      withTimeout(clientData.client.destroy(), `destroy user ${userId}`, DESTROY_TIMEOUT)
        .catch((err) => console.error(`Error destroying client for user ${userId}: ${err.message}`))
    );
  }
  await Promise.allSettled(destroyPromises);
  clients.clear();
  console.log('All WhatsApp clients destroyed');
}

module.exports = {
  getLastMessageAt,
  clients,
  connecting,
  getGroups,
  createWhatsAppClient,
  restoreExistingSessions,
  destroyAllClients,
  startHealthCheck,
  stopHealthCheck,
  recoverLostSessions,
  getAuthenticatedSessionUids,
  recoveryState,
  inProgress,
};
