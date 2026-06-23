const { Pool } = require('pg');
const { DATABASE_URL, DB_POOL_MAX, DB_STATEMENT_TIMEOUT } = require('./config');

const pool = new Pool({
  connectionString: DATABASE_URL,
  max: DB_POOL_MAX,
  statement_timeout: DB_STATEMENT_TIMEOUT,
});

pool.on('error', (err) => {
  console.error('Unexpected PG pool error:', err);
});

async function getChatPairs(tgUserId) {
  const { rows } = await pool.query(
    `SELECT cp.id, cp.wa_chat_id, cp.wa_chat_name, cp.tg_chat_id, cp.tg_chat_title, cp.status
     FROM chat_pairs cp
     JOIN users u ON u.id = cp.user_id
     WHERE u.tg_user_id = $1
     ORDER BY cp.created_at DESC`,
    [tgUserId]
  );
  return rows;
}

async function getWaConnected(tgUserId) {
  const { rows } = await pool.query(
    'SELECT wa_connected FROM users WHERE tg_user_id = $1',
    [tgUserId]
  );
  return rows.length > 0 ? rows[0].wa_connected : false;
}

async function setWaConnected(tgUserId, connected) {
  const { rowCount } = await pool.query(
    'UPDATE users SET wa_connected = $1 WHERE tg_user_id = $2',
    [connected, tgUserId]
  );
  return rowCount > 0;
}

async function setChatPairStatus(pairId, status) {
  const { rowCount } = await pool.query(
    'UPDATE chat_pairs SET status = $1 WHERE id = $2',
    [status, pairId]
  );
  return rowCount > 0;
}

async function deleteChatPair(pairId) {
  const { rowCount } = await pool.query(
    'DELETE FROM chat_pairs WHERE id = $1',
    [pairId]
  );
  return rowCount > 0;
}

async function setWaDisconnected(tgUserId) {
  await pool.query(
    'UPDATE users SET wa_connected = false WHERE tg_user_id = $1',
    [tgUserId]
  );
}

// Whitelist gate for client creation: only known/active users may spin up a WhatsApp
// (Chromium) client. Without this, any numeric userId hitting /connect or /qr/image
// spawns a ~300 MB browser — a trivial memory-DoS on the 3.8 GiB VPS.
async function userExists(tgUserId) {
  const { rows } = await pool.query(
    'SELECT 1 FROM users WHERE tg_user_id = $1 AND is_active = true',
    [tgUserId]
  );
  return rows.length > 0;
}

module.exports = { pool, getChatPairs, getWaConnected, setWaConnected, setChatPairStatus, deleteChatPair, setWaDisconnected, userExists };
