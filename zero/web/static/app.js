// Phone console. Rows are fixed 16-column grids so a label cannot wrap a letter
// onto the next line. Shift changes the glyph and sends the unshifted key plus
// the shift modifier. Fn holds F-keys, macros, and power off the letter rows.
// The status word comes from the Zero (link), the same three words as the GEEK.
let pin = localStorage.getItem('kvmPin') || '';
const askPin = msg => {
  const p = prompt(msg || 'Console PIN');
  if (p !== null) { pin = p.trim(); if (pin) localStorage.setItem('kvmPin', pin); else localStorage.removeItem('kvmPin'); }
};
async function post(u, b) {
  if (!pin) { askPin(); if (!pin) return new Response('{}', {status: 401}); }
  const r = await fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-KVM-PIN': pin}, body: JSON.stringify(b)});
  if (r.status === 401) { pin = ''; localStorage.removeItem('kvmPin'); askPin('Wrong or missing PIN. Console PIN:'); }
  else if (r.status === 429) { alert('Too many wrong PINs. Wait a minute.'); }
  return r;
}
if (!pin) askPin();
document.getElementById('pin').onclick = () => { pin = ''; localStorage.removeItem('kvmPin'); askPin(); };

async function power(target, action, label) {
  if (target === 'zero') { if (prompt(`Type "${action}" to ${label}`) !== action) return; }
  else if (!confirm(`${label}?`)) return;
  const r = await post('/api/power', {target, action, confirm: true});
  const j = await r.json().catch(() => ({}));
  document.getElementById('pwr').textContent = r.ok ? `${label}: sent` : `${label}: ${j.error || r.status}`;
}

// Serial mode toggle: helper (stats + still) <-> login shell on the host (term.html).
let serialMode = null;
function showMode(s) {
  const b = document.getElementById('mode');
  serialMode = s.serial_mode;
  const pend = s.serial_mode_requested && s.serial_mode_requested !== s.serial_mode;
  b.textContent = 'serial: ' + (serialMode || 'no helper') + (pend ? ' \u2192 ' + s.serial_mode_requested + '\u2026' : '');
  b.className = serialMode === 'shell' ? 'shell' : '';
}
document.getElementById('mode').onclick = async () => {
  const want = serialMode === 'shell' ? 'helper' : 'shell';
  if (want === 'shell' && !confirm('Start a login shell on the host serial port? Stats and the still pause.')) return;
  const r = await post('/api/mode', {mode: want});
  const j = await r.json().catch(() => ({}));
  document.getElementById('pwr').textContent = r.ok ? `serial \u2192 ${want}: asked` : `serial mode: ${j.error || r.status}`;
  poll();
};

let held = 0;
let q = Promise.resolve();
const m = o => {
  if (!pin) return q;
  const b = Object.assign({dx: 0, dy: 0, wheel: 0, buttons: held}, o);
  q = q.then(() => post('/api/mouse', b)).catch(() => {}); return q;
};

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
  post('/api/key', {keys: [k.key], mods});
  shiftOn = ctrlOn = altOn = winOn = false;
  drawKeys();
}
drawKeys();

function typeTxt() {
  const t = document.getElementById('txt');
  if (t.value) post('/api/type', {text: t.value});
  t.value = '';
}
document.getElementById('send').onclick = typeTxt;
document.getElementById('txt').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); typeTxt(); } });

document.querySelectorAll('[data-b]').forEach(b => {
  b.onpointerdown = e => { e.preventDefault(); flush(); held |= +b.dataset.b; m({}); };
  b.onpointerup = b.onpointercancel = () => { if (held & +b.dataset.b) { flush(); held &= ~+b.dataset.b; m({}); } };
});

const pad = document.getElementById('surface');
let last = null, acc = {dx: 0, dy: 0}, moved = false;
pad.onpointerdown = e => { last = [e.clientX, e.clientY]; moved = false; pad.setPointerCapture(e.pointerId); };
pad.onpointermove = e => { if (!last) return; acc.dx += e.clientX - last[0]; acc.dy += e.clientY - last[1];
  if (Math.abs(acc.dx) + Math.abs(acc.dy) > 2) moved = true; last = [e.clientX, e.clientY]; };
pad.onpointerup = () => { flush(); if (!moved) { m({buttons: held | 1}); m({}); } last = null; };
pad.onwheel = e => { e.preventDefault(); m({wheel: e.deltaY < 0 ? 1 : -1}); };
const clampMove = v => Math.max(-4096, Math.min(4096, Math.round(v)));
function flush() { if (acc.dx || acc.dy) { const d = {dx: clampMove(acc.dx), dy: clampMove(acc.dy)}; acc = {dx: 0, dy: 0}; m(d); } }
setInterval(flush, 30);

function bindWheel(id, dir) {
  const btn = document.getElementById(id);
  let timer = null;
  const stop = () => { if (timer) { clearInterval(timer); timer = null; } };
  btn.onpointerdown = e => { e.preventDefault(); m({wheel: dir}); timer = setInterval(() => m({wheel: dir}), 120); };
  btn.onpointerup = btn.onpointercancel = btn.onpointerleave = stop;
}
bindWheel('su', 1);
bindWheel('sd', -1);
document.addEventListener('contextmenu', e => { if (e.target.closest('#pad, #kb, header')) e.preventDefault(); });

async function poll() {
  const pill = document.getElementById('pill'), ip = document.getElementById('ip');
  try {
    const s = await (await fetch('/api/status')).json();
    pill.textContent = s.link || '\u2026';
    pill.className = 'pill ' + (s.link === 'LINKED' ? 'linked' : s.link === 'KEYS' ? 'keys' : s.link === 'NO HOST' ? 'nohost' : '');
    ip.textContent = s.ip || '';
    showMode(s);
  } catch (e) {
    pill.textContent = 'offline'; pill.className = 'pill'; ip.textContent = '';
  }
}
setInterval(poll, 2000); poll();
