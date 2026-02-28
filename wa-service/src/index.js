require('dotenv').config();

const express = require('express');
const { restoreExistingSessions, destroyAllClients } = require('./whatsapp-client');
const { redis } = require('./redis-publisher');
const routes = require('./routes');

// ── Global error handlers ─────────────────────────────────
process.on('unhandledRejection', (reason) => {
  console.error('Unhandled rejection — exiting:', reason);
  process.exit(1);
});

process.on('uncaughtException', (error) => {
  console.error('Uncaught exception:', error);
  process.exit(1);
});

// ── Graceful shutdown ─────────────────────────────────────
let isShuttingDown = false;

async function gracefulShutdown(signal) {
  if (isShuttingDown) return;
  isShuttingDown = true;
  console.log(`${signal} received — shutting down gracefully...`);

  try {
    await destroyAllClients();
  } catch (err) {
    console.error('Error destroying WA clients:', err.message);
  }

  try {
    redis.disconnect();
    console.log('Redis disconnected');
  } catch (err) {
    console.error('Error disconnecting Redis:', err.message);
  }

  try {
    const db = require('./db');
    if (db.pool) {
      await db.pool.end();
      console.log('PostgreSQL pool closed');
    }
  } catch (err) {
    // db module may not have a pool export — that's fine
    if (err.code !== 'MODULE_NOT_FOUND') {
      console.error('Error closing PG pool:', err.message);
    }
  }

  console.log('Shutdown complete');
  process.exit(0);
}

process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));

// ── Express app ───────────────────────────────────────────
const app = express();
app.use(express.json());
app.use('/', routes);

const PORT = process.env.PORT || 3000;

async function main() {
  // Start HTTP server first so health checks pass immediately
  app.listen(PORT, () => {
    console.log(`wa-service listening on :${PORT}`);
  });

  // Then restore any persisted sessions
  await restoreExistingSessions();
}

main().catch((err) => {
  console.error('Fatal startup error:', err);
  process.exit(1);
});
