// Plain JS console: on-screen keys, mouse pad (drag=move, tap=click, 2-finger/wheel=scroll).
// PIN is kept in this browser (localStorage) and sent on every POST as X-KVM-PIN.
let pin = localStorage.getItem('kvmPin') || '';
const askPin = msg => { const p = prompt(msg || 'Console PIN'); if (p !== null) { pin = p.trim(); localStorage.setItem('kvmPin', pin); } };
async function post(u, b) {
  const r = await fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-KVM-PIN': pin}, body: JSON.stringify(b)});
  if (r.status === 401) { askPin('Wrong or missing PIN. Console PIN:'); }
  else if (r.status === 429) { alert('Too many wrong PINs. Wait a minute.'); }
  return r;
}
if (!pin) askPin();
function logout() { pin = ''; localStorage.removeItem('kvmPin'); askPin(); }

// Power: every action asks first; Zero actions need the word typed for extra safety.
async function power(target, action, label) {
  if (target === 'zero') { if (prompt(`Type "${action}" to ${label}`) !== action) return; }
  else if (!confirm(`${label}?`)) return;
  const r = await post('/api/power', {target, action, confirm: true});
  const j = await r.json().catch(() => ({}));
  document.getElementById('pwr').textContent = r.ok ? `${label}: sent` : `${label}: ${j.error || r.status}`;
}
let held = 0;                                      // currently held mouse buttons (for drag)
// Ordered send queue: mouse reports go one at a time so a release can't overtake the last move.
let q = Promise.resolve();
const m = o => { const b = Object.assign({dx: 0, dy: 0, wheel: 0, buttons: held}, o);
  q = q.then(() => post('/api/mouse', b)).catch(() => {}); return q; };
const mods = () => ['ctrl', 'alt', 'shift', 'gui'].filter(k => document.getElementById(k).checked);

const rows = ['esc f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12', '` 1 2 3 4 5 6 7 8 9 0 - = backspace',
  'tab q w e r t y u i o p [ ] \\', 'a s d f g h j k l ; \' enter', 'z x c v b n m , . /',
  'space left up down right home end delete'];
const kb = document.getElementById('kb');
rows.forEach(r => { const d = document.createElement('div');
  r.split(' ').forEach(k => { const b = document.createElement('button'); b.textContent = k;
    b.onclick = () => post('/api/key', {keys: [k], mods: mods()}); d.appendChild(b); });
  kb.appendChild(d); });

function typeTxt() { const t = document.getElementById('txt'); post('/api/type', {text: t.value}); t.value = ''; }

// Buttons: press on down, release on up (enables drag via pad while held).
document.querySelectorAll('[data-b]').forEach(b => {
  b.onpointerdown = () => { flush(); held |= +b.dataset.b; m({}); };
  b.onpointerup = b.onpointerleave = () => { if (held & +b.dataset.b) { flush(); held &= ~+b.dataset.b; m({}); } };
});

// Pad: batch movement and flush every 30 ms to limit HTTP request rate.
const pad = document.getElementById('pad'); let last = null, acc = {dx: 0, dy: 0}, moved = false;
pad.onpointerdown = e => { last = [e.clientX, e.clientY]; moved = false; pad.setPointerCapture(e.pointerId); };
pad.onpointermove = e => { if (!last) return; acc.dx += e.clientX - last[0]; acc.dy += e.clientY - last[1];
  if (Math.abs(acc.dx) + Math.abs(acc.dy) > 2) moved = true; last = [e.clientX, e.clientY]; };
pad.onpointerup = () => { flush(); if (!moved) { m({buttons: held | 1}); m({}); } last = null; };  // tap = left click (queued in order)
pad.onwheel = e => { e.preventDefault(); m({wheel: e.deltaY < 0 ? 1 : -1}); };
const clampMove = v => Math.max(-4096, Math.min(4096, Math.round(v)));
function flush() { if (acc.dx || acc.dy) { const d = {dx: clampMove(acc.dx), dy: clampMove(acc.dy)}; acc = {dx: 0, dy: 0}; m(d); } }
setInterval(flush, 30);

async function poll() { try { const s = await (await fetch('/api/status')).json();
  document.getElementById('st').innerHTML = `HID <span class=${s.hid_ready ? 'on' : 'off'}>${s.hid_ready}</span> · host <span class=${s.host_connected ? 'on' : 'off'}>${s.udc_state}</span> · ${s.ip}`;
} catch (e) { document.getElementById('st').textContent = 'status: offline'; } }
setInterval(poll, 2000); poll();
