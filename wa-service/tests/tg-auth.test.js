const crypto = require('crypto');

// Redis is only used for QR tokens here; stub it so the suite needs no server.
const mockStore = new Map();
jest.mock('ioredis', () => {
  return jest.fn().mockImplementation(() => ({
    setex: jest.fn(async (k, ttl, v) => { mockStore.set(k, v); return 'OK'; }),
    get: jest.fn(async (k) => (mockStore.has(k) ? mockStore.get(k) : null)),
    on: jest.fn(),
    eval: jest.fn(),
    disconnect: jest.fn(),
  }));
});

const BOT_TOKEN = '123456:TEST-TOKEN';
process.env.TELEGRAM_BOT_TOKEN = BOT_TOKEN;
process.env.INTERNAL_API_TOKEN = 'internal-secret';

const { verifyInitData, authenticate, requireSelf, createQrToken } = require('../src/middleware/tg-auth');

/** Build a correctly signed initData blob, the way Telegram does. */
function signInitData(user, { authDate = Math.floor(Date.now() / 1000), token = BOT_TOKEN } = {}) {
  const params = new URLSearchParams({
    auth_date: String(authDate),
    query_id: 'AAE',
    user: JSON.stringify(user),
  });
  const dataCheckString = [...params.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join('\n');
  const secret = crypto.createHmac('sha256', 'WebAppData').update(token).digest();
  const hash = crypto.createHmac('sha256', secret).update(dataCheckString).digest('hex');
  params.set('hash', hash);
  return params.toString();
}

function mockRes() {
  return {
    statusCode: null,
    body: null,
    status(code) { this.statusCode = code; return this; },
    json(payload) { this.body = payload; return this; },
  };
}

function mockReq({ headers = {}, params = {}, query = {} } = {}) {
  return {
    headers,
    params,
    query,
    get(name) { return headers[name]; },
  };
}

describe('verifyInitData', () => {
  test('accepts data signed with the bot token', () => {
    const result = verifyInitData(signInitData({ id: 42, first_name: 'A' }), BOT_TOKEN);
    expect(result).toEqual(expect.objectContaining({ userId: 42 }));
  });

  test('rejects a forged user id', () => {
    // The whole point: initDataUnsafe.user.id used to be taken at face value, so anyone
    // could claim to be any Telegram user and collect their WhatsApp QR.
    const signed = signInitData({ id: 42 });
    const params = new URLSearchParams(signed);
    params.set('user', JSON.stringify({ id: 9999 })); // swap the identity, keep the signature
    expect(verifyInitData(params.toString(), BOT_TOKEN)).toBeNull();

    // sanity: the untampered blob really does verify, so the rejection above is the swap
    expect(verifyInitData(signed, BOT_TOKEN)).toEqual(expect.objectContaining({ userId: 42 }));
  });

  test('rejects data signed with a different bot token', () => {
    const signed = signInitData({ id: 42 }, { token: '999:OTHER' });
    expect(verifyInitData(signed, BOT_TOKEN)).toBeNull();
  });

  test('rejects stale data', () => {
    const old = Math.floor(Date.now() / 1000) - 90000; // > 24h
    expect(verifyInitData(signInitData({ id: 42 }, { authDate: old }), BOT_TOKEN)).toBeNull();
  });

  test('rejects missing or malformed input', () => {
    expect(verifyInitData('', BOT_TOKEN)).toBeNull();
    expect(verifyInitData('user=%7B%7D', BOT_TOKEN)).toBeNull();
    expect(verifyInitData(signInitData({ id: 42 }), '')).toBeNull();
  });
});

describe('authenticate', () => {
  test('valid initData populates req.auth', async () => {
    const req = mockReq({ headers: { 'X-Tg-Init-Data': signInitData({ id: 7 }) } });
    const res = mockRes();
    const next = jest.fn();

    await authenticate(req, res, next);

    expect(next).toHaveBeenCalled();
    expect(req.auth).toEqual({ userId: 7, via: 'initdata' });
  });

  test('no credentials at all → 401', async () => {
    const req = mockReq();
    const res = mockRes();
    const next = jest.fn();

    await authenticate(req, res, next);

    expect(next).not.toHaveBeenCalled();
    expect(res.statusCode).toBe(401);
  });

  test('bad initData → 401, never falls through to another scheme', async () => {
    const req = mockReq({ headers: { 'X-Tg-Init-Data': 'user=%7B%22id%22%3A1%7D&hash=' + 'a'.repeat(64) } });
    const res = mockRes();
    const next = jest.fn();

    await authenticate(req, res, next);

    expect(res.statusCode).toBe(401);
    expect(next).not.toHaveBeenCalled();
  });

  test('internal token authenticates the bot', async () => {
    const req = mockReq({ headers: { 'X-Internal-Token': 'internal-secret' }, params: { userId: '55' } });
    const res = mockRes();
    const next = jest.fn();

    await authenticate(req, res, next);

    expect(req.auth).toEqual({ userId: 55, via: 'internal' });
  });

  test('wrong internal token is not accepted', async () => {
    const req = mockReq({ headers: { 'X-Internal-Token': 'guess' }, params: { userId: '55' } });
    const res = mockRes();
    const next = jest.fn();

    await authenticate(req, res, next);

    expect(res.statusCode).toBe(401);
  });

  test('a minted QR token authenticates its own user', async () => {
    const token = await createQrToken(88);
    const req = mockReq({ query: { t: token } });
    const res = mockRes();
    const next = jest.fn();

    await authenticate(req, res, next);

    expect(req.auth).toEqual({ userId: 88, via: 'qr-token' });
  });

  test('an unknown QR token does not', async () => {
    const req = mockReq({ query: { t: 'f'.repeat(32) } });
    const res = mockRes();
    const next = jest.fn();

    await authenticate(req, res, next);

    expect(res.statusCode).toBe(401);
  });
});

describe('requireSelf', () => {
  test('lets a user reach their own resources', () => {
    const req = mockReq({ params: { userId: '7' } });
    req.auth = { userId: 7, via: 'initdata' };
    const res = mockRes();
    const next = jest.fn();

    requireSelf(req, res, next);

    expect(next).toHaveBeenCalled();
  });

  test('blocks reaching another user — the QR takeover path', () => {
    const req = mockReq({ params: { userId: '191440421' } });
    req.auth = { userId: 7, via: 'initdata' };
    const res = mockRes();
    const next = jest.fn();

    requireSelf(req, res, next);

    expect(next).not.toHaveBeenCalled();
    expect(res.statusCode).toBe(403);
  });

  test('the bot may act on behalf of a user', () => {
    const req = mockReq({ params: { userId: '191440421' } });
    req.auth = { userId: 191440421, via: 'internal' };
    const res = mockRes();
    const next = jest.fn();

    requireSelf(req, res, next);

    expect(next).toHaveBeenCalled();
  });
});
