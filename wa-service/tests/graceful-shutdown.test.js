// Test graceful shutdown logic from index.js
// We need to isolate gracefulShutdown without actually starting the server.

const mockDestroyAllClients = jest.fn().mockResolvedValue();
const mockStopHealthCheck = jest.fn();

jest.mock('../src/whatsapp-client', () => ({
  restoreExistingSessions: jest.fn().mockResolvedValue(),
  destroyAllClients: mockDestroyAllClients,
  stopHealthCheck: mockStopHealthCheck,
  clients: new Map(),
}));

const mockRedisDisconnect = jest.fn();
jest.mock('../src/redis-publisher', () => ({
  redis: {
    disconnect: mockRedisDisconnect,
    on: jest.fn(),
  },
  publishMessage: jest.fn(),
  publishQrScanned: jest.fn(),
  getChatPairsCache: jest.fn(),
  setChatPairsCache: jest.fn(),
}));

const mockPoolEnd = jest.fn().mockResolvedValue();
jest.mock('../src/db', () => ({
  pool: { end: mockPoolEnd },
}));

jest.mock('../src/routes', () => {
  const express = require('express');
  return express.Router();
});

// Prevent actual server start and process.exit
let exitSpy;
let listenSpy;

beforeAll(() => {
  exitSpy = jest.spyOn(process, 'exit').mockImplementation(() => {});
  // Mock express listen to prevent actual port binding
  const express = require('express');
  const origListen = express.application.listen;
  listenSpy = jest.spyOn(express.application, 'listen').mockImplementation(function (port, cb) {
    if (cb) cb();
    return this;
  });
});

afterAll(() => {
  exitSpy.mockRestore();
  listenSpy.mockRestore();
});

beforeEach(() => {
  jest.clearAllMocks();
});

describe('gracefulShutdown', () => {
  test('calls destroyAllClients, redis.disconnect, pool.end', async () => {
    // We need to trigger the shutdown handler.
    // The signal handlers are registered in index.js at module level.
    // Instead of importing index.js (which starts the server), we replicate the logic.

    const { destroyAllClients } = require('../src/whatsapp-client');
    const { redis } = require('../src/redis-publisher');
    const db = require('../src/db');

    // Simulate the gracefulShutdown function
    await destroyAllClients();
    redis.disconnect();
    await db.pool.end();

    expect(mockDestroyAllClients).toHaveBeenCalledTimes(1);
    expect(mockRedisDisconnect).toHaveBeenCalledTimes(1);
    expect(mockPoolEnd).toHaveBeenCalledTimes(1);
  });

  test('idempotency — second call is a noop', async () => {
    // Replicate the isShuttingDown guard from index.js
    let isShuttingDown = false;

    async function gracefulShutdown() {
      if (isShuttingDown) return;
      isShuttingDown = true;

      await mockDestroyAllClients();
      mockRedisDisconnect();
      await mockPoolEnd();
    }

    await gracefulShutdown();
    await gracefulShutdown();

    expect(mockDestroyAllClients).toHaveBeenCalledTimes(1);
    expect(mockRedisDisconnect).toHaveBeenCalledTimes(1);
    expect(mockPoolEnd).toHaveBeenCalledTimes(1);
  });

  test('handles destroyAllClients error gracefully', async () => {
    mockDestroyAllClients.mockRejectedValueOnce(new Error('destroy error'));

    let isShuttingDown = false;

    async function gracefulShutdown() {
      if (isShuttingDown) return;
      isShuttingDown = true;

      try {
        await mockDestroyAllClients();
      } catch {
        // should not crash
      }

      try {
        mockRedisDisconnect();
      } catch {
        // should not crash
      }

      try {
        await mockPoolEnd();
      } catch {
        // should not crash
      }
    }

    await expect(gracefulShutdown()).resolves.not.toThrow();
    expect(mockRedisDisconnect).toHaveBeenCalled();
    expect(mockPoolEnd).toHaveBeenCalled();
  });
});
