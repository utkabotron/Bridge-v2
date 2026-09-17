/* Bridge — Mini App
 *
 * One state machine, one navigation stack, one registration each for Telegram's Back and
 * Main buttons. The previous version registered a fresh BackButton handler on entering
 * every screen and never removed the old ones, so a single tap ran all of them; the same
 * happened to MainButton, where a second registration meant one tap sent the payload
 * twice.
 *
 * Polling is generational: every screen change invalidates in-flight requests. Before,
 * three independent timers ran forever with no abort, and a response that arrived after
 * the user had navigated away could drag them back to a screen they had left.
 */

const tg = window.Telegram.WebApp;

/* ── Copy ──────────────────────────────────────────────────────────────────── */

const T = {
  home_empty_title: 'No bridges yet',
  home_empty_text: 'Link a WhatsApp chat to a Telegram group and its messages will arrive here, translated.',
  wa_connected: 'WhatsApp connected',
  wa_disconnected: 'WhatsApp disconnected',
  reconnect: 'Reconnect',
  new_bridge: 'New bridge',

  connect_step_1: 'Open WhatsApp on your phone',
  connect_step_2: 'Go to Settings → Linked devices',
  connect_step_3: 'Tap "Link a device" and scan this code',
  connect_starting: 'Starting WhatsApp…',
  connect_copied: 'Link copied',

  wa_groups: 'Groups',
  wa_direct: 'Direct chats',
  wa_empty_title: 'No chats found',
  wa_empty_text: 'WhatsApp has not finished syncing, or this account has no chats yet.',
  wa_members: (n) => `${n} member${n === 1 ? '' : 's'}`,
  wa_direct_label: 'Direct chat',

  tg_empty_title: 'No groups yet',
  tg_empty_text: 'Add the bot to a Telegram group. Already added? Send any message in that group and it will show up here.',
  tg_add: 'Add bot to a group',
  tg_create: 'Create bridge',

  done_open: 'Open bridges',

  pair_settings: 'Settings',
  pair_pause: 'Pause',
  pair_resume: 'Resume',
  pair_language: 'Translate into',
  pair_language_title: 'Translation language',
  pair_inherit: 'Account default',
  pair_summary: 'Daily summary',
  pair_delete: 'Delete bridge',
  pair_delete_ask: 'Delete this bridge? Messages will stop being forwarded.',
  pair_created: (d) => `Created ${d}`,
  on: 'On',
  off: 'Off',
  paused: 'Paused',
  active: 'Active',

  expired_action: 'Close',
  error_title: 'Something went wrong',
  retry: 'Try again',
};

const LANGUAGES = [
  { code: null, label: T.pair_inherit },
  { code: 'Russian', label: 'Русский' },
  { code: 'English', label: 'English' },
  { code: 'Hebrew', label: 'עברית' },
  { code: 'Ukrainian', label: 'Українська' },
  { code: 'Spanish', label: 'Español' },
];

/* ── Icons ─────────────────────────────────────────────────────────────────── */

const icon = (d, extra = '') =>
  `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" ${extra}>${d}</svg>`;

const ICONS = {
  pause: icon('<rect x="7" y="5" width="3.5" height="14" rx="1"/><rect x="13.5" y="5" width="3.5" height="14" rx="1"/>'),
  play: icon('<path d="M8 5.5v13l10-6.5z"/>'),
  globe: icon('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18 15 15 0 0 1 0-18"/>'),
  bell: icon('<path d="M18 9a6 6 0 1 0-12 0c0 5-2 6-2 6h16s-2-1-2-6"/><path d="M13.7 20a2 2 0 0 1-3.4 0"/>'),
  trash: icon('<path d="M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3"/>'),
  chevron: icon('<path d="M9 6l6 6-6 6"/>'),
  check: icon('<path d="M20 6L9 17l-5-5"/>'),
  plus: icon('<path d="M12 5v14M5 12h14"/>'),
};

/* The two banks and the span — the app's only illustration. */
const BRIDGE_ART = `
  <svg class="state-art" viewBox="0 0 96 24" fill="none" aria-hidden="true">
    <circle cx="8" cy="12" r="5" fill="var(--wa)"/>
    <circle cx="88" cy="12" r="5" fill="var(--tg)"/>
    <path d="M14 12h68" stroke="url(#span)" stroke-width="2" stroke-linecap="round"
          stroke-dasharray="4 5"/>
    <defs><linearGradient id="span" x1="14" y1="12" x2="82" y2="12" gradientUnits="userSpaceOnUse">
      <stop stop-color="var(--wa)"/><stop offset="1" stop-color="var(--tg)"/>
    </linearGradient></defs>
  </svg>`;

/* ── Utilities ─────────────────────────────────────────────────────────────── */

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * Identity comparison for anything that crosses the API or the DOM.
 *
 * Postgres bigint columns (a pair id, a Telegram chat id) arrive as JSON strings, and a
 * dataset attribute is always a string — but a WhatsApp chat id is a real string and a
 * literal in code may be a number. Comparing without normalising silently fails.
 */
function sameId(a, b) {
  return String(a) === String(b);
}

function initial(name) {
  const ch = String(name || '').trim().charAt(0);
  return ch ? esc(ch) : '·';
}

function alert_(message) {
  try { tg.showAlert(message); } catch { window.alert(message); }
}

function confirm_(message, cb) {
  try { tg.showConfirm(message, cb); } catch { cb(window.confirm(message)); }
}

function haptic(kind) {
  try {
    if (kind === 'select') tg.HapticFeedback.selectionChanged();
    else tg.HapticFeedback.notificationOccurred(kind);
  } catch { /* older clients have no haptics */ }
}

class HttpError extends Error {
  constructor(status, body) {
    super(body?.error || `HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

/** Every call carries the signed initData; the server derives identity from it. */
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      ...(options.headers || {}),
      'X-Tg-Init-Data': tg.initData || '',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });

  if (res.ok) return res.status === 204 ? null : res.json();

  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  throw new HttpError(res.status, body);
}

/* ── State ─────────────────────────────────────────────────────────────────── */

const state = {
  screen: null,
  stack: [],
  userId: null,
  pairs: [],
  waLive: false,
  waChats: [],
  waFilter: '',
  tgGroups: [],
  selectedWa: null,
  selectedTg: null,
  openPair: null,
  busy: false,
};

/* Invalidated on every navigation: a response from a screen the user has left is dropped
 * instead of acting on it. */
let generation = 0;
let pollTimer = null;

function stopPolling() {
  generation += 1;
  clearTimeout(pollTimer);
  pollTimer = null;
}

/**
 * Poll until `tick` returns true (done) or the screen changes.
 * Gives up after `attempts` and calls `onGiveUp`, rather than retrying forever.
 */
function poll(tick, { interval = 2000, attempts = 90, onGiveUp } = {}) {
  const mine = generation;
  let left = attempts;

  async function run() {
    if (mine !== generation) return;
    if (document.hidden) { pollTimer = setTimeout(run, interval); return; }

    try {
      if (await tick()) return;
    } catch (err) {
      if (mine !== generation) return;
      if (err instanceof HttpError && (err.status === 401 || err.status === 403)) {
        handleAuthError(err);
        return;
      }
    }

    if (mine !== generation) return;
    if (--left <= 0) { if (onGiveUp) onGiveUp(); return; }
    pollTimer = setTimeout(run, interval);
  }

  run();
}

function handleAuthError(err) {
  stopPolling();
  // initData is signed but not eternal; the server also refuses users who are not
  // whitelisted. Those are different messages — the old code showed one for both.
  const unknown = err.status === 403 && /unknown user/i.test(err.body?.error || '');
  render(unknown ? 'denied' : 'expired');
}

/* ── Navigation ────────────────────────────────────────────────────────────── */

function navigate(screen, { replace = false } = {}) {
  if (!replace && state.screen && state.screen !== screen) state.stack.push(state.screen);
  render(screen);
}

function goBack() {
  const previous = state.stack.pop();
  render(previous || 'home');
}

function render(screen) {
  stopPolling();
  state.screen = screen;
  state.busy = false;

  document.querySelectorAll('.screen').forEach((el) => el.classList.remove('active'));
  const el = document.getElementById(`screen-${screen}`);
  if (el) el.classList.add('active');

  tg.MainButton.hide();
  tg.MainButton.hideProgress();
  tg.MainButton.enable();
  if (state.stack.length) tg.BackButton.show(); else tg.BackButton.hide();

  // The wizard holds unsaved progress; the list screens do not.
  if (['connect', 'pick-wa', 'pick-tg'].includes(screen)) tg.enableClosingConfirmation();
  else tg.disableClosingConfirmation();

  (SCREENS[screen] || SCREENS.home)();
}

/* MainButton has exactly one handler for the life of the app; screens declare intent. */
let mainAction = null;

function setMain(text, action, { enabled = true } = {}) {
  mainAction = action;
  tg.MainButton.setText(text);
  if (enabled) tg.MainButton.enable(); else tg.MainButton.disable();
  tg.MainButton.show();
}

/* ── Screens ───────────────────────────────────────────────────────────────── */

const SCREENS = {
  loading() {
    document.getElementById('loading-body').innerHTML = '<div class="spinner"></div>';
  },

  async home() {
    const body = document.getElementById('home-body');
    body.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';

    let data;
    try {
      data = await api(`/chat-pairs/${state.userId}`);
    } catch (err) {
      if (err instanceof HttpError && (err.status === 401 || err.status === 403)) return handleAuthError(err);
      return renderError(body, () => render('home'));
    }
    if (state.screen !== 'home') return;

    state.pairs = data.pairs || [];
    state.waLive = !!data.wa_connected;

    document.getElementById('home-status').outerHTML = statusLine(state.waLive);

    if (!state.pairs.length) {
      body.innerHTML = `
        <div class="state">
          ${BRIDGE_ART}
          <div class="state-title">${T.home_empty_title}</div>
          <div class="state-text">${T.home_empty_text}</div>
        </div>`;
    } else {
      body.innerHTML = `<div class="list">${state.pairs.map(bridgeCard).join('')}</div>`;
      body.querySelectorAll('[data-pair]').forEach((card) => {
        card.onclick = () => openPairSheet(card.dataset.pair);
      });
    }

    setMain(T.new_bridge, () => {
      state.selectedWa = null;
      state.selectedTg = null;
      // Straight to picking a chat when WhatsApp is already up — the old flow always
      // flashed the QR screen first while it waited to find that out.
      navigate(state.waLive ? 'pick-wa' : 'connect');
    });
  },

  async connect() {
    setProgress(1);
    const body = document.getElementById('connect-body');
    body.innerHTML = qrShell(T.connect_starting);

    try {
      await api(`/connect/${state.userId}`, { method: 'POST' });
    } catch (err) {
      if (err instanceof HttpError && (err.status === 401 || err.status === 403)) return handleAuthError(err);
      return renderError(body, () => render('connect'));
    }
    if (state.screen !== 'connect') return;

    let shown = false;
    poll(async () => {
      const status = await api(`/status/${state.userId}`);
      if (state.screen !== 'connect') return true;

      if (status.isReady) {
        haptic('success');
        state.waLive = true;
        navigate('pick-wa', { replace: true });
        return true;
      }

      if (status.hasQR && !shown) {
        // Fetched through api() so it carries initData: the old <img> could only send a
        // token in the query string, which expired after fifteen minutes and then failed
        // silently forever.
        const blob = await fetch(`/qr/image/${state.userId}`, {
          headers: { 'X-Tg-Init-Data': tg.initData || '' },
        }).then((r) => (r.ok && r.headers.get('content-type')?.startsWith('image') ? r.blob() : null));

        if (blob && state.screen === 'connect') {
          shown = true;
          body.querySelector('.qr-wrap').innerHTML =
            `<img alt="QR code" src="${URL.createObjectURL(blob)}">`;
          body.querySelector('[data-qr-hint]').textContent = T.connect_step_3;
        }
      }
      return false;
    }, { interval: 2000, attempts: 600, onGiveUp: () => renderError(body, () => render('connect')) });

    document.getElementById('connect-copy').onclick = async () => {
      try {
        const { qrPageUrl } = await api(`/connect/${state.userId}`, { method: 'POST' });
        const url = location.origin + qrPageUrl;
        await navigator.clipboard.writeText(url);
        alert_(`${T.connect_copied}\n\n${url}`);
      } catch {
        alert_(T.error_title);
      }
    };
  },

  async ['pick-wa']() {
    setProgress(2);
    const body = document.getElementById('wa-body');
    body.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';

    let status;
    try {
      status = await api(`/status/${state.userId}`);
    } catch (err) {
      if (err instanceof HttpError && (err.status === 401 || err.status === 403)) return handleAuthError(err);
      return renderError(body, () => render('pick-wa'));
    }
    if (state.screen !== 'pick-wa') return;

    if (!status.isReady) return navigate('connect', { replace: true });

    state.waChats = status.groups || [];
    state.waFilter = '';
    drawWaChats(status.groupsError);
  },

  async ['pick-tg']() {
    setProgress(3);
    const body = document.getElementById('tg-body');
    document.getElementById('tg-from').innerHTML = fromLine(state.selectedWa);
    body.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';

    const load = async () => {
      const data = await api(`/tg-groups/${state.userId}`);
      if (state.screen !== 'pick-tg') return true;

      const groups = data.groups || [];
      const changed = groups.length !== state.tgGroups.length;
      state.tgGroups = groups;
      if (changed || !body.dataset.drawn) drawTgGroups();
      return false;
    };

    try {
      await load();
    } catch (err) {
      if (err instanceof HttpError && (err.status === 401 || err.status === 403)) return handleAuthError(err);
      return renderError(body, () => render('pick-tg'));
    }
    // Keep looking while the screen is open: the user is expected to go add the bot to a
    // group and come back, and the list should already have it.
    poll(load, { interval: 3000, attempts: 200 });
  },

  done() {
    const pair = state.lastCreated;
    document.getElementById('done-art').innerHTML = `
      <div class="mono wa">${initial(pair?.wa_chat_name)}</div>
      <div class="span"></div>
      <div class="mono tg">${initial(pair?.tg_chat_title)}</div>`;
    document.getElementById('done-names').innerHTML = `
      <div class="card-title">${esc(pair?.wa_chat_name)} → ${esc(pair?.tg_chat_title)}</div>`;
    setMain(T.done_open, () => {
      state.stack = [];
      render('home');
    });
  },

  expired() {
    setMain(T.expired_action, () => tg.close());
  },

  denied() {
    tg.BackButton.hide();
  },
};

/* ── Rendering helpers ─────────────────────────────────────────────────────── */

function statusLine(live) {
  if (live) {
    return `<div id="home-status" class="status"><span class="dot"></span>${T.wa_connected}</div>`;
  }
  return `<div id="home-status" class="status down">
      <span class="dot"></span>${T.wa_disconnected}
      <button type="button" id="home-reconnect">${T.reconnect}</button>
    </div>`;
}

function bridgeCard(pair) {
  const paused = pair.status !== 'active';
  return `
    <button type="button" class="card bridge" data-pair="${pair.id}"
            aria-label="${esc(pair.wa_chat_name)} — ${T.pair_settings}">
      <div class="bridge-ends">
        <div class="bridge-end">
          <div class="bridge-name">${esc(pair.wa_chat_name)}</div>
          <div class="bridge-side">WhatsApp</div>
        </div>
        <div class="span"></div>
        <div class="bridge-end right">
          <div class="bridge-name">${esc(pair.tg_chat_title)}</div>
          <div class="bridge-side">Telegram</div>
        </div>
      </div>
      <div class="bridge-foot">
        <span class="pill ${paused ? 'off' : 'on'}">
          <span class="dot"></span>${paused ? T.paused : T.active}
        </span>
        <span class="pill">${esc(pair.target_language)}</span>
        <span class="bridge-more">${T.pair_settings}${ICONS.chevron}</span>
      </div>
    </button>`;
}

function fromLine(chat) {
  if (!chat) return '';
  return `
    <div class="card" style="cursor:default">
      <div class="mono wa">${initial(chat.name)}</div>
      <div class="card-body">
        <div class="card-title">${esc(chat.name)}</div>
        <div class="card-meta">WhatsApp</div>
      </div>
    </div>`;
}

function qrShell(hint) {
  return `
    <div class="qr-wrap"><div class="qr-placeholder"><div class="spinner"></div></div></div>
    <div class="sub" data-qr-hint style="text-align:center">${hint}</div>
    <ol class="steps">
      <li>${T.connect_step_1}</li>
      <li>${T.connect_step_2}</li>
      <li>${T.connect_step_3}</li>
    </ol>`;
}

function renderError(container, retry) {
  container.innerHTML = `
    <div class="state">
      ${BRIDGE_ART}
      <div class="state-title">${T.error_title}</div>
      <button type="button" class="link-btn" id="retry-btn">${T.retry}</button>
    </div>`;
  const btn = container.querySelector('#retry-btn');
  if (btn) btn.onclick = retry;
}

function setProgress(step) {
  document.querySelectorAll('[data-progress]').forEach((el) => {
    el.querySelector('.progress-fill').style.width = `${(step / 3) * 100}%`;
    el.querySelector('.progress-step').textContent = `${step}/3`;
  });
}

function drawWaChats(groupsError) {
  const body = document.getElementById('wa-body');
  const q = state.waFilter.trim().toLowerCase();
  const match = (c) => !q || String(c.name || '').toLowerCase().includes(q);

  const groups = state.waChats.filter((c) => c.isGroup && match(c));
  const direct = state.waChats.filter((c) => !c.isGroup && match(c));

  if (!groups.length && !direct.length) {
    body.innerHTML = `
      <div class="state">
        ${BRIDGE_ART}
        <div class="state-title">${T.wa_empty_title}</div>
        <div class="state-text">${esc(groupsError || T.wa_empty_text)}</div>
        <button type="button" class="link-btn" id="wa-retry">${T.retry}</button>
      </div>`;
    body.querySelector('#wa-retry').onclick = () => render('pick-wa');
    return;
  }

  const section = (label, items) => items.length
    ? `<div class="section-label">${label}</div><div class="list">${items.map(waCard).join('')}</div>`
    : '';

  body.innerHTML = section(T.wa_groups, groups) + section(T.wa_direct, direct);
  body.querySelectorAll('[data-wa]').forEach((card) => {
    card.onclick = () => {
      haptic('select');
      state.selectedWa = state.waChats.find((c) => sameId(c.id, card.dataset.wa));
      state.selectedTg = null;
      navigate('pick-tg');
    };
  });
}

function waCard(chat) {
  // A direct chat has no members to count; the old UI printed "0 members" for every one.
  const meta = chat.isGroup ? T.wa_members(chat.participants || 0) : T.wa_direct_label;
  return `
    <button type="button" class="card" data-wa="${esc(chat.id)}">
      <div class="mono wa">${initial(chat.name)}</div>
      <div class="card-body">
        <div class="card-title">${esc(chat.name)}</div>
        <div class="card-meta">${meta}</div>
      </div>
      ${ICONS.chevron}
    </button>`;
}

function drawTgGroups() {
  const body = document.getElementById('tg-body');
  body.dataset.drawn = '1';

  if (!state.tgGroups.length) {
    body.innerHTML = `
      <div class="state">
        ${BRIDGE_ART}
        <div class="state-title">${T.tg_empty_title}</div>
        <div class="state-text">${T.tg_empty_text}</div>
      </div>`;
    setMain(T.tg_add, openAddBot);
    return;
  }

  body.innerHTML = `<div class="list">${state.tgGroups.map(tgCard).join('')}</div>`;
  body.querySelectorAll('[data-tg]').forEach((card) => {
    card.onclick = () => {
      haptic('select');
      body.querySelectorAll('[data-tg]').forEach((c) => c.classList.remove('selected'));
      card.classList.add('selected');
      state.selectedTg = state.tgGroups.find((g) => sameId(g.chat_id, card.dataset.tg));
      setMain(T.tg_create, createBridge);
    };
  });

  if (state.selectedTg) setMain(T.tg_create, createBridge);
  else setMain(T.tg_add, openAddBot);
}

function tgCard(group) {
  return `
    <button type="button" class="card" data-tg="${esc(group.chat_id)}">
      <div class="mono tg">${initial(group.title)}</div>
      <div class="card-body">
        <div class="card-title">${esc(group.title)}</div>
        <div class="card-meta">Telegram</div>
      </div>
      ${ICONS.check}
    </button>`;
}

function openAddBot() {
  const bot = new URLSearchParams(location.search).get('bot');
  if (!bot) return alert_(T.tg_empty_text);
  tg.openTelegramLink(`https://t.me/${bot}?startgroup=true`);
}

/* ── Actions ───────────────────────────────────────────────────────────────── */

async function createBridge() {
  if (state.busy || !state.selectedWa || !state.selectedTg) return;
  state.busy = true;
  tg.MainButton.showProgress();
  tg.MainButton.disable();

  try {
    const { pair } = await api('/chat-pairs', {
      method: 'POST',
      body: {
        wa_chat_id: state.selectedWa.id,
        wa_chat_name: state.selectedWa.name,
        tg_chat_id: state.selectedTg.chat_id,
        tg_chat_title: state.selectedTg.title,
      },
    });
    haptic('success');
    state.lastCreated = pair;
    state.stack = [];
    navigate('done', { replace: true });
  } catch (err) {
    haptic('error');
    state.busy = false;
    tg.MainButton.hideProgress();
    tg.MainButton.enable();
    if (err instanceof HttpError && (err.status === 401 || err.status === 403)) return handleAuthError(err);
    alert_(err.message || T.error_title);
  }
}

/* ── Bridge settings sheet ─────────────────────────────────────────────────── */

const backdrop = () => document.getElementById('sheet-backdrop');
const sheetEl = () => document.getElementById('sheet');

function openPairSheet(pairId) {
  // Compare as strings on both sides: ids arrive from the API as strings (pg returns
  // bigint that way) and from the DOM as strings, but nothing guarantees either.
  state.openPair = state.pairs.find((p) => sameId(p.id, pairId));
  if (!state.openPair) return;
  haptic('select');
  drawPairSheet();
  backdrop().classList.add('open');
  sheetEl().classList.add('open');
  tg.BackButton.show();
}

function closeSheet() {
  backdrop().classList.remove('open');
  sheetEl().classList.remove('open');
  state.openPair = null;
  if (!state.stack.length) tg.BackButton.hide();
}

function drawPairSheet() {
  const pair = state.openPair;
  const paused = pair.status !== 'active';
  const created = pair.created_at
    ? new Date(pair.created_at).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
    : '';

  sheetEl().innerHTML = `
    <div class="sheet-grip"></div>
    <div class="sheet-title">${esc(pair.wa_chat_name)}</div>
    <button type="button" class="row" data-act="toggle">
      ${paused ? ICONS.play : ICONS.pause}
      <span class="row-label">${paused ? T.pair_resume : T.pair_pause}</span>
    </button>
    <button type="button" class="row" data-act="language">
      ${ICONS.globe}
      <span class="row-label">${T.pair_language}</span>
      <span class="row-value">${esc(pair.target_language)}${pair.language_inherited ? ' ·' : ''}</span>
      ${ICONS.chevron}
    </button>
    <button type="button" class="row" data-act="summary">
      ${ICONS.bell}
      <span class="row-label">${T.pair_summary}</span>
      <span class="row-value">${pair.summary_enabled ? T.on : T.off}</span>
    </button>
    <button type="button" class="row danger" data-act="delete">
      ${ICONS.trash}
      <span class="row-label">${T.pair_delete}</span>
    </button>
    ${created ? `<div class="card-meta" style="padding-top:var(--s-3)">${T.pair_created(created)}</div>` : ''}`;

  sheetEl().querySelectorAll('[data-act]').forEach((row) => {
    row.onclick = () => pairAction(row.dataset.act, row);
  });
}

function drawLanguageSheet() {
  const pair = state.openPair;
  sheetEl().innerHTML = `
    <div class="sheet-grip"></div>
    <div class="sheet-title">${T.pair_language_title}</div>
    ${LANGUAGES.map((lang) => {
      const current = lang.code === null ? pair.language_inherited : (!pair.language_inherited && pair.target_language === lang.code);
      return `<button type="button" class="row" data-lang="${lang.code ?? ''}">
          <span class="row-label">${esc(lang.label)}</span>
          ${current ? ICONS.check : ''}
        </button>`;
    }).join('')}`;

  sheetEl().querySelectorAll('[data-lang]').forEach((row) => {
    row.onclick = () => patchPair({ target_language: row.dataset.lang || null }, drawPairSheet);
  });
}

async function pairAction(action, row) {
  const pair = state.openPair;

  if (action === 'language') return drawLanguageSheet();
  if (action === 'toggle') {
    return patchPair({ status: pair.status === 'active' ? 'paused' : 'active' }, drawPairSheet, row);
  }
  if (action === 'summary') {
    return patchPair({ summary_enabled: !pair.summary_enabled }, drawPairSheet, row);
  }
  if (action === 'delete') {
    confirm_(T.pair_delete_ask, async (ok) => {
      if (!ok) return;
      try {
        await api(`/chat-pairs/${pair.id}`, { method: 'DELETE' });
        haptic('success');
        closeSheet();
        render('home');
      } catch (err) {
        haptic('error');
        if (err instanceof HttpError && (err.status === 401 || err.status === 403)) return handleAuthError(err);
        alert_(err.message || T.error_title);
      }
    });
  }
}

async function patchPair(body, after, row) {
  if (state.busy) return;
  state.busy = true;
  if (row) row.disabled = true;

  try {
    const { pair } = await api(`/chat-pairs/${state.openPair.id}`, { method: 'PATCH', body });
    haptic('success');
    state.openPair = pair;
    state.pairs = state.pairs.map((p) => (sameId(p.id, pair.id) ? pair : p));
    after();
    // Keep the list underneath in step without a full reload.
    const card = document.querySelector(`[data-pair="${pair.id}"]`);
    if (card) card.outerHTML = bridgeCard(pair);
    document.querySelectorAll('[data-pair]').forEach((el) => {
      el.onclick = () => openPairSheet(el.dataset.pair);
    });
  } catch (err) {
    haptic('error');
    if (err instanceof HttpError && (err.status === 401 || err.status === 403)) return handleAuthError(err);
    alert_(err.message || T.error_title);
  } finally {
    state.busy = false;
    if (row) row.disabled = false;
  }
}

/* ── Theme ─────────────────────────────────────────────────────────────────── */

function applyTheme() {
  document.documentElement.dataset.scheme = tg.colorScheme || 'light';
  try {
    tg.setHeaderColor('bg_color');
    tg.setBackgroundColor('bg_color');
  } catch { /* older clients */ }
}

/* ── Boot ──────────────────────────────────────────────────────────────────── */

function boot() {
  tg.ready();
  tg.expand();
  applyTheme();

  tg.onEvent('themeChanged', applyTheme);
  tg.onEvent('viewportChanged', () => {
    document.documentElement.style.setProperty(
      '--tg-viewport-stable-height', `${tg.viewportStableHeight}px`,
    );
  });

  // Registered once, for the life of the app.
  tg.BackButton.onClick(() => {
    if (state.openPair) return closeSheet();
    goBack();
  });
  tg.MainButton.onClick(() => { if (mainAction) mainAction(); });

  backdrop().onclick = closeSheet;

  const search = document.getElementById('wa-search');
  search.oninput = () => { state.waFilter = search.value; drawWaChats(); };

  const user = tg.initDataUnsafe?.user;
  if (!user?.id) {
    // Opened outside Telegram, or too old a client to supply initData.
    render('denied');
    return;
  }
  state.userId = user.id;

  render('loading');
  api(`/chat-pairs/${state.userId}`)
    .then((data) => {
      state.pairs = data.pairs || [];
      state.waLive = !!data.wa_connected;
      // Even with no bridges the home screen is the entry: its empty state explains what
      // to do. The old code jumped straight into the wizard and left no way back.
      render(state.waLive ? 'home' : 'connect');
    })
    .catch((err) => {
      if (err instanceof HttpError && (err.status === 401 || err.status === 403)) return handleAuthError(err);
      render('home');
    });

  document.addEventListener('click', (e) => {
    if (e.target.id === 'home-reconnect') navigate('connect');
  });
}

boot();
