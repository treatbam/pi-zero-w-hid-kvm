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

document.getElementById('clip-push').onclick = async () => {
  const t = document.getElementById('txt');
  if (!t.value) { document.getElementById('pwr').textContent = 'Clipboard push: enter text first'; return; }
  const r = await post('/api/clipboard', {text: t.value});
  const j = await r.json().catch(() => ({}));
  document.getElementById('pwr').textContent = r.ok ? `Clipboard pushed (${j.bytes} B)` : `Clipboard push: ${j.error || r.status}`;
};

document.getElementById('clip-pull').onclick = async () => {
  if (!pin) { askPin(); if (!pin) return; }
  const r = await fetch('/api/clipboard', {headers: {'X-KVM-PIN': pin}});
  if (r.status === 401) { pin = ''; localStorage.removeItem('kvmPin'); askPin('Wrong or missing PIN. Console PIN:'); return; }
  const j = await r.json().catch(() => ({}));
  if (r.ok) {
    document.getElementById('txt').value = j.text || '';
    if (j.text && navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(j.text).catch(() => {});
    document.getElementById('pwr').textContent = j.text ? `Clipboard pulled (${j.text.length} chars)` : 'Host clipboard empty / unavailable';
  } else {
    document.getElementById('pwr').textContent = `Clipboard pull: ${j.error || r.status}`;
  }
};

function showLeds(leds) {
  const l = leds || {};
  document.getElementById('led-num').className = 'led' + (l.num ? ' on' : '');
  document.getElementById('led-caps').className = 'led' + (l.caps ? ' on' : '');
  document.getElementById('led-scroll').className = 'led' + (l.scroll ? ' on' : '');
}

document.getElementById('ping').onclick = async () => {
  document.getElementById('pwr').textContent = 'Pinging host via CapsLock LED…';
  const r = await post('/api/ping', {});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    document.getElementById('pwr').textContent = `Ping: ${j.error || r.status}`;
    return;
  }
  if (j.leds) showLeds(j.leds);
  document.getElementById('pwr').textContent = j.alive
    ? `Host alive: LED echo in ${j.latency_ms} ms`
    : 'Host frozen / unresponsive (no LED echo)';
};

document.querySelectorAll('[data-b]').forEach(b => {
  b.onpointerdown = e => { e.preventDefault(); flush(true); held |= +b.dataset.b; m({}); };
  b.onpointerup = b.onpointercancel = () => { if (held & +b.dataset.b) { flush(true); held &= ~+b.dataset.b; m({}); } };
});

const pad = document.getElementById('surface');
const viewCanvas = document.getElementById('view');
const padLabel = document.getElementById('pad-label');
const loupeBtn = document.getElementById('loupe');
const viewCtx = viewCanvas.getContext('2d');
const viewImg = viewCtx ? viewCtx.createImageData(240, 135) : null;
let viewMode = 'off'; // 'off' | 'full' | 'loupe'
let viewBusy = false;

loupeBtn.onpointerdown = e => e.stopPropagation();
loupeBtn.onclick = e => {
  e.stopPropagation();
  viewMode = viewMode === 'off' ? 'full' : viewMode === 'full' ? 'loupe' : 'off';
  loupeBtn.textContent = viewMode === 'off' ? 'view: off' : viewMode === 'full' ? 'view: full' : 'view: loupe 1:1';
  loupeBtn.className = viewMode === 'off' ? '' : 'active';
  if (viewMode === 'off') {
    viewCanvas.hidden = true;
    padLabel.textContent = 'TRACKPAD';
    padLabel.hidden = false;
  } else {
    pollFrame();
  }
};

async function pollFrame() {
  if (viewMode === 'off' || viewBusy || !pin || !viewCtx || !viewImg) return;
  viewBusy = true;
  try {
    const r = await fetch(`/api/frame?crop=${viewMode}`, {headers: {'X-KVM-PIN': pin}});
    if (r.status === 200) {
      const buf = await r.arrayBuffer();
      if (buf.byteLength === 240 * 135 * 2 && viewMode !== 'off') {
        const src = new DataView(buf);
        const dst = viewImg.data;
        for (let i = 0, p = 0; i < buf.byteLength; i += 2, p += 4) {
          const w = src.getUint16(i, true);
          dst[p]     = ((w >> 11) & 31) * 255 / 31;
          dst[p + 1] = ((w >> 5) & 63) * 255 / 63;
          dst[p + 2] = (w & 31) * 255 / 31;
          dst[p + 3] = 255;
        }
        viewCtx.putImageData(viewImg, 0, 0);
        viewCanvas.hidden = false;
        padLabel.hidden = true;
      }
    } else if (viewMode !== 'off') {
      viewCanvas.hidden = true;
      padLabel.textContent = 'WAITING FOR STILL…';
      padLabel.hidden = false;
    }
  } catch (e) {
    if (viewMode !== 'off') {
      viewCanvas.hidden = true;
      padLabel.textContent = 'TRACKPAD';
      padLabel.hidden = false;
    }
  } finally {
    viewBusy = false;
  }
}
setInterval(pollFrame, 1500);

let last = null, acc = {dx: 0, dy: 0}, moved = false, dist = 0;
// A tap is a gesture that never travelled: measure the whole gesture, not what is still
// waiting in acc (flush() empties acc every 30 ms, which hid slow drags from this check).
pad.onpointerdown = e => { if (e.target === loupeBtn) return; last = [e.clientX, e.clientY]; moved = false; dist = 0; pad.setPointerCapture(e.pointerId); };
pad.onpointermove = e => { if (!last) return;
  const ddx = e.clientX - last[0], ddy = e.clientY - last[1];
  acc.dx += ddx; acc.dy += ddy; dist += Math.abs(ddx) + Math.abs(ddy);
  if (dist > 3) moved = true; last = [e.clientX, e.clientY]; };
pad.onpointerup = () => { if (!last) return; flush(true); if (!moved) { m({buttons: held | 1}); m({}); } last = null; };
pad.onwheel = e => { e.preventDefault(); m({wheel: e.deltaY < 0 ? 1 : -1}); };
const clampMove = v => Math.max(-4096, Math.min(4096, Math.round(v)));
// Only one motion POST is in flight at a time. Movement that happens meanwhile keeps adding
// up in acc and goes out as one bigger step when the reply returns, so the cursor stops when
// the finger stops instead of replaying a backlog. force=true (button edges, release) sends
// anyway, queued behind the in-flight one so the order stays right.
let inflight = 0;
function flush(force) {
  if ((inflight && !force) || !(acc.dx || acc.dy)) return;
  const d = {dx: clampMove(acc.dx), dy: clampMove(acc.dy)};
  acc = {dx: 0, dy: 0};
  inflight++;
  m(d).finally(() => { inflight--; if (!inflight) flush(); });
}
setInterval(() => flush(), 30);

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

const fmtRate = bps => {
  if (bps == null || !Number.isFinite(bps) || bps < 0) return '--';
  if (bps < 999.5) return Math.round(bps) + 'B';
  if (bps < 999500) return (bps / 1000).toFixed(bps < 9950 ? 1 : 0) + 'K';
  if (bps < 999500000) return (bps / 1e6).toFixed(bps < 9950000 ? 1 : 0) + 'M';
  return (bps / 1e9).toFixed(1) + 'G';
};

// Desktop preview: poll changed JPEG tiles and composite them on a canvas.
// The Zero passes the helper's JPEGs through untouched; this page does the drawing.
const PV_MS = {low: 1000, med: 500, high: 250};
let pvSeq = 0, pvQuality = 'med', pvBusy = false;
async function pvPoll() {
  const box = document.getElementById('pv'), st = document.getElementById('pvst');
  if (!box.open || !pin || pvBusy || serialMode === 'shell') {
    if (serialMode === 'shell') st.textContent = 'paused (shell mode)';
    return setTimeout(pvPoll, 500);
  }
  pvBusy = true;
  try {
    const r = await fetch('/api/preview?since=' + pvSeq, {headers: {'X-KVM-PIN': pin}});
    if (r.status === 204) st.textContent = 'no preview (helper not sending)';
    else if (r.ok) {
      const j = await r.json(), c = document.getElementById('pvc'), g = c.getContext('2d');
      if (c.width !== j.w || c.height !== j.h) { c.width = j.w; c.height = j.h; }
      if (j.reset) { g.fillStyle = '#000'; g.fillRect(0, 0, c.width, c.height); }
      const imgs = await Promise.all(j.tiles.map(t => {
        const b = Uint8Array.from(atob(t.jpg), ch => ch.charCodeAt(0));
        return createImageBitmap(new Blob([b], {type: 'image/jpeg'})).then(im => [t, im]);
      }));
      for (const [t, im] of imgs) { g.drawImage(im, t.x, t.y); im.close(); }
      pvSeq = j.seq; pvQuality = j.quality;
      st.textContent = `${j.w}×${j.h} ${j.quality}, ${j.tiles.length} tiles`;
    } else st.textContent = 'HTTP ' + r.status;
  } catch (e) { st.textContent = 'preview error'; }
  pvBusy = false;
  setTimeout(pvPoll, PV_MS[pvQuality] || 500);
}
// The helper streams nothing (no preview, no extended stats) until asked: send q:<level>
// when the panel opens and q:off when it closes. After a 202, wait for the helper's ack.
let qWant = null, qAskedAt = 0, qAckSeen = null;
async function askQuality(q) {
  const st = document.getElementById('pvst');
  qWant = q; qAskedAt = Date.now();
  const r = await post('/api/quality', {quality: q});
  if (!r.ok) { const j = await r.json().catch(() => ({})); st.textContent = 'quality: ' + (j.error || r.status); qWant = null; }
}
document.getElementById('pvq').onchange = e => askQuality(e.target.value);
document.getElementById('pv').addEventListener('toggle', e => askQuality(e.target.open ? document.getElementById('pvq').value : 'off'));
window.addEventListener('pagehide', () => { if (document.getElementById('pv').open && pin)
  fetch('/api/quality', {method: 'POST', keepalive: true, headers: {'Content-Type': 'application/json', 'X-KVM-PIN': pin}, body: '{"quality":"off"}'}); });
function checkQualityAck(s) {
  const st = document.getElementById('pvst'), open = document.getElementById('pv').open;
  if (qWant && s.preview_quality === qWant) { qWant = null; return; }
  if (qWant && s.preview_quality === 'off' && qWant !== 'off') { st.textContent = 'preview unavailable on the PC (no Pillow/ffmpeg/X)'; qWant = null; return; }
  if (qWant && Date.now() - qAskedAt > 5000) { st.textContent = `no ack from the helper for "${qWant}" (is kvm_helper running?)`; qWant = null; return; }
  // helper restarted while the panel is open: it forgot the level, ask again (at most every 10 s)
  if (open && !qWant && s.serial_mode === 'helper' && s.preview_quality == null && Date.now() - qAskedAt > 10000)
    askQuality(document.getElementById('pvq').value);
}
function showStats(s) {
  const el = document.getElementById('pcstats'), parts = [];
  if (s.link !== 'LINKED') { el.textContent = ''; return; }
  const f = (v, u) => v == null ? null : v + u;
  const t = [['CPU', f(s.pc_cpu_temp_c ?? s.pc_temp_c, '°C')], ['GPU', f(s.pc_gpu_temp_c, '°C')], ['NVMe', f(s.pc_nvme_temp_c, '°C')]]
    .filter(x => x[1]).map(x => x.join(' '));
  parts.push(`load ${s.pc_load1} · mem ${s.pc_mem_pct}%` + (s.pc_gpu_pct != null ? ` · GPU ${s.pc_gpu_pct}%` : ''));
  if (t.length) parts.push(t.join(' · '));
  el.innerHTML = parts.map(p => `<div>${p}</div>`).join('') + ((s.pc_cores_all || s.pc_cores) ? '<div>cores ' +
    (s.pc_cores_all || s.pc_cores).map(c => `<span class="core" title="${c}%"><i style="block-size:${Math.round(c)}%"></i></span>`).join('') + '</div>' : '');
  if (s.preview_quality && s.preview_quality !== 'off' && !qWant) document.getElementById('pvq').value = s.preview_quality;
}
pvPoll();

async function poll() {
  const pill = document.getElementById('pill'), ip = document.getElementById('ip');
  try {
    const s = await (await fetch('/api/status', {headers: pin ? {'X-KVM-PIN': pin} : {}})).json();
    pill.textContent = s.link || '\u2026';
    pill.className = 'pill ' + (s.link === 'LINKED' ? 'linked' : s.link === 'KEYS' ? 'keys' : s.link === 'NO HOST' ? 'nohost' : '');
    showLeds(s.leds);
    const parts = [s.pc_ip || s.ip || ''];
    if (s.link === 'LINKED') {
      if (s.pc_rx_bps != null) parts.push(`dn ${fmtRate(s.pc_rx_bps)} up ${fmtRate(s.pc_tx_bps)}`);
      if (s.pc_disk_pct != null) parts.push(`/:${s.pc_disk_pct}%`);
      if (s.pc_top_proc) parts.push(`${s.pc_top_proc}${s.pc_top_cpu_pct != null ? ' ' + Math.round(s.pc_top_cpu_pct) + '%' : ''}`);
    }
    ip.textContent = parts.filter(Boolean).join('  ');
    showMode(s); showStats(s); checkQualityAck(s);
  } catch (e) {
    pill.textContent = 'offline'; pill.className = 'pill'; ip.textContent = '';
  }
}
setInterval(poll, 2000); poll();
