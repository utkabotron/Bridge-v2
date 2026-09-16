// ── Mock ioredis ─────────────────────────────────────────

let capturedRetryStrategy;

const mockRedisInstance = {
  set: jest.fn(),
  get: jest.fn(),
  setex: jest.fn(),
  lpush: jest.fn(),
  eval: jest.fn(),
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

  test('never returns null — reconnects forever with capped delay', () => {
    // Returning null would put ioredis into a terminal state and silently drop every
    // message on the single replica until a manual restart. It must keep retrying.
    expect(capturedRetryStrategy(11)).toBe(5000);
    expect(capturedRetryStrategy(20)).toBe(5000);
    expect(capturedRetryStrategy(1000)).toBe(5000);
  });

  test('caps delay at 5000ms', () => {
    expect(capturedRetryStrategy(10)).toBe(5000);
    // For times=9: min(4500, 5000) = 4500
    expect(capturedRetryStrategy(9)).toBe(4500);
  });
});

// ── publishMessage ───────────────────────────────────────

describe('publishMessage', () => {
  function basePayload() {
    return {
      wa_message_id: 'msg_123',
      body: 'hello',
      wa_chat_id: 'chat_1',
      user_id: 42,
      timestamp: 1700000000,
    };
  }

  test('new message — atomic dedup+enqueue via eval', async () => {
    mockRedisInstance.eval.mockResolvedValue(1);
    const payload = basePayload();

    await publishMessage(payload);

    expect(mockRedisInstance.eval).toHaveBeenCalledTimes(1);
    const args = mockRedisInstance.eval.mock.calls[0];
    // args: [lua, numKeys, dedupKey, queueKey, ttl, json]
    expect(args[1]).toBe(2);
    expect(args[2]).toBe('dedup:msg:msg_123');
    expect(args[3]).toBe('messages:in');
    expect(args[4]).toBe(300);
    expect(JSON.parse(args[5]).wa_message_id).toBe('msg_123');
  });

  test('missing wa_message_id — stable content fallback assigned to payload', async () => {
    mockRedisInstance.eval.mockResolvedValue(1);
    const payload = basePayload();
    delete payload.wa_message_id;

    await publishMessage(payload);

    // The fallback id is written back into the payload so Redis dedup AND the processor's
    // DB unique key use the same stable per-message id (root-cause fix for the media loss).
    expect(payload.wa_message_id).toMatch(/^fallback:[0-9a-f]{16}$/);
    const args = mockRedisInstance.eval.mock.calls[0];
    expect(args[2]).toBe(`dedup:msg:${payload.wa_message_id}`);
  });

  test('missing wa_message_id — same group message from two clients collapses to one enqueue', async () => {
    // Every WA client in a group receives the same message. While the fallback id hashed
    // user_id, four clients produced four ids → four translations → four Telegram messages.
    mockRedisInstance.eval.mockResolvedValue(1);
    const fromClientA = { ...basePayload(), user_id: 42 };
    const fromClientB = { ...basePayload(), user_id: 77, sender_name: 'Alice (work)' };
    delete fromClientA.wa_message_id;
    delete fromClientB.wa_message_id;

    await publishMessage(fromClientA);
    await publishMessage(fromClientB);

    expect(fromClientB.wa_message_id).toBe(fromClientA.wa_message_id);
    expect(mockRedisInstance.eval.mock.calls[1][2]).toBe(
      mockRedisInstance.eval.mock.calls[0][2]
    );
  });

  test('missing wa_message_id — different chats keep different dedup ids', async () => {
    mockRedisInstance.eval.mockResolvedValue(1);
    const first = { ...basePayload(), wa_chat_id: 'chat_1' };
    const second = { ...basePayload(), wa_chat_id: 'chat_2' };
    delete first.wa_message_id;
    delete second.wa_message_id;

    await publishMessage(first);
    await publishMessage(second);

    expect(second.wa_message_id).not.toBe(first.wa_message_id);
  });

  test('edit — gets its own dedup namespace (not dropped as duplicate of original)', async () => {
    mockRedisInstance.eval.mockResolvedValue(1);
    const payload = { ...basePayload(), is_edited: true };

    await publishMessage(payload);

    expect(payload.wa_message_id).toMatch(/^msg_123:edit:[0-9a-f]{12}$/);
  });

  test('duplicate message — eval returns 0, does not throw', async () => {
    mockRedisInstance.eval.mockResolvedValue(0);

    await expect(publishMessage(basePayload())).resolves.not.toThrow();
    expect(mockRedisInstance.eval).toHaveBeenCalledTimes(1);
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
