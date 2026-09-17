const { S3Client, PutObjectCommand } = require('@aws-sdk/client-s3');

const s3 = new S3Client({
  region: process.env.AWS_REGION || 'us-east-1',
  endpoint: process.env.S3_ENDPOINT || undefined,
  forcePathStyle: true,
  credentials: process.env.AWS_ACCESS_KEY_ID
    ? {
        accessKeyId: process.env.AWS_ACCESS_KEY_ID,
        secretAccessKey: process.env.AWS_SECRET_ACCESS_KEY,
      }
    : undefined,
});

const config = require('./config');
const { serializedMsgId } = require('./message-id');

const S3_PUBLIC_URL = config.S3_PUBLIC_URL;

const BUCKET = config.S3_BUCKET;
const MAX_FILE_SIZE = config.MAX_FILE_SIZE;
const MAX_CONCURRENT_MEDIA = config.MAX_CONCURRENT_MEDIA;

// Bound concurrent media downloads. Each in-flight download holds the base64 payload
// (~1.33×) plus the decoded Buffer in memory; on the 3.8 GiB VPS a few parallel large
// videos on top of the ~2 GiB Chromium baseline can OOM-kill the single replica.
let activeMedia = 0;
const mediaWaiters = [];

async function acquireMediaSlot() {
  if (activeMedia < MAX_CONCURRENT_MEDIA) {
    activeMedia++;
    return;
  }
  await new Promise((resolve) => mediaWaiters.push(resolve));
  activeMedia++;
}

function releaseMediaSlot() {
  activeMedia--;
  const next = mediaWaiters.shift();
  if (next) next();
}

const ALLOWED_MIME_TYPES = new Set([
  'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/heic', 'image/heif',
  'video/mp4', 'video/quicktime', 'video/mpeg', 'video/3gpp', 'video/webm',
  // Voice notes arrive as audio/ogg; iPhone voice memos and forwarded audio show up as
  // m4a/aac/opus, which were rejected outright and silently lost.
  'audio/mpeg', 'audio/ogg', 'audio/wav', 'audio/mp4', 'audio/x-m4a', 'audio/aac',
  'audio/opus', 'audio/webm', 'audio/amr', 'audio/3gpp',
  'application/pdf', 'application/zip', 'application/rtf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/msword', 'application/vnd.ms-excel', 'application/vnd.ms-powerpoint',
  'text/plain', 'text/csv', 'text/html',
]);

/**
 * Download media without going through window.Store.
 *
 * whatsapp-web.js's Message.downloadMedia() reads window.Store.Msg and
 * window.Store.DownloadManager. WhatsApp periodically renames the modules those aliases
 * are built from, and when that happens window.Store is never populated at all — every
 * download then fails with a bare 'r' and two months of photos and voice notes arrived
 * as "не удалось получить файл". The same rename also drops message.id._serialized,
 * so the message cannot even be looked up in the page by id any more.
 *
 * Everything the download manager needs is already on message._data, so hand it those
 * fields directly and skip both the Store alias and the id lookup. mimetype is required:
 * without it the manager defaults to application/octet-stream and rejects the response
 * ("Unexpected mimetype application/octet-stream for media type image").
 */
async function downloadMediaDirect(message) {
  const page = message.client?.pupPage;
  if (!page) throw new Error('pupPage unavailable');

  const d = message._data || {};
  const meta = {
    directPath: d.directPath,
    encFilehash: d.encFilehash,
    filehash: d.filehash,
    mediaKey: d.mediaKey,
    mediaKeyTimestamp: d.mediaKeyTimestamp,
    type: d.type || message.type,
    // Stickers occasionally arrive without a MIME type; webp is the only format WA uses.
    mimetype: d.mimetype || (message.type === 'sticker' ? 'image/webp' : undefined),
    filename: d.filename || null,
    size: d.size ?? null,
  };
  if (!meta.directPath || !meta.mediaKey) {
    throw new Error('media metadata missing on message (no directPath/mediaKey)');
  }

  const data = await page.evaluate(async (m) => {
    const tryRequire = (name) => {
      try {
        return window.require(name);
      } catch {
        return null;
      }
    };

    const dm =
      window.Store?.DownloadManager ||
      tryRequire('WAWebDownloadManager')?.downloadManager;
    if (!dm?.downloadAndMaybeDecrypt) {
      throw new Error(
        `download manager unavailable (store=${typeof window.Store}, require=${typeof window.require})`
      );
    }

    const buffer = await dm.downloadAndMaybeDecrypt({
      directPath: m.directPath,
      encFilehash: m.encFilehash,
      filehash: m.filehash,
      mediaKey: m.mediaKey,
      mediaKeyTimestamp: m.mediaKeyTimestamp,
      type: m.type,
      mimetype: m.mimetype,
      signal: new AbortController().signal,
      // The real QPL logger lives behind another Store alias; the manager only calls
      // these two methods on it.
      downloadQpl: {
        addAnnotations() {
          return this;
        },
        addPoint() {
          return this;
        },
      },
    });

    return window.WWebJS.arrayBufferToBase64Async
      ? await window.WWebJS.arrayBufferToBase64Async(buffer)
      : btoa(String.fromCharCode(...new Uint8Array(buffer)));
  }, meta);

  if (!data) throw new Error('direct download returned no data');

  return {
    data,
    mimetype: meta.mimetype,
    filename: meta.filename,
    filesize: meta.size,
  };
}

/**
 * Download media from a WhatsApp message, validate, upload to S3.
 * Returns { s3Key, s3Url, mimeType, filename } or null if no media / sticker.
 */
async function handleMedia(message, userId) {
  if (!message.hasMedia || message.type === 'poll_creation') return null;

  // Cheap pre-download size gate: WhatsApp exposes the byte size on _data for most media,
  // so reject oversized files before allocating the (much larger) base64 + Buffer copies.
  const declaredSize = message._data?.size;
  if (typeof declaredSize === 'number' && declaredSize > MAX_FILE_SIZE) {
    throw new Error(`File too large (declared): ${(declaredSize / 1024 / 1024).toFixed(1)} MB`);
  }

  await acquireMediaSlot();
  try {
    let media;
    let libError = null;
    try {
      media = await message.downloadMedia();
    } catch (err) {
      libError = err;
    }

    // The library path is dead whenever WhatsApp has renamed the Store modules; fall
    // back to the download manager directly rather than dropping the attachment.
    if (!media?.data) {
      try {
        media = await downloadMediaDirect(message);
      } catch (err) {
        if (message.type === 'sticker') return null; // treat an unfetchable sticker as text
        const why = libError ? `${libError.message} / direct: ${err.message}` : err.message;
        throw new Error(`downloadMedia failed for type=${message.type}: ${why}`);
      }
    }

    // Stickers: force webp MIME if missing (whatsapp-web.js sometimes omits it)
    if (message.type === 'sticker' && !media.mimetype) {
      media.mimetype = 'image/webp';
    }

    if (!media.mimetype) {
      throw new Error(`downloadMedia returned no mimetype for type=${message.type}`);
    }

    const buffer = Buffer.from(media.data, 'base64');
    const baseMime = media.mimetype.split(';')[0].trim();

    if (!ALLOWED_MIME_TYPES.has(baseMime)) {
      throw new Error(`Unsupported MIME type: ${media.mimetype}`);
    }
    if (buffer.length > MAX_FILE_SIZE) {
      throw new Error(`File too large: ${(buffer.length / 1024 / 1024).toFixed(1)} MB`);
    }

    const ext = baseMime.split('/')[1] || 'bin';
    const s3Key = `${userId}/${Date.now()}_${safeIdPart(message)}.${ext}`;

    await s3.send(new PutObjectCommand({
      Bucket: BUCKET,
      Key: s3Key,
      Body: buffer,
      ContentType: baseMime,
    }));

    const s3Url = `${S3_PUBLIC_URL}/${BUCKET}/${s3Key}`;
    console.log(`Media uploaded to S3: ${s3Key}`);

    return {
      s3Key,
      s3Url,
      mimeType: baseMime,
      filename: media.filename || null,
    };
  } finally {
    releaseMediaSlot();
  }
}

function safeIdPart(message) {
  const id = serializedMsgId(message);
  if (id) return id;
  const { randomBytes } = require('crypto');
  return `noid_${randomBytes(6).toString('hex')}`;
}

module.exports = { handleMedia, downloadMediaDirect };
