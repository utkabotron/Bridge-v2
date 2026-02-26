require('dotenv').config();

const express = require('express');
const { createClient } = require('./whatsapp-client');
const { restoreExistingSessions } = require('./whatsapp-client');
const routes = require('./routes');

// ── Global error handlers ─────────────────────────────────
process.on('unhandledRejection', (reason) => {
  console.error('Unhandled rejection:', reason);
});

process.on('uncaughtException', (error) => {
  console.error('Uncaught exception:', error);
  process.exit(1);
});

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
