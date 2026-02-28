// ── Mock ioredis ─────────────────────────────────────────

let capturedRetryStrategy;

const mockRedisInstance = {
  set: jest.fn(),
  get: jest.fn(),
  setex: jest.fn(),
  lpush: jest.fn(),
  publish: jest.fn(),
  on: jest.fn(),
  disconnect: jest.fn(),
};

jest.mock('ioredis', () => {
  return jest.fn().mockImplementation((opts) => {
    if (opts && opts.retryStrategy) {
      capturedRetryStrategy = opts.retryStrategy;
    }
    return mockRedisInstance;
  });
});

const { publishMessage, getChatPairsCache, setChatPairsCache } = require('../src/redis-publisher');

beforeEach(() => {
  jest.clearAllMocks();
});

// ── retryStrategy ────────────────────────────────────────

describe('retryStrategy', () => {
  test('returns increasing delay for attempts 1-10', () => {
    expect(capturedRetryStrategy).toBeDefined();

    expect(capturedRetryStrategy(1)).toBe(500);
    expect(capturedRetryStrategy(2)).toBe(1000);
    expect(capturedRetryStrategy(5)).toBe(2500);
    expect(capturedRetryStrategy(10)).toBe(5000);
  });

  test('returns null after 10 attempts', () => {
    expect(capturedRetryStrategy(11)).toBeNull();
    expect(capturedRetryStrategy(20)).toBeNull();
  });

  test('caps delay at 5000ms', () => {
    // times * 500 for times=10 is 5000, which equals min(5000, 5000)
    expect(capturedRetryStrategy(10)).toBe(5000);
    // For times=9: min(4500, 5000) = 4500
    expect(capturedRetryStrategy(9)).toBe(4500);
  });
});

// ── publishMessage ───────────────────────────────────────

describe('publishMessage', () => {
  const payload = {
    wa_message_id: 'msg_123',
    body: 'hello',
    wa_chat_id: 'chat_1',
    user_id: 42,
  };

  test('new message — dedup SET NX succeeds, LPUSH called', async () => {
    mockRedisInstance.set.mockResolvedValue('OK');
    mockRedisInstance.lpush.mockResolvedValue(1);

    await publishMessage(payload);

    expect(mockRedisInstance.set).toHaveBeenCalledWith(
      'dedup:msg:msg_123',
      '1',
      'EX',
      300,
      'NX'
    );
    expect(mockRedisInstance.lpush).toHaveBeenCalledWith(
      'messages:in',
      JSON.stringify(payload)
    );
  });

  test('duplicate message — SET NX returns null, LPUSH NOT called', async () => {
    mockRedisInstance.set.mockResolvedValue(null);

    await publishMessage(payload);

    expect(mockRedisInstance.set).toHaveBeenCalled();
    expect(mockRedisInstance.lpush).not.toHaveBeenCalled();
  });
});

// ── getChatPairsCache ────────────────────────────────────

describe('getChatPairsCache', () => {
  test('cache hit — returns parsed JSON', async () => {
    const cached = [{ id: 1, wa_chat_id: 'chat_1', tg_chat_id: '-100123' }];
    mockRedisInstance.get.mockResolvedValue(JSON.stringify(cached));

    const result = await getChatPairsCache(42, 'chat_1');

    expect(mockRedisInstance.get).toHaveBeenCalledWith('chat_pairs:user:42:chat:chat_1');
    expect(result).toEqual(cached);
  });

  test('cache miss — returns null', async () => {
    mockRedisInstance.get.mockResolvedValue(null);

    const result = await getChatPairsCache(42, 'chat_1');

    expect(result).toBeNull();
  });

  test('redis error — returns null (non-critical)', async () => {
    mockRedisInstance.get.mockRejectedValue(new Error('connection lost'));

    const result = await getChatPairsCache(42, 'chat_1');

    expect(result).toBeNull();
  });
});

// ── setChatPairsCache ────────────────────────────────────

describe('setChatPairsCache', () => {
  test('sets value with TTL', async () => {
    mockRedisInstance.setex.mockResolvedValue('OK');

    await setChatPairsCache(42, 'chat_1', { id: 1 });

    expect(mockRedisInstance.setex).toHaveBeenCalledWith(
      'chat_pairs:user:42:chat:chat_1',
      3600,
      JSON.stringify({ id: 1 })
    );
  });

  test('redis error — does not throw', async () => {
    mockRedisInstance.setex.mockRejectedValue(new Error('connection lost'));

    await expect(setChatPairsCache(42, 'chat_1', { id: 1 })).resolves.not.toThrow();
  });
});
