#!/usr/bin/env python3
"""Tiny stdlib HTTP server: static console + JSON API. Binds 0.0.0.0 (default port 8080).
API:
  GET  /api/status              -> {"hid_ready","host_connected","udc_state","leds","ip",
                                      "temp_c","load1","mem_pct","uptime_s",
                                      "link","pc_temp_c","pc_load1","pc_mem_pct",
                                      "pc_rx_bps","pc_tx_bps","pc_ncpu","pc_cores",
                                      "pc_ip","pc_disk_pct","pc_top_proc","pc_top_cpu_pct",
                                      "pc_age_s","frame_fresh",
                                      "pc_cores_all","pc_gpu_pct","pc_cpu_temp_c","pc_gpu_temp_c","pc_nvme_temp_c",
                                      "serial_mode","serial_mode_age_s","serial_mode_requested",
                                      "preview_quality","preview_quality_requested"}
                                    pc_cores_all etc. (type 8) are null when the PC can't report them.
                                    serial_* and preview_* only with a valid X-KVM-PIN header.
                                    serial_mode is the helper's last ack ("helper"/"shell")
                                    or null when no helper has answered yet.
                                    link is "LINKED", "KEYS", or "NO HOST".
                                    PC fields are null unless link is LINKED.
  GET  /api/frame[?crop=full|loupe] -> 240x135 RGB565-LE still, or 204 if it is stale
                                    (PIN required; requests on-demand capture from helper)
  GET  /api/clipboard            -> {"ok":true,"text":"...","age_s":0.1} (PIN required)
  POST /api/clipboard {"text":"..."} -> push UTF-8 text to host clipboard via serial helper
  POST /api/ping   {}            -> bounce CapsLock twice & measure host LED echo latency_ms
  POST /api/key    {"keys":["a"],"mods":["ctrl"]}
  POST /api/type   {"text":"hello"}
  POST /api/mouse  {"dx":0,"dy":0,"wheel":0,"buttons":0}
  POST /api/macro  {"name":"ctrl_alt_del"}
  POST /api/auth   {}                          -> 200 if the PIN header is right (UI login check)
  GET  /api/term?n=40            -> {"seq","lines":[..],"partial","open","error"}  (PIN required)
  POST /api/term   {"text":"ls","newline":true}  -> keystrokes to the host's serial console
  POST /api/mode   {"mode":"shell"|"helper"}   -> asks the host helper to switch the serial port
                   (202; the helper's ack shows up as serial_mode in /api/status)
  POST /api/quality {"quality":"low"|"med"|"high"|"off"} -> preview quality (202; 409 in shell mode)
  GET  /api/preview?since=N   -> {"seq","w","h","quality","reset","tiles":[{x,y,w,h,jpg(base64)}]}
                                 changed JPEG tiles since seq N (PIN required; 204 when stale)
  POST /api/power  {"target":"host","action":"power|sleep|wake|hard_reset|hard_power","confirm":true}
                   {"target":"zero","action":"shutdown|reboot","confirm":true}
Every POST needs Content-Type: application/json, a same-origin Origin (if sent), and the PIN
in the X-KVM-PIN header. The PIN lives in /etc/pi-zero-w-hid-kvm/pin (made by install.sh).
A custom header also forces a CORS preflight, so other web pages can't send these POSTs.
5 wrong PINs from one address locks that address out for 60 s.
GET /api/status stays open (read-only; the GEEK and the console poll it).
GET /api/term needs the X-KVM-PIN header too (host terminal output can hold secrets).
"""
import argparse, hmac, json, os, socket, subprocess, threading, time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import hid, hostview, term
from urllib.parse import urlsplit, parse_qs

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MACROS = {"ctrl_alt_del": lambda: hid.key_combo(["delete"], ["ctrl", "alt"]),
          "win_l": lambda: hid.key_combo(["l"], ["gui"]),
          "alt_tab": lambda: hid.key_combo(["tab"], ["alt"])}
LOCK = threading.Lock()      # serialize writes so reports don't interleave
PIN_LOCK = threading.Lock()  # guard per-IP failed-PIN counters

MAX_BODY = 8192
PIN_FILE = os.environ.get("KVM_PIN_FILE", "/etc/pi-zero-w-hid-kvm/pin")
FAILS = {}            # ip -> (count, first_fail_time)
MAX_FAILS, LOCKOUT_S = 5, 60
HOST_POWER = ("power", "sleep", "wake")          # soft: System Control keys over USB
HOST_HARD = ("hard_reset", "hard_power")         # needs optocoupler wiring (not built yet)
MODE_REQ = {"mode": None, "quality": None}   # last mode asked for via /api/mode
FRAME_REQ = {"crop": "frame", "at": 0.0, "loupe_until": 0.0}
ZERO_POWER = {"shutdown": "poweroff", "reboot": "reboot"}

def load_pin():
    try: pin = open(PIN_FILE).read().strip()
    except OSError: pin = ""
    if not pin: raise SystemExit(f"no PIN in {PIN_FILE}; re-run install.sh (refusing to run without auth)")
    return pin

def zero_power(action):
    """Run after the HTTP reply is sent. Root runs systemctl directly; a non-root service
    needs the sudoers rule from the README (only these two commands, no password)."""
    time.sleep(1)
    cmd = ["systemctl", ZERO_POWER[action]]
    if os.geteuid() != 0: cmd = ["sudo", "-n"] + cmd
    subprocess.run(cmd, check=False)

def _int(body, k, lo, hi):
    v = body.get(k, 0)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{k} must be an int in [{lo},{hi}]")
    return v

def _strlist(body, k, allowed, maxn):
    v = body.get(k, [])
    if not isinstance(v, list) or len(v) > maxn or not all(isinstance(x, str) and x in allowed for x in v):
        raise ValueError(f"{k} must be a list of known names (max {maxn})")
    return v

def validate(path, body):
    """Return positional args for the handler or raise ValueError. Unknown path -> 404 later."""
    if path == "/api/key":
        return (_strlist(body, "keys", hid.KEYS, 6), _strlist(body, "mods", hid.MOD, 8))
    if path == "/api/type":
        t = body.get("text", "")
        if not isinstance(t, str): raise ValueError("text must be a string")
        return (t[:1000],)
    if path == "/api/clipboard":
        t = body.get("text", "")
        if not isinstance(t, str) or len(t) > 1000: raise ValueError("text must be a string (max 1000)")
        return (t,)
    if path == "/api/ping":
        return ()
    if path == "/api/mouse":
        m = hid.MAX_MOVE
        return (_int(body, "dx", -m, m), _int(body, "dy", -m, m),
                _int(body, "wheel", -127, 127), _int(body, "buttons", 0, 7))
    if path == "/api/auth":
        return ()
    if path == "/api/term":
        t = body.get("text", "")
        if not isinstance(t, str) or len(t) > 1000: raise ValueError("text must be a string (max 1000)")
        if "\x1e" in t or "\x1f" in t: raise ValueError("text may not contain \\x1e or \\x1f (mode-control bytes)")
        nl = body.get("newline", True)
        if not isinstance(nl, bool): raise ValueError("newline must be a bool")
        return (t, nl)
    if path == "/api/mode":
        m = body.get("mode")
        if m not in ("shell", "helper"): raise ValueError('mode must be "shell" or "helper"')
        return (m,)
    if path == "/api/quality":
        q = body.get("quality")
        if q not in ("low", "med", "high", "off"): raise ValueError('quality must be "low", "med", "high" or "off"')
        return (q,)
    if path == "/api/power":
        if body.get("confirm") is not True: raise ValueError("confirm must be true")
        t, a = body.get("target"), body.get("action")
        if t == "host" and a in HOST_POWER + HOST_HARD: return (t, a)
        if t == "zero" and a in ZERO_POWER: return (t, a)
        raise ValueError("unknown target/action")
    if path == "/api/macro":
        if body.get("name") not in MACROS: raise ValueError("unknown macro")
        return (body["name"],)
    raise LookupError(path)

def sysstats():
    """Cheap /proc reads for the GEEK dashboard; any failure just omits the field."""
    out = {}
    try: out["temp_c"] = round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
    except (OSError, ValueError): pass
    try: out["load1"] = float(open("/proc/loadavg").read().split()[0])
    except (OSError, ValueError, IndexError): pass
    try:
        m = dict(l.split(":", 1) for l in open("/proc/meminfo"))
        tot, avail = int(m["MemTotal"].split()[0]), int(m["MemAvailable"].split()[0])
        out["mem_pct"] = round(100 * (tot - avail) / tot)
    except (OSError, ValueError, KeyError, ZeroDivisionError): pass
    try: out["uptime_s"] = int(float(open("/proc/uptime").read().split()[0]))
    except (OSError, ValueError, IndexError): pass
    return out

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]; s.close(); return ip
    except OSError:
        return "0.0.0.0"

class H(SimpleHTTPRequestHandler):
    timeout = 5

    def __init__(self, *a, **k): super().__init__(*a, directory=STATIC, **k)

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _my_ip(self):
        """The address this request arrived on. Works as a standalone AP with no default
        route, where local_ip()'s routing trick has nothing to route through."""
        try: ip = self.connection.getsockname()[0]
        except OSError: ip = ""
        return ip if ip and not ip.startswith("127.") and ip != "0.0.0.0" else local_ip()

    def do_GET(self):
        if self.path == "/api/status":
            st = hid.status()
            pc = hostview.shared.status_fields(st["host_connected"])
            sm = {}
            if self.headers.get("X-KVM-PIN") and self._pin_ok():   # serial fields need the PIN
                mode, age = hostview.shared.mode()
                sm = {"serial_mode": mode, "serial_mode_age_s": age, "serial_mode_requested": MODE_REQ["mode"],
                      "preview_quality": hostview.shared.quality(), "preview_quality_requested": MODE_REQ["quality"]}
            return self._json(200, {**st, "ip": self._my_ip(), **sysstats(), **pc, **sm})
        u = urlsplit(self.path)
        if u.path == "/api/frame":
            ok = self._pin_ok()
            if ok is None: return self._json(429, {"error": "too many wrong PINs, wait a minute"})
            if not ok: return self._json(401, {"error": "wrong or missing PIN"})
            now = time.monotonic()
            qcrop = parse_qs(u.query).get("crop", [None])[0]
            if qcrop == "loupe":
                FRAME_REQ["loupe_until"] = now + 4.0
                cmd = "loupe"
            elif qcrop == "full":
                FRAME_REQ["loupe_until"] = 0.0
                cmd = "frame"
            else:
                cmd = "loupe" if now < FRAME_REQ["loupe_until"] else "frame"
            host_conn = hid.status()["host_connected"]
            hmode = hostview.shared.mode()[0]
            helper_active = (hmode == "helper") or (hmode != "shell" and hostview.shared.status_fields(True)["link"] == "LINKED")
            crop_changed = cmd != FRAME_REQ["crop"]
            fseq0 = hostview.shared.frame_seq()
            sent = False
            if helper_active and (crop_changed or now - FRAME_REQ["at"] >= 1.2):
                FRAME_REQ["crop"] = cmd
                FRAME_REQ["at"] = now
                try: sent = bool(term.send_ctl(cmd))
                except (TimeoutError, OSError): sent = False
            if sent and (crop_changed or hostview.shared.frame_bytes(host_conn) is None):
                deadline = time.monotonic() + 0.45
                while time.monotonic() < deadline:
                    if hostview.shared.frame_seq() != fseq0:
                        break
                    time.sleep(0.02)
            blob = hostview.shared.frame_bytes(host_conn)
            if not blob:
                self.send_response(204); self.send_header("Content-Length", "0")
                self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers(); self.wfile.write(blob); return
        if u.path == "/api/clipboard":
            ok = self._pin_ok()
            if ok is None: return self._json(429, {"error": "too many wrong PINs, wait a minute"})
            if not ok: return self._json(401, {"error": "wrong or missing PIN"})
            _, _, seq0 = hostview.shared.clipboard()
            try:
                sent = term.send_ctl("cget")
            except (TimeoutError, OSError):
                sent = False
            if sent:
                deadline = time.monotonic() + 0.35
                while time.monotonic() < deadline:
                    if hostview.shared.clipboard()[2] != seq0:
                        break
                    time.sleep(0.02)
            text, age, _ = hostview.shared.clipboard()
            return self._json(200, {"ok": True, "text": text or "", "age_s": age})
        if u.path == "/api/preview":
            ok = self._pin_ok()
            if ok is None: return self._json(429, {"error": "too many wrong PINs, wait a minute"})
            if not ok: return self._json(401, {"error": "wrong or missing PIN"})
            try: since = int(parse_qs(u.query).get("since", ["0"])[0])
            except ValueError: return self._json(400, {"error": "since must be an int"})
            pv = hostview.shared.preview(since, hid.status()["host_connected"])
            if pv is None:
                self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
            return self._json(200, pv)
        if u.path == "/api/term":
            ok = self._pin_ok()
            if ok is None: return self._json(429, {"error": "too many wrong PINs, wait a minute"})
            if not ok: return self._json(401, {"error": "wrong or missing PIN"})
            try: n = int(parse_qs(u.query).get("n", ["200"])[0])
            except ValueError: return self._json(400, {"error": "n must be an int"})
            return self._json(200, term.snapshot(n))
        super().do_GET()

    def _origin_ok(self):
        o = self.headers.get("Origin")
        if not o: return True  # curl / same-origin GET-less clients
        return o.split("://", 1)[-1] == self.headers.get("Host", "")

    def _pin_ok(self):
        ip = self.client_address[0]; now = time.monotonic()
        given = (self.headers.get("X-KVM-PIN") or "").encode()
        with PIN_LOCK:
            n, t0 = FAILS.get(ip, (0, now))
            if n >= MAX_FAILS and now - t0 < LOCKOUT_S: return None      # locked out
            if now - t0 >= LOCKOUT_S: n, t0 = 0, now
            if hmac.compare_digest(given, PIN.encode()):
                FAILS.pop(ip, None); return True
            for k, (_, ts) in list(FAILS.items()):
                if now - ts >= LOCKOUT_S: FAILS.pop(k, None)
            FAILS[ip] = (n + 1, t0 if n else now); return False

    def do_POST(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
        if not self._origin_ok():
            return self._json(403, {"error": "cross-origin request refused"})
        ok = self._pin_ok()
        if ok is None: return self._json(429, {"error": "too many wrong PINs, wait a minute"})
        if not ok: return self._json(401, {"error": "wrong or missing PIN"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n < 0 or n > MAX_BODY: raise ValueError("bad length")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict): raise ValueError("body must be an object")
            args = validate(self.path, body)
        except LookupError:
            return self._json(404, {"error": "not found"})
        except (ValueError, TypeError) as e:  # JSONDecodeError is a ValueError
            return self._json(400, {"error": f"bad request: {e}"})
        if self.path == "/api/auth": return self._json(200, {"ok": True})
        if self.path == "/api/term":
            try:
                if not term.send(*args): return self._json(409, {"error": "serial port not open"})
            except (TimeoutError, OSError) as e:
                return self._json(503, {"error": str(e)})
            return self._json(200, {"ok": True})
        if self.path == "/api/clipboard":
            try:
                if not term.send_clipboard(args[0]): return self._json(409, {"error": "serial port not open"})
            except (TimeoutError, OSError) as e:
                return self._json(503, {"error": str(e)})
            return self._json(200, {"ok": True, "bytes": len(args[0].encode("utf-8")[:1024])})
        if self.path == "/api/mode":
            try:
                if not term.send_mode(args[0]): return self._json(409, {"error": "serial port not open"})
            except (TimeoutError, OSError) as e:
                return self._json(503, {"error": str(e)})
            MODE_REQ["mode"] = args[0]
            hostview.shared.requested_mode = args[0]   # while "shell", types 7-9 are not parsed
            return self._json(202, {"ok": True, "requested": args[0], "note": "watch serial_mode in /api/status"})
        if self.path == "/api/quality":
            mode = hostview.shared.mode()[0]
            if mode == "shell" or (MODE_REQ["mode"] == "shell" and mode != "helper"):
                return self._json(409, {"error": "serial port is in shell mode; switch to helper first"})
            if MODE_REQ["mode"] == "shell":      # bash exited on its own; the user is back in helper
                MODE_REQ["mode"] = "helper"; hostview.shared.requested_mode = "helper"
            try:
                if not term.send_quality(args[0]): return self._json(409, {"error": "serial port not open"})
            except (TimeoutError, OSError) as e:
                return self._json(503, {"error": str(e)})
            MODE_REQ["quality"] = args[0]
            return self._json(202, {"ok": True, "requested": args[0]})
        if self.path == "/api/power":
            target, action = args
            if target == "zero":
                threading.Thread(target=zero_power, args=(action,), daemon=True).start()
                return self._json(202, {"ok": True, "note": f"Zero {action} in 1 s"})
            if action in HOST_HARD:
                return self._json(501, {"error": "hard power/reset needs the optocoupler wiring (not installed)"})
        if not hid.status()["host_connected"]:
            return self._json(409, {"error": "host not connected"})  # avoid blocking write
        try:
            with LOCK:
                if self.path == "/api/ping":
                    res = hid.ping_host()
                    return self._json(200, {"ok": True, **res})
                if self.path == "/api/key": hid.key_combo(*args)
                elif self.path == "/api/type": hid.type_text(*args)
                elif self.path == "/api/mouse": hid.mouse(*args)
                elif self.path == "/api/macro": MACROS[args[0]]()
                elif self.path == "/api/power": hid.system_key(args[1])
        except hid.HIDTimeout as e:
            return self._json(503, {"error": str(e)})
        except OSError as e:
            return self._json(500, {"error": str(e)})
        self._json(200, {"ok": True})

    def log_message(self, *a): pass  # quiet; mouse moves are chatty

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080); a = p.parse_args()
    PIN = load_pin()
    term.start()
    print(f"KVM console on http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
