/**
 * Static checks on the Mini App bundle.
 *
 * There is no DOM in this suite and pulling in jsdom for a few assertions would add a
 * dependency to ship and pin. These checks instead catch the failures that are invisible
 * until someone opens the app on a phone: an id the script reaches for that the markup
 * does not define, a screen with no handler, unescaped interpolation of server data.
 */
const fs = require('fs');
const path = require('path');

const PUBLIC = path.join(__dirname, '..', 'public');
const html = fs.readFileSync(path.join(PUBLIC, 'miniapp.html'), 'utf8');
const js = fs.readFileSync(path.join(PUBLIC, 'assets', 'miniapp.js'), 'utf8');
const css = fs.readFileSync(path.join(PUBLIC, 'assets', 'miniapp.css'), 'utf8');

const idsIn = (source) => new Set([...source.matchAll(/id="([\w-]+)"/g)].map((m) => m[1]));

describe('markup and script agree', () => {
  test('every getElementById target exists in the markup', () => {
    const declared = idsIn(html);
    // Ids the script creates at runtime inside rendered HTML.
    const runtime = new Set(['retry-btn', 'wa-retry', 'home-reconnect', 'qr']);

    const referenced = [...js.matchAll(/getElementById\('([\w-]+)'\)/g)].map((m) => m[1]);
    const missing = referenced.filter((id) => !declared.has(id) && !runtime.has(id));

    expect(missing).toEqual([]);
  });

  test('every screen in the markup has a handler, and vice versa', () => {
    const screens = [...html.matchAll(/id="screen-([\w-]+)"/g)].map((m) => m[1]).sort();

    const block = js.slice(js.indexOf('const SCREENS = {'), js.indexOf('/* ── Rendering helpers'));
    const handlers = [...block.matchAll(/^ {2}(?:async )?(?:\['([\w-]+)'\]|(\w+))\(\)/gm)]
      .map((m) => m[1] || m[2])
      .sort();

    // A screen without a handler renders blank; a handler without a screen is dead code.
    expect(handlers).toEqual(screens);
  });

  test('classes the script emits are defined in the stylesheet', () => {
    const used = new Set([...js.matchAll(/class="([^"$]+)"/g)]
      .flatMap((m) => m[1].split(/\s+/))
      .filter(Boolean));

    const defined = new Set([...css.matchAll(/\.([\w-]+)/g)].map((m) => m[1]));
    const missing = [...used].filter((c) => !defined.has(c));

    expect(missing).toEqual([]);
  });
});

describe('server data is escaped', () => {
  test('chat and group names always go through esc()', () => {
    // Names come from WhatsApp and Telegram — untrusted text rendered via innerHTML.
    const fields = ['wa_chat_name', 'tg_chat_title', 'chat.name', 'group.title', 'pair.target_language'];

    for (const field of fields) {
      const raw = new RegExp(`\\$\\{\\s*(?:esc\\()?${field.replace('.', '\\.')}`, 'g');
      for (const match of js.matchAll(raw)) {
        expect(match[0]).toContain('esc(');
      }
    }
  });

  test('esc covers the characters that break out of markup', () => {
    const body = js.slice(js.indexOf('function esc('), js.indexOf('function initial('));
    for (const entity of ['&amp;', '&lt;', '&gt;', '&quot;', '&#39;']) {
      expect(body).toContain(entity);
    }
  });
});

describe('Telegram integration', () => {
  test('Back and Main are each registered exactly once', () => {
    // Registering per screen without offClick was the old bug: one tap fired every
    // handler ever added, and a second MainButton registration sent the payload twice.
    expect(js.match(/BackButton\.onClick/g)).toHaveLength(1);
    expect(js.match(/MainButton\.onClick/g)).toHaveLength(1);
  });

  test('no dependence on sendData', () => {
    // Telegram delivers sendData only from reply-keyboard buttons; this app opens from an
    // inline button, so the old final step silently did nothing. Pair creation is HTTP.
    expect(js).not.toContain('sendData');
    expect(js).toContain("api('/chat-pairs'");
  });

  test('polling is generation-guarded and bounded', () => {
    const poll = js.slice(js.indexOf('function poll('), js.indexOf('function handleAuthError'));
    expect(poll).toContain('mine !== generation');
    expect(poll).toContain('onGiveUp');
    expect(poll).toContain('document.hidden');
  });

  test('401 and 403 are distinguished rather than merged', () => {
    const handler = js.slice(js.indexOf('function handleAuthError'), js.indexOf('/* ── Navigation'));
    expect(handler).toContain('unknown user');
    expect(handler).toContain("'denied'");
    expect(handler).toContain("'expired'");
  });
});

describe('theme', () => {
  test('colours come from the host theme, not hardcoded hex', () => {
    // Two exceptions by design: the bank colours the app owns, and the white QR backing,
    // which must stay light in either theme to remain scannable.
    const allowed = new Set(['#25d366', '#2aabee', '#1fb85a', '#3aa6db', '#fff', '#ffffff',
                             '#f4f4f5', '#0f0f0f', '#8b8b90', '#e5484d', '#17212b',
                             '#232e3c', '#f5f5f5', '#7d8b99']);
    const hexes = [...css.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((m) => m[0].toLowerCase());
    const unexpected = [...new Set(hexes)].filter((h) => !allowed.has(h));

    expect(unexpected).toEqual([]);
  });

  test('viewport height comes from Telegram, not 100vh alone', () => {
    // In the Telegram WebView 100vh overshoots the visible area and adds a phantom scroll.
    expect(css).toContain('--tg-viewport-stable-height');
    expect(css).toContain('env(safe-area-inset-bottom)');
  });
});
