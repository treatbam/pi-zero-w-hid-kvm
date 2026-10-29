// Trackpad-first KVM cockpit (Phone Portrait, Touch-Ergonomic Tablet Mode, Wide Split Cockpit & Full Stats Dashboard).
// Shares the GEEK visual lineage (lifted charcoal tiles, bone figures until warm, mint/indigo telemetry),
// low-latency /api/ws WebSocket HID transport with HTTP/1.1 keep-alive fallback, 2-finger scroll + right-tap,
// sticky/locked modifiers, Drag-Lock, acceleration curves, and supervised 1-tap target host pairing.

const escHtml = s => String(s ?? '')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

let noteTimer = null;
const statusNote = msg => {
  const el = document.getElementById('pwr');
  if (!el) return;
  el.textContent = msg || '';
  if (noteTimer) clearTimeout(noteTimer);
  if (msg) noteTimer = setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 6500);
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

// ---- Responsive & Explicit Layout Mode (Auto | Tablet | Phone | Desktop) ----------------
let layoutPref = localStorage.getItem('kvmLayout') || 'auto';
if (!['auto', 'tablet', 'mobile', 'desktop'].includes(layoutPref)) layoutPref = 'auto';

function computeResolvedLayout() {
  if (layoutPref !== 'auto') return layoutPref;
  const w = window.innerWidth;
  if (w < 680) return 'mobile';
  if (w < 960) return 'tablet';
  const coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
  if (coarse && w <= 1280) return 'tablet';
  return 'desktop';
}

function syncLayoutUI() {
  const resolved = computeResolvedLayout();
  document.body.dataset.layout = layoutPref;
  document.body.dataset.resolvedLayout = resolved;

  const tabBtn = document.getElementById('tablet-btn');
  if (tabBtn) {
    const isTab = resolved === 'tablet';
    tabBtn.classList.toggle('chip-on', isTab);
    tabBtn.setAttribute('aria-pressed', isTab ? 'true' : 'false');
    tabBtn.textContent = layoutPref === 'tablet' ? 'Tablet ●' : 'Tablet';
  }
  document.querySelectorAll('[data-layout-pick]').forEach(btn => {
    btn.classList.toggle('chip-on', btn.dataset.layoutPick === layoutPref);
  });
  syncDeckButtons();
  requestAnimationFrame(drawSparks);
}

function setLayoutPref(pref, announce) {
  if (!['auto', 'tablet', 'mobile', 'desktop'].includes(pref)) return;
  layoutPref = pref;
  localStorage.setItem('kvmLayout', pref);
  syncLayoutUI();
  if (announce) {
    const resolved = document.body.dataset.resolvedLayout;
    const names = {tablet: 'Tablet touch layout', mobile: 'Phone compact layout', desktop: 'Desktop split cockpit'};
    statusNote(pref === 'auto' ? `Layout: Auto (${names[resolved] || resolved})` : `Layout: ${names[pref] || pref}`);
  }
}

document.getElementById('tablet-btn').onclick = () => {
  const cur = document.body.dataset.resolvedLayout;
  if (cur === 'tablet') {
    setLayoutPref(window.innerWidth < 680 ? 'mobile' : 'desktop', true);
  } else {
    setLayoutPref('tablet', true);
  }
};
document.querySelectorAll('[data-layout-pick]').forEach(btn => {
  btn.onclick = () => {
    setLayoutPref(btn.dataset.layoutPick, true);
    menuDlg.close();
  };
});
window.addEventListener('resize', () => syncLayoutUI());

// ---- Deck & Drawer Switching ------------------------------------------------------------
let activeDeck = 'pad';
let prevConsoleDeck = 'pad';
let activeDrawer = 'pad';

function syncDeckButtons() {
  const resolved = document.body.dataset.resolvedLayout;
  document.querySelectorAll('[data-deck-btn]').forEach(x => {
    const d = x.dataset.deckBtn;
    const on = (resolved === 'desktop' && activeDeck === 'keys' && d === 'pad') ? true : (d === activeDeck);
    x.classList.toggle('chip-on', on);
    x.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}

function setDeck(deck) {
  if (!['pad', 'keys', 'stats', 'term'].includes(deck)) return;
  if (activeDeck !== 'stats' && deck === 'stats') prevConsoleDeck = activeDeck;
  activeDeck = deck;
  document.body.dataset.deck = activeDeck;
  syncDeckButtons();
  requestAnimationFrame(drawSparks);
}

document.querySelectorAll('[data-deck-btn]').forEach(btn => {
  btn.onclick = () => setDeck(btn.dataset.deckBtn);
});
document.querySelectorAll('[data-open-stats]').forEach(el => {
  el.onclick = () => setDeck('stats');
});
document.getElementById('stats-back').onclick = () => setDeck(prevConsoleDeck || 'pad');
document.getElementById('st-refresh').onclick = () => {
  poll();
  statusNote('Telemetry refreshed');
};

document.querySelectorAll('[data-drawer-btn]').forEach(btn => {
  btn.onclick = () => {
    activeDrawer = btn.dataset.drawerBtn;
    document.body.dataset.drawer = activeDrawer;
    document.querySelectorAll('[data-drawer-btn]').forEach(x => {
      const on = x === btn;
      x.classList.toggle('chip-on', on);
      x.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  };
});

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
  requestAnimationFrame(drawSparks);
  if (pushServer && pin) post('/api/theme', {theme: t}).catch(() => {});
}
applyTheme(currentTheme, false);
document.querySelectorAll('[data-theme-pick]').forEach(btn => {
  btn.onclick = () => applyTheme(btn.dataset.themePick, true);
});
syncLayoutUI();

// ---- Low-Latency WebSocket Transport (/api/ws) + HTTP/1.1 Fallback ----------------------
let ws = null, wsAuthed = false, wsRtt = null, wsPingTimer = null, wsRetryTimer = null;
const xportEl = document.getElementById('xport');

function xportText() {
  if (wsAuthed && ws && ws.readyState === WebSocket.OPEN) {
    return wsRtt != null ? `WS ${wsRtt}ms` : 'WS live';
  }
  return 'HTTP/1.1';
}

function updateXportBadge() {
  const live = wsAuthed && ws && ws.readyState === WebSocket.OPEN;
  xportEl.textContent = xportText();
  xportEl.classList.toggle('ws-live', live);
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
updateXportBadge();

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

async function resetNetHist() {
  if (!confirm('Reset saved cumulative network history for this PC?')) return;
  menuDlg.close();
  const r = await post('/api/net_hist/reset', {});
  const j = await r.json().catch(() => ({}));
  statusNote(r.ok ? 'Network history reset' : `History reset: ${j.error || r.status}`);
  if (r.ok) poll();
}
document.getElementById('rst-hist').onclick = resetNetHist;
document.getElementById('st-rst-hist').onclick = resetNetHist;

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

// ---- Serial Mode Toggle & Integrated Serial Terminal ------------------------------------
let serialMode = null;
function showMode(s) {
  const b = document.getElementById('mode');
  serialMode = s.serial_mode;
  const pend = s.serial_mode_requested && s.serial_mode_requested !== s.serial_mode;
  b.textContent = 'serial: ' + (serialMode || 'idle') + (pend ? ' → ' + s.serial_mode_requested : ' ⇄');
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
const termHist = [];
let termHistIdx = -1;

function isTermVisible() {
  const resolved = document.body.dataset.resolvedLayout;
  if (activeDeck === 'stats') return false;
  if (activeDeck === 'term') return true;
  if (resolved === 'desktop' && activeDrawer === 'term') return true;
  return false;
}

async function pollInlineTerm() {
  if (!isTermVisible() || !pin) {
    setTimeout(pollInlineTerm, 900);
    return;
  }
  try {
    const r = await fetch('/api/term?n=160', {headers: {'X-KVM-PIN': pin}});
    if (r.ok) {
      const j = await r.json();
      const txt = (j.lines || []).concat([j.partial || '']).join('\n');
      termSt.textContent = (j.open ? '● ttyGS0 open' : '○ ' + (j.error || 'serial closed')) + (serialMode ? ` (${serialMode})` : '');
      if (txt !== lastTermTxt) {
        const stick = termOut.scrollTop + termOut.clientHeight >= termOut.scrollHeight - 28;
        termOut.textContent = lastTermTxt = txt;
        if (stick) termOut.scrollTop = termOut.scrollHeight;
      }
    }
  } catch (e) {}
  setTimeout(pollInlineTerm, 700);
}
pollInlineTerm();

async function sendTerm(text, newline) {
  if (newline && text.trim()) {
    if (termHist[termHist.length - 1] !== text) termHist.push(text);
    if (termHist.length > 40) termHist.shift();
    termHistIdx = termHist.length;
  }
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
  } else if (e.key === 'ArrowUp' && termHist.length) {
    e.preventDefault();
    termHistIdx = Math.max(0, (termHistIdx < 0 ? termHist.length : termHistIdx) - 1);
    termCmd.value = termHist[termHistIdx] || '';
  } else if (e.key === 'ArrowDown' && termHist.length) {
    e.preventDefault();
    termHistIdx = Math.min(termHist.length, termHistIdx + 1);
    termCmd.value = termHist[termHistIdx] || '';
  }
});
document.querySelectorAll('[data-tctl]').forEach(btn => {
  btn.onclick = () => sendTerm(btn.dataset.tctl, false);
});
document.getElementById('term-hist-up').onclick = () => {
  if (!termHist.length) return;
  termHistIdx = Math.max(0, (termHistIdx <= 0 ? termHist.length : termHistIdx) - 1);
  termCmd.value = termHist[termHistIdx] || '';
  termCmd.focus();
};
document.getElementById('term-copy').onclick = () => {
  if (!lastTermTxt || !navigator.clipboard) return;
  navigator.clipboard.writeText(lastTermTxt).then(
    () => statusNote('Copied serial buffer to clipboard'),
    () => statusNote('Clipboard copy blocked by browser')
  );
};

// ---- Tactile 16-Column Keyboard + Sticky/Locked Modifiers -------------------------------
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

// Modifiers: 0 = off, 1 = one-shot latched, 2 = double-tap locked
const modState = {shift: 0, ctrl: 0, alt: 0, win: 0};
const modTapAt = {shift: 0, ctrl: 0, alt: 0, win: 0};
let fnOn = false;

function toggleModifier(m) {
  const now = performance.now();
  if (modState[m] === 1 && now - modTapAt[m] < 380) {
    modState[m] = 2; // double-tap locks modifier
    statusNote(`${m.toUpperCase()} locked (tap again to release)`);
  } else if (modState[m] > 0) {
    modState[m] = 0;
  } else {
    modState[m] = 1;
  }
  modTapAt[m] = now;
  drawKeys();
}

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
    const st = modState[m] || 0;
    btn.classList.toggle('latched', st === 1);
    btn.classList.toggle('locked', st === 2);
  });
}

function face(k) {
  if (k.kind !== 'key') return k.label;
  const sh = modState.shift > 0;
  if (sh && SYM[k.key]) return SYM[k.key];
  if (sh && k.key.length === 1 && k.key >= 'a' && k.key <= 'z') return k.key.toUpperCase();
  return k.label;
}

function modClass(k) {
  if (k.kind === 'fn') return fnOn ? 'latched' : '';
  if (k.kind in modState) {
    return modState[k.kind] === 2 ? 'locked' : modState[k.kind] === 1 ? 'latched' : '';
  }
  return '';
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
      b.className = [k.cls || '', modClass(k)].filter(Boolean).join(' ');
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
  if (k.kind in modState) { toggleModifier(k.kind); return; }
  if (k.kind === 'macro') { post('/api/macro', {name: k.key}); return; }
  if (k.kind === 'power') { power(k.target, k.action, k.confirm); return; }
  const mods = [];
  if (modState.shift > 0) mods.push('shift');
  if (modState.ctrl > 0) mods.push('ctrl');
  if (modState.alt > 0) mods.push('alt');
  if (modState.win > 0) mods.push('gui');
  sendKeyCombo([k.key], mods);
  let changed = false;
  for (const m of ['shift', 'ctrl', 'alt', 'win']) {
    if (modState[m] === 1) { modState[m] = 0; changed = true; }
  }
  if (changed) drawKeys();
}
drawKeys();

document.querySelectorAll('[data-qk]').forEach(btn => {
  btn.onclick = () => onKey({kind: 'key', key: btn.dataset.qk});
});
document.querySelectorAll('[data-qm]').forEach(btn => {
  btn.onclick = () => toggleModifier(btn.dataset.qm);
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

async function runCapsPing() {
  menuDlg.close();
  const topBtn = document.getElementById('ping-top');
  statusNote('Pinging host via CapsLock LED…');
  const r = await post('/api/ping', {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { statusNote(`Ping: ${j.error || r.status}`); return; }
  if (j.leds) showLeds(j.leds);
  if (topBtn && j.alive) topBtn.textContent = `Ping ${j.latency_ms}ms`;
  statusNote(j.alive ? `Host alive: USB LED echo in ${j.latency_ms} ms` : 'Host unresponsive (no LED echo)');
}
document.getElementById('ping').onclick = runCapsPing;
document.getElementById('ping-top').onclick = runCapsPing;

// ---- Relative Trackpad: WS + HTTP/1.1, Multi-Touch, Accel & Drag-Lock -------------------
let held = 0;
let dragLock = false;
let naturalScroll = localStorage.getItem('kvmNaturalScroll') !== '0';
const ACCELS = [
  {label: '1.0x', base: 1.0, k: 0.0, cap: 1.0},
  {label: '1.6x', base: 1.05, k: 0.08, cap: 2.3},
  {label: '2.2x', base: 1.25, k: 0.13, cap: 3.4},
];
let accelIdx = 1;

const pad = document.getElementById('surface');
const touchRing = document.getElementById('pad-touch-ring');
const dragLockBtn = document.getElementById('drag-lock');
const accelBtn = document.getElementById('accel-btn');
const scrollDirBtn = document.getElementById('scroll-dir-btn');

function syncScrollBtn() {
  scrollDirBtn.textContent = naturalScroll ? '2F Scroll' : '2F Classic';
  scrollDirBtn.classList.toggle('chip-on', naturalScroll);
}
syncScrollBtn();

scrollDirBtn.onclick = () => {
  naturalScroll = !naturalScroll;
  localStorage.setItem('kvmNaturalScroll', naturalScroll ? '1' : '0');
  syncScrollBtn();
  statusNote(naturalScroll ? 'Two-finger scroll: Natural' : 'Two-finger scroll: Classic');
};

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
  accelBtn.classList.toggle('chip-on', accelIdx > 0);
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

function updateTouchRing(clientX, clientY, show) {
  if (!touchRing) return;
  if (!show) { touchRing.classList.remove('active'); return; }
  const r = pad.getBoundingClientRect();
  const x = Math.max(14, Math.min(r.width - 14, clientX - r.left));
  const y = Math.max(14, Math.min(r.height - 14, clientY - r.top));
  touchRing.style.transform = `translate3d(${x}px, ${y}px, 0)`;
  touchRing.classList.add('active');
}

pad.addEventListener('pointerdown', e => {
  ptrs.set(e.pointerId, {x: e.clientX, y: e.clientY});
  try { pad.setPointerCapture(e.pointerId); } catch (_) {}
  updateTouchRing(e.clientX, e.clientY, true);
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
  updateTouchRing(e.clientX, e.clientY, true);
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
    gesture.scrollCarry += ry / ptrs.size;
    const step = 18;
    if (Math.abs(gesture.scrollCarry) >= step) {
      const ticks = Math.trunc(gesture.scrollCarry / step);
      gesture.scrollCarry -= ticks * step;
      acc.w += naturalScroll ? -ticks : ticks;
      scheduleFlush();
    }
  }
});

function onPtrEnd(e) {
  if (!ptrs.has(e.pointerId)) return;
  ptrs.delete(e.pointerId);
  if (ptrs.size === 0) {
    updateTouchRing(0, 0, false);
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
  if (e.target.closest('#pad, #kb-wrap, .topbar, .quick-strip, .deck-tabs')) e.preventDefault();
});

// ---- 240x135 Still & 1:1 Cursor Loupe (Stage Inset + Optional Trackpad Overlay) ---------
const viewCanvas = document.getElementById('view');
const padCenter = document.getElementById('pad-center');
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
    padCenter.hidden = false;
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
          padCenter.hidden = true;
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
const stageEmptyMsg = document.getElementById('stage-empty-msg');

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
    stageEmptyMsg.textContent = 'Desktop preview paused (select Low / Med / High above)';
  }
  askQuality(q);
};
window.addEventListener('pagehide', () => {
  if (pin && pvSelect.value !== 'off')
    fetch('/api/quality', {method: 'POST', keepalive: true, headers: {'Content-Type': 'application/json', 'X-KVM-PIN': pin}, body: '{"quality":"off"}'});
});

async function pvPoll() {
  const wantQ = pvSelect.value;
  if (wantQ === 'off' || !pin || pvBusy || serialMode === 'shell' || activeDeck === 'stats') {
    if (serialMode === 'shell') pvSt.textContent = 'paused (shell mode)';
    return setTimeout(pvPoll, 500);
  }
  pvBusy = true;
  try {
    const r = await fetch('/api/preview?since=' + pvSeq, {headers: {'X-KVM-PIN': pin}});
    if (r.status === 204) {
      if (!pvHasFrame) pvSt.textContent = 'awaiting helper tiles…';
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

// ---- Formatting & GEEK Heat-Ramp Helpers ------------------------------------------------
const fmtRate = bps => {
  if (bps == null || !Number.isFinite(bps) || bps < 0) return '--';
  if (bps < 999.5) return Math.round(bps) + ' B/s';
  if (bps < 999500) return (bps / 1000).toFixed(bps < 9950 ? 1 : 0) + ' K/s';
  if (bps < 999500000) return (bps / 1e6).toFixed(bps < 9950000 ? 1 : 0) + ' M/s';
  return (bps / 1e9).toFixed(1) + ' G/s';
};
const fmtBytes = b => {
  if (b == null || !Number.isFinite(b) || b < 0) return '--';
  if (b < 1024) return Math.round(b) + 'B';
  if (b < 1048576) return (b / 1024).toFixed(b < 10240 ? 1 : 0) + 'K';
  if (b < 1073741824) return (b / 1048576).toFixed(b < 10485760 ? 1 : 0) + 'M';
  if (b < 1099511627776) return (b / 1073741824).toFixed(b < 10737418240 ? 1 : 0) + 'G';
  return (b / 1099511627776).toFixed(2) + 'T';
};
const fmtGBPair = (used, tot) => {
  if (used == null || tot == null || !Number.isFinite(used) || !Number.isFinite(tot) || tot <= 0) return null;
  if (tot >= 1099511627776) {
    return {u: (used / 1099511627776).toFixed(2), t: (tot / 1099511627776).toFixed(2), unit: 'T'};
  }
  const dec = tot < 10737418240 ? 1 : 1;
  return {u: (used / 1073741824).toFixed(dec), t: Math.round(tot / 1073741824), unit: 'G'};
};
const fmtUp = s => {
  if (s == null || s < 0) return '--';
  if (s < 60) return Math.floor(s) + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  if (s < 86400) return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  return Math.floor(s / 86400) + 'd ' + Math.floor((s % 86400) / 3600) + 'h';
};

const histTemp = [], histLoad = [], histRx = [], histTx = [], histZeroLoad = [], histGeekRssi = [];
const MAX_HIST = 240; // 240 * 2s = 8 minutes (matches GEEK HIST = 240!)

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#989898';
}
function hexToRgba(hex, a) {
  let h = (hex || '').trim().replace('#', '');
  if (h.length === 3) h = h.split('').map(c => c + c).join('');
  const n = parseInt(h.slice(0, 6), 16);
  if (!Number.isFinite(n)) return `rgba(240,240,240,${a})`;
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

// Cool figures stay bone (--fig) until `warm`, then warn → hot → bad. Same stops as the GEEK vHeat ramp.
function heatColorCss(val, warm, hot) {
  if (val == null || !Number.isFinite(val)) return 'var(--fig)';
  if (val >= hot) return 'var(--bad)';
  if (val >= (warm + hot) * 0.5) return 'var(--hot)';
  if (val >= warm) return 'var(--warn)';
  return 'var(--fig)';
}
function meterColorCss(val, warm, hot) {
  const c = heatColorCss(val, warm, hot);
  return c === 'var(--fig)' ? 'color-mix(in oklab, var(--fig) 55%, transparent)' : c;
}
let loadTop = 4;

function prepHiDpiCanvas(cv) {
  if (!cv) return null;
  const r = cv.getBoundingClientRect();
  if (r.width < 8 || r.height < 8) return null;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const pw = Math.round(r.width * dpr), ph = Math.round(r.height * dpr);
  if (cv.width !== pw || cv.height !== ph) {
    cv.width = pw;
    cv.height = ph;
  }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, r.width, r.height);
  return {g, w: r.width, h: r.height};
}

function hexRgb(hex) {
  let h = (hex || '').trim().replace('#', '');
  if (h.length === 3) h = h.split('').map(c => c + c).join('');
  const n = parseInt(h.slice(0, 6), 16);
  return Number.isFinite(n) ? [(n >> 16) & 255, (n >> 8) & 255, n & 255] : [240, 240, 240];
}
// Heat colour for a level 0..1: green when low, yellow halfway, red at the top of the range.
// `invert` flips it where higher is better (Wi-Fi signal). Same ramp as the panel.
let heatStops = null;   // [ok, warn, bad] as RGB, read from the theme once per redraw
function heatColor(t, alpha, invert, lift = 0) {
  if (!heatStops) heatStops = ['--ok', '--warn', '--bad'].map(v => hexRgb(cssVar(v)));
  t = Math.max(0, Math.min(1, invert ? 1 - t : t));
  const [ca, cb] = t < 0.5 ? [heatStops[0], heatStops[1]] : [heatStops[1], heatStops[2]];
  const u = t < 0.5 ? t * 2 : (t - 0.5) * 2;
  const ch = ca.map((v, i) => {
    const m = v + (cb[i] - v) * u;
    return Math.round(m + (255 - m) * lift);
  });
  return `rgba(${ch[0]},${ch[1]},${ch[2]},${alpha})`;
}
function seriesPoints(series, w, h, map) {
  const n = series.length;
  const pts = [];
  for (let i = 0; i < n; i++) {
    const v = series[i];
    if (!Number.isFinite(v)) continue;
    const x = n === 1 ? w - 1 : (i / (n - 1)) * (w - 1);
    const t = Math.max(0, Math.min(1, map(v)));
    pts.push({x, y: h - 1 - t * (h - 1), t});
  }
  return pts;
}
// Each slice under the line takes the heat colour of its own sample, strongest just under the
// line and fading toward the bottom.
function fillUnder(g, pts, h, invert) {
  // Slice edges sit on whole pixels so neighbouring slices neither overlap nor leave a gap.
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    const x0 = Math.round(a.x), x1 = Math.round(b.x);
    if (x1 <= x0) continue;
    const top = Math.min(a.y, b.y);
    const grad = g.createLinearGradient(0, top, 0, h);
    grad.addColorStop(0, heatColor(b.t, 0.62, invert));
    grad.addColorStop(1, heatColor(b.t, 0.1, invert));
    g.beginPath();
    g.moveTo(x0, h);
    g.lineTo(x0, a.y);
    g.lineTo(x1, b.y);
    g.lineTo(x1, h);
    g.closePath();
    g.fillStyle = grad;
    g.fill();
  }
}
// The history line in a brighter tint of the same heat colours, on a dark drop shadow.
function heatLine(g, pts, invert) {
  if (pts.length < 2) return;
  g.lineJoin = 'round';
  g.lineCap = 'round';
  for (const [dy, width] of [[1.25, 2.75], [0, 1.75]]) {
    for (let i = 1; i < pts.length; i++) {
      g.beginPath();
      g.moveTo(pts[i - 1].x, pts[i - 1].y + dy);
      g.lineTo(pts[i].x, pts[i].y + dy);
      g.strokeStyle = dy ? 'rgba(0,0,0,0.7)' : heatColor(pts[i].t, 1, invert, 0.35);
      g.lineWidth = width;
      g.stroke();
    }
  }
}
// Second series as a plain line with a dark drop shadow (upload, load).
function shadowLine(g, pts, color, width) {
  if (pts.length < 2) return;
  const path = () => {
    g.beginPath();
    pts.forEach((p, i) => { if (i) g.lineTo(p.x, p.y); else g.moveTo(p.x, p.y); });
  };
  g.lineJoin = 'round';
  g.lineCap = 'round';
  g.save();
  g.translate(0, 1.25);
  path();
  g.strokeStyle = 'rgba(0,0,0,0.7)';
  g.lineWidth = width + 1;
  g.stroke();
  g.restore();
  path();
  g.strokeStyle = color;
  g.lineWidth = width;
  g.stroke();
}
// Filled history on the plain cell, coloured by each sample's level (green low, red high).
// `extra` is an optional second, dimmer line (upload, load).
function drawHeatGraph(cv, series, map, {invert = false, extra = null, extraMap = map} = {}) {
  const ctx = prepHiDpiCanvas(cv);
  if (!ctx) return;
  const {g, w, h} = ctx;
  const pts = seriesPoints(series, w, h, map);
  fillUnder(g, pts, h, invert);
  if (extra) shadowLine(g, seriesPoints(extra, w, h, extraMap), hexToRgba(cssVar('--fig'), 0.6), 1.25);
  heatLine(g, pts, invert);
}
const logRate = bps => (!Number.isFinite(bps) || bps <= 300) ? 0 : Math.min(1, Math.max(0, Math.log10(bps / 300) / 5.5));
const tempLevel = v => (v - 40) / 50;
const zeroLoadLevel = v => v / 2;
const rssiLevel = v => (v + 90) / 50;
function drawSparks() {
  heatStops = null;
  drawHeatGraph(document.getElementById('sp-temp'), histTemp, tempLevel, {extra: histLoad, extraMap: v => v / loadTop});
  drawHeatGraph(document.getElementById('sp-net'), histRx, logRate, {extra: histTx});
  if (activeDeck === 'stats') {
    drawHeatGraph(document.getElementById('st-canvas-temp'), histTemp, tempLevel);
    drawHeatGraph(document.getElementById('st-canvas-load'), histLoad, v => v / loadTop);
    drawHeatGraph(document.getElementById('st-canvas-net'), histRx, logRate, {extra: histTx});
    drawHeatGraph(document.getElementById('st-canvas-zload'), histZeroLoad, zeroLoadLevel);
    drawHeatGraph(document.getElementById('st-canvas-geek'), histGeekRssi, rssiLevel, {invert: true});
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
    histLoad.push(s.pc_load1 ?? 0); if (histLoad.length > MAX_HIST) histLoad.shift();
    histRx.push(s.pc_rx_bps ?? 0); if (histRx.length > MAX_HIST) histRx.shift();
    histTx.push(s.pc_tx_bps ?? 0); if (histTx.length > MAX_HIST) histTx.shift();
  }
  histZeroLoad.push(s.load1 ?? NaN); if (histZeroLoad.length > MAX_HIST) histZeroLoad.shift();
  histGeekRssi.push(s.geek_rssi ?? NaN); if (histGeekRssi.length > MAX_HIST) histGeekRssi.shift();

  // ---- Mini Telemetry Strip (4 GEEK Cards under Desktop/Tablet Stage) ----
  const ncpu = s.pc_ncpu || (Array.isArray(s.pc_cores_all) ? s.pc_cores_all.length : null);
  document.getElementById('mc-cpu-hdr').textContent = linked && ncpu ? `CPU · ${ncpu} CORES` : 'CPU · THERMAL';

  const tempEl = document.getElementById('tc-temp');
  tempEl.textContent = linked && cpuT != null ? `${Math.round(cpuT)}°C` : '--°C';
  tempEl.style.color = 'var(--fig)';
  document.getElementById('tc-temp-sr').textContent = linked && cpuT != null ? `CPU temperature ${Math.round(cpuT)} degrees Celsius` : 'Offline';
  document.getElementById('tc-load').textContent = linked && s.pc_load1 != null ? Number(s.pc_load1).toFixed(2) : 'offline';
  if (linked && ncpu) loadTop = Math.max(4, ncpu * 0.5);

  const top = linked && s.pc_top_proc
    ? `${s.pc_top_proc}${s.pc_top_cpu_pct != null ? ` ${Math.round(s.pc_top_cpu_pct)}%` : ''}`
    : '';
  document.getElementById('tc-sensors').textContent = top
    || (linked ? `up ${fmtUp(s.pc_uptime_s)}` : `Zero ${s.temp_c ?? '--'}°C · up ${fmtUp(s.uptime_s)}`);

  const rawCores = linked ? (s.pc_cores_all || s.pc_cores || []) : [];
  const sortedCores = rawCores.slice().sort((a, b) => b - a);
  document.getElementById('tc-ncpu').textContent = linked && ncpu ? `${ncpu}t` : '--';
  const cEl = document.getElementById('tc-cores');
  if (sortedCores.length) {
    cEl.innerHTML = sortedCores.slice(0, 20).map(v => {
      const col = heatColorCss(v, 75, 98);
      const pct = Math.max(6, Math.min(100, Math.round(v)));
      return `<span class="core-bar"><i style="block-size:${pct}%;background:linear-gradient(to top, color-mix(in oklab, var(--ok) 20%, transparent), ${col})"></i></span>`;
    }).join('');
  } else {
    cEl.innerHTML = Array.from({length: 12}, () => `<span class="core-bar"><i style="block-size:6%;opacity:.25"></i></span>`).join('');
  }

  const pEl = document.getElementById('tc-procs');
  const procs = linked && Array.isArray(s.pc_procs) ? s.pc_procs : [];
  if (procs.length) {
    pEl.innerHTML = procs.slice(0, 3).map(([n, p]) => {
      const col = heatColorCss(p, 55, 90);
      const w = Math.max(3, Math.min(100, Math.round(p)));
      return `<div class="proc-row"><span>${escHtml(n)}</span><span style="color:${col};font-weight:700">${Math.round(p)}%</span><i class="proc-line" style="inline-size:${w}%;background:${col}"></i></div>`;
    }).join('');
  } else if (linked && s.pc_top_proc) {
    const p = s.pc_top_cpu_pct ?? 0;
    pEl.innerHTML = `<div class="proc-row"><span>${escHtml(s.pc_top_proc)}</span><span>${Math.round(p)}%</span></div>`;
  } else {
    pEl.innerHTML = `<div class="t-sub">${linked ? 'idle' : 'waiting for helper…'}</div>`;
  }

  // Memory & Disk meters
  const mPair = fmtGBPair(s.pc_mem_used, s.pc_mem_total);
  const memPct = linked ? (s.pc_mem_pct ?? (mPair ? Math.round((s.pc_mem_used / s.pc_mem_total) * 100) : 0)) : 0;
  document.getElementById('tc-up').textContent = linked ? `up ${fmtUp(s.pc_uptime_s)}` : '';
  document.getElementById('tc-mem').innerHTML = linked
    ? (mPair ? `${mPair.u} <small>/ ${mPair.t}${mPair.unit}</small>` : s.pc_mem_pct != null ? `${s.pc_mem_pct}% <small>RAM</small>` : '--')
    : '-- <small>/ --G</small>';
  const barMem = document.getElementById('bar-mem');
  barMem.style.inlineSize = `${Math.max(0, Math.min(100, memPct))}%`;
  barMem.style.background = meterColorCss(memPct, 80, 97);

  const dPair = fmtGBPair(s.pc_disk_used, s.pc_disk_total);
  const diskPct = linked ? (s.pc_disk_pct ?? (dPair ? Math.round((s.pc_disk_used / s.pc_disk_total) * 100) : 0)) : 0;
  const diskDev = (linked && s.pc_disk_dev) ? s.pc_disk_dev : 'disk';
  document.getElementById('tc-disk-lbl').textContent = diskDev;
  document.getElementById('tc-disk-pct').textContent = linked && diskPct ? `${diskPct}%` : '';
  document.getElementById('tc-disk').innerHTML = linked
    ? (dPair ? `${dPair.u} <small>/ ${dPair.t}${dPair.unit}</small>` : s.pc_disk_pct != null ? `${s.pc_disk_pct}% <small>used</small>` : '--')
    : '-- <small>/ --G</small>';
  const barDisk = document.getElementById('bar-disk');
  barDisk.style.inlineSize = `${Math.max(0, Math.min(100, diskPct))}%`;
  barDisk.style.background = meterColorCss(diskPct, 90, 99);

  // Network card + cumulative totals
  document.getElementById('tc-iface').textContent = (linked && s.pc_net_iface) ? s.pc_net_iface : 'NET';
  document.getElementById('tc-net-ip').textContent = linked ? (s.pc_net_ip || s.pc_ip || '') : '';
  document.getElementById('tc-rx').textContent = linked ? `dn ${fmtRate(s.pc_rx_bps)}` : 'dn --';
  document.getElementById('tc-tx').textContent = linked ? `up ${fmtRate(s.pc_tx_bps)}` : 'up --';

  const netHist = Array.isArray(s.net_hist) ? s.net_hist : [];
  let sumRx = 0, sumTx = 0;
  for (const row of netHist) {
    sumRx += Number(row.rx) || 0;
    sumTx += Number(row.tx) || 0;
  }
  document.getElementById('tc-net-tot').textContent = netHist.length
    ? `Total: ↓${fmtBytes(sumRx)} / ↑${fmtBytes(sumTx)}`
    : (linked && Array.isArray(s.pc_net_procs) && s.pc_net_procs[0]
        ? `${s.pc_net_procs[0][0]}: ${fmtRate(s.pc_net_procs[0][1])}`
        : 'Total: -- / --');

  // ---- Full Proper Stats Dashboard (#stats-dash) ----
  document.getElementById('st-host-name').textContent = s.pc_host || (linked ? 'Linked Host' : 'Host Unlinked');
  document.getElementById('st-host-ip').textContent = s.pc_net_ip || s.pc_ip || '--';
  document.getElementById('st-host-os').textContent = [s.helper_os, s.helper_arch].filter(Boolean).join(' · ') || '--';
  document.getElementById('st-host-up').textContent = linked ? fmtUp(s.pc_uptime_s) : '--';
  document.getElementById('st-host-id').textContent = s.pc_id || '--';

  const kCpu = document.getElementById('st-kpi-cpu');
  kCpu.textContent = linked && cpuT != null ? `${cpuT.toFixed(1)}°C` : '--°C';
  kCpu.style.color = 'var(--fig)';
  if (histTemp.length >= 2) {
    const mn = Math.round(Math.min(...histTemp)), mx = Math.round(Math.max(...histTemp));
    document.getElementById('st-kpi-cpu-sub').textContent = `min ${mn}° · peak ${mx}°`;
    document.getElementById('st-temp-range').textContent = `${histTemp.length * 2}s window · min ${mn}°C / peak ${mx}°C`;
  } else {
    document.getElementById('st-kpi-cpu-sub').textContent = 'warm 70°C · red 90°C';
  }

  const kGpu = document.getElementById('st-kpi-gpu');
  kGpu.textContent = linked && s.pc_gpu_temp_c != null ? `${Math.round(s.pc_gpu_temp_c)}°C` : 'n/a';
  kGpu.style.color = 'var(--fig)';
  document.getElementById('st-kpi-gpu-sub').textContent = linked && s.pc_gpu_pct != null ? `busy ${Math.round(s.pc_gpu_pct)}%` : 'sensor optional';
  const kNvme = document.getElementById('st-kpi-nvme');
  kNvme.textContent = linked && s.pc_nvme_temp_c != null ? `${Math.round(s.pc_nvme_temp_c)}°C` : 'n/a';
  kNvme.style.color = 'var(--fig)';
  document.getElementById('st-kpi-load').textContent = linked && s.pc_load1 != null ? Number(s.pc_load1).toFixed(2) : '--';
  document.getElementById('st-heat-temp').textContent = linked && cpuT != null ? `${Math.round(cpuT)}°C` : '--';
  document.getElementById('st-heat-load').textContent = linked && s.pc_load1 != null ? Number(s.pc_load1).toFixed(2) : '--';
  document.getElementById('st-kpi-threads').textContent = linked && ncpu ? `${ncpu} logical threads` : '-- threads';

  document.getElementById('st-mem-pct').textContent = linked ? `${memPct}%` : '--%';
  document.getElementById('st-mem-detail').textContent = linked
    ? (mPair ? `${mPair.u}G used of ${mPair.t}${mPair.unit} (${fmtBytes(Math.max(0, s.pc_mem_total - s.pc_mem_used))} free)` : `${memPct}% used`)
    : 'Waiting for host telemetry';
  const stBarMem = document.getElementById('st-bar-mem');
  stBarMem.style.inlineSize = `${Math.max(0, Math.min(100, memPct))}%`;
  stBarMem.style.background = meterColorCss(memPct, 80, 97);

  document.getElementById('st-disk-title').textContent = linked && s.pc_disk_dev ? `Primary Storage (${s.pc_disk_dev})` : 'Host Primary Storage';
  document.getElementById('st-disk-pct').textContent = linked ? `${diskPct}%` : '--%';
  document.getElementById('st-disk-detail').textContent = linked
    ? (dPair ? `${dPair.u}${dPair.unit} used of ${dPair.t}${dPair.unit} (${fmtBytes(Math.max(0, s.pc_disk_total - s.pc_disk_used))} free)` : `${diskPct}% used`)
    : 'Waiting for host telemetry';
  const stBarDisk = document.getElementById('st-bar-disk');
  stBarDisk.style.inlineSize = `${Math.max(0, Math.min(100, diskPct))}%`;
  stBarDisk.style.background = meterColorCss(diskPct, 90, 99);

  // Pi Zero W Appliance & Radio Link (Page 4)
  document.getElementById('st-zero-ip').textContent = `Zero ${s.ip || '--'}`;
  const zTemp = document.getElementById('st-z-temp');
  zTemp.textContent = s.temp_c != null ? `${s.temp_c}°C` : '--°C';
  zTemp.style.color = 'var(--fig)';
  document.getElementById('st-z-load').textContent = s.load1 != null ? `load ${s.load1}` : 'load --';
  document.getElementById('st-z-mem').textContent = s.mem_pct != null ? `${s.mem_pct}%` : '--%';
  document.getElementById('st-z-up').textContent = `up ${fmtUp(s.uptime_s)}`;
  document.getElementById('st-z-udc').textContent = s.udc_state || '--';
  document.getElementById('st-z-xport').textContent = `${s.hid_ready ? 'HID ready' : 'HID off'} · ${xportText()}`;
  document.getElementById('st-z-rssi').textContent = s.zero_wifi_rssi != null ? `${s.zero_wifi_rssi} dBm` : (pin ? 'n/a' : 'PIN req');
  document.getElementById('st-z-ts').textContent = s.tailscale ? s.tailscale.toUpperCase() : 'n/a';
  document.getElementById('st-z-tsip').textContent = s.tailscale_ip || (s.tailscale === 'down' ? 'interface down' : '--');
  document.getElementById('st-z-helper').textContent = s.helper_state || s.link || '--';
  document.getElementById('st-z-serial').textContent = `serial: ${s.serial_mode || s.mode || 'idle'}`;
  document.getElementById('st-heat-zload').textContent = s.load1 != null ? Number(s.load1).toFixed(2) : '--';
  document.getElementById('st-heat-geek').textContent = s.geek_rssi != null ? `${s.geek_rssi}` : (pin ? '--' : 'PIN req');

  // Per-Core Matrix + Top 8 CPU Processes (Page 2)
  const coreGrid = document.getElementById('st-cores-grid');
  if (rawCores.length) {
    const avgCore = Math.round(rawCores.reduce((a, b) => a + b, 0) / rawCores.length);
    const maxCore = Math.round(Math.max(...rawCores));
    document.getElementById('st-cores-summary').textContent = `${rawCores.length} threads · avg ${avgCore}% · peak ${maxCore}%`;
    coreGrid.innerHTML = rawCores.map((v, idx) => {
      const pct = Math.max(0, Math.min(100, Math.round(v)));
      const col = heatColorCss(pct, 75, 98);
      return `<div class="s-core-tile"><div class="s-core-top"><span>C${idx}</span><b style="color:${col}">${pct}%</b></div><div class="s-core-track"><i style="inline-size:${pct}%;background:${col}"></i></div></div>`;
    }).join('');
  } else {
    document.getElementById('st-cores-summary').textContent = 'awaiting core telemetry';
    coreGrid.innerHTML = `<div class="t-sub" style="grid-column:1/-1;padding:6px">Per-core metrics populate automatically when LINKED.</div>`;
  }

  const cpuTbody = document.getElementById('st-cpu-tbody');
  if (procs.length) {
    cpuTbody.innerHTML = procs.slice(0, 8).map(([n, p]) => {
      const pct = Number(p) || 0;
      const col = heatColorCss(pct, 55, 90);
      const w = Math.max(2, Math.min(100, Math.round(pct)));
      return `<tr>
        <td>${escHtml(n)}</td>
        <td style="inline-size:38%"><div class="s-share-bar"><span style="inline-size:${w}%;background:${col};display:block;block-size:100%"></span></div></td>
        <td class="num" style="color:${col};font-weight:700">${pct.toFixed(pct < 10 ? 1 : 0)}%</td>
      </tr>`;
    }).join('');
  } else {
    cpuTbody.innerHTML = `<tr><td colspan="3" style="color:var(--grey)">${linked ? 'No active CPU processes reported.' : 'Waiting for LINKED telemetry…'}</td></tr>`;
  }

  // Duplex Network & Cumulative History Table (Page 3)
  document.getElementById('st-net-title').textContent = linked && s.pc_net_iface
    ? `Page 3 · ${s.pc_net_iface} Duplex & Traffic History`
    : 'Page 3 · Duplex Network & Traffic History';
  document.getElementById('st-net-rates').textContent = linked
    ? `↓ ${fmtRate(s.pc_rx_bps)} · ↑ ${fmtRate(s.pc_tx_bps)}`
    : '↓-- · ↑--';
  document.getElementById('st-heat-iface').textContent = linked && s.pc_net_iface ? s.pc_net_iface : 'NET';
  document.getElementById('st-heat-rx').textContent = `↓ ${linked ? fmtRate(s.pc_rx_bps) : '--'}`;
  document.getElementById('st-heat-tx').textContent = `↑ ${linked ? fmtRate(s.pc_tx_bps) : '--'}`;

  const liveNetProcs = linked && Array.isArray(s.pc_net_procs) ? s.pc_net_procs : [];
  document.getElementById('st-net-live').textContent = liveNetProcs.length
    ? 'Live sockets: ' + liveNetProcs.slice(0, 4).map(([n, b]) => `${n} (${fmtRate(b)})`).join(' · ')
    : 'Live sockets: quiet';

  if (s.net_hist_since) {
    const ageS = Math.max(0, Math.floor(Date.now() / 1000) - Number(s.net_hist_since));
    document.getElementById('st-hist-since').textContent = `Cumulative TCP traffic (${fmtUp(ageS)} window)`;
  } else {
    document.getElementById('st-hist-since').textContent = 'Cumulative per-process TCP traffic';
  }
  document.getElementById('st-hist-totals').textContent = netHist.length
    ? `Σ ↓${fmtBytes(sumRx)} · ↑${fmtBytes(sumTx)}`
    : 'Σ --';

  const netTbody = document.getElementById('st-net-hist-tbody');
  if (netHist.length) {
    const maxTot = Math.max(1, ...netHist.map(r => (Number(r.rx) || 0) + (Number(r.tx) || 0)));
    netTbody.innerHTML = netHist.map(r => {
      const rx = Number(r.rx) || 0, tx = Number(r.tx) || 0;
      const rxW = Math.round((rx / maxTot) * 100);
      const txW = Math.round((tx / maxTot) * 100);
      return `<tr>
        <td>${escHtml(r.p)}</td>
        <td class="num rx-txt">${fmtBytes(rx)}</td>
        <td class="num tx-txt">${fmtBytes(tx)}</td>
        <td style="inline-size:32%"><div class="s-share-bar"><span class="s-share-rx" style="inline-size:${rxW}%"></span><span class="s-share-tx" style="inline-size:${txW}%"></span></div></td>
      </tr>`;
    }).join('');
  } else {
    netTbody.innerHTML = `<tr><td colspan="4" style="color:var(--grey)">${pin ? 'No cumulative per-process history recorded yet.' : 'Unlock Console PIN to view per-process traffic history.'}</td></tr>`;
  }

  drawSparks();
}

function setGlance(id, state) {
  const el = document.getElementById(id);
  if (el) el.className = 'glance' + (state ? ' ' + state : '');
}
function updateGlance(s) {
  let hid = '';
  if (s) hid = !s.hid_ready ? 'bad' : (s.udc_state && s.udc_state !== 'configured' ? 'warn' : 'ok');
  setGlance('zero-glance', hid);
  const dbm = document.getElementById('radio-dbm');
  let radio = '';
  if (s && s.zero_wifi_rssi != null && Number.isFinite(Number(s.zero_wifi_rssi))) {
    const n = Number(s.zero_wifi_rssi);
    if (dbm) dbm.textContent = String(n);
    radio = n >= -65 ? 'ok' : n >= -78 ? 'warn' : 'bad';
  } else if (dbm) dbm.textContent = '--';
  setGlance('radio-glance', radio);
}

async function poll() {
  const pill = document.getElementById('pill');
  const ip = document.getElementById('ip');
  const barSub = document.getElementById('bar-sub');
  const stageHost = document.getElementById('stage-host');
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
    updateGlance(s);

    const hostLabel = s.pc_host
      ? (s.pc_net_ip || s.pc_ip ? `${s.pc_host} (${s.pc_net_ip || s.pc_ip})` : s.pc_host)
      : (s.pc_ip || s.ip || 'Zero KVM');
    ip.textContent = hostLabel;
    if (stageHost) stageHost.textContent = s.pc_host ? `${s.pc_host} · desktop` : 'Live Desktop';

    const subParts = [];
    if (s.link === 'LINKED') {
      const ct = s.pc_cpu_temp_c ?? s.pc_temp_c;
      if (ct != null) subParts.push(`${Math.round(ct)}°C`);
      if (s.pc_load1 != null) subParts.push(`ld ${s.pc_load1}`);
      if (s.pc_rx_bps != null) subParts.push(`↓${fmtRate(s.pc_rx_bps).replace(' ', '')}`);
      if (s.zero_wifi_rssi != null) subParts.push(`Wi-Fi ${s.zero_wifi_rssi}dBm`);
    } else if (s.host_suspended) {
      subParts.push('USB S3 suspended — open ⚡ Menu → Wake Host');
    } else {
      subParts.push(s.hid_ready ? 'ZERO HID ready' : 'HID init');
      if (s.ip) subParts.push(`Zero ${s.ip}`);
    }
    barSub.textContent = subParts.join(' · ');

    showMode(s);
    updatePairBanner(s);
    updateTelemetry(s);
    checkQualityAck(s);
    if (pin && (!ws || ws.readyState === WebSocket.CLOSED)) connectWs();
  } catch (e) {
    pill.textContent = 'OFFLINE';
    pill.className = 'pill';
    ip.textContent = 'Zero unreachable';
    barSub.textContent = '';
    updateGlance(null);
  }
}
setInterval(poll, 2000);
poll();
