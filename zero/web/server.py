#!/usr/bin/env python3
"""Tiny stdlib HTTP server: static console + JSON API. Binds 0.0.0.0 (default port 8080).
API:
  GET  /api/status              -> {"hid_ready","host_connected","udc_state","ip"}
  POST /api/key    {"keys":["a"],"mods":["ctrl"]}
  POST /api/type   {"text":"hello"}
  POST /api/mouse  {"dx":0,"dy":0,"wheel":0,"buttons":0}
  POST /api/macro  {"name":"ctrl_alt_del"}
POSTs require Content-Type: application/json and (if sent) a same-origin Origin header;
this blocks simple cross-page form POSTs but is NOT auth.
NO AUTH: anyone on the LAN/AP can type into the host. TODO: token or AP-only binding.
"""
import argparse, json, os, socket, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import hid

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MACROS = {"ctrl_alt_del": lambda: hid.key_combo(["delete"], ["ctrl", "alt"]),
          "win_l": lambda: hid.key_combo(["l"], ["gui"]),
          "alt_tab": lambda: hid.key_combo(["tab"], ["alt"])}
LOCK = threading.Lock()  # serialize writes so reports don't interleave

MAX_BODY = 8192

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
    if path == "/api/mouse":
        m = hid.MAX_MOVE
        return (_int(body, "dx", -m, m), _int(body, "dy", -m, m),
                _int(body, "wheel", -127, 127), _int(body, "buttons", 0, 7))
    if path == "/api/macro":
        if body.get("name") not in MACROS: raise ValueError("unknown macro")
        return (body["name"],)
    raise LookupError(path)

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]; s.close(); return ip
    except OSError:
        return "0.0.0.0"

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=STATIC, **k)

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/api/status":
            return self._json(200, {**hid.status(), "ip": local_ip()})
        super().do_GET()

    def _origin_ok(self):
        o = self.headers.get("Origin")
        if not o: return True  # curl / same-origin GET-less clients
        return o.split("://", 1)[-1] == self.headers.get("Host", "")

    def do_POST(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
        if not self._origin_ok():
            return self._json(403, {"error": "cross-origin request refused"})
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
        if not hid.status()["host_connected"]:
            return self._json(409, {"error": "host not connected"})  # avoid blocking write
        try:
            with LOCK:
                if self.path == "/api/key": hid.key_combo(*args)
                elif self.path == "/api/type": hid.type_text(*args)
                elif self.path == "/api/mouse": hid.mouse(*args)
                elif self.path == "/api/macro": MACROS[args[0]]()
        except hid.HIDTimeout as e:
            return self._json(503, {"error": str(e)})
        except OSError as e:
            return self._json(500, {"error": str(e)})
        self._json(200, {"ok": True})

    def log_message(self, *a): pass  # quiet; mouse moves are chatty

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080); a = p.parse_args()
    print(f"KVM console on http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
