// Tests for HTTP routes in src/routes/index.js
const express = require('express');
const request = require('supertest');

// Every route below authenticates. These tests use the bot's server-to-server scheme:
// a shared secret plus the user being acted for.
process.env.INTERNAL_API_TOKEN = 'test-internal';
const AUTH = { 'X-Internal-Token': 'test-internal', 'X-Internal-User-Id': '42' };
const authFor = (id) => ({ 'X-Internal-Token': 'test-internal', 'X-Internal-User-Id': String(id) });

// ── Mocks ─────────────────────────────────────────────────

const mockClients = new Map();

const mockRedis = {
  hgetall: jest.fn(),
  setex: jest.fn().mockResolvedValue('OK'),
  get: jest.fn().mockResolvedValue(null),
  status: 'ready',
};

jest.mock('../src/whatsapp-client', () => ({
  clients: mockClients,
  createWhatsAppClient: jest.fn(),
  getGroups: jest.fn(),
  getLastMessageAt: jest.fn(() => null),
  getHealthPasses: jest.fn(() => 0),
}));

jest.mock('../src/redis-publisher', () => ({
  redis: mockRedis,
}));

jest.mock('../src/db', () => ({
  getChatPairs: jest.fn(),
  getWaConnected: jest.fn(),
  setChatPairStatus: jest.fn(),
  deleteChatPair: jest.fn(),
  userExists: jest.fn(),
}));

// QRCode mock (avoids real PNG generation in tests)
jest.mock('qrcode', () => ({
  toBuffer: jest.fn().mockResolvedValue(Buffer.from('fake-png')),
}));

const { getChatPairs, getWaConnected, setChatPairStatus, deleteChatPair, userExists } = require('../src/db');
const { createWhatsAppClient, getGroups } = require('../src/whatsapp-client');
const router = require('../src/routes/index');

const app = express();
app.use(express.json());
app.use('/', router);

beforeEach(() => {
  jest.clearAllMocks();
  mockClients.clear();
  // Default: caller is a known/active user so the whitelist gate on client-creating
  // routes (/connect, /reconnect, /qr/image) passes. Individual tests can override.
  userExists.mockResolvedValue(true);
});

// ── GET /health ───────────────────────────────────────────

describe('no-store cache headers', () => {
  test('dynamic API responses are not cacheable', async () => {
    const res = await request(app).get('/status/42').set(AUTH);
    expect(res.headers['cache-control']).toMatch(/no-store/);
  });
});

describe('GET /health', () => {
  test('returns status ok with activeClients and redis', async () => {
    const res = await request(app).get('/health').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ok');
    expect(res.body).toHaveProperty('activeClients');
    expect(res.body).toHaveProperty('redis');
  });

  test('reports liveness for the monitoring dead-man switch', async () => {
    const { getLastMessageAt } = require('../src/whatsapp-client');
    getLastMessageAt.mockReturnValueOnce(1700000000000);

    const res = await request(app).get('/health').set(AUTH);

    expect(res.body.lastMessageAt).toBe(1700000000000);
    expect(res.body).toHaveProperty('readyClients');
  });

  test('reflects connected redis status', async () => {
    const res = await request(app).get('/health').set(AUTH);
    expect(res.body.redis).toBe('connected');
  });
});

// ── GET /status/:userId ───────────────────────────────────

describe('GET /status/:userId', () => {
  test('unknown user returns isReady: false, hasQR: false', async () => {
    const res = await request(app).get('/status/99').set(authFor(99));
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ isReady: false, hasQR: false });
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).get('/status/abc').set(AUTH);
    expect(res.status).toBe(400);
  });

  test('client not ready with QR — hasQR: true', async () => {
    mockClients.set(42, { isReady: false, qr: 'qr-data' });
    const res = await request(app).get('/status/42').set(AUTH);
    expect(res.body.isReady).toBe(false);
    expect(res.body.hasQR).toBe(true);
  });

  test('client not ready without QR — hasQR: false', async () => {
    mockClients.set(42, { isReady: false, qr: null });
    const res = await request(app).get('/status/42').set(AUTH);
    expect(res.body.hasQR).toBe(false);
  });

  test('ready client returns its groups', async () => {
    mockClients.set(42, { isReady: true, qr: null, client: {} });
    getGroups.mockResolvedValue([{ id: 'g1@g.us', name: 'Team', participants: 2 }]);
    const res = await request(app).get('/status/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.body.isReady).toBe(true);
    expect(res.body.groups).toEqual([{ id: 'g1@g.us', name: 'Team', participants: 2 }]);
    expect(res.body.groupsError).toBeUndefined();
  });

  // Regression: getChats() throws a bare 'r' whenever WhatsApp ships a build the library
  // hasn't caught up with. This used to answer 500, and the Mini App — which only reads
  // data.isReady — fell back to the Connect screen and waited forever for a QR that a
  // connected user never gets. The session state must survive a failing group lookup.
  test('getChats failure still reports the session as connected', async () => {
    mockClients.set(42, { isReady: true, qr: null, client: {} });
    getGroups.mockRejectedValue(new Error('r'));
    const res = await request(app).get('/status/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.body.isReady).toBe(true);
    expect(res.body.hasQR).toBe(false);
    expect(res.body.groups).toEqual([]);
    expect(res.body.groupsError).toBe('r');
  });
});

// ── GET /qr/image/:userId ─────────────────────────────────

describe('GET /qr/image/:userId', () => {
  test('invalid userId returns 400', async () => {
    const res = await request(app).get('/qr/image/abc').set(AUTH);
    expect(res.status).toBe(400);
  });

  test('no client — starts one and returns 202', async () => {
    createWhatsAppClient.mockResolvedValue();
    const res = await request(app).get('/qr/image/42').set(AUTH);
    expect(res.status).toBe(202);
    expect(createWhatsAppClient).toHaveBeenCalledWith(42);
  });

  test('client ready — returns JSON status:ready', async () => {
    mockClients.set(42, { isReady: true, qr: null });
    const res = await request(app).get('/qr/image/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ready');
  });

  test('client pending (no QR yet) — returns 202 waiting', async () => {
    mockClients.set(42, { isReady: false, qr: null });
    const res = await request(app).get('/qr/image/42').set(AUTH);
    expect(res.status).toBe(202);
    expect(res.body.status).toBe('waiting');
  });

  test('client has QR — returns PNG image', async () => {
    mockClients.set(42, { isReady: false, qr: '1@abc' });
    const res = await request(app).get('/qr/image/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/image\/png/);
  });
});

// ── POST /connect/:userId ─────────────────────────────────

describe('POST /connect/:userId', () => {
  test('starts client and returns qrPageUrl', async () => {
    createWhatsAppClient.mockResolvedValue();
    const res = await request(app).post('/connect/42').set(AUTH);
    expect(res.status).toBe(200);
    // The QR page is addressed by a one-time token, not by user id: the old
    // /qr/page/:userId handed anyone who guessed an id the QR that links a device.
    expect(res.body.qrPageUrl).toMatch(/^\/qr\/page\?t=[a-f0-9]{32}$/);
    expect(createWhatsAppClient).toHaveBeenCalledWith(42);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).post('/connect/abc').set(AUTH);
    expect(res.status).toBe(400);
  });
});

// ── POST /disconnect/:userId ──────────────────────────────

describe('POST /disconnect/:userId', () => {
  test('disconnects existing client and removes from map', async () => {
    const mockDestroy = jest.fn().mockResolvedValue();
    mockClients.set(42, { client: { destroy: mockDestroy }, isReady: true });

    const res = await request(app).post('/disconnect/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(mockDestroy).toHaveBeenCalled();
    expect(mockClients.has(42)).toBe(false);
  });

  test('unknown client returns 404', async () => {
    const res = await request(app).post('/disconnect/99').set(authFor(99));
    expect(res.status).toBe(404);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).post('/disconnect/abc').set(AUTH);
    expect(res.status).toBe(400);
  });
});

// ── POST /reconnect/:userId ───────────────────────────────

describe('POST /reconnect/:userId', () => {
  test('destroys existing client and starts new one', async () => {
    const mockDestroy = jest.fn().mockResolvedValue();
    mockClients.set(42, { client: { destroy: mockDestroy }, isReady: true });
    createWhatsAppClient.mockResolvedValue();

    const res = await request(app).post('/reconnect/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(mockDestroy).toHaveBeenCalled();
    expect(createWhatsAppClient).toHaveBeenCalledWith(42);
  });

  test('no existing client — still starts new one', async () => {
    createWhatsAppClient.mockResolvedValue();
    const res = await request(app).post('/reconnect/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(createWhatsAppClient).toHaveBeenCalledWith(42);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).post('/reconnect/abc').set(AUTH);
    expect(res.status).toBe(400);
  });
});

// ── GET /chat-pairs/:userId ───────────────────────────────

describe('GET /chat-pairs/:userId', () => {
  test('returns pairs and wa_connected', async () => {
    const pairs = [{ id: 1, wa_chat_id: 'chat_1', tg_chat_id: '-100' }];
    getChatPairs.mockResolvedValue(pairs);
    getWaConnected.mockResolvedValue(true);

    const res = await request(app).get('/chat-pairs/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.body.pairs).toEqual(pairs);
    expect(res.body.wa_connected).toBe(true);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).get('/chat-pairs/abc').set(AUTH);
    expect(res.status).toBe(400);
  });

  test('db error returns 500', async () => {
    getChatPairs.mockRejectedValue(new Error('db down'));
    const res = await request(app).get('/chat-pairs/42').set(AUTH);
    expect(res.status).toBe(500);
  });
});

// ── PATCH /chat-pairs/:pairId ─────────────────────────────

describe('PATCH /chat-pairs/:pairId', () => {
  test('updates to paused', async () => {
    setChatPairStatus.mockResolvedValue(true);
    const res = await request(app).patch('/chat-pairs/1').set(AUTH).send({ status: 'paused' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(setChatPairStatus).toHaveBeenCalledWith(1, 'paused', 42); // scoped to the owner
  });

  test('updates to active', async () => {
    setChatPairStatus.mockResolvedValue(true);
    const res = await request(app).patch('/chat-pairs/1').set(AUTH).send({ status: 'active' });
    expect(res.status).toBe(200);
  });

  test('invalid status returns 400', async () => {
    const res = await request(app).patch('/chat-pairs/1').set(AUTH).send({ status: 'deleted' });
    expect(res.status).toBe(400);
  });

  test('pair not found returns 404', async () => {
    setChatPairStatus.mockResolvedValue(false);
    const res = await request(app).patch('/chat-pairs/99').set(AUTH).send({ status: 'active' });
    expect(res.status).toBe(404);
  });

  test('invalid pairId returns 400', async () => {
    const res = await request(app).patch('/chat-pairs/abc').set(AUTH).send({ status: 'active' });
    expect(res.status).toBe(400);
  });
});

// ── DELETE /chat-pairs/:pairId ────────────────────────────

describe('DELETE /chat-pairs/:pairId', () => {
  test('deletes pair', async () => {
    deleteChatPair.mockResolvedValue(true);
    const res = await request(app).delete('/chat-pairs/1').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(deleteChatPair).toHaveBeenCalledWith(1, 42); // scoped to the owner
  });

  test('pair not found returns 404', async () => {
    deleteChatPair.mockResolvedValue(false);
    const res = await request(app).delete('/chat-pairs/99').set(AUTH);
    expect(res.status).toBe(404);
  });

  test('invalid pairId returns 400', async () => {
    const res = await request(app).delete('/chat-pairs/abc').set(AUTH);
    expect(res.status).toBe(400);
  });
});

// ── GET /tg-groups/:userId ────────────────────────────────

describe('GET /tg-groups/:userId', () => {
  test('returns groups parsed from Redis hash', async () => {
    const group = { id: '-100123', name: 'Test Group' };
    mockRedis.hgetall.mockResolvedValue({ g1: JSON.stringify(group) });

    const res = await request(app).get('/tg-groups/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.body.groups).toEqual([group]);
    expect(mockRedis.hgetall).toHaveBeenCalledWith('bot:user_groups:42');
  });

  test('empty Redis hash returns empty array', async () => {
    mockRedis.hgetall.mockResolvedValue(null);
    const res = await request(app).get('/tg-groups/42').set(AUTH);
    expect(res.status).toBe(200);
    expect(res.body.groups).toEqual([]);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).get('/tg-groups/abc').set(AUTH);
    expect(res.status).toBe(400);
  });

  test('redis error returns 500', async () => {
    mockRedis.hgetall.mockRejectedValue(new Error('redis down'));
    const res = await request(app).get('/tg-groups/42').set(AUTH);
    expect(res.status).toBe(500);
  });
});


// ── Authentication ────────────────────────────────────────
// These routes sat on nginx's `location /` with no auth of their own: anyone on the
// internet could walk Telegram user ids and collect QR codes, spawn Chromium instances,
// or delete other people's bridges.

describe('authentication', () => {
  const PROTECTED = [
    ['get', '/qr/image/42'],
    ['get', '/status/42'],
    ['get', '/tg-groups/42'],
    ['get', '/chat-pairs/42'],
    ['post', '/connect/42'],
    ['post', '/disconnect/42'],
    ['post', '/reconnect/42'],
    ['patch', '/chat-pairs/1'],
    ['delete', '/chat-pairs/1'],
  ];

  test.each(PROTECTED)('%s %s requires credentials', async (method, path) => {
    const res = await request(app)[method](path);
    expect(res.status).toBe(401);
  });

  test.each(PROTECTED)('%s %s rejects a wrong shared secret', async (method, path) => {
    const res = await request(app)[method](path).set({ 'X-Internal-Token': 'guessed' });
    expect(res.status).toBe(401);
  });

  test('a user cannot reach another user\'s QR', async () => {
    const res = await request(app)
      .get('/qr/image/191440421')
      .set({ 'X-Internal-Token': 'test-internal' }); // no X-Internal-User-Id → no identity
    expect(res.status).toBe(403);
  });

  test('/health stays open for the monitoring flow', async () => {
    const res = await request(app).get('/health');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ok');
  });

  test('an expired QR token does not open the QR page', async () => {
    mockRedis.get.mockResolvedValueOnce(null);
    const res = await request(app).get('/qr/page?t=' + 'a'.repeat(32));
    expect(res.status).toBe(401);
  });
});
