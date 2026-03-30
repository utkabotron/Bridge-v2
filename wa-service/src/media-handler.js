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

const S3_PUBLIC_URL = process.env.S3_PUBLIC_URL || 'http://localhost:9000';

const BUCKET = process.env.S3_BUCKET || 'bridge-v2-media';
const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB

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
  const s3Key = `${userId}/${Date.now()}_${message.id._serialized}.${ext}`;

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
}

module.exports = { handleMedia };
