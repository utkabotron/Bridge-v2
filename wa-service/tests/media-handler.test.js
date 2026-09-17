// S3 is the only external dependency of the module under test.
const mockSend = jest.fn().mockResolvedValue({});
jest.mock('@aws-sdk/client-s3', () => ({
  S3Client: jest.fn().mockImplementation(() => ({ send: mockSend })),
  PutObjectCommand: jest.fn().mockImplementation((input) => ({ input })),
}));

const { handleMedia, downloadMediaDirect } = require('../src/media-handler');

const JPEG_META = {
  directPath: '/v/t62.7118-24/abc',
  encFilehash: 'enc',
  filehash: 'hash',
  mediaKey: 'key',
  mediaKeyTimestamp: 1789391193,
  type: 'image',
  mimetype: 'image/jpeg',
  size: 1234,
};

function makeMessage({ data = JPEG_META, downloadMedia, evaluate } = {}) {
  return {
    hasMedia: true,
    type: data.type,
    // whatsapp-web.js keeps the id as a MsgKey; after a WA rename it has no _serialized.
    id: { fromMe: false, remote: '123@g.us', id: 'ABC', $1: 'false_123@g.us_ABC' },
    _data: data,
    downloadMedia: downloadMedia || jest.fn().mockRejectedValue(new Error('r')),
    client: { pupPage: { evaluate: evaluate || jest.fn().mockResolvedValue('AAAA') } },
  };
}

beforeEach(() => jest.clearAllMocks());

describe('downloadMediaDirect', () => {
  test('passes the mimetype through — the download manager rejects the response without it', async () => {
    const evaluate = jest.fn().mockResolvedValue('AAAA');
    const media = await downloadMediaDirect(makeMessage({ evaluate }));

    const [, meta] = evaluate.mock.calls[0];
    expect(meta.mimetype).toBe('image/jpeg');
    expect(meta.directPath).toBe(JPEG_META.directPath);
    expect(meta.mediaKey).toBe(JPEG_META.mediaKey);
    expect(media).toMatchObject({ data: 'AAAA', mimetype: 'image/jpeg' });
  });

  test('defaults a mimetype-less sticker to webp', async () => {
    const evaluate = jest.fn().mockResolvedValue('AAAA');
    const msg = makeMessage({
      data: { ...JPEG_META, type: 'sticker', mimetype: undefined },
      evaluate,
    });
    msg.type = 'sticker';
    await downloadMediaDirect(msg);

    expect(evaluate.mock.calls[0][1].mimetype).toBe('image/webp');
  });

  test('fails fast when the message carries no media metadata', async () => {
    await expect(downloadMediaDirect(makeMessage({ data: { type: 'image' } })))
      .rejects.toThrow(/metadata missing/);
  });
});

describe('handleMedia', () => {
  test("falls back to the direct download when the library path throws WhatsApp's bare 'r'", async () => {
    const msg = makeMessage();
    const result = await handleMedia(msg, 42);

    expect(msg.downloadMedia).toHaveBeenCalled();
    expect(msg.client.pupPage.evaluate).toHaveBeenCalled();
    expect(result.mimeType).toBe('image/jpeg');
    expect(result.s3Key).toContain('42/');
    expect(mockSend).toHaveBeenCalled();
  });

  test('uses the library result when it works and never touches the page', async () => {
    const msg = makeMessage({
      downloadMedia: jest.fn().mockResolvedValue({ data: 'BBBB', mimetype: 'image/png', filename: 'x.png' }),
    });
    const result = await handleMedia(msg, 7);

    expect(msg.client.pupPage.evaluate).not.toHaveBeenCalled();
    expect(result.mimeType).toBe('image/png');
    expect(result.filename).toBe('x.png');
  });

  test('reports both failures when the fallback fails too', async () => {
    const msg = makeMessage({ evaluate: jest.fn().mockRejectedValue(new Error('download manager unavailable')) });

    await expect(handleMedia(msg, 1)).rejects.toThrow(/r \/ direct: download manager unavailable/);
  });

  test('an unfetchable sticker is dropped rather than failing the message', async () => {
    const msg = makeMessage({
      data: { ...JPEG_META, type: 'sticker' },
      evaluate: jest.fn().mockRejectedValue(new Error('nope')),
    });
    msg.type = 'sticker';

    await expect(handleMedia(msg, 1)).resolves.toBeNull();
  });

  test('rejects oversized media before downloading anything', async () => {
    const msg = makeMessage({ data: { ...JPEG_META, size: 60 * 1024 * 1024 } });

    await expect(handleMedia(msg, 1)).rejects.toThrow(/too large \(declared\)/);
    expect(msg.downloadMedia).not.toHaveBeenCalled();
  });
});
