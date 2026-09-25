/**
 * Resolve a WhatsApp message id to its canonical serialized form.
 *
 * whatsapp-web.js reads `msg.id._serialized`, but that property only exists while
 * WhatsApp's minifier keeps the name: after a WA release it can come back as `$1`
 * (observed 2026-09), and every message then reaches us with `id._serialized ===
 * undefined`. Messages still flowed — they just lost their real id and fell back to a
 * content hash, which breaks reply threading and revoke/edit matching.
 *
 * Chat and contact ids (Wid) are a different class. They kept `_serialized` on the
 * models whatsapp-web.js hands us, but the raw Store models lost it too (2026-09), which
 * is why `serializedWid` exists for anything read straight off the Store.
 */
function serializedMsgId(messageOrId) {
  const id = messageOrId?.id ?? messageOrId;
  if (!id || typeof id !== 'object') return null;

  if (typeof id._serialized === 'string') return id._serialized;

  // Minified alias. Guard on the type: Wid has a `$1` too, and it is not a string.
  for (const key of Object.keys(id)) {
    if (key.startsWith('$') && typeof id[key] === 'string' && id[key].includes('@')) {
      return id[key];
    }
  }

  // MsgKey's own toString() still yields the canonical string when it survives the
  // structured clone across the puppeteer boundary.
  const asString = typeof id.toString === 'function' ? id.toString() : '';
  if (asString.includes('@') && asString.includes('_')) return asString;

  // Last resort — rebuild the canonical form from the parts, which keep their names.
  const remote = id.remote?._serialized || (typeof id.remote === 'string' ? id.remote : null);
  if (remote && typeof id.id === 'string') {
    const parts = [id.fromMe ? 'true' : 'false', remote, id.id];
    const participant = id.participant?._serialized
      || (typeof id.participant === 'string' ? id.participant : null);
    if (participant) parts.push(participant);
    return parts.join('_');
  }

  return null;
}

/**
 * Resolve a chat/contact id (Wid) to `user@server`. Same minifier drift as above:
 * `_serialized` first, then a string `$`-alias, then the parts, which keep their names.
 */
function serializedWid(wid) {
  if (!wid) return null;
  if (typeof wid === 'string') return wid;
  if (typeof wid !== 'object') return null;

  if (typeof wid._serialized === 'string') return wid._serialized;

  for (const key of Object.keys(wid)) {
    if (key.startsWith('$') && typeof wid[key] === 'string' && wid[key].includes('@')) {
      return wid[key];
    }
  }

  if (typeof wid.user === 'string' && typeof wid.server === 'string') {
    return `${wid.user}@${wid.server}`;
  }

  const asString = typeof wid.toString === 'function' ? wid.toString() : '';
  return asString.includes('@') && !asString.includes('[object') ? asString : null;
}

module.exports = { serializedMsgId, serializedWid };
