const { serializedMsgId } = require('../src/message-id');

const SERIALIZED = 'false_972528976286-1567850034@g.us_3BA5EB7EB2C6F707A6E5_78576594460761@lid';

describe('serializedMsgId', () => {
  test('prefers _serialized when whatsapp-web.js still gets it', () => {
    expect(serializedMsgId({ id: { _serialized: SERIALIZED } })).toBe(SERIALIZED);
  });

  test('reads the minified alias WhatsApp renamed _serialized to', () => {
    expect(serializedMsgId({ id: { fromMe: false, remote: 'x@g.us', id: 'A', $1: SERIALIZED } }))
      .toBe(SERIALIZED);
  });

  test('ignores a non-string $1 — Wid has one and it is not an id', () => {
    const id = { server: 'g.us', user: '123', $1: {}, remote: '123@g.us', id: 'A', fromMe: true };
    expect(serializedMsgId({ id })).toBe('true_123@g.us_A');
  });

  test('rebuilds the id from its parts when every alias is gone', () => {
    const id = {
      fromMe: false,
      remote: { _serialized: '972@g.us' },
      id: 'ABC',
      participant: { _serialized: '78@lid' },
    };
    expect(serializedMsgId({ id })).toBe('false_972@g.us_ABC_78@lid');
  });

  test('accepts a bare id object as well as a message', () => {
    expect(serializedMsgId({ _serialized: SERIALIZED })).toBe(SERIALIZED);
  });

  test('returns null when there is nothing usable — callers fall back to a content hash', () => {
    expect(serializedMsgId(undefined)).toBeNull();
    expect(serializedMsgId({ id: {} })).toBeNull();
    expect(serializedMsgId({ id: 'not-an-object' })).toBeNull();
  });
});
