const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL || 'postgresql://bridge:bridge@localhost:5432/bridge',
  max: 5,
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

module.exports = { pool, getChatPairs, getWaConnected, setChatPairStatus, deleteChatPair };
