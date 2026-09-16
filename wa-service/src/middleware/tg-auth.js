/**
 * Authentication for wa-service.
 *
 * Every route here can hijack a WhatsApp account: /qr/image hands out the QR that links
 * a new device, /connect spawns a client, /chat-pairs reads and deletes other people's
 * bridges. nginx proxies this service on `location /` with no auth of its own, so until
 * this middleware existed every one of those was reachable anonymously from the internet
 * by guessing a Telegram user id.
 *
 * Three ways to authenticate, in order of preference:
 *
 *   1. `X-Tg-Init-Data` — the signed initData blob from the Mini App. Verified against the
 *      bot token per Telegram's spec, so the user id inside cannot be forged.
 *   2. `X-Internal-Token` — shared secret for server-to-server calls from the bot, which
 *      has no initData of its own (it drives /connect during onboarding).
 *   3. `?t=` one-time QR token — for the QR page opened in a plain browser, outside
 *      Telegram. Minted by /connect, lives in Redis, expires in minutes.
 *
 * The resolved identity lands in `req.auth = { userId, via }`; `requireSelf` then refuses
 * any request whose path points at a different user.
 */
const crypto = require('crypto');

const config = require('../config');
const { redis } = require('../redis-publisher');

const QR_TOKEN_PREFIX = 'qr:token:';

/**
 * Verify Telegram Mini App initData.
 * https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
 * Returns { userId, authDate } or null when the signature is absent, wrong or stale.
 */
function verifyInitData(initData, botToken, maxAgeSec = config.INIT_DATA_MAX_AGE) {
  if (!initData || !botToken) return null;

  let params;
  try {
    params = new URLSearchParams(initData);
  } catch {
    return null;
  }

  const hash = params.get('hash');
  if (!hash || !/^[a-f0-9]{64}$/i.test(hash)) return null;
  params.delete('hash');

  // Spec: every remaining field as "key=value", sorted by key, joined with \n.
  const dataCheckString = [...params.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join('\n');

  const secret = crypto.createHmac('sha256', 'WebAppData').update(botToken).digest();
  const computed = crypto.createHmac('sha256', secret).update(dataCheckString).digest('hex');

  const a = Buffer.from(computed, 'hex');
  const b = Buffer.from(hash.toLowerCase(), 'hex');
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;

  // A valid signature is forever — Telegram never expires it. Without an age check a blob
  // captured once (shared link, proxy log) would authenticate for good.
  const authDate = parseInt(params.get('auth_date'), 10);
  if (!Number.isFinite(authDate)) return null;
  if (Math.floor(Date.now() / 1000) - authDate > maxAgeSec) return null;

  let user;
  try {
    user = JSON.parse(params.get('user') || 'null');
  } catch {
    return null;
  }
  if (!user || !Number.isFinite(user.id)) return null;

  return { userId: user.id, authDate };
}

/** Mint a one-time QR token for a user. Returns the token string. */
async function createQrToken(userId) {
  const token = crypto.randomBytes(16).toString('hex');
  await redis.setex(`${QR_TOKEN_PREFIX}${token}`, config.QR_TOKEN_TTL, String(userId));
  return token;
}

/** Resolve a QR token to its user id, or null. */
async function resolveQrToken(token) {
  if (!token || !/^[a-f0-9]{32}$/.test(token)) return null;
  try {
    const raw = await redis.get(`${QR_TOKEN_PREFIX}${token}`);
    const userId = parseInt(raw, 10);
    return Number.isFinite(userId) ? userId : null;
  } catch (err) {
    console.error('QR token lookup failed:', err.message);
    return null;
  }
}

function timingSafeStringEqual(a, b) {
  const bufA = Buffer.from(String(a));
  const bufB = Buffer.from(String(b));
  if (bufA.length !== bufB.length) return false;
  return crypto.timingSafeEqual(bufA, bufB);
}

/**
 * Populate req.auth from any of the three sources. Rejects with 401 when none succeed.
 */
async function authenticate(req, res, next) {
  const internal = req.get('X-Internal-Token');
  if (internal && config.INTERNAL_API_TOKEN && timingSafeStringEqual(internal, config.INTERNAL_API_TOKEN)) {
    // Server-to-server (bot). The caller names the user it acts for in a header: this
    // middleware runs at router level, where Express has not populated req.params for the
    // route yet, so the path is not a reliable source here.
    const claimed = parseInt(req.get('X-Internal-User-Id') || req.params.userId, 10);
    req.auth = { userId: Number.isFinite(claimed) ? claimed : null, via: 'internal' };
    return next();
  }

  const initData = req.get('X-Tg-Init-Data');
  if (initData) {
    const verified = verifyInitData(initData, config.TELEGRAM_BOT_TOKEN);
    if (verified) {
      req.auth = { userId: verified.userId, via: 'initdata' };
      return next();
    }
    return res.status(401).json({ error: 'Invalid or expired initData' });
  }

  const tokenUser = await resolveQrToken(req.query.t);
  if (tokenUser !== null) {
    req.auth = { userId: tokenUser, via: 'qr-token' };
    return next();
  }

  return res.status(401).json({ error: 'Authentication required' });
}

/**
 * Require that the authenticated identity is the user named in the path.
 *
 * This holds for the bot too: holding the shared secret says "I am the bot", not "I may
 * act for anyone", so it has to name the user it is acting for (X-Internal-User-Id).
 */
function requireSelf(req, res, next) {
  const target = parseInt(req.params.userId, 10);
  if (!Number.isFinite(target)) return res.status(400).json({ error: 'Invalid userId' });

  if (req.auth?.userId === target) return next();

  console.warn(`Forbidden: ${req.auth?.userId} (${req.auth?.via}) tried to access user ${target}`);
  return res.status(403).json({ error: 'Forbidden' });
}

module.exports = {
  authenticate,
  requireSelf,
  verifyInitData,
  createQrToken,
  resolveQrToken,
};
