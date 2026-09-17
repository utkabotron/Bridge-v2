const express = require('express');
const path = require('path');
const QRCode = require('qrcode');
const { clients, createWhatsAppClient, getGroups, getLastMessageAt, getHealthPasses } = require('../whatsapp-client');
const config = require('../config');
const { redis } = require('../redis-publisher');
const {
  getChatPairs, getChatPairOwned, addChatPair, getTgGroups, ownsTgGroup,
  setPairLanguage, setPairSummary, getWaConnected, setChatPairStatus,
  deleteChatPair, userExists,
} = require('../db');
const { authenticate, requireSelf, createQrToken, resolveQrToken } = require('../middleware/tg-auth');

const router = express.Router();

// Dynamic API responses must never be cached. Telegram's WebView aggressively caches
// GETs (ETag/304), which served a stale `wa_connected:false` / `isReady:false` long
// after WhatsApp reconnected — forcing users back to the QR screen and blocking
// "Add new pair". no-store guarantees the Mini App always sees fresh state.
// (The /miniapp static file manages its own caching headers via res.sendFile.)
router.use((req, res, next) => {
  res.set('Cache-Control', 'no-store, no-cache, must-revalidate, proxy-revalidate');
  next();
});

// ── Public: Mini App shell and health ────────────────────
// The shell carries no user data — it authenticates its own API calls with initData.

const PUBLIC_DIR = path.join(__dirname, '..', '..', 'public');

router.get('/miniapp', (req, res) => {
  res.sendFile('miniapp.html', { root: PUBLIC_DIR });
});

// Stylesheet and script for the Mini App and the standalone QR page. Immutable names are
// not worth the machinery here; a short max-age keeps Telegram's WebView from serving a
// stale bundle after a deploy while still avoiding a fetch per screen.
router.use('/miniapp-assets', express.static(path.join(PUBLIC_DIR, 'assets'), {
  maxAge: '5m',
  fallthrough: false,
}));

// Consumed by the analytics health-check flow, which has no Telegram identity.
// Exposes counters and liveness only — never chat or user content.
router.get('/health', (req, res) => {
  const perClient = [];
  for (const [userId, data] of clients.entries()) {
    perClient.push({
      userId,
      isReady: !!data.isReady,
      lastMessageAt: data.lastMessageAt || null,
      // How long this client has been initialising, so a session that never reaches
      // "ready" is distinguishable from one that is merely slow to sync.
      initAgeSec: data.initStartedAt ? Math.round((Date.now() - data.initStartedAt) / 1000) : null,
      hasQR: !!data.qr,
    });
  }
  res.json({
    status: 'ok',
    activeClients: clients.size,
    readyClients: perClient.filter((c) => c.isReady).length,
    clients: perClient,
    // When any client last received a message. The monitoring flow uses this as a
    // dead-man switch: connected clients that stop delivering look healthy otherwise.
    lastMessageAt: getLastMessageAt(),
    // Proves the watchdog is actually ticking; a frozen counter is itself a symptom.
    healthPasses: getHealthPasses(),
    redis: redis.status === 'ready' ? 'connected' : 'disconnected',
  });
});

// ── QR web page ───────────────────────────────────────────
// Reached from the onboarding link the bot sends, i.e. a plain browser with no initData.
// The user id lives in a short-lived Redis token instead of the path: the old
// /qr/page/:userId both handed any passer-by another user's QR (account takeover) and
// reflected the raw path segment into the HTML and two JS string literals (XSS on the
// real domain, which is also the Mini App's origin).
router.get('/qr/page', async (req, res) => {
  res.setHeader('Content-Type', 'text/html');

  const userId = await resolveQrToken(req.query.t);
  if (userId === null) {
    return res.status(401).send(qrPage(null, null));
  }

  // userId is an integer read back from Redis and the token is hex-validated, so nothing
  // user-controlled reaches the markup.
  res.send(qrPage(userId, String(req.query.t)));
});

/**
 * Standalone QR page, for scanning outside Telegram.
 *
 * Shares the Mini App's stylesheet rather than carrying its own — the two used to drift,
 * with separate colours and a different poll interval.
 */
function qrPage(userId, token) {
  const expired = userId === null;
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Connect WhatsApp — Bridge</title>
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Rubik:wght@400;500;600&display=swap">
  <link rel="stylesheet" href="/miniapp-assets/miniapp.css">
</head>
<body>
  <div class="screen active">
    ${expired ? `
      <div class="state">
        <div class="state-title">Link expired</div>
        <div class="state-text">Open the bot in Telegram and tap Connect again.</div>
      </div>` : `
      <div class="head" style="text-align:center">
        <div class="title">Connect WhatsApp</div>
        <div class="sub">Settings &rarr; Linked devices &rarr; Link a device</div>
      </div>
      <div class="qr-wrap">
        <img id="qr" width="232" height="232" alt="QR code"
             src="/qr/image/${userId}?t=${token}">
      </div>
      <div class="sub" id="status" style="text-align:center">Preparing the code…</div>
      <script>
        const USER_ID = ${userId};
        const TOKEN = ${JSON.stringify(token)};
        const img = document.getElementById('qr');
        const status = document.getElementById('status');

        async function poll() {
          try {
            const r = await fetch('/status/' + USER_ID + '?t=' + TOKEN);
            const d = await r.json();
            if (d.isReady) {
              status.textContent = 'Connected. You can close this page.';
              img.style.display = 'none';
              return;
            }
          } catch {}
          img.src = '/qr/image/' + USER_ID + '?t=' + TOKEN + '&ts=' + Date.now();
          status.textContent = 'Scan the code above';
          setTimeout(poll, 3000);
        }
        poll();
      <\/script>`}
  </div>
</body>
</html>`;
}

// ── Everything below requires an authenticated Telegram identity ──
router.use(authenticate);

// ── QR image ──────────────────────────────────────────────
router.get('/qr/image/:userId', requireSelf, async (req, res) => {
  const userId = parseInt(req.params.userId, 10);

  const clientData = clients.get(userId);

  if (!clientData) {
    // Auto-create client and start QR generation — only for known/active users.
    if (!(await userExists(userId))) {
      return res.status(403).json({ error: 'Unknown user' });
    }
    try {
      createWhatsAppClient(userId).catch(console.error); // fire & forget
      return res.status(202).json({ status: 'initializing', message: 'Client starting, retry in 5s' });
    } catch (err) {
      return res.status(500).json({ error: err.message });
    }
  }

  if (clientData.isReady) {
    return res.json({ status: 'ready', message: 'Already connected' });
  }

  if (!clientData.qr) {
    return res.status(202).json({ status: 'waiting', message: 'QR not yet generated, retry in 3s' });
  }

  try {
    const png = await QRCode.toBuffer(clientData.qr);
    res.setHeader('Content-Type', 'image/png');
    res.send(png);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Status ────────────────────────────────────────────────
router.get('/status/:userId', requireSelf, async (req, res) => {
  const userId = parseInt(req.params.userId, 10);

  const clientData = clients.get(userId);
  if (!clientData) {
    return res.json({ isReady: false, hasQR: false });
  }

  if (!clientData.isReady) {
    return res.json({ isReady: false, hasQR: !!clientData.qr });
  }

  try {
    const groups = await getGroups(clientData.client, config.GET_CHATS_TIMEOUT);
    res.json({ isReady: true, hasQR: false, groups });
  } catch (err) {
    // getChats() reaches into WA's minified Store and dies (a bare 'r') whenever WA ships
    // a build the library hasn't caught up with. That must not read as "not connected":
    // a 500 here sent the Mini App back to the Connect screen to wait for a QR that can
    // never arrive, because the user is in fact connected. Report the session honestly
    // and let the client show an empty group list with an error instead.
    console.error(`getChats failed for user ${userId}: ${err.message}`);
    res.json({ isReady: true, hasQR: false, groups: [], groupsError: err.message });
  }
});

// ── TG groups (from Redis, written by bot) ───────────────
router.get('/tg-groups/:userId', requireSelf, async (req, res) => {
  const userId = parseInt(req.params.userId, 10);

  try {
    // Read from tg_groups (written by the bot). This used to be a Redis hash keyed by
    // whoever added the bot and expiring after an hour, so the picker was usually blank.
    res.json({ groups: await getTgGroups(userId) });
  } catch (err) {
    console.error('getTgGroups error:', err);
    res.status(500).json({ error: err.message });
  }
});

// ── Connect (create new client) ───────────────────────────
router.post('/connect/:userId', requireSelf, async (req, res) => {
  const userId = parseInt(req.params.userId, 10);

  if (!(await userExists(userId))) {
    return res.status(403).json({ error: 'Unknown user' });
  }

  try {
    createWhatsAppClient(userId).catch(console.error); // fire & forget
    const token = await createQrToken(userId);
    res.json({ message: 'Client starting', qrPageUrl: `/qr/page?t=${token}` });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Disconnect ────────────────────────────────────────────
router.post('/disconnect/:userId', requireSelf, async (req, res) => {
  const userId = parseInt(req.params.userId, 10);

  const clientData = clients.get(userId);
  if (!clientData) return res.status(404).json({ error: 'Not found' });

  try {
    // Mark intentional so the 'disconnected' event that destroy() may emit does not
    // trigger an auto-reconnect that resurrects the client we're tearing down.
    clientData.intentionalDestroy = true;
    await clientData.client.destroy();
    clients.delete(userId);
    res.json({ message: 'Disconnected' });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Reconnect ─────────────────────────────────────────────
router.post('/reconnect/:userId', requireSelf, async (req, res) => {
  const userId = parseInt(req.params.userId, 10);

  if (!(await userExists(userId))) {
    return res.status(403).json({ error: 'Unknown user' });
  }

  const existing = clients.get(userId);
  if (existing?.client) {
    // Intentional teardown — suppress the auto-reconnect that destroy() may trigger,
    // so it doesn't race the explicit createWhatsAppClient below.
    existing.intentionalDestroy = true;
    try { await existing.client.destroy(); } catch {}
    clients.delete(userId);
  }

  try {
    createWhatsAppClient(userId).catch(console.error);
    const token = await createQrToken(userId);
    res.json({ message: 'Reconnecting', qrPageUrl: `/qr/page?t=${token}` });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Chat Pairs CRUD ──────────────────────────────────────

router.get('/chat-pairs/:userId', requireSelf, async (req, res) => {
  const userId = parseInt(req.params.userId, 10);

  try {
    const [pairs, waConnectedFlag] = await Promise.all([
      getChatPairs(userId),
      getWaConnected(userId),
    ]);
    // Trust a live, ready client over the stored flag. The two disagree whenever the
    // flag write was missed, and the app used to bounce between its home screen and the
    // QR screen forever when that happened.
    const live = clients.get(userId)?.isReady === true;
    res.json({ pairs, wa_connected: live || waConnectedFlag, wa_live: live });
  } catch (err) {
    console.error('getChatPairs error:', err);
    res.status(500).json({ error: err.message });
  }
});

/**
 * Create a bridge.
 *
 * The Mini App used to finish onboarding with tg.sendData(), which Telegram delivers only
 * from reply-keyboard buttons — the app opens from an inline one, so the final tap did
 * nothing at all and no pair was ever created through it.
 */
router.post('/chat-pairs', async (req, res) => {
  const owner = req.auth?.userId;
  if (!Number.isFinite(owner)) return res.status(403).json({ error: 'Forbidden' });

  const { wa_chat_id: waChatId, wa_chat_name: waChatName,
          tg_chat_id: tgChatIdRaw, tg_chat_title: tgChatTitle } = req.body || {};

  if (typeof waChatId !== 'string' || !/@(g\.us|c\.us)$/.test(waChatId)) {
    return res.status(400).json({ error: 'Invalid wa_chat_id' });
  }
  const tgChatId = parseInt(tgChatIdRaw, 10);
  if (!Number.isFinite(tgChatId)) {
    return res.status(400).json({ error: 'Invalid tg_chat_id' });
  }

  if (!(await userExists(owner))) {
    return res.status(403).json({ error: 'Unknown user' });
  }
  // The group must be one this user administers, or anyone could bridge a WhatsApp chat
  // into a Telegram group they merely know the id of.
  if (!(await ownsTgGroup(owner, tgChatId))) {
    return res.status(403).json({ error: 'You are not an admin of that Telegram group' });
  }

  try {
    const pair = await addChatPair(
      owner, waChatId, String(waChatName || '').slice(0, 200),
      tgChatId, String(tgChatTitle || '').slice(0, 200),
    );
    res.status(201).json({ pair });
  } catch (err) {
    console.error('addChatPair error:', err);
    res.status(500).json({ error: err.message });
  }
});

// These are keyed by pairId, not userId, so requireSelf cannot guard them —
// ownership is enforced in SQL against the authenticated user instead.
router.patch('/chat-pairs/:pairId', async (req, res) => {
  const pairId = parseInt(req.params.pairId, 10);
  if (isNaN(pairId)) return res.status(400).json({ error: 'Invalid pairId' });

  const owner = req.auth?.userId;
  if (!Number.isFinite(owner)) return res.status(403).json({ error: 'Forbidden' });

  const body = req.body || {};
  const hasStatus = body.status !== undefined;
  const hasLanguage = body.target_language !== undefined;
  const hasSummary = body.summary_enabled !== undefined;

  if (!hasStatus && !hasLanguage && !hasSummary) {
    return res.status(400).json({ error: 'Nothing to update' });
  }
  if (hasStatus && !['active', 'paused'].includes(body.status)) {
    return res.status(400).json({ error: 'Status must be "active" or "paused"' });
  }
  // null is meaningful: it clears the override so the bridge follows the account setting.
  if (hasLanguage && body.target_language !== null && typeof body.target_language !== 'string') {
    return res.status(400).json({ error: 'target_language must be a string or null' });
  }
  if (hasSummary && typeof body.summary_enabled !== 'boolean') {
    return res.status(400).json({ error: 'summary_enabled must be a boolean' });
  }

  try {
    let found = false;
    if (hasStatus) found = await setChatPairStatus(pairId, body.status, owner) || found;
    if (hasLanguage) found = await setPairLanguage(pairId, body.target_language, owner) || found;
    if (hasSummary) found = await setPairSummary(pairId, body.summary_enabled, owner) || found;

    if (!found) return res.status(404).json({ error: 'Pair not found' });
    res.json({ ok: true, pair: await getChatPairOwned(pairId, owner) });
  } catch (err) {
    console.error('updateChatPair error:', err);
    res.status(500).json({ error: err.message });
  }
});

router.delete('/chat-pairs/:pairId', async (req, res) => {
  const pairId = parseInt(req.params.pairId, 10);
  if (isNaN(pairId)) return res.status(400).json({ error: 'Invalid pairId' });

  const owner = req.auth?.userId;
  if (!Number.isFinite(owner)) return res.status(403).json({ error: 'Forbidden' });

  try {
    const ok = await deleteChatPair(pairId, owner);
    if (!ok) return res.status(404).json({ error: 'Pair not found' });
    res.json({ ok: true });
  } catch (err) {
    console.error('deleteChatPair error:', err);
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
