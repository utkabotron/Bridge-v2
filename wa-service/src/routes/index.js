const express = require('express');
const path = require('path');
const QRCode = require('qrcode');
const { clients, createWhatsAppClient, getGroups } = require('../whatsapp-client');
const config = require('../config');
const { redis } = require('../redis-publisher');
const { getChatPairs, getWaConnected, setChatPairStatus, deleteChatPair, userExists } = require('../db');
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

router.get('/miniapp', (req, res) => {
  res.sendFile('miniapp.html', { root: path.join(__dirname, '..', '..', 'public') });
});

// Consumed by the analytics health-check flow, which has no Telegram identity.
// Exposes counters and liveness only — never chat or user content.
router.get('/health', (req, res) => {
  const perClient = [];
  for (const [userId, data] of clients.entries()) {
    perClient.push({
      userId,
      isReady: !!data.isReady,
      lastMessageAt: data.lastMessageAt || null,
    });
  }
  res.json({
    status: 'ok',
    activeClients: clients.size,
    readyClients: perClient.filter((c) => c.isReady).length,
    clients: perClient,
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
  const userId = await resolveQrToken(req.query.t);
  if (userId === null) {
    res.status(401).setHeader('Content-Type', 'text/html');
    return res.send('<!DOCTYPE html><html><body style="font-family:sans-serif;text-align:center;padding-top:80px">'
      + '<h3>Link expired</h3><p>Open the bot in Telegram and tap Connect again.</p></body></html>');
  }

  // userId is an integer read back from Redis and the token is hex-validated, so nothing
  // user-controlled reaches the markup below.
  const token = String(req.query.t);

  res.setHeader('Content-Type', 'text/html');
  res.send(`<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Connect WhatsApp — Bridge v2</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body { font-family: sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #f0f2f5; }
    h2 { color: #128C7E; }
    img { border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,.15); }
    p { color: #555; }
    #status { margin-top: 12px; font-weight: bold; }
  </style>
</head>
<body>
  <h2>Scan QR code in WhatsApp</h2>
  <p>Settings → Linked Devices → Link a Device</p>
  <img id="qr" src="/qr/image/${userId}?t=${token}" width="280" height="280" alt="QR Code">
  <p id="status">Waiting for QR...</p>
  <script>
    const USER_ID = ${userId};
    const TOKEN = ${JSON.stringify(token)};
    const img = document.getElementById('qr');
    const status = document.getElementById('status');
    let connected = false;

    async function poll() {
      try {
        const r = await fetch('/status/' + USER_ID + '?t=' + TOKEN);
        const d = await r.json();
        if (d.isReady) {
          connected = true;
          status.textContent = '✅ Connected! You can close this page.';
          img.style.display = 'none';
          return;
        }
      } catch {}

      if (!connected) {
        img.src = '/qr/image/' + USER_ID + '?t=' + TOKEN + '&ts=' + Date.now();
        status.textContent = 'Scan the QR code above';
        setTimeout(poll, 5000);
      }
    }

    poll();
  </script>
</body>
</html>`);
});

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
    const raw = await redis.hgetall(`bot:user_groups:${userId}`);
    const groups = Object.values(raw || {}).map((v) => JSON.parse(v));
    res.json({ groups });
  } catch (err) {
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
    const [pairs, waConnected] = await Promise.all([
      getChatPairs(userId),
      getWaConnected(userId),
    ]);
    res.json({ pairs, wa_connected: waConnected });
  } catch (err) {
    console.error('getChatPairs error:', err);
    res.status(500).json({ error: err.message });
  }
});

// These two are keyed by pairId, not userId, so requireSelf cannot guard them —
// ownership is enforced in SQL against the authenticated user instead.
router.patch('/chat-pairs/:pairId', async (req, res) => {
  const pairId = parseInt(req.params.pairId, 10);
  if (isNaN(pairId)) return res.status(400).json({ error: 'Invalid pairId' });

  const { status } = req.body || {};
  if (!['active', 'paused'].includes(status)) {
    return res.status(400).json({ error: 'Status must be "active" or "paused"' });
  }

  const owner = req.auth?.userId;
  if (!Number.isFinite(owner)) return res.status(403).json({ error: 'Forbidden' });

  try {
    const ok = await setChatPairStatus(pairId, status, owner);
    if (!ok) return res.status(404).json({ error: 'Pair not found' });
    res.json({ ok: true });
  } catch (err) {
    console.error('setChatPairStatus error:', err);
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
