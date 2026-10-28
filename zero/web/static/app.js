// Trackpad-first KVM cockpit (mobile portrait + wide split cockpit).
// Shares the 3-palette GEEK visual lineage (slate / braun / cobalt), low-latency /api/ws
// WebSocket HID transport with HTTP/1.1 keep-alive fallback, 2-finger scroll + right-tap,
// Drag-Lock, acceleration curves, and supervised 1-tap target host pairing.

const statusNote = msg => {
  const el = document.getElementById('pwr');
  if (el) el.textContent = msg;
};

// ---- Modal light-dismiss fallback (Safari / pre-closedby browsers) ----------------------
const menuDlg = document.getElementById('menu-dlg');
const pinDlg = document.getElementById('pin-dlg');

for (const dlg of [menuDlg, pinDlg]) {
  if (dlg && !('closedBy' in HTMLDialogElement.prototype)) {
    dlg.addEventListener('click', event => {
      if (event.target !== dlg) return;
      const r = dlg.getBoundingClientRect();
      const inside = r.top <= event.clientY && event.clientY <= r.top + r.height &&
                     r.left <= event.clientX && event.clientX <= r.left + r.width;
      if (!inside) dlg.close();
    });
  }
}

document.getElementById('menu-btn').onclick = () => menuDlg.showModal();

// ---- PIN auth & modal dialog ------------------------------------------------------------
let pin = localStorage.getItem('kvmPin') || '';
let pinWaiters = [];

function openPinDialog(msg) {
  document.getElementById('pin-msg').textContent = msg || 'Enter the Console PIN from /etc/pi-zero-w-hid-kvm/pin.';
  const inp = document.getElementById('pin-inp');
  inp.value = '';
  if (!pinDlg.open) pinDlg.showModal();
  setTimeout(() => inp.focus(), 30);
  return new Promise(resolve => pinWaiters.push(resolve));
}
function resolvePinWaiters(val) {
  const list = pinWaiters;
  pinWaiters = [];
  for (const fn of list) fn(val);
}
document.getElementById('pin-form').addEventListener('submit', e => {
  e.preventDefault();
  const v = document.getElementById('pin-inp').value.trim();
  if (v) {
    pin = v;
    localStorage.setItem('kvmPin', pin);
    pinDlg.close();
    connectWs();
    poll();
  }
  resolvePinWaiters(pin);
});
document.getElementById('pin-cancel').onclick = () => {
  pinDlg.close();
  resolvePinWaiters('');
};
pinDlg.addEventListener('close', () => resolvePinWaiters(pin));
document.getElementById('pin').onclick = () => {
  menuDlg.close();
  pin = '';
  localStorage.removeItem('kvmPin');
  closeWs();
  openPinDialog('Enter new Console PIN:');
};
if (!pin) setTimeout(() => openPinDialog(), 120);

async function post(u, b) {
  if (!pin) {
    await openPinDialog();
    if (!pin) return new Response('{}', {status: 401});
  }
  const r = await fetch(u, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-KVM-PIN': pin},
    body: JSON.stringify(b)
  });
  if (r.status === 401) {
    pin = '';
    localStorage.removeItem('kvmPin');
    closeWs();
    openPinDialog('Wrong or missing PIN. Try again:');
  } else if (r.status === 429) {
    statusNote('Too many wrong PINs — locked out for 60 s');
  }
  return r;
}

// ---- Live-Synced Theme (Web + GEEK) -----------------------------------------------------
let currentTheme = localStorage.getItem('kvmTheme') || 'slate';
function applyTheme(t, pushServer) {
  if (!['slate', 'braun', 'cobalt'].includes(t)) return;
  currentTheme = t;
  document.documentElement.dataset.theme = t;
  localStorage.setItem('kvmTheme', t);
  document.querySelectorAll('[data-theme-pick]').forEach(btn => {
    btn.classList.toggle('chip-on', btn.dataset.themePick === t);
  });
  drawSparks();
  if (pushServer && pin) post('/api/theme', {theme: t}).catch(() => {});
}
applyTheme(currentTheme, false);
document.querySelectorAll('[data-theme-pick]').forEach(btn => {
  btn.onclick = () => applyTheme(btn.dataset.themePick, true);
});

// ---- Deck & Drawer Switching ------------------------------------------------------------
let activeDeck = 'pad';
let activeDrawer = 'kb';
document.querySelectorAll('[data-deck-btn]').forEach(btn => {
  btn.onclick = () => {
    activeDeck = btn.dataset.deckBtn;
    document.body.dataset.deck = activeDeck;
    document.querySelectorAll('[data-deck-btn]').forEach(x => {
      const on = x === btn;
      x.classList.toggle('chip-on', on);
      x.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  };
});
document.querySelectorAll('[data-drawer-btn]').forEach(btn => {
  btn.onclick = () => {
    activeDrawer = btn.dataset.drawerBtn;
    document.body.dataset.drawer = activeDrawer;
    document.querySelectorAll('[data-drawer-btn]').forEach(x => x.classList.toggle('chip-on', x === btn));
  };
});

// ---- Low-Latency WebSocket Transport (/api/ws) + HTTP/1.1 Fallback ----------------------
let ws = null, wsAuthed = false, wsRtt = null, wsPingTimer = null, wsRetryTimer = null;
const xportEl = document.getElementById('xport');

function updateXportBadge() {
  if (wsAuthed && ws && ws.readyState === WebSocket.OPEN) {
    xportEl.textContent = wsRtt != null ? `ws ${wsRtt}ms` : 'ws live';
    xportEl.classList.add('ws-live');
  } else {
    xportEl.textContent = 'http';
    xportEl.classList.remove('ws-live');
  }
}

function closeWs() {
  wsAuthed = false;
  wsRtt = null;
  if (wsPingTimer) { clearInterval(wsPingTimer); wsPingTimer = null; }
  if (wsRetryTimer) { clearTimeout(wsRetryTimer); wsRetryTimer = null; }
  if (ws) { try { ws.close(); } catch (e) {} ws = null; }
  updateXportBadge();
}

function connectWs() {
  if (!pin || (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING))) return;
  closeWs();
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  try {
    ws = new WebSocket(`${proto}//${location.host}/api/ws`);
  } catch (e) {
    return;
  }
  ws.onopen = () => {
    ws.send(JSON.stringify({op: 'auth', pin}));
  };
  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.op === 'hello' && msg.ok) {
      wsAuthed = true;
      updateXportBadge();
      const ping = () => {
        if (ws && ws.readyState === WebSocket.OPEN && wsAuthed)
          ws.send(JSON.stringify({op: 'p', t: performance.now()}));
      };
      ping();
      wsPingTimer = setInterval(ping, 5000);
    } else if (msg.op === 'p' && typeof msg.t === 'number') {
      wsRtt = Math.max(1, Math.round(performance.now() - msg.t));
      updateXportBadge();
    } else if (msg.ok === false && msg.error === 'bad_pin') {
      pin = '';
      localStorage.removeItem('kvmPin');
      closeWs();
      openPinDialog('Wrong PIN. Enter Console PIN:');
    }
  };
  ws.onclose = () => {
    wsAuthed = false;
    if (wsPingTimer) { clearInterval(wsPingTimer); wsPingTimer = null; }
    updateXportBadge();
    if (pin && !wsRetryTimer) wsRetryTimer = setTimeout(() => { wsRetryTimer = null; connectWs(); }, 2500);
  };
}
if (pin) connectWs();

// ---- Power, Macros, Ping & Host Pairing -------------------------------------------------
async function power(target, action, label) {
  if (target === 'zero') {
    if (prompt(`Type "${action}" to ${label}`) !== action) return;
  } else if (action !== 'wake' && !confirm(`${label}?`)) {
    return;
  }
  menuDlg.close();
  const r = await post('/api/power', {target, action, confirm: true});
  const j = await r.json().catch(() => ({}));
  statusNote(r.ok ? `${label}: sent` : `${label}: ${j.error || r.status}`);
}

document.querySelectorAll('[data-pwr]').forEach(btn => {
  btn.onclick = () => {
    const [target, action, label] = btn.dataset.pwr.split(':');
    power(target, action, label);
  };
});
document.querySelectorAll('[data-macro]').forEach(btn => {
  btn.onclick = () => {
    menuDlg.close();
    post('/api/macro', {name: btn.dataset.macro});
    statusNote(`Macro ${btn.dataset.macro}: sent`);
  };
});
document.getElementById('qk-cad').onclick = () => {
  if (confirm('Send Ctrl+Alt+Del to target host?')) post('/api/macro', {name: 'ctrl_alt_del'});
};

document.getElementById('rst-hist').onclick = async () => {
  if (!confirm('Reset saved cumulative network history for this PC?')) return;
  menuDlg.close();
  const r = await post('/api/net_hist/reset', {});
  const j = await r.json().catch(() => ({}));
  statusNote(r.ok ? 'Network history reset' : `History reset: ${j.error || r.status}`);
};

async function pairHost(osName) {
  const pretty = osName === 'windows' ? 'Windows (PowerShell)' : 'Linux / macOS';
  if (!confirm(`Pair ${pretty} host via USB HID?\n\nMake sure the target desktop is unlocked so the shortcut can open a terminal and install the 3 KB USB watcher stub.`)) return;
  menuDlg.close();
  const r = await post('/api/bootstrap', {method: 'hid', os: osName, confirm: true});
  const j = await r.json().catch(() => ({}));
  statusNote(r.ok ? `Pairing ${pretty} host over USB…` : `Pairing failed: ${j.error || r.status}`);
  setTimeout(poll, 600);
}
document.getElementById('pair-linux').onclick = () => pairHost('linux');
document.getElementById('pair-win').onclick = () => pairHost('windows');
document.getElementById('dlg-pair-linux').onclick = () => pairHost('linux');
document.getElementById('dlg-pair-win').onclick = () => pairHost('windows');
document.getElementById('pair-probe').onclick = async () => {
  const r = await post('/api/bootstrap', {method: 'probe'});
  statusNote(r.ok ? 'Sent KVMCTL:probe over USB serial…' : 'Probe failed');
  setTimeout(poll, 500);
};

// ---- Serial Mode Toggle & Inline Serial Terminal ----------------------------------------
let serialMode = null;
function showMode(s) {
  const b = document.getElementById('mode');
  serialMode = s.serial_mode;
  const pend = s.serial_mode_requested && s.serial_mode_requested !== s.serial_mode;
  b.textContent = 'serial: ' + (serialMode || 'idle') + (pend ? ' → ' + s.serial_mode_requested : '');
  b.classList.toggle('chip-on', serialMode === 'shell');
}
async function toggleSerialMode() {
  const want = serialMode === 'shell' ? 'helper' : 'shell';
  if (want === 'shell' && !confirm('Switch USB serial to interactive login shell? (Telemetry & preview pause until switched back.)')) return;
  const r = await post('/api/mode', {mode: want});
  const j = await r.json().catch(() => ({}));
  statusNote(r.ok ? `serial → ${want}: requested` : `serial mode: ${j.error || r.status}`);
  poll();
}
document.getElementById('mode').onclick = toggleSerialMode;
document.getElementById('term-mode-btn').onclick = toggleSerialMode;

const termOut = document.getElementById('term-out');
const termCmd = document.getElementById('term-cmd');
const termSt = document.getElementById('term-st');
let lastTermTxt = '';

async function pollInlineTerm() {
  const visible = (window.innerWidth >= 920 && activeDrawer === 'term') || (window.innerWidth < 920 && activeDeck === 'term');
  if (!visible || !pin) {
    setTimeout(pollInlineTerm, 900);
    return;
  }
  try {
    const r = await fetch('/api/term?n=120', {headers: {'X-KVM-PIN': pin}});
    if (r.ok) {
      const j = await r.json();
      const txt = (j.lines || []).concat([j.partial || '']).join('\n');
      termSt.textContent = (j.open ? '● serial open' : '○ ' + (j.error || 'serial closed')) + (serialMode ? ` (${serialMode})` : '');
      if (txt !== lastTermTxt) {
        const stick = termOut.scrollTop + termOut.clientHeight >= termOut.scrollHeight - 24;
        termOut.textContent = lastTermTxt = txt;
        if (stick) termOut.scrollTop = termOut.scrollHeight;
      }
    }
  } catch (e) {}
  setTimeout(pollInlineTerm, 750);
}
pollInlineTerm();

async function sendTerm(text, newline) {
  const r = await post('/api/term', {text, newline});
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    statusNote('Serial send: ' + (j.error || r.status));
  }
}
document.getElementById('term-form').addEventListener('submit', e => {
  e.preventDefault();
  sendTerm(termCmd.value, true);
  termCmd.value = '';
});
termCmd.addEventListener('keydown', e => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter') {
    e.preventDefault();
    sendTerm(termCmd.value, true);
    termCmd.value = '';
  }
});
document.querySelectorAll('[data-tctl]').forEach(btn => {
  btn.onclick = () => sendTerm(btn.dataset.tctl, false);
});

// ---- Tactile 16-Column Keyboard + Quick-Key Strip ---------------------------------------
const SYM = {
  '`': '~', '1': '!', '2': '@', '3': '#', '4': '$', '5': '%', '6': '^', '7': '&', '8': '*',
  '9': '(', '0': ')', '-': '_', '=': '+', '[': '{', ']': '}', '\\': '|', ';': ':', "'": '"',
  ',': '<', '.': '>', '/': '?'
};
const key = (label, name, span, extra) => Object.assign({label, key: name, span: span || 1, kind: 'key'}, extra);
const dig = '1234567890'.split('').map(d => key(d, d));
const letters = s => s.split('').map(ch => key(ch, ch));

const BASE = [
  [key('esc', 'esc', 1, {cls: 'mod'}), key('`', '`'), ...dig, key('-', '-'), key('=', '='), key('\u232b', 'backspace', 2, {cls: 'mod', aria: 'backspace'})],
  [key('tab', 'tab', 2, {cls: 'mod'}), ...letters('qwertyuiop'), key('[', '['), key(']', ']'), key('\\', '\\', 2)],
  [{label: 'Fn', span: 2, kind: 'fn', cls: 'fn'}, ...letters('asdfghjkl'), key(';', ';'), key("'", "'"), key('\u23ce', 'enter', 3, {cls: 'go', aria: 'enter'})],
  [{label: '\u21e7', span: 2, kind: 'shift', cls: 'mod', aria: 'shift'}, ...letters('zxcvbnm'), key(',', ','), key('.', '.'), key('/', '/'), {label: '\u21e7', span: 4, kind: 'shift', cls: 'mod', aria: 'shift'}],
];
const BOTTOM = [
  {label: 'ctrl', span: 2, kind: 'ctrl', cls: 'mod'},
  {label: 'alt', span: 2, kind: 'alt', cls: 'mod'},
  {label: 'win', span: 2, kind: 'win', cls: 'mod'},
  key('space', 'space', 6, {cls: 'sp'}),
  key('\u2190', 'left', 1, {cls: 'mod', aria: 'left'}),
  key('\u2191', 'up', 1, {cls: 'mod', aria: 'up'}),
  key('\u2193', 'down', 1, {cls: 'mod', aria: 'down'}),
  key('\u2192', 'right', 1, {cls: 'mod', aria: 'right'}),
];
const fkey = i => key('f' + i, 'f' + i, 1, {cls: 'mod'});
const FN = [
  [...Array.from({length: 12}, (_, i) => fkey(i + 1)),
    key('Hm', 'home', 1, {cls: 'mod', aria: 'home'}),
    key('End', 'end', 1, {cls: 'mod', aria: 'end'}),
    key('PgU', 'pageup', 1, {cls: 'mod', aria: 'page up'}),
    key('PgD', 'pagedown', 1, {cls: 'mod', aria: 'page down'})],
  [
    key('Del', 'delete', 3, {cls: 'mod', aria: 'delete'}),
    {label: 'C-A-Del', span: 5, kind: 'macro', key: 'ctrl_alt_del', cls: 'mod', aria: 'Ctrl+Alt+Del'},
    {label: 'Lock', span: 4, kind: 'macro', key: 'win_l', cls: 'mod', aria: 'Win+L'},
    {label: 'A-Tab', span: 4, kind: 'macro', key: 'alt_tab', cls: 'mod', aria: 'Alt+Tab'},
  ],
  [
    {label: 'Fn', span: 2, kind: 'fn', cls: 'fn'},
    {label: 'Off', span: 4, kind: 'power', target: 'host', action: 'power', confirm: 'Shut down host', cls: 'mod'},
    {label: 'Sleep', span: 3, kind: 'power', target: 'host', action: 'sleep', confirm: 'Sleep host', cls: 'mod'},
    {label: 'Wake', span: 3, kind: 'power', target: 'host', action: 'wake', confirm: 'Wake host', cls: 'mod'},
    {label: 'Rst', span: 4, kind: 'power', target: 'host', action: 'hard_reset', confirm: 'Hard reset host', cls: 'hw', aria: 'Hard reset'},
  ],
  [
    {label: 'Pwr', span: 4, kind: 'power', target: 'host', action: 'hard_power', confirm: 'Hard power host', cls: 'hw', aria: 'Hard power'},
    {label: 'Reboot', span: 6, kind: 'power', target: 'zero', action: 'reboot', confirm: 'Reboot the Zero', cls: 'danger'},
    {label: 'Halt', span: 6, kind: 'power', target: 'zero', action: 'shutdown', confirm: 'Shut down the Zero', cls: 'danger'},
  ],
];

let shiftOn = false, ctrlOn = false, altOn = false, winOn = false, fnOn = false;

function sendKeyCombo(keys, mods) {
  if (wsAuthed && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({op: 'k', keys, mods}));
  } else {
    post('/api/key', {keys, mods});
  }
}

function syncQuickMods() {
  document.querySelectorAll('[data-qm]').forEach(btn => {
    const m = btn.dataset.qm;
    const on = (m === 'ctrl' && ctrlOn) || (m === 'alt' && altOn) || (m === 'win' && winOn);
    btn.classList.toggle('latched', on);
  });
}

function face(k) {
  if (k.kind !== 'key') return k.label;
  if (shiftOn && SYM[k.key]) return SYM[k.key];
  if (shiftOn && k.key.length === 1 && k.key >= 'a' && k.key <= 'z') return k.key.toUpperCase();
  return k.label;
}
function latched(k) {
  return (k.kind === 'fn' && fnOn) || (k.kind === 'shift' && shiftOn) || (k.kind === 'ctrl' && ctrlOn)
    || (k.kind === 'alt' && altOn) || (k.kind === 'win' && winOn);
}
function drawKeys() {
  const kb = document.getElementById('kb');
  kb.replaceChildren();
  for (const row of (fnOn ? FN : BASE).concat([BOTTOM])) {
    const d = document.createElement('div');
    d.className = 'row';
    let span = 0;
    for (const k of row) span += k.span || 1;
    if (span !== 16) console.error('key row is ' + span + ' columns, want 16');
    for (const k of row) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = face(k);
      b.style.gridColumn = 'span ' + (k.span || 1);
      b.className = [k.cls || '', latched(k) ? 'latched' : ''].filter(Boolean).join(' ');
      if (k.aria) b.setAttribute('aria-label', k.aria);
      b.onclick = () => onKey(k);
      d.appendChild(b);
    }
    kb.appendChild(d);
  }
  syncQuickMods();
}
function onKey(k) {
  if (k.kind === 'fn') { fnOn = !fnOn; drawKeys(); return; }
  if (k.kind === 'shift') { shiftOn = !shiftOn; drawKeys(); return; }
  if (k.kind === 'ctrl') { ctrlOn = !ctrlOn; drawKeys(); return; }
  if (k.kind === 'alt') { altOn = !altOn; drawKeys(); return; }
  if (k.kind === 'win') { winOn = !winOn; drawKeys(); return; }
  if (k.kind === 'macro') { post('/api/macro', {name: k.key}); return; }
  if (k.kind === 'power') { power(k.target, k.action, k.confirm); return; }
  const mods = [];
  if (shiftOn) mods.push('shift');
  if (ctrlOn) mods.push('ctrl');
  if (altOn) mods.push('alt');
  if (winOn) mods.push('gui');
  sendKeyCombo([k.key], mods);
  shiftOn = ctrlOn = altOn = winOn = false;
  drawKeys();
}
drawKeys();

document.querySelectorAll('[data-qk]').forEach(btn => {
  btn.onclick = () => onKey({kind: 'key', key: btn.dataset.qk});
});
document.querySelectorAll('[data-qm]').forEach(btn => {
  btn.onclick = () => onKey({kind: btn.dataset.qm});
});

// ---- Type / Clipboard Form (IME-safe Enter submit) --------------------------------------
function typeTxt() {
  const t = document.getElementById('txt');
  if (t.value) {
    post('/api/type', {text: t.value});
    statusNote(`Typed ${t.value.length} chars`);
  }
  t.value = '';
}
document.getElementById('type-form').addEventListener('submit', e => {
  e.preventDefault();
  typeTxt();
});
document.getElementById('txt').addEventListener('keydown', e => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter') {
    e.preventDefault();
    typeTxt();
  }
});

document.getElementById('clip-push').onclick = async () => {
  const t = document.getElementById('txt');
  if (!t.value) { statusNote('Clipboard push: enter text first'); return; }
  const r = await post('/api/clipboard', {text: t.value});
  const j = await r.json().catch(() => ({}));
  statusNote(r.ok ? `Clipboard pushed (${j.bytes} B)` : `Clipboard push: ${j.error || r.status}`);
};

document.getElementById('clip-pull').onclick = async () => {
  if (!pin) { await openPinDialog(); if (!pin) return; }
  const r = await fetch('/api/clipboard', {headers: {'X-KVM-PIN': pin}});
  if (r.status === 401) { pin = ''; localStorage.removeItem('kvmPin'); openPinDialog('Wrong PIN:'); return; }
  const j = await r.json().catch(() => ({}));
  if (r.ok) {
    document.getElementById('txt').value = j.text || '';
    if (j.text && navigator.clipboard && navigator.clipboard.writeText)
      navigator.clipboard.writeText(j.text).catch(() => {});
    statusNote(j.text ? `Clipboard pulled (${j.text.length} chars)` : 'Host clipboard empty');
  } else {
    statusNote(`Clipboard pull: ${j.error || r.status}`);
  }
};

function showLeds(leds) {
  const l = leds || {};
  document.getElementById('led-num').className = 'led' + (l.num ? ' on' : '');
  document.getElementById('led-caps').className = 'led' + (l.caps ? ' on' : '');
  document.getElementById('led-scroll').className = 'led' + (l.scroll ? ' on' : '');
}

document.getElementById('ping').onclick = async () => {
  menuDlg.close();
  statusNote('Pinging host via CapsLock LED…');
  const r = await post('/api/ping', {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { statusNote(`Ping: ${j.error || r.status}`); return; }
  if (j.leds) showLeds(j.leds);
  statusNote(j.alive ? `Host alive: USB LED echo in ${j.latency_ms} ms` : 'Host unresponsive (no LED echo)');
};

// ---- Relative Trackpad: WS + HTTP/1.1, Multi-Touch, Accel & Drag-Lock -------------------
let held = 0;
let dragLock = false;
const ACCELS = [
  {label: '1.0x', base: 1.0, k: 0.0, cap: 1.0},
  {label: '1.6x', base: 1.05, k: 0.08, cap: 2.3},
  {label: '2.2x', base: 1.25, k: 0.13, cap: 3.4},
];
let accelIdx = 1;

const pad = document.getElementById('surface');
const dragLockBtn = document.getElementById('drag-lock');
const accelBtn = document.getElementById('accel-btn');

dragLockBtn.onclick = () => {
  dragLock = !dragLock;
  if (dragLock) held |= 1; else held &= ~1;
  dragLockBtn.classList.toggle('latched', dragLock);
  pad.classList.toggle('dragging', dragLock);
  sendMouseImmediate({buttons: held});
};
accelBtn.onclick = () => {
  accelIdx = (accelIdx + 1) % ACCELS.length;
  accelBtn.textContent = 'Accel: ' + ACCELS[accelIdx].label;
};

let httpQueue = Promise.resolve();
let httpInflight = 0;
let acc = {dx: 0, dy: 0, w: 0};
let rafPending = false;
const clampMove = v => Math.max(-4096, Math.min(4096, Math.round(v)));

function sendMouseHttp(o) {
  if (!pin) return httpQueue;
  const b = Object.assign({dx: 0, dy: 0, wheel: 0, buttons: held}, o);
  httpQueue = httpQueue.then(() => post('/api/mouse', b)).catch(() => {});
  return httpQueue;
}

function sendMouseImmediate(o) {
  flushMotion(true);
  const dx = clampMove(o.dx || 0), dy = clampMove(o.dy || 0);
  const w = Math.max(-127, Math.min(127, Math.round(o.wheel || 0)));
  const b = o.buttons != null ? o.buttons : held;
  if (wsAuthed && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({op: 'm', dx, dy, w, b}));
  } else {
    sendMouseHttp({dx, dy, wheel: w, buttons: b});
  }
}

function flushMotion(force) {
  rafPending = false;
  if (!(acc.dx || acc.dy || acc.w)) return;
  const dx = clampMove(acc.dx), dy = clampMove(acc.dy);
  const w = Math.max(-127, Math.min(127, Math.round(acc.w)));
  if (wsAuthed && ws && ws.readyState === WebSocket.OPEN) {
    acc = {dx: acc.dx - dx, dy: acc.dy - dy, w: 0};
    ws.send(JSON.stringify({op: 'm', dx, dy, w, b: held}));
    return;
  }
  if (httpInflight && !force) return;
  acc = {dx: 0, dy: 0, w: 0};
  httpInflight++;
  sendMouseHttp({dx, dy, wheel: w, buttons: held}).finally(() => {
    httpInflight--;
    if (!httpInflight && (acc.dx || acc.dy || acc.w)) flushMotion(false);
  });
}

function scheduleFlush() {
  if (wsAuthed && ws && ws.readyState === WebSocket.OPEN) {
    if (!rafPending) { rafPending = true; requestAnimationFrame(() => flushMotion(false)); }
  }
}
setInterval(() => flushMotion(false), 28);

document.querySelectorAll('[data-b]').forEach(b => {
  const bit = +b.dataset.b;
  b.onpointerdown = e => {
    e.preventDefault();
    if (bit === 1 && dragLock) {
      dragLock = false;
      dragLockBtn.classList.remove('latched');
      pad.classList.remove('dragging');
    }
    held |= bit;
    sendMouseImmediate({buttons: held});
  };
  b.onpointerup = b.onpointercancel = () => {
    if (held & bit) {
      held &= ~bit;
      sendMouseImmediate({buttons: held});
    }
  };
});

// Multi-touch pointer tracking on #surface (1F move/tap + 2F scroll & 2F right-click tap)
const ptrs = new Map();
let gesture = {maxCount: 0, moved: false, dist: 0, startAt: 0, scrollCarry: 0};

pad.addEventListener('pointerdown', e => {
  ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
  try { pad.setPointerCapture(e.pointerId); } catch (_) {}
  if (ptrs.size === 1) {
    gesture = {maxCount: 1, moved: false, dist: 0, startAt: performance.now(), scrollCarry: 0};
  } else if (ptrs.size >= 2) {
    gesture.maxCount = Math.max(gesture.maxCount, ptrs.size);
  }
});

pad.addEventListener('pointermove', e => {
  const prev = ptrs.get(e.pointerId);
  if (!prev) return;
  const rx = e.clientX - prev.x, ry = e.clientY - prev.y;
  prev.x = e.clientX; prev.y = e.clientY;
  gesture.dist += Math.abs(rx) + Math.abs(ry);
  if (gesture.dist > 5) gesture.moved = true;

  if (ptrs.size === 1 && gesture.maxCount === 1) {
    const curve = ACCELS[accelIdx];
    const spd = Math.hypot(rx, ry);
    const gain = Math.min(curve.cap, curve.base + spd * curve.k);
    acc.dx += rx * gain;
    acc.dy += ry * gain;
    scheduleFlush();
  } else if (ptrs.size >= 2) {
    // Average dy across 2 pointers -> vertical wheel ticks
    gesture.scrollCarry += ry / ptrs.size;
    const step = 18;
    if (Math.abs(gesture.scrollCarry) >= step) {
      const ticks = Math.trunc(gesture.scrollCarry / step);
      gesture.scrollCarry -= ticks * step;
      acc.w += -ticks;
      scheduleFlush();
    }
  }
});

function onPtrEnd(e) {
  if (!ptrs.has(e.pointerId)) return;
  ptrs.delete(e.pointerId);
  if (ptrs.size === 0) {
    flushMotion(true);
    const dur = performance.now() - gesture.startAt;
    if (!gesture.moved && dur < 280) {
      if (gesture.maxCount === 2) {
        sendMouseImmediate({buttons: held | 2});
        sendMouseImmediate({buttons: held});
      } else if (gesture.maxCount === 1 && !dragLock) {
        sendMouseImmediate({buttons: held | 1});
        sendMouseImmediate({buttons: held});
      }
    }
  }
}
pad.addEventListener('pointerup', onPtrEnd);
pad.addEventListener('pointercancel', onPtrEnd);
pad.addEventListener('wheel', e => {
  e.preventDefault();
  acc.w += e.deltaY < 0 ? 1 : -1;
  scheduleFlush();
}, {passive: false});

function bindWheel(id, dir) {
  const btn = document.getElementById(id);
  let timer = null;
  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  btn.onpointerdown = e => {
    e.preventDefault();
    sendMouseImmediate({wheel: dir});
    timer = setInterval(() => sendMouseImmediate({wheel: dir}), 120);
  };
  btn.onpointerup = btn.onpointercancel = btn.onpointerleave = stop;
}
bindWheel('su', 1);
bindWheel('sd', -1);
document.addEventListener('contextmenu', e => {
  if (e.target.closest('#pad, #kb, .topbar, .quick-strip')) e.preventDefault();
});

// ---- 240x135 Still & 1:1 Cursor Loupe (Stage Inset + Optional Trackpad Overlay) ---------
const viewCanvas = document.getElementById('view');
const padLabel = document.getElementById('pad-label');
const loupeBtn = document.getElementById('loupe');
const loupeWrap = document.getElementById('loupe-wrap');
const loupeCv = document.getElementById('loupe-cv');
const loupeInsetBtn = document.getElementById('loupe-inset-btn');
const viewCtx = viewCanvas.getContext('2d');
const loupeCtx = loupeCv.getContext('2d');
const frameImg = viewCtx ? viewCtx.createImageData(240, 135) : null;

let padViewMode = 'off';     // 'off' | 'full' | 'loupe'
let stageLoupeOn = false;
let viewBusy = false;

loupeInsetBtn.onclick = () => {
  stageLoupeOn = !stageLoupeOn;
  loupeInsetBtn.classList.toggle('chip-on', stageLoupeOn);
  loupeWrap.hidden = !stageLoupeOn;
  if (stageLoupeOn) pollFrame();
};

loupeBtn.onclick = () => {
  padViewMode = padViewMode === 'off' ? 'full' : padViewMode === 'full' ? 'loupe' : 'off';
  loupeBtn.textContent = padViewMode === 'off' ? 'Pad View: Off' : padViewMode === 'full' ? 'Pad View: Full' : 'Pad View: Loupe';
  loupeBtn.classList.toggle('chip-on', padViewMode !== 'off');
  if (padViewMode === 'off') {
    viewCanvas.hidden = true;
    padLabel.hidden = false;
  } else {
    pollFrame();
  }
};

async function pollFrame() {
  const wantCrop = stageLoupeOn ? 'loupe' : padViewMode;
  if (wantCrop === 'off' || viewBusy || !pin || !frameImg) return;
  viewBusy = true;
  try {
    const r = await fetch(`/api/frame?crop=${wantCrop}`, {headers: {'X-KVM-PIN': pin}});
    if (r.status === 200) {
      const buf = await r.arrayBuffer();
      if (buf.byteLength === 240 * 135 * 2) {
        const src = new DataView(buf);
        const dst = frameImg.data;
        for (let i = 0, p = 0; i < buf.byteLength; i += 2, p += 4) {
          const w = src.getUint16(i, true);
          dst[p]     = ((w >> 11) & 31) * 255 / 31;
          dst[p + 1] = ((w >> 5) & 63) * 255 / 63;
          dst[p + 2] = (w & 31) * 255 / 31;
          dst[p + 3] = 255;
        }
        if (stageLoupeOn && loupeCtx) loupeCtx.putImageData(frameImg, 0, 0);
        if (padViewMode !== 'off' && viewCtx) {
          viewCtx.putImageData(frameImg, 0, 0);
          viewCanvas.hidden = false;
          padLabel.hidden = true;
        }
      }
    }
  } catch (e) {}
  viewBusy = false;
}
setInterval(pollFrame, 1200);

// ---- Live Desktop Stage (Changed 64x64 JPEG Tiles) --------------------------------------
const PV_MS = {low: 800, med: 400, high: 220};
let pvSeq = 0, pvQuality = 'med', pvBusy = false, pvHasFrame = false;
const pvSelect = document.getElementById('pvq');
const pvSt = document.getElementById('pvst');
const stageEmpty = document.getElementById('stage-empty');

document.getElementById('fs-btn').onclick = () => {
  const el = document.getElementById('stage-well');
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
};

let qWant = null, qAskedAt = 0, userPickedOff = false;
async function askQuality(q) {
  qWant = q; qAskedAt = Date.now();
  const r = await post('/api/quality', {quality: q});
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    pvSt.textContent = 'quality: ' + (j.error || r.status);
    qWant = null;
  }
}
pvSelect.onchange = e => {
  const q = e.target.value;
  userPickedOff = (q === 'off');
  if (q === 'off') {
    pvHasFrame = false;
    stageEmpty.hidden = false;
    stageEmpty.textContent = 'Preview paused (Off)';
  }
  askQuality(q);
};
window.addEventListener('pagehide', () => {
  if (pin && pvSelect.value !== 'off')
    fetch('/api/quality', {method: 'POST', keepalive: true, headers: {'Content-Type': 'application/json', 'X-KVM-PIN': pin}, body: '{"quality":"off"}'});
});

async function pvPoll() {
  const wantQ = pvSelect.value;
  if (wantQ === 'off' || !pin || pvBusy || serialMode === 'shell') {
    if (serialMode === 'shell') pvSt.textContent = 'paused (shell mode)';
    return setTimeout(pvPoll, 500);
  }
  pvBusy = true;
  try {
    const r = await fetch('/api/preview?since=' + pvSeq, {headers: {'X-KVM-PIN': pin}});
    if (r.status === 204) {
      if (!pvHasFrame) pvSt.textContent = 'waiting for helper tiles…';
    } else if (r.ok) {
      const j = await r.json(), c = document.getElementById('pvc'), g = c.getContext('2d');
      if (c.width !== j.w || c.height !== j.h) { c.width = j.w; c.height = j.h; }
      if (j.reset) { g.fillStyle = '#000'; g.fillRect(0, 0, c.width, c.height); }
      const imgs = await Promise.all(j.tiles.map(t => {
        const b = Uint8Array.from(atob(t.jpg), ch => ch.charCodeAt(0));
        return createImageBitmap(new Blob([b], {type: 'image/jpeg'})).then(im => [t, im]);
      }));
      for (const [t, im] of imgs) { g.drawImage(im, t.x, t.y); im.close(); }
      pvSeq = j.seq; pvQuality = j.quality; pvHasFrame = true;
      stageEmpty.hidden = true;
      pvSt.textContent = `${j.w}×${j.h} · ${j.quality}`;
    }
  } catch (e) {}
  pvBusy = false;
  setTimeout(pvPoll, PV_MS[pvQuality] || 400);
}
pvPoll();

function checkQualityAck(s) {
  if (qWant && s.preview_quality === qWant) { qWant = null; return; }
  if (qWant && s.preview_quality === 'off' && qWant !== 'off') {
    pvSt.textContent = 'preview unavailable on host';
    qWant = null;
    return;
  }
  if (qWant && Date.now() - qAskedAt > 5000) { qWant = null; return; }
  if (!userPickedOff && pvSelect.value !== 'off' && !qWant && s.link === 'LINKED' &&
      s.serial_mode === 'helper' && (!s.preview_quality || s.preview_quality === 'off') && Date.now() - qAskedAt > 9000) {
    askQuality(pvSelect.value);
  }
}

// ---- GEEK-Lineage Telemetry Cells & Canvas Sparklines -----------------------------------
const fmtRate = bps => {
  if (bps == null || !Number.isFinite(bps) || bps < 0) return '--';
  if (bps < 999.5) return Math.round(bps) + 'B/s';
  if (bps < 999500) return (bps / 1000).toFixed(bps < 9950 ? 1 : 0) + 'K/s';
  if (bps < 999500000) return (bps / 1e6).toFixed(bps < 9950000 ? 1 : 0) + 'M/s';
  return (bps / 1e9).toFixed(1) + 'G/s';
};
const fmtGB = b => (b == null || !Number.isFinite(b)) ? null : (b >= 1099511627776 ? (b / 1099511627776).toFixed(1) + 'T' : (b / 1073741824).toFixed(b < 10737418240 ? 1 : 0) + 'G');
const fmtUp = s => {
  if (s == null || s < 0) return '--';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
};

const histTemp = [], histRx = [], histTx = [];
const MAX_HIST = 90;

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#5edaf2';
}

function drawSparks() {
  const ct = document.getElementById('sp-temp');
  if (ct) {
    const g = ct.getContext('2d'), w = ct.width, h = ct.height;
    g.clearRect(0, 0, w, h);
    const cLab = cssVar('--lab'), cWarn = cssVar('--warn'), cBad = cssVar('--bad');
    const n = histTemp.length;
    for (let i = 0; i < n; i++) {
      const v = histTemp[i];
      if (!Number.isFinite(v)) continue;
      const t = Math.max(0.08, Math.min(1, (v - 30) / 65));
      const bh = Math.max(2, Math.round(t * (h - 2)));
      const x = w - (n - i) * 2.4;
      if (x < 0) continue;
      g.fillStyle = v >= 82 ? cBad : v >= 68 ? cWarn : cLab;
      g.globalAlpha = 0.35;
      g.fillRect(x, h - bh, 2, bh);
      g.globalAlpha = 1;
      g.fillRect(x, h - bh, 2, 2);
    }
  }
  const cn = document.getElementById('sp-net');
  if (cn) {
    const g = cn.getContext('2d'), w = cn.width, h = cn.height, mid = Math.floor(h / 2);
    g.clearRect(0, 0, w, h);
    g.fillStyle = cssVar('--div');
    g.fillRect(0, mid, w, 1);
    const cRx = cssVar('--rx'), cTx = cssVar('--tx');
    const logR = bps => (!Number.isFinite(bps) || bps <= 500) ? 0 : Math.min(1, Math.max(0, Math.log10(bps / 500) / 5.3));
    const n = histRx.length;
    for (let i = 0; i < n; i++) {
      const x = w - (n - i) * 2.4;
      if (x < 0) continue;
      const rh = Math.round(logR(histRx[i]) * (mid - 1));
      const th = Math.round(logR(histTx[i]) * (mid - 2));
      if (rh > 0) { g.fillStyle = cRx; g.fillRect(x, mid - rh, 2, rh); }
      if (th > 0) { g.fillStyle = cTx; g.fillRect(x, mid + 1, 2, th); }
    }
  }
}

function updatePairBanner(s) {
  const banner = document.getElementById('pair-banner');
  const title = document.getElementById('pair-title');
  const sub = document.getElementById('pair-sub');
  const hs = s.helper_state;
  if (!s.host_connected || s.link === 'LINKED' || hs === 'linked' || hs === 'no_host' || serialMode === 'shell') {
    banner.hidden = true;
    return;
  }
  if (hs === 'watching') {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  if (hs === 'probing') {
    title.textContent = 'Probing target PC over USB serial…';
    sub.textContent = 'Checking for kvm_stub or active helper on /dev/ttyGS0.';
  } else if (hs === 'streaming') {
    title.textContent = `Streaming verified ${s.helper_os || 'host'} helper into RAM…`;
    sub.textContent = 'SHA-256 verified bundle sent over USB serial; awaiting telemetry link.';
  } else if (hs === 'pairing') {
    title.textContent = `Pairing ${s.helper_os || 'target'} host via USB HID…`;
    sub.textContent = 'Installing lightweight USB watcher stub — keep focus on the opened terminal.';
  } else {
    title.textContent = 'Helper not detected on target PC';
    sub.textContent = (s.setup_drive ? 'KVM-SETUP USB drive attached · ' : '') +
      'Keyboard & trackpad are live. Pair once (unlocked desktop) for auto-streamed telemetry & preview.';
  }
}

function updateTelemetry(s) {
  const linked = s.link === 'LINKED';
  const cpuT = s.pc_cpu_temp_c ?? s.pc_temp_c;
  if (linked && cpuT != null) {
    histTemp.push(cpuT); if (histTemp.length > MAX_HIST) histTemp.shift();
    histRx.push(s.pc_rx_bps ?? 0); if (histRx.length > MAX_HIST) histRx.shift();
    histTx.push(s.pc_tx_bps ?? 0); if (histTx.length > MAX_HIST) histTx.shift();
  }
  document.getElementById('tc-temp').textContent = linked && cpuT != null ? `${Math.round(cpuT)}°C` : '--°C';
  document.getElementById('tc-temp-sr').textContent = linked && cpuT != null ? `CPU temperature ${Math.round(cpuT)} degrees Celsius` : 'Offline';
  document.getElementById('tc-load').textContent = linked && s.pc_load1 != null ? `${s.pc_load1} load` : 'offline';
  const sens = [];
  if (s.pc_gpu_temp_c != null) sens.push(`GPU ${Math.round(s.pc_gpu_temp_c)}°`);
  if (s.pc_nvme_temp_c != null) sens.push(`NVMe ${Math.round(s.pc_nvme_temp_c)}°`);
  document.getElementById('tc-sensors').textContent = sens.join(' · ') || (s.pc_gpu_pct != null ? `GPU ${Math.round(s.pc_gpu_pct)}%` : '');

  const cores = linked ? (s.pc_cores_all || s.pc_cores || []) : [];
  document.getElementById('tc-ncpu').textContent = linked && s.pc_ncpu ? `${s.pc_ncpu} threads` : '--';
  const cEl = document.getElementById('tc-cores');
  cEl.innerHTML = cores.slice(0, 24).map(v => {
    const col = v >= 85 ? 'var(--bad)' : v >= 65 ? 'var(--warn)' : 'var(--rx)';
    return `<span class="core-bar"><i style="block-size:${Math.round(v)}%;background:${col}"></i></span>`;
  }).join('');

  const pEl = document.getElementById('tc-procs');
  const procs = linked && Array.isArray(s.pc_procs) ? s.pc_procs.slice(0, 4) : [];
  if (procs.length) {
    pEl.innerHTML = procs.map(([n, p]) => `<div class="proc-row"><span>${n.replace(/</g, '&lt;')}</span><span>${Math.round(p)}%</span></div>`).join('');
  } else if (linked && s.pc_top_proc) {
    pEl.innerHTML = `<div class="proc-row"><span>${s.pc_top_proc.replace(/</g, '&lt;')}</span><span>${s.pc_top_cpu_pct != null ? Math.round(s.pc_top_cpu_pct) + '%' : ''}</span></div>`;
  } else {
    pEl.innerHTML = `<div class="t-sub">${linked ? 'idle' : 'waiting for helper…'}</div>`;
  }

  const mUsed = fmtGB(s.pc_mem_used), mTot = fmtGB(s.pc_mem_total);
  document.getElementById('tc-mem').textContent = linked
    ? (mUsed && mTot ? `RAM ${mUsed} / ${mTot}` : s.pc_mem_pct != null ? `RAM ${s.pc_mem_pct}%` : 'RAM --')
    : 'RAM --';
  const dUsed = fmtGB(s.pc_disk_used), dTot = fmtGB(s.pc_disk_total);
  document.getElementById('tc-disk').textContent = linked
    ? (dUsed && dTot ? `${s.pc_disk_dev || 'DISK'} ${dUsed} / ${dTot}` : s.pc_disk_pct != null ? `DISK ${s.pc_disk_pct}%` : 'DISK --')
    : `Zero ${s.temp_c != null ? s.temp_c + '°C' : ''} · up ${fmtUp(s.uptime_s)}`;
  document.getElementById('tc-up').textContent = linked ? `up ${fmtUp(s.pc_uptime_s)}` : '';
  document.getElementById('tc-host').textContent = linked
    ? [s.pc_host, s.pc_net_ip || s.pc_ip].filter(Boolean).join(' · ')
    : `Zero IP ${s.tailscale_ip || s.ip || '--'}`;

  document.getElementById('tc-iface').textContent = (linked && s.pc_net_iface) ? s.pc_net_iface : 'Network';
  document.getElementById('tc-rates').textContent = linked ? `↓${fmtRate(s.pc_rx_bps)} ↑${fmtRate(s.pc_tx_bps)}` : '↓-- ↑--';
  const npEl = document.getElementById('tc-netp');
  const netp = linked && Array.isArray(s.pc_net_procs) ? s.pc_net_procs.slice(0, 2) : [];
  npEl.innerHTML = netp.map(([n, b]) => `<div class="proc-row"><span>${n.replace(/</g, '&lt;')}</span><span>${fmtRate(b)}</span></div>`).join('');

  drawSparks();
}

async function poll() {
  const pill = document.getElementById('pill'), ip = document.getElementById('ip');
  try {
    const s = await (await fetch('/api/status', {headers: pin ? {'X-KVM-PIN': pin} : {}})).json();
    if (s.pin_ok === false && pin) {
      pin = '';
      localStorage.removeItem('kvmPin');
      closeWs();
      openPinDialog('Wrong PIN — please re-enter:');
    }
    if (s.ui_theme && s.ui_theme !== currentTheme) applyTheme(s.ui_theme, false);

    if (s.host_suspended) {
      pill.textContent = 'SLEEP (S3)';
      pill.className = 'pill suspended';
    } else {
      pill.textContent = s.link || '…';
      pill.className = 'pill ' + (s.link === 'LINKED' ? 'linked' : s.link === 'KEYS' ? 'keys' : s.link === 'NO HOST' ? 'nohost' : '');
    }
    showLeds(s.leds);

    const barParts = [s.pc_host || s.pc_ip || s.ip || ''];
    if (s.link === 'LINKED') {
      const ct = s.pc_cpu_temp_c ?? s.pc_temp_c;
      if (ct != null) barParts.push(`${Math.round(ct)}°C`);
      if (s.pc_load1 != null) barParts.push(`ld ${s.pc_load1}`);
      if (s.pc_rx_bps != null) barParts.push(`↓${fmtRate(s.pc_rx_bps)} ↑${fmtRate(s.pc_tx_bps)}`);
    } else if (s.host_suspended) {
      barParts.push('Host suspended — open ⚡ Menu → Wake Host');
    }
    ip.textContent = barParts.filter(Boolean).join(' · ');

    showMode(s);
    updatePairBanner(s);
    updateTelemetry(s);
    checkQualityAck(s);
    if (pin && (!ws || ws.readyState === WebSocket.CLOSED)) connectWs();
  } catch (e) {
    pill.textContent = 'OFFLINE';
    pill.className = 'pill';
    ip.textContent = 'Zero unreachable';
  }
}
setInterval(poll, 2000);
poll();
