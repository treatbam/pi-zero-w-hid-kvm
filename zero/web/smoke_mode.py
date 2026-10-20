#!/usr/bin/env python3
"""Smoke test for /api/mode, /api/term filtering and PIN-gated serial status. Run from zero/web; uses 127.0.0.1:8137 and a fake pty."""
import os, tty, subprocess, sys, time, json, urllib.request, select
sys.path.insert(0, "."); import hostview
m, s = os.openpty(); tty.setraw(m); tty.setraw(s)
open("/tmp/kvmpin","w").write("1234\n")
env = dict(os.environ, KVM_TERM_DEV=os.ttyname(s), KVM_PIN_FILE="/tmp/kvmpin")
p = subprocess.Popen([sys.executable, "server.py", "--host", "127.0.0.1", "--port", "8137"], env=env)
time.sleep(1.5)
def req(path, body=None, pin="1234"):
    h = {"Content-Type": "application/json"}
    if pin: h["X-KVM-PIN"] = pin
    r = urllib.request.Request("http://127.0.0.1:8137" + path, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(r) as f: return f.status, json.loads(f.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
try:
    print("status no pin has serial:", any(k.startswith("serial") for k in req("/api/status", pin=None)[1]))
    print("status wrong pin has serial:", any(k.startswith("serial") for k in req("/api/status", pin="9999")[1]))
    print("status pin has serial:", any(k.startswith("serial") for k in req("/api/status")[1]))
    print("lone 1e", req("/api/term", {"text": "\x1e"}))
    print("lone 1f", req("/api/term", {"text": "a\x1fb"}))
    print("nopin", req("/api/mode", {"mode": "shell"}, pin=None))
    print("bad", req("/api/mode", {"mode": "root"}))
    print("ctl in term", req("/api/term", {"text": "\x1e\x1fKVMCTL:helper"}))
    print("ok", req("/api/mode", {"mode": "shell"}))
    time.sleep(0.3); print("wire", os.read(m, 100))
    os.write(m, hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x01"); time.sleep(0.5)
    st = req("/api/status")[1]; print("after ack", {k: st[k] for k in st if k.startswith("serial")})
finally:
    p.terminate(); p.wait()
