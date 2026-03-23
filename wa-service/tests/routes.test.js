// Tests for HTTP routes in src/routes/index.js
const express = require('express');
const request = require('supertest');

// ── Mocks ─────────────────────────────────────────────────

const mockClients = new Map();

const mockRedis = {
  hgetall: jest.fn(),
  status: 'ready',
};

jest.mock('../src/whatsapp-client', () => ({
  clients: mockClients,
  createWhatsAppClient: jest.fn(),
}));

jest.mock('../src/redis-publisher', () => ({
  redis: mockRedis,
}));

jest.mock('../src/db', () => ({
  getChatPairs: jest.fn(),
  getWaConnected: jest.fn(),
  setChatPairStatus: jest.fn(),
  deleteChatPair: jest.fn(),
}));

// QRCode mock (avoids real PNG generation in tests)
jest.mock('qrcode', () => ({
  toBuffer: jest.fn().mockResolvedValue(Buffer.from('fake-png')),
}));

const { getChatPairs, getWaConnected, setChatPairStatus, deleteChatPair } = require('../src/db');
const { createWhatsAppClient } = require('../src/whatsapp-client');
const router = require('../src/routes/index');

const app = express();
app.use(express.json());
app.use('/', router);

beforeEach(() => {
  jest.clearAllMocks();
  mockClients.clear();
});

// ── GET /health ───────────────────────────────────────────

describe('GET /health', () => {
  test('returns status ok with activeClients and redis', async () => {
    const res = await request(app).get('/health');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ok');
    expect(res.body).toHaveProperty('activeClients');
    expect(res.body).toHaveProperty('redis');
  });

  test('reflects connected redis status', async () => {
    const res = await request(app).get('/health');
    expect(res.body.redis).toBe('connected');
  });
});

// ── GET /status/:userId ───────────────────────────────────

describe('GET /status/:userId', () => {
  test('unknown user returns isReady: false, hasQR: false', async () => {
    const res = await request(app).get('/status/99');
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ isReady: false, hasQR: false });
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).get('/status/abc');
    expect(res.status).toBe(400);
  });

  test('client not ready with QR — hasQR: true', async () => {
    mockClients.set(42, { isReady: false, qr: 'qr-data' });
    const res = await request(app).get('/status/42');
    expect(res.body.isReady).toBe(false);
    expect(res.body.hasQR).toBe(true);
  });

  test('client not ready without QR — hasQR: false', async () => {
    mockClients.set(42, { isReady: false, qr: null });
    const res = await request(app).get('/status/42');
    expect(res.body.hasQR).toBe(false);
  });
});

// ── GET /qr/image/:userId ─────────────────────────────────

describe('GET /qr/image/:userId', () => {
  test('invalid userId returns 400', async () => {
    const res = await request(app).get('/qr/image/abc');
    expect(res.status).toBe(400);
  });

  test('no client — starts one and returns 202', async () => {
    createWhatsAppClient.mockResolvedValue();
    const res = await request(app).get('/qr/image/42');
    expect(res.status).toBe(202);
    expect(createWhatsAppClient).toHaveBeenCalledWith(42);
  });

  test('client ready — returns JSON status:ready', async () => {
    mockClients.set(42, { isReady: true, qr: null });
    const res = await request(app).get('/qr/image/42');
    expect(res.status).toBe(200);
    expect(res.body.status).toBe('ready');
  });

  test('client pending (no QR yet) — returns 202 waiting', async () => {
    mockClients.set(42, { isReady: false, qr: null });
    const res = await request(app).get('/qr/image/42');
    expect(res.status).toBe(202);
    expect(res.body.status).toBe('waiting');
  });

  test('client has QR — returns PNG image', async () => {
    mockClients.set(42, { isReady: false, qr: '1@abc' });
    const res = await request(app).get('/qr/image/42');
    expect(res.status).toBe(200);
    expect(res.headers['content-type']).toMatch(/image\/png/);
  });
});

// ── POST /connect/:userId ─────────────────────────────────

describe('POST /connect/:userId', () => {
  test('starts client and returns qrPageUrl', async () => {
    createWhatsAppClient.mockResolvedValue();
    const res = await request(app).post('/connect/42');
    expect(res.status).toBe(200);
    expect(res.body.qrPageUrl).toBe('/qr/page/42');
    expect(createWhatsAppClient).toHaveBeenCalledWith(42);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).post('/connect/abc');
    expect(res.status).toBe(400);
  });
});

// ── POST /disconnect/:userId ──────────────────────────────

describe('POST /disconnect/:userId', () => {
  test('disconnects existing client and removes from map', async () => {
    const mockDestroy = jest.fn().mockResolvedValue();
    mockClients.set(42, { client: { destroy: mockDestroy }, isReady: true });

    const res = await request(app).post('/disconnect/42');
    expect(res.status).toBe(200);
    expect(mockDestroy).toHaveBeenCalled();
    expect(mockClients.has(42)).toBe(false);
  });

  test('unknown client returns 404', async () => {
    const res = await request(app).post('/disconnect/99');
    expect(res.status).toBe(404);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).post('/disconnect/abc');
    expect(res.status).toBe(400);
  });
});

// ── POST /reconnect/:userId ───────────────────────────────

describe('POST /reconnect/:userId', () => {
  test('destroys existing client and starts new one', async () => {
    const mockDestroy = jest.fn().mockResolvedValue();
    mockClients.set(42, { client: { destroy: mockDestroy }, isReady: true });
    createWhatsAppClient.mockResolvedValue();

    const res = await request(app).post('/reconnect/42');
    expect(res.status).toBe(200);
    expect(mockDestroy).toHaveBeenCalled();
    expect(createWhatsAppClient).toHaveBeenCalledWith(42);
  });

  test('no existing client — still starts new one', async () => {
    createWhatsAppClient.mockResolvedValue();
    const res = await request(app).post('/reconnect/42');
    expect(res.status).toBe(200);
    expect(createWhatsAppClient).toHaveBeenCalledWith(42);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).post('/reconnect/abc');
    expect(res.status).toBe(400);
  });
});

// ── GET /chat-pairs/:userId ───────────────────────────────

describe('GET /chat-pairs/:userId', () => {
  test('returns pairs and wa_connected', async () => {
    const pairs = [{ id: 1, wa_chat_id: 'chat_1', tg_chat_id: '-100' }];
    getChatPairs.mockResolvedValue(pairs);
    getWaConnected.mockResolvedValue(true);

    const res = await request(app).get('/chat-pairs/42');
    expect(res.status).toBe(200);
    expect(res.body.pairs).toEqual(pairs);
    expect(res.body.wa_connected).toBe(true);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).get('/chat-pairs/abc');
    expect(res.status).toBe(400);
  });

  test('db error returns 500', async () => {
    getChatPairs.mockRejectedValue(new Error('db down'));
    const res = await request(app).get('/chat-pairs/42');
    expect(res.status).toBe(500);
  });
});

// ── PATCH /chat-pairs/:pairId ─────────────────────────────

describe('PATCH /chat-pairs/:pairId', () => {
  test('updates to paused', async () => {
    setChatPairStatus.mockResolvedValue(true);
    const res = await request(app).patch('/chat-pairs/1').send({ status: 'paused' });
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(setChatPairStatus).toHaveBeenCalledWith(1, 'paused');
  });

  test('updates to active', async () => {
    setChatPairStatus.mockResolvedValue(true);
    const res = await request(app).patch('/chat-pairs/1').send({ status: 'active' });
    expect(res.status).toBe(200);
  });

  test('invalid status returns 400', async () => {
    const res = await request(app).patch('/chat-pairs/1').send({ status: 'deleted' });
    expect(res.status).toBe(400);
  });

  test('pair not found returns 404', async () => {
    setChatPairStatus.mockResolvedValue(false);
    const res = await request(app).patch('/chat-pairs/99').send({ status: 'active' });
    expect(res.status).toBe(404);
  });

  test('invalid pairId returns 400', async () => {
    const res = await request(app).patch('/chat-pairs/abc').send({ status: 'active' });
    expect(res.status).toBe(400);
  });
});

// ── DELETE /chat-pairs/:pairId ────────────────────────────

describe('DELETE /chat-pairs/:pairId', () => {
  test('deletes pair', async () => {
    deleteChatPair.mockResolvedValue(true);
    const res = await request(app).delete('/chat-pairs/1');
    expect(res.status).toBe(200);
    expect(res.body.ok).toBe(true);
    expect(deleteChatPair).toHaveBeenCalledWith(1);
  });

  test('pair not found returns 404', async () => {
    deleteChatPair.mockResolvedValue(false);
    const res = await request(app).delete('/chat-pairs/99');
    expect(res.status).toBe(404);
  });

  test('invalid pairId returns 400', async () => {
    const res = await request(app).delete('/chat-pairs/abc');
    expect(res.status).toBe(400);
  });
});

// ── GET /tg-groups/:userId ────────────────────────────────

describe('GET /tg-groups/:userId', () => {
  test('returns groups parsed from Redis hash', async () => {
    const group = { id: '-100123', name: 'Test Group' };
    mockRedis.hgetall.mockResolvedValue({ g1: JSON.stringify(group) });

    const res = await request(app).get('/tg-groups/42');
    expect(res.status).toBe(200);
    expect(res.body.groups).toEqual([group]);
    expect(mockRedis.hgetall).toHaveBeenCalledWith('bot:user_groups:42');
  });

  test('empty Redis hash returns empty array', async () => {
    mockRedis.hgetall.mockResolvedValue(null);
    const res = await request(app).get('/tg-groups/42');
    expect(res.status).toBe(200);
    expect(res.body.groups).toEqual([]);
  });

  test('invalid userId returns 400', async () => {
    const res = await request(app).get('/tg-groups/abc');
    expect(res.status).toBe(400);
  });

  test('redis error returns 500', async () => {
    mockRedis.hgetall.mockRejectedValue(new Error('redis down'));
    const res = await request(app).get('/tg-groups/42');
    expect(res.status).toBe(500);
  });
});
