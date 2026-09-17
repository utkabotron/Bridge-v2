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

const PAIR_COLUMNS = `
  cp.id, cp.wa_chat_id, cp.wa_chat_name, cp.tg_chat_id, cp.tg_chat_title,
  cp.status, cp.created_at,
  -- NULL means "follow the account setting"; the app shows the effective value and
  -- whether it is inherited, so "Inherit" is a real, selectable choice.
  COALESCE(cp.target_language, u.target_language) AS target_language,
  (cp.target_language IS NULL) AS language_inherited,
  COALESCE(css.enabled, true) AS summary_enabled`;

const PAIR_FROM = `
  FROM chat_pairs cp
  JOIN users u ON u.id = cp.user_id
  LEFT JOIN chat_summary_schedule css ON css.chat_pair_id = cp.id`;

async function getChatPairs(tgUserId) {
  const { rows } = await pool.query(
    `SELECT ${PAIR_COLUMNS} ${PAIR_FROM}
     WHERE u.tg_user_id = $1
     ORDER BY cp.created_at DESC`,
    [tgUserId]
  );
  return rows;
}

/** One pair, only if the caller owns it. */
async function getChatPairOwned(pairId, tgUserId) {
  const { rows } = await pool.query(
    `SELECT ${PAIR_COLUMNS} ${PAIR_FROM}
     WHERE cp.id = $1 AND u.tg_user_id = $2`,
    [pairId, tgUserId]
  );
  return rows[0] || null;
}

/**
 * Create (or re-activate) a bridge. Mirrors bot/src/db.py add_chat_pair, including the
 * conflict target — the same (user, WA chat, TG chat) triple may be re-linked after being
 * deleted, and re-linking should revive it rather than fail.
 */
async function addChatPair(tgUserId, waChatId, waChatName, tgChatId, tgChatTitle) {
  const client = await pool.connect();
  try {
    await client.query('BEGIN');
    const { rows } = await client.query(
      `INSERT INTO chat_pairs (user_id, wa_chat_id, wa_chat_name, tg_chat_id, tg_chat_title, status)
       VALUES ((SELECT id FROM users WHERE tg_user_id = $1), $2, $3, $4, $5, 'active')
       ON CONFLICT (user_id, wa_chat_id, tg_chat_id) DO UPDATE
         SET status = 'active',
             wa_chat_name = EXCLUDED.wa_chat_name,
             tg_chat_title = EXCLUDED.tg_chat_title
       RETURNING id`,
      [tgUserId, waChatId, waChatName, tgChatId, tgChatTitle]
    );
    await client.query(
      `UPDATE onboarding_sessions SET state = 'done'
       WHERE user_id = (SELECT id FROM users WHERE tg_user_id = $1)`,
      [tgUserId]
    );
    await client.query('COMMIT');
    return getChatPairOwned(rows[0].id, tgUserId);
  } catch (err) {
    await client.query('ROLLBACK').catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}

/** Telegram groups this user administers and may therefore link. */
async function getTgGroups(tgUserId) {
  const { rows } = await pool.query(
    `SELECT tg_chat_id AS chat_id, title
     FROM tg_groups
     WHERE tg_user_id = $1
     ORDER BY updated_at DESC`,
    [tgUserId]
  );
  return rows;
}

/** True if this user administers that group — guards linking someone else's group. */
async function ownsTgGroup(tgUserId, tgChatId) {
  const { rowCount } = await pool.query(
    'SELECT 1 FROM tg_groups WHERE tg_user_id = $1 AND tg_chat_id = $2',
    [tgUserId, tgChatId]
  );
  return rowCount > 0;
}

/** Set a bridge's language, or NULL to follow the account setting. Owner-scoped. */
async function setPairLanguage(pairId, language, tgUserId) {
  const { rowCount } = await pool.query(
    `UPDATE chat_pairs SET target_language = $1
     WHERE id = $2 AND user_id = (SELECT id FROM users WHERE tg_user_id = $3)`,
    [language, pairId, tgUserId]
  );
  return rowCount > 0;
}

/**
 * Turn the daily summary on or off for a bridge. Creates the schedule row when absent so
 * the preference sticks before the scheduling flow has computed an hour for this chat.
 */
async function setPairSummary(pairId, enabled, tgUserId) {
  const { rowCount } = await pool.query(
    `INSERT INTO chat_summary_schedule (chat_pair_id, enabled)
     SELECT cp.id, $1
     FROM chat_pairs cp
     JOIN users u ON u.id = cp.user_id
     WHERE cp.id = $2 AND u.tg_user_id = $3
     ON CONFLICT (chat_pair_id) DO UPDATE SET enabled = EXCLUDED.enabled`,
    [enabled, pairId, tgUserId]
  );
  return rowCount > 0;
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

// Both mutations are scoped to the owner. Keyed by id alone (as they were), walking
// pairId 1..N from the open internet paused or permanently deleted every user's bridges.
// The bot has always scoped its equivalent (bot/src/db.py set_chat_pair_status_owned).
async function setChatPairStatus(pairId, status, tgUserId) {
  const { rowCount } = await pool.query(
    `UPDATE chat_pairs SET status = $1
     WHERE id = $2 AND user_id = (SELECT id FROM users WHERE tg_user_id = $3)`,
    [status, pairId, tgUserId]
  );
  return rowCount > 0;
}

async function deleteChatPair(pairId, tgUserId) {
  const { rowCount } = await pool.query(
    `DELETE FROM chat_pairs
     WHERE id = $1 AND user_id = (SELECT id FROM users WHERE tg_user_id = $2)`,
    [pairId, tgUserId]
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

module.exports = {
  pool,
  getChatPairs,
  getChatPairOwned,
  addChatPair,
  getTgGroups,
  ownsTgGroup,
  setPairLanguage,
  setPairSummary,
  getWaConnected,
  setWaConnected,
  setChatPairStatus,
  deleteChatPair,
  setWaDisconnected,
  userExists,
};
