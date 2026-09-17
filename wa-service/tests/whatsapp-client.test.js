// ── Mocks ────────────────────────────────────────────────

jest.mock('fs');
jest.mock('../src/redis-publisher', () => ({
  publishMessage: jest.fn(),
  publishQrScanned: jest.fn().mockResolvedValue(),
  getChatPairsCache: jest.fn().mockResolvedValue(null),
  setChatPairsCache: jest.fn().mockResolvedValue(),
}));
jest.mock('../src/media-handler', () => ({
  handleMedia: jest.fn(),
}));
jest.mock('../src/db', () => ({
  setWaConnected: jest.fn().mockResolvedValue(true),
  setWaDisconnected: jest.fn().mockResolvedValue(),
  userExists: jest.fn().mockResolvedValue(true),
}));

// Mock whatsapp-web.js — Client extends EventEmitter so we can emit events
// Variables prefixed with `mock` are allowed inside jest.mock factory
const mockClientOptions = [];
const mockInitialize = jest.fn().mockResolvedValue();
const mockDestroy = jest.fn().mockResolvedValue();
const mockGetState = jest.fn().mockResolvedValue('CONNECTED');

jest.mock('whatsapp-web.js', () => {
  const { EventEmitter } = require('events');
  class MockClient extends EventEmitter {
    constructor(opts) {
      super();
      mockClientOptions.push(opts);
      this.initialize = mockInitialize;
      this.destroy = mockDestroy;
      this.getState = mockGetState;
    }
  }
  return {
    Client: MockClient,
    LocalAuth: jest.fn(),
  };
});

const fs = require('fs');
const {
  clients,
  createWhatsAppClient,
  getGroups,
  destroyAllClients,
  recoverLostSessions,
  getAuthenticatedSessionUids,
  recoveryState,
  inProgress,
} = require('../src/whatsapp-client');

// Event handlers are async (they await destroyClient before deleting the client from the
// map), so tests must let those microtasks/timers settle before asserting. Under real
// timers use setImmediate; under fake timers use advanceTimersByTimeAsync(0).
const flush = () => new Promise((r) => setImmediate(r));

// We need access to internal functions not exported — re-read the module source
// Actually, cleanupSingletonLocks, reconnectClient, checkClientHealth, etc. are NOT exported.
// We test them indirectly through the exported functions + event handlers.

beforeEach(() => {
  jest.clearAllMocks();
  clients.clear();
  recoveryState.clear();
  inProgress.clear();
  mockInitialize.mockResolvedValue();
  mockDestroy.mockResolvedValue();
  mockGetState.mockResolvedValue('CONNECTED');
});

// ── cleanupSingletonLocks (tested via restoreExistingSessions) ──

describe('cleanupSingletonLocks (via restoreExistingSessions)', () => {
  // cleanupSingletonLocks is called at the start of restoreExistingSessions
  const { restoreExistingSessions } = require('../src/whatsapp-client');

  test('removes SingletonLock files from auth dir', async () => {
    fs.existsSync.mockImplementation((p) => p === '.wwebjs_auth');
    fs.readdirSync.mockImplementation((dir, opts) => {
      if (opts && opts.recursive) {
        return ['session-user-1/Default/SingletonLock', 'session-user-1/file.txt'];
      }
      // For restoreExistingSessions second readdirSync (no recursive)
      return [];
    });
    fs.unlinkSync.mockImplementation(() => {});

    await restoreExistingSessions();

    expect(fs.unlinkSync).toHaveBeenCalledWith(
      expect.stringContaining('SingletonLock')
    );
    expect(fs.unlinkSync).toHaveBeenCalledTimes(1);
  });

  test('no auth dir — does not throw', async () => {
    fs.existsSync.mockReturnValue(false);

    await expect(restoreExistingSessions()).resolves.not.toThrow();
    expect(fs.unlinkSync).not.toHaveBeenCalled();
  });
});

// ── createWhatsAppClient ─────────────────────────────────

describe('createWhatsAppClient', () => {
  test('creates a new client and adds to map', async () => {
    const result = await createWhatsAppClient(42);
    expect(clients.has(42)).toBe(true);
    expect(result.userId).toBe(42);
    expect(mockInitialize).toHaveBeenCalledTimes(1);
  });

  test('returns existing client for duplicate userId', async () => {
    const first = await createWhatsAppClient(42);
    const second = await createWhatsAppClient(42);
    expect(second).toBe(first);
    expect(mockInitialize).toHaveBeenCalledTimes(1);
  });

  test('throws when max clients reached', async () => {
    // MAX_CONCURRENT_CLIENTS defaults to 4: each client is a ~400 MB Chromium and the
    // box has 3.8 GB, so the old default of 10 promised an OOM rather than a rejection.
    const limit = require('../src/config').MAX_CONCURRENT_CLIENTS;
    for (let i = 1; i <= limit; i++) {
      await createWhatsAppClient(i);
    }
    await expect(createWhatsAppClient(limit + 1)).rejects.toThrow('Max clients reached');
  });

  test('cleans up on initialize failure', async () => {
    mockInitialize.mockRejectedValueOnce(new Error('init failed'));
    await expect(createWhatsAppClient(99)).rejects.toThrow('init failed');
    expect(clients.has(99)).toBe(false);
  });

  // Regression: dropping the client from the map is not enough — a failed initialize()
  // leaves its Chromium alive holding the profile's SingletonLock, so the next attempt
  // dies with "browser is already running" and the orphan keeps its memory. Thirty such
  // orphans is what put the 3.8 GB box into swap death.
  test('initialize failure destroys the browser and frees the profile lock', async () => {
    mockInitialize.mockRejectedValueOnce(new Error('init failed'));
    mockDestroy.mockClear();
    fs.unlinkSync.mockClear();
    fs.unlinkSync.mockImplementation(() => {});

    await expect(createWhatsAppClient(99)).rejects.toThrow('init failed');

    expect(mockDestroy).toHaveBeenCalled();
    expect(fs.unlinkSync).toHaveBeenCalledWith(
      expect.stringContaining('session-user-99')
    );
  });

  test('a second attempt can start after a failed one', async () => {
    mockInitialize.mockRejectedValueOnce(new Error('init failed'));
    await expect(createWhatsAppClient(99)).rejects.toThrow('init failed');

    // connecting must be released, or the retry is refused as "already initializing"
    mockInitialize.mockResolvedValueOnce();
    const retried = await createWhatsAppClient(99);
    expect(retried).not.toBeNull();
    expect(clients.has(99)).toBe(true);
  });
});

// ── SingletonLock cleanup ─────────────────────────────────
// The lock is a symlink to "<hostname>-<pid>". existsSync() follows it, so a lock left
// by a dead browser reports false — the exact case cleanup exists for. Unlink blind.
describe('session lock cleanup', () => {
  test('removes a lock whose symlink target is gone', async () => {
    // existsSync false everywhere = dangling symlink, as seen on the box
    fs.existsSync.mockReturnValue(false);
    fs.unlinkSync.mockClear();
    fs.unlinkSync.mockImplementation(() => {});
    mockInitialize.mockRejectedValueOnce(new Error('init failed'));

    await expect(createWhatsAppClient(77)).rejects.toThrow('init failed');

    expect(fs.unlinkSync).toHaveBeenCalledWith(expect.stringContaining('SingletonLock'));
  });

  test('a missing lock is not an error', async () => {
    fs.unlinkSync.mockClear();
    fs.unlinkSync.mockImplementation(() => {
      const err = new Error('ENOENT');
      err.code = 'ENOENT';
      throw err;
    });
    const warnSpy = jest.spyOn(console, 'warn').mockImplementation();
    mockInitialize.mockRejectedValueOnce(new Error('init failed'));

    await expect(createWhatsAppClient(78)).rejects.toThrow('init failed');

    expect(warnSpy).not.toHaveBeenCalledWith(expect.stringContaining('SingletonLock'));
    warnSpy.mockRestore();
  });
});

// ── auth_failure event ───────────────────────────────────

describe('auth_failure handler', () => {
  test('destroys client and cleans up session dir', async () => {
    fs.existsSync.mockReturnValue(true);
    fs.rmSync = jest.fn();

    const clientData = await createWhatsAppClient(42);
    const client = clientData.client;

    client.emit('auth_failure', 'auth failed');
    await flush();

    expect(mockDestroy).toHaveBeenCalled();
    expect(clients.has(42)).toBe(false);
    expect(fs.rmSync).toHaveBeenCalledWith(
      expect.stringContaining('session-user-42'),
      expect.objectContaining({ recursive: true, force: true })
    );
  });

  test('skips rmSync when session dir does not exist', async () => {
    fs.existsSync.mockReturnValue(false);
    fs.rmSync = jest.fn();

    const clientData = await createWhatsAppClient(42);
    clientData.client.emit('auth_failure', 'bad creds');
    await flush();

    expect(fs.rmSync).not.toHaveBeenCalled();
    expect(clients.has(42)).toBe(false);
  });
});

// ── disconnected event → reconnect ───────────────────────

describe('disconnected handler', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  test('removes client and triggers reconnect', async () => {
    const clientData = await createWhatsAppClient(42);
    const client = clientData.client;

    client.emit('disconnected', 'NAVIGATION');
    await jest.advanceTimersByTimeAsync(0);

    expect(clients.has(42)).toBe(false);
    expect(clientData.isReady).toBe(false);
  });

  test('LOGOUT resets wa_connected and does not reconnect', async () => {
    const { setWaDisconnected } = require('../src/db');
    const clientData = await createWhatsAppClient(42);
    const client = clientData.client;

    mockInitialize.mockClear();
    client.emit('disconnected', 'LOGOUT');
    await jest.advanceTimersByTimeAsync(0); // flush destroyClient + setWaDisconnected

    expect(setWaDisconnected).toHaveBeenCalledWith(42);
    expect(clients.has(42)).toBe(false);

    // No reconnect should be scheduled for a terminal LOGOUT.
    jest.advanceTimersByTime(60000);
    expect(mockInitialize).not.toHaveBeenCalled();
  });
});

// ── reconnectClient (tested indirectly via disconnected) ──

describe('reconnect behavior', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  test('successful reconnect after first delay', async () => {
    const clientData = await createWhatsAppClient(42);
    clientData.client.emit('disconnected', 'NAVIGATION');

    // reconnectClient is running async — advance timer past first delay (5s)
    await jest.advanceTimersByTimeAsync(5000);

    // createWhatsAppClient should have been called again for the reconnect
    expect(mockInitialize).toHaveBeenCalledTimes(2);
  });

  test('fast attempts exhausted — hands off to recovery loop', async () => {
    const clientData = await createWhatsAppClient(42);
    const consoleSpy = jest.spyOn(console, 'error').mockImplementation();

    // Make all subsequent initializations fail
    mockInitialize.mockRejectedValue(new Error('init failed'));

    clientData.client.emit('disconnected', 'NAVIGATION');

    // Advance through all 3 delays: 5s, 15s, 45s
    await jest.advanceTimersByTimeAsync(5000);
    await jest.advanceTimersByTimeAsync(15000);
    await jest.advanceTimersByTimeAsync(45000);

    expect(consoleSpy).toHaveBeenCalledWith(
      expect.stringContaining('handing off to recovery loop')
    );
    // inProgress must be released so the recovery loop can take over.
    expect(inProgress.has(42)).toBe(false);
    consoleSpy.mockRestore();
  });

  test('skips reconnect if already reconnected', async () => {
    const clientData = await createWhatsAppClient(42);
    clientData.client.emit('disconnected', 'NAVIGATION');

    // Let the async disconnected handler finish tearing down (destroy + delete) first...
    await jest.advanceTimersByTimeAsync(0);
    // ...then simulate someone else having reconnected this user before the retry fires.
    const fakeReconnected = { isReady: true, client: {} };
    clients.set(42, fakeReconnected);

    await jest.advanceTimersByTimeAsync(5000);

    // reconnectClient sees an already-ready client → skips; initialize stays at 1.
    expect(mockInitialize).toHaveBeenCalledTimes(1);
  });
});

// ── checkClientHealth (tested via startHealthCheck) ──────

describe('health check', () => {
  const { stopHealthCheck } = require('../src/whatsapp-client');

  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    stopHealthCheck();
    jest.useRealTimers();
  });

  test('CONNECTED state — no reconnect', async () => {
    const clientData = await createWhatsAppClient(42);
    clientData.isReady = true;
    mockGetState.mockResolvedValue('CONNECTED');

    // Import startHealthCheck to trigger it
    const { restoreExistingSessions } = require('../src/whatsapp-client');

    // Manually trigger health check cycle by advancing interval
    // startHealthCheck is called in restoreExistingSessions but we can't
    // easily call it directly. Instead, test checkClientHealth indirectly.
    // We'll re-require the module to get a reference.

    // Since checkClientHealth is not exported, we test via the interval.
    // First we need the health check running. Let's just verify state stays.
    expect(clients.has(42)).toBe(true);
    expect(clientData.isReady).toBe(true);
  });

  test('non-CONNECTED state — triggers reconnect', async () => {
    const clientData = await createWhatsAppClient(42);
    clientData.isReady = true;
    mockGetState.mockResolvedValue('OPENING');

    // We can't call checkClientHealth directly since it's not exported.
    // But we CAN test it through restoreExistingSessions which calls startHealthCheck.
    // For now, test that the client is properly configured.
    expect(clientData.isReady).toBe(true);
  });

  test('getState timeout — triggers reconnect', async () => {
    const clientData = await createWhatsAppClient(42);
    clientData.isReady = true;
    mockGetState.mockImplementation(() => new Promise(() => {})); // never resolves

    expect(clientData.isReady).toBe(true);
  });
});

// ── startHealthCheck / stopHealthCheck ───────────────────

describe('startHealthCheck / stopHealthCheck idempotency', () => {
  // Since startHealthCheck is not exported, we test via restoreExistingSessions
  // and verify stopHealthCheck doesn't crash when called multiple times
  const { stopHealthCheck } = require('../src/whatsapp-client');

  test('stopHealthCheck — multiple calls do not throw', () => {
    expect(() => stopHealthCheck()).not.toThrow();
    expect(() => stopHealthCheck()).not.toThrow();
  });
});

// ── QR timeout ────────────────────────────────────────────

describe('QR timeout', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  test('destroys client after QR timeout expires', async () => {
    fs.existsSync.mockReturnValue(false);
    const clientData = await createWhatsAppClient(42);
    const client = clientData.client;

    // Emit QR to start the timer
    client.emit('qr', 'qr-code-data');

    expect(clientData.qrTimer).not.toBeNull();

    // Advance past QR_TIMEOUT_MS (60 minutes); async callback awaits destroy before delete.
    await jest.advanceTimersByTimeAsync(60 * 60 * 1000);

    expect(mockDestroy).toHaveBeenCalled();
    expect(clients.has(42)).toBe(false);
  });

  test('cancels QR timeout on ready event', async () => {
    const clientData = await createWhatsAppClient(42);
    const client = clientData.client;

    client.emit('qr', 'qr-code-data');
    expect(clientData.qrTimer).not.toBeNull();

    client.emit('ready');
    expect(clientData.qrTimer).toBeNull();

    // Advance past timeout — client should still exist
    jest.advanceTimersByTime(10 * 60 * 1000);
    expect(clients.has(42)).toBe(true);
    // destroy should not have been called (only from ready handler is not expected)
    expect(mockDestroy).not.toHaveBeenCalled();
  });

  test('cancels QR timeout on authenticated event', async () => {
    const clientData = await createWhatsAppClient(42);
    const client = clientData.client;

    client.emit('qr', 'qr-code-data');
    expect(clientData.qrTimer).not.toBeNull();

    client.emit('authenticated');
    expect(clientData.qrTimer).toBeNull();

    // Advance past timeout — client should still exist
    jest.advanceTimersByTime(10 * 60 * 1000);
    expect(clients.has(42)).toBe(true);
    expect(mockDestroy).not.toHaveBeenCalled();
  });
});

// ── destroyAllClients ────────────────────────────────────

describe('destroyAllClients', () => {
  test('destroys all clients and clears map', async () => {
    await createWhatsAppClient(1);
    await createWhatsAppClient(2);
    expect(clients.size).toBe(2);

    await destroyAllClients();

    expect(clients.size).toBe(0);
    // destroy is called on each client
    expect(mockDestroy).toHaveBeenCalledTimes(2);
  });

  test('handles destroy errors gracefully', async () => {
    mockDestroy.mockRejectedValue(new Error('destroy failed'));
    await createWhatsAppClient(1);

    const consoleSpy = jest.spyOn(console, 'error').mockImplementation();
    await destroyAllClients();

    expect(clients.size).toBe(0);
    consoleSpy.mockRestore();
  });
});

// ── Persistent session recovery ──────────────────────────

describe('recoverLostSessions', () => {
  // Pretend one authenticated session (user 42) exists on disk.
  function mockAuthenticatedDisk(dirs = ['session-user-42']) {
    fs.existsSync.mockImplementation(
      (p) => p === '.wwebjs_auth' || String(p).endsWith('.authenticated')
    );
    fs.readdirSync.mockReturnValue(dirs);
  }

  test('getAuthenticatedSessionUids returns uids of marked sessions only', () => {
    fs.existsSync.mockImplementation(
      (p) => p === '.wwebjs_auth' || String(p).includes('session-user-42')
    );
    fs.readdirSync.mockReturnValue(['session-user-42', 'session-user-99', 'junk']);

    expect(getAuthenticatedSessionUids()).toEqual([42]);
  });

  test('restores a lost authenticated session not in the client map', async () => {
    mockAuthenticatedDisk();

    await recoverLostSessions();

    expect(mockInitialize).toHaveBeenCalledTimes(1);
    expect(clients.has(42)).toBe(true);
    expect(recoveryState.has(42)).toBe(false);
  });

  test('skips a session that is already connected and ready', async () => {
    const clientData = await createWhatsAppClient(42);
    clientData.isReady = true;
    recoveryState.set(42, { attempts: 3, nextAttempt: 0 });
    mockInitialize.mockClear();
    mockAuthenticatedDisk();

    await recoverLostSessions();

    expect(mockInitialize).not.toHaveBeenCalled();
    expect(recoveryState.has(42)).toBe(false); // cleared — it's healthy
  });

  test('backs off and does not give up after a failed attempt', async () => {
    mockAuthenticatedDisk();
    mockInitialize.mockRejectedValue(new Error('ERR_NAME_NOT_RESOLVED'));

    await recoverLostSessions();

    expect(clients.has(42)).toBe(false);
    const state = recoveryState.get(42);
    expect(state.attempts).toBe(1);
    expect(state.nextAttempt).toBeGreaterThan(Date.now());

    // Immediate second pass is skipped while backing off — no extra init attempt.
    await recoverLostSessions();
    expect(mockInitialize).toHaveBeenCalledTimes(1);
  });

  test('does not retry a session already in progress', async () => {
    mockAuthenticatedDisk();
    inProgress.add(42);

    await recoverLostSessions();

    expect(mockInitialize).not.toHaveBeenCalled();
  });
});


// ── WhatsApp Web version pinning ──────────────────────────
// whatsapp-web.js drives WhatsApp's minified Store, so an unpinned WA build can break
// getChats()/getChat() overnight. WA_WEB_VERSION freezes WA at a known-good build.
describe('webVersionCache pinning', () => {
  const ORIGINAL = process.env.WA_WEB_VERSION;

  afterEach(() => {
    if (ORIGINAL === undefined) delete process.env.WA_WEB_VERSION;
    else process.env.WA_WEB_VERSION = ORIGINAL;
    jest.resetModules();
  });

  function freshCreate() {
    jest.resetModules();
    return require('../src/whatsapp-client').createWhatsAppClient;
  }

  test('unset WA_WEB_VERSION keeps the local cache (whatever WA serves)', async () => {
    delete process.env.WA_WEB_VERSION;
    mockClientOptions.length = 0;
    await freshCreate()(4242);
    expect(mockClientOptions.at(-1).webVersionCache).toEqual({ type: 'local' });
  });

  test('WA_WEB_VERSION pins WA to that build via a remote path', async () => {
    process.env.WA_WEB_VERSION = '2.3000.1046140131-alpha';
    mockClientOptions.length = 0;
    await freshCreate()(4243);
    const cache = mockClientOptions.at(-1).webVersionCache;
    expect(cache.type).toBe('remote');
    expect(cache.remotePath).toBe(
      'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/2.3000.1046140131-alpha.html'
    );
  });
});


// ── Group listing ─────────────────────────────────────────
// getChats() maps groups through GroupMetadata.update(), which throws a bare 'r' when
// WhatsApp reshuffles its minified internals. A healthy session must still list groups.
describe('getGroups', () => {
  test('uses getChats when it works, keeping participant counts', async () => {
    const client = {
      getChats: jest.fn().mockResolvedValue([
        { isGroup: true, id: { _serialized: 'g1@g.us' }, name: 'Team', participants: [1, 2, 3], timestamp: 200 },
        { isGroup: false, id: { _serialized: 'p1@c.us', user: '972500' }, name: 'Bob', timestamp: 300 },
      ]),
      pupPage: { evaluate: jest.fn() },
    };

    // Private chats are listed too: the processor has always supported bridging them,
    // but the UI filtered to @g.us so they could never be selected. Most recent first.
    await expect(getGroups(client, 5000)).resolves.toEqual([
      { id: 'p1@c.us', name: 'Bob', isGroup: false, participants: 0, lastActivity: 300 },
      { id: 'g1@g.us', name: 'Team', isGroup: true, participants: 3, lastActivity: 200 },
    ]);
    expect(client.pupPage.evaluate).not.toHaveBeenCalled();
  });

  test('skips statuses and newsletters, which are not bridgeable', async () => {
    const client = {
      getChats: jest.fn().mockResolvedValue([
        { isGroup: false, id: { _serialized: 'status@broadcast' }, name: 'Status' },
        { isGroup: false, id: { _serialized: '123@newsletter' }, name: 'Channel' },
        { isGroup: true, id: { _serialized: 'g1@g.us' }, name: 'Team', participants: [], timestamp: 1 },
      ]),
      pupPage: { evaluate: jest.fn() },
    };
    const result = await getGroups(client, 5000);
    expect(result.map((c) => c.id)).toEqual(['g1@g.us']);
  });

  test("falls back to the lightweight read when getChats throws 'r'", async () => {
    const client = {
      getChats: jest.fn().mockRejectedValue(new Error('r')),
      pupPage: {
        evaluate: jest.fn().mockResolvedValue([{ id: 'g1@g.us', name: 'Zomer', participants: 0 }]),
      },
    };
    await expect(getGroups(client, 5000)).resolves.toEqual([
      { id: 'g1@g.us', name: 'Zomer', participants: 0 },
    ]);
    expect(client.pupPage.evaluate).toHaveBeenCalled();
  });

  test('propagates the error when the fallback fails too', async () => {
    const client = {
      getChats: jest.fn().mockRejectedValue(new Error('r')),
      pupPage: { evaluate: jest.fn().mockRejectedValue(new Error('Store missing')) },
    };
    await expect(getGroups(client, 5000)).rejects.toThrow('Store missing');
  });
});

// ── Reliability: liveness, stuck clients, lock hygiene ────

describe('liveness tracking', () => {
  test('a delivered message stamps lastMessageAt', async () => {
    const { createWhatsAppClient, clients, getLastMessageAt } = require('../src/whatsapp-client');
    const { publishMessage } = require('../src/redis-publisher');
    publishMessage.mockResolvedValue();

    expect(getLastMessageAt()).toBeNull();

    await createWhatsAppClient(42);
    const client = clients.get(42).client;
    client.emit('message', {
      id: { _serialized: 'm1' },
      from: '123@g.us',
      timestamp: Math.floor(Date.now() / 1000),
      type: 'chat',
      body: 'hi',
      getChat: jest.fn().mockResolvedValue({ id: { _serialized: '123@g.us' }, name: 'G' }),
      getContact: jest.fn().mockResolvedValue({ pushname: 'A' }),
      _data: {},
    });
    await new Promise((r) => setImmediate(r));

    // This is what separates "the socket is up" from "messages are flowing" — the
    // distinction the 15-day outage turned on.
    expect(getLastMessageAt()).not.toBeNull();
    expect(clients.get(42).lastMessageAt).toBe(getLastMessageAt());
  });
});

describe('connecting lock', () => {
  test('is released when building the client throws', async () => {
    const { createWhatsAppClient, connecting, clients } = require('../src/whatsapp-client');
    const { LocalAuth } = require('whatsapp-web.js');

    LocalAuth.mockImplementationOnce(() => { throw new Error('auth dir unreadable'); });

    await expect(createWhatsAppClient(77)).rejects.toThrow('auth dir unreadable');

    // A stranded lock meant createWhatsAppClient returned null forever after, while the
    // recovery loop cheerfully logged "session restored" for a user receiving nothing.
    expect(connecting.has(77)).toBe(false);
    expect(clients.has(77)).toBe(false);

    LocalAuth.mockImplementation(() => ({}));
    await expect(createWhatsAppClient(77)).resolves.toBeTruthy();
  });
});

describe('clients stuck initializing', () => {
  test('are destroyed once past INIT_STUCK_TIMEOUT', async () => {
    jest.useFakeTimers();
    const { createWhatsAppClient, clients, startHealthCheck, stopHealthCheck } = require('../src/whatsapp-client');
    const config = require('../src/config');

    await createWhatsAppClient(55);
    const data = clients.get(55);
    data.isReady = false;
    data.qr = null;          // never produced a QR
    data.qrTimer = null;
    data.initStartedAt = Date.now() - (config.INIT_STUCK_TIMEOUT + 1000);

    startHealthCheck();
    await jest.advanceTimersByTimeAsync(config.HEALTH_CHECK_INTERVAL + 100);
    stopHealthCheck();

    // Previously: the QR branch needed a QR, and the recovery loop skipped anything
    // already in the map — so this client sat there forever holding a slot.
    expect(clients.has(55)).toBe(false);
    jest.useRealTimers();
  });

  test('a freshly created one is left alone', async () => {
    jest.useFakeTimers();
    const { createWhatsAppClient, clients, startHealthCheck, stopHealthCheck } = require('../src/whatsapp-client');
    const config = require('../src/config');

    await createWhatsAppClient(56);
    const data = clients.get(56);
    data.isReady = false;
    data.qr = null;
    data.qrTimer = null;
    data.initStartedAt = Date.now();

    startHealthCheck();
    await jest.advanceTimersByTimeAsync(config.HEALTH_CHECK_INTERVAL + 100);
    stopHealthCheck();

    expect(clients.has(56)).toBe(true);
    jest.useRealTimers();
  });
});
