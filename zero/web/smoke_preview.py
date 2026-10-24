#!/usr/bin/env python3
"""Smoke test for the preview/quality/extended-stats API (types 7-9) plus /api/mode, /api/term
filtering and PIN-gated status. Run from zero/web. Uses 127.0.0.1:8137, a fake pty as the
gadget serial port and a fake UDC state file; only stops the server it started."""
import base64, select, json, os, subprocess, sys, tempfile, time, tty, urllib.request
sys.path.insert(0, "."); sys.path.insert(0, os.path.join("..", "..", "host"))
import hostview, kvm_preview, kvm_stats

tmp = tempfile.mkdtemp()
open(f"{tmp}/pin", "w").write("1234\n"); open(f"{tmp}/udc_state", "w").write("configured\n")
m, s = os.openpty(); tty.setraw(m); tty.setraw(s)
os.makedirs(f"{tmp}/net/proc/net"); os.makedirs(f"{tmp}/net/sys/class/net/tailscale0")
open(f"{tmp}/net/proc/net/wireless", "w").write(
    "Inter-| sta-|   Quality        |   Discarded packets\n face | tus | link level noise |  nwid\n"
    " wlan0: 0000   54.  -61.  -256        0      0      0\n")
open(f"{tmp}/net/sys/class/net/tailscale0/flags", "w").write("0x1091\n")
env = dict(os.environ, KVM_STATS_MIN_S="1", KVM_NET_ROOT=f"{tmp}/net", KVM_TERM_DEV=os.ttyname(s), KVM_PIN_FILE=f"{tmp}/pin", KVM_UDC_GLOB=f"{tmp}/udc_state")
p = subprocess.Popen([sys.executable, "server.py", "--host", "127.0.0.1", "--port", "8137"], env=env)
fails = []

def req(path, body=None, pin="1234"):
    h = {"Content-Type": "application/json"}
    if pin: h["X-KVM-PIN"] = pin
    r = urllib.request.Request("http://127.0.0.1:8137" + path, headers=h,
                               data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(r) as f:
            raw = f.read(); return f.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

def check(name, cond, got=""):
    print(("PASS " if cond else "FAIL ") + name, "" if cond else got); cond or fails.append(name)

STATS_ON = b"\x1e\x1fKVMCTL:stats:on\n"
stats_on_seen = [0]

def wire():
    """Next control bytes from the Zero, minus kvm-web's automatic stats:on (counted)."""
    time.sleep(0.3)
    r, _, _ = select.select([m], [], [], 1.0)
    got = os.read(m, 1000) if r else b""
    stats_on_seen[0] += got.count(STATS_ON)
    return got.replace(STATS_ON, b"")

try:
    for _ in range(30):
        try: req("/api/status"); break
        except OSError: time.sleep(0.2)
    serial = lambda st: any(k.startswith(("serial", "preview")) for k in st)
    check("status no pin hides serial/preview", not serial(req("/api/status", pin=None)[1]))
    bad = req("/api/status", pin="9999")
    check("status wrong pin hides serial/preview", not serial(bad[1]))
    check("status wrong pin: 200 + pin_ok false", bad[0] == 200 and bad[1].get("pin_ok") is False, bad)
    check("status no pin: no pin_ok", "pin_ok" not in req("/api/status", pin=None)[1])
    check("status good pin: pin_ok true", req("/api/status")[1].get("pin_ok") is True)
    check("status pin shows serial/preview", serial(req("/api/status")[1]))
    st0 = req("/api/status")[1]
    check("zero link fields with PIN", st0.get("zero_wifi_rssi") == -61 and st0.get("tailscale") == "up", st0)
    np0 = req("/api/status", pin=None)[1]
    check("zero link fields hidden without PIN", "zero_wifi_rssi" not in np0 and "tailscale" not in np0)
    check("tailscale_ip key with PIN, hidden without", "tailscale_ip" in st0 and "tailscale_ip" not in np0, st0)
    open(f"{tmp}/net/sys/class/net/tailscale0/flags", "w").write("0x1090\n")
    check("tailscale down when the interface is not up", req("/api/status")[1].get("tailscale") == "down")
    for t in ("\x1e", "a\x1fb", "\x1e\x1fKVMCTL:helper", "\x1e\x1fKVMCTL:q:high"):
        check(f"/api/term rejects {t!r}", req("/api/term", {"text": t})[0] == 400)
    check("/api/mode no pin 401", req("/api/mode", {"mode": "shell"}, pin=None)[0] == 401)
    check("/api/mode bad 400", req("/api/mode", {"mode": "root"})[0] == 400)
    c, j = req("/api/mode", {"mode": "shell"}); w = wire()
    check("/api/mode 202 + wire", c == 202 and w == b"\x1e\x1fKVMCTL:shell\n", (c, w))
    req("/api/mode", {"mode": "helper"}); wire()          # back to helper before quality tests
    os.write(m, hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x00"); time.sleep(0.3)
    check("/api/quality no pin 401", req("/api/quality", {"quality": "low"}, pin=None)[0] == 401)
    check("/api/quality bad 400", req("/api/quality", {"quality": "ultra"})[0] == 400)
    c, j = req("/api/quality", {"quality": "low"}); w = wire()
    check("/api/quality 202 + wire", c == 202 and w == b"\x1e\x1fKVMCTL:q:low\n", (c, w))
    c, j = req("/api/quality", {"quality": "off"}); w = wire()
    check("/api/quality off 202 + wire", c == 202 and w == b"\x1e\x1fKVMCTL:q:off\n", (c, w))
    req("/api/quality", {"quality": "low"}); wire()
    check("/api/preview no pin 401", req("/api/preview?since=0", pin=None)[0] == 401)
    check("/api/preview stale 204", req("/api/preview?since=0")[0] == 204)
    # play the helper: stats, ext stats, preview frame, acks
    enc = kvm_preview.TileEncoder("low"); f = bytes(640 * 360 * 3)
    blob = hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x00"   # helper mode first
    blob += (hostview.MAGIC + bytes([1]) + (12).to_bytes(4, "little") + bytes(12)[:0] +
            __import__("struct").pack("<fff", 45.0, 0.5, 30.0))
    blob += kvm_stats.pack_ext({"v": 1, "cores": [10, 20], "temps": {"nvme": 40}})
    blob += enc.encode(f) + kvm_preview.pack_quality("low")
    blob += hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x00"
    os.write(m, blob); time.sleep(0.6)
    c, pv = req("/api/preview?since=0")
    ok = c == 200 and pv["w"] == 640 and pv["reset"] and len(pv["tiles"]) == 60 and \
        base64.b64decode(pv["tiles"][0]["jpg"])[:2] == b"\xff\xd8"
    check("/api/preview returns 60 JPEG tiles, unmodified", ok, c)
    c, pv2 = req(f"/api/preview?since={pv['seq']}")
    check("/api/preview since=seq is empty", c == 200 and pv2["tiles"] == [] and not pv2["reset"], pv2)
    st = req("/api/status")[1]
    check("status ext stats", st["pc_cores_all"] == [10.0, 20.0] and st["pc_nvme_temp_c"] == 40.0
          and st["pc_gpu_pct"] is None and st["pc_cpu_temp_c"] is None, st)
    check("status quality ack", st["preview_quality"] == "low" and st["preview_quality_requested"] == "low", st)
    check("status serial mode ack", st["serial_mode"] == "helper", st)
    nopin = req("/api/status", pin=None)[1]
    check("status no pin still has pc stats", nopin["pc_cores_all"] == [10.0, 20.0] and "preview_quality" not in nopin)
    os.write(m, hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x01"); time.sleep(0.4)
    c, j = req("/api/quality", {"quality": "med"})
    check("/api/quality 409 in shell mode", c == 409, (c, j))
    # bash exits on its own (Ctrl-D): a helper ack right after a shell ack ends the shell request
    req("/api/mode", {"mode": "shell"}); wire()
    os.write(m, hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x00"); time.sleep(0.4)
    check("helper ack after shell ack clears the shell request",
          req("/api/status")[1]["serial_mode_requested"] is None)
    # /api/mode shell request while in helper: a stale helper ack must not re-open types 7-9
    c, _ = req("/api/mode", {"mode": "shell"}); wire()
    os.write(m, hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x00"
             + kvm_preview.pack_quality("high")); time.sleep(0.4)
    check("type 9 ignored while shell is requested", req("/api/status")[1]["preview_quality"] == "low")
    os.set_blocking(m, False)
    try: leftover = os.read(m, 1000)
    except BlockingIOError: leftover = b""
    check("nothing written to the host on 409", leftover == b"", leftover)
    os.write(m, hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x01" + kvm_preview.pack_quality("high")); time.sleep(0.4)
    check("type 9 ignored in shell mode", req("/api/status")[1]["preview_quality"] == "low")
    # automatic stats:on: sent after the first helper ack; again after a helper restart
    check("kvm-web sent stats:on after the first helper ack", stats_on_seen[0] >= 1, stats_on_seen)
    req("/api/mode", {"mode": "helper"}); wire()
    c, j = req("/api/stats", {"on": "yes"}); check("/api/stats bad body 400", c == 400, c)
    c, _ = req("/api/stats", {"on": True}, pin=None); check("/api/stats no pin 401", c == 401, c)
    hello = hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x00"
    os.write(m, hello); time.sleep(1.5); wire()
    n0 = stats_on_seen[0]
    time.sleep(hostview.MODE_ACK_TTL_S + 1.5)                 # helper silent: looks like a restart
    os.write(m, hello)
    end = time.time() + 4
    while time.time() < end and stats_on_seen[0] == n0: wire()
    check("stats:on re-sent after a helper restart", stats_on_seen[0] > n0, stats_on_seen)
    c, j = req("/api/stats", {"on": False}); w = wire()
    check("/api/stats off 202 + wire", c == 202 and w == b"\x1e\x1fKVMCTL:stats:off\n", (c, w))
    n1 = stats_on_seen[0]
    time.sleep(hostview.MODE_ACK_TTL_S + 1.5); os.write(m, hello); time.sleep(2.5); wire()
    check("no automatic stats:on after stats:off", stats_on_seen[0] == n1, stats_on_seen)
    os.write(m, hostview.MAGIC + bytes([3]) + (1).to_bytes(4, "little") + b"\x01"); time.sleep(0.4)
    c, _ = req("/api/stats", {"on": True}); check("/api/stats 409 in shell mode", c == 409, c)
finally:
    p.terminate(); p.wait()
print("SMOKE", "OK" if not fails else f"FAILED {fails}")
sys.exit(1 if fails else 0)
