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
  'image/jpeg', 'image/png', 'image/gif', 'image/webp',
  'video/mp4', 'video/quicktime', 'video/mpeg',
  'audio/mpeg', 'audio/ogg', 'audio/wav', 'audio/mp4',
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'text/plain', 'text/csv',
]);

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
    try {
      media = await message.downloadMedia();
    } catch (err) {
      throw new Error(`downloadMedia failed: ${err.message}`);
    }

    if (!media) {
      if (message.type === 'sticker') return null; // sticker download failed — treat as text
      throw new Error(`downloadMedia returned null for type=${message.type}`);
    }

    // Stickers: force webp MIME if missing (whatsapp-web.js sometimes omits it)
    if (message.type === 'sticker' && !media.mimetype) {
      media.mimetype = 'image/webp';
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
  const id = message.id?._serialized;
  if (id) return id;
  const { randomBytes } = require('crypto');
  return `noid_${randomBytes(6).toString('hex')}`;
}

module.exports = { handleMedia };
