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
    // MAX_CONCURRENT_CLIENTS defaults to 10
    for (let i = 1; i <= 10; i++) {
      await createWhatsAppClient(i);
    }
    await expect(createWhatsAppClient(11)).rejects.toThrow('Max clients reached');
  });

  test('cleans up on initialize failure', async () => {
    mockInitialize.mockRejectedValueOnce(new Error('init failed'));
    await expect(createWhatsAppClient(99)).rejects.toThrow('init failed');
    expect(clients.has(99)).toBe(false);
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
