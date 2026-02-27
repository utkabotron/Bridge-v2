const express = require('express');
const path = require('path');
const QRCode = require('qrcode');
const { clients, createWhatsAppClient } = require('../whatsapp-client');
const { redis } = require('../redis-publisher');

const router = express.Router();

// ── Mini App ─────────────────────────────────────────────
router.get('/miniapp', (req, res) => {
  res.sendFile('miniapp.html', { root: path.join(__dirname, '..', '..', 'public') });
});

// ── Health ────────────────────────────────────────────────
router.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    activeClients: clients.size,
    redis: redis.status === 'ready' ? 'connected' : 'disconnected',
  });
});

// ── QR image ──────────────────────────────────────────────
router.get('/qr/image/:userId', async (req, res) => {
  const userId = parseInt(req.params.userId, 10);
  if (isNaN(userId)) return res.status(400).json({ error: 'Invalid userId' });

  const clientData = clients.get(userId);

  if (!clientData) {
    // Auto-create client and start QR generation
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

// ── QR web page ───────────────────────────────────────────
router.get('/qr/page/:userId', (req, res) => {
  const { userId } = req.params;
  const host = req.get('host') || `localhost:${process.env.PORT || 3000}`;

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
  <img id="qr" src="/qr/image/${userId}" width="280" height="280" alt="QR Code">
  <p id="status">Waiting for QR...</p>
  <script>
    const img = document.getElementById('qr');
    const status = document.getElementById('status');
    let connected = false;

    async function poll() {
      try {
        const r = await fetch('/status/${userId}');
        const d = await r.json();
        if (d.isReady) {
          connected = true;
          status.textContent = '✅ Connected! You can close this page.';
          img.style.display = 'none';
          return;
        }
      } catch {}

      if (!connected) {
        img.src = '/qr/image/${userId}?t=' + Date.now();
        status.textContent = 'Scan the QR code above';
        setTimeout(poll, 5000);
      }
    }

    poll();
  </script>
</body>
</html>`);
});

// ── Status ────────────────────────────────────────────────
router.get('/status/:userId', async (req, res) => {
  const userId = parseInt(req.params.userId, 10);
  if (isNaN(userId)) return res.status(400).json({ error: 'Invalid userId' });

  const clientData = clients.get(userId);
  if (!clientData) {
    return res.json({ isReady: false, hasQR: false });
  }

  if (!clientData.isReady) {
    return res.json({ isReady: false, hasQR: !!clientData.qr });
  }

  try {
    const chats = await clientData.client.getChats();
    const groups = chats.filter((c) => c.isGroup).map((c) => ({
      id: c.id._serialized,
      name: c.name,
      participants: c.participants?.length || 0,
    }));
    res.json({ isReady: true, hasQR: false, groups });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── TG groups (from Redis, written by bot) ───────────────
router.get('/tg-groups/:userId', async (req, res) => {
  const userId = parseInt(req.params.userId, 10);
  if (isNaN(userId)) return res.status(400).json({ error: 'Invalid userId' });

  try {
    const raw = await redis.hgetall(`bot:user_groups:${userId}`);
    const groups = Object.values(raw || {}).map((v) => JSON.parse(v));
    res.json({ groups });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Connect (create new client) ───────────────────────────
router.post('/connect/:userId', async (req, res) => {
  const userId = parseInt(req.params.userId, 10);
  if (isNaN(userId)) return res.status(400).json({ error: 'Invalid userId' });

  try {
    createWhatsAppClient(userId).catch(console.error); // fire & forget
    res.json({ message: 'Client starting', qrPageUrl: `/qr/page/${userId}` });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Disconnect ────────────────────────────────────────────
router.post('/disconnect/:userId', async (req, res) => {
  const userId = parseInt(req.params.userId, 10);
  if (isNaN(userId)) return res.status(400).json({ error: 'Invalid userId' });

  const clientData = clients.get(userId);
  if (!clientData) return res.status(404).json({ error: 'Not found' });

  try {
    await clientData.client.destroy();
    clients.delete(userId);
    res.json({ message: 'Disconnected' });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Reconnect ─────────────────────────────────────────────
router.post('/reconnect/:userId', async (req, res) => {
  const userId = parseInt(req.params.userId, 10);
  if (isNaN(userId)) return res.status(400).json({ error: 'Invalid userId' });

  const existing = clients.get(userId);
  if (existing?.client) {
    try { await existing.client.destroy(); } catch {}
    clients.delete(userId);
  }

  try {
    createWhatsAppClient(userId).catch(console.error);
    res.json({ message: 'Reconnecting', qrPageUrl: `/qr/page/${userId}` });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

module.exports = router;
