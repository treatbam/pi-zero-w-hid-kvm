"""Tests for kvmctl against a stub HTTP server that mimics the real API's
PIN, Origin and media-lock behaviour. Runs from the repo root or from zero/cli.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from zero.cli import kvmctl
except ModuleNotFoundError:  # run from zero/cli
    import os, sys
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
    from zero.cli import kvmctl

PIN = "1234"


class StubHandler(BaseHTTPRequestHandler):
    log = []

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _pin_ok(self):
        return self.headers.get("X-KVM-PIN") == PIN

    def do_GET(self):
        StubHandler.log.append(("GET", self.path, dict(self.headers)))
        if self.path == "/api/status":
            return self._send(200, {"host_connected": True, "ip": "10.0.0.5"})
        if self.path == "/api/media":
            return self._send(200, {"images": ["netboot.xyz.iso", "debian.iso"],
                                    "mounted": "netboot.xyz.iso"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._send(415, {"error": "bad content-type"})
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        StubHandler.log.append(("POST", self.path, body, dict(self.headers)))
        if not self._pin_ok():
            return self._send(401, {"error": "wrong or missing PIN"})
        if self.path == "/api/media/mount":
            if not body.get("ro", True) and False:
                pass
            return self._send(200, {"ok": True, "mounted": body["image"], "ro": body.get("ro", True),
                                    "cdrom": body.get("cdrom")})
        if self.path == "/api/media/eject":
            return self._send(200, {"ok": True, "ejected": True})
        if self.path == "/api/media/netboot":
            return self._send(200, {"ok": True, "image": "netboot.xyz.iso", "sha256": "deadbeef"})
        if self.path == "/api/media/delete":
            return self._send(200, {"ok": True, "deleted": body["image"]})
        if self.path == "/api/type":
            return self._send(200, {"ok": True})
        if self.path == "/api/key":
            return self._send(200, {"ok": True, "keys": body.get("keys"), "mods": body.get("mods")})
        if self.path == "/api/power":
            if body.get("confirm") is not True:
                return self._send(400, {"error": "confirm must be true"})
            return self._send(202, {"ok": True})
        if self.path == "/api/macro":
            return self._send(200, {"ok": True, "name": body.get("name")})
        return self._send(404, {"error": "not found"})


class KvmctlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), StubHandler)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        StubHandler.log.clear()

    def client(self, pin=PIN):
        return kvmctl.Client(self.url, pin=pin, timeout=5)

    # --- reads work without a PIN ---
    def test_status_no_pin(self):
        c = self.client(pin=None)
        self.assertEqual(c.status()["ip"], "10.0.0.5")

    def test_media_list(self):
        c = self.client(pin=None)
        m = c.media()
        self.assertIn("netboot.xyz.iso", m["images"])

    # --- writes need a PIN, raised client-side before hitting the net ---
    def test_mount_without_pin_refused_locally(self):
        c = self.client(pin=None)
        with self.assertRaises(kvmctl.ApiError) as cm:
            c.mount("debian.iso")
        self.assertEqual(cm.exception.status, 0)
        self.assertEqual(StubHandler.log, [])  # never sent

    def test_wrong_pin_401(self):
        c = self.client(pin="bad")
        with self.assertRaises(kvmctl.ApiError) as cm:
            c.eject()
        self.assertEqual(cm.exception.status, 401)

    # --- mount defaults to read-only CD; --rw flips ro ---
    def test_mount_defaults_readonly(self):
        c = self.client()
        res = c.mount("debian.iso")
        self.assertTrue(res["ro"])
        last = StubHandler.log[-1]
        self.assertEqual(last[2]["ro"], True)

    def test_mount_rw_sets_ro_false(self):
        c = self.client()
        res = c.mount("recovery.img", ro=False)
        self.assertFalse(res["ro"])

    def test_mount_disk_sets_cdrom_false(self):
        c = self.client()
        c.mount("debian.iso", cdrom=False)
        self.assertEqual(StubHandler.log[-1][2]["cdrom"], False)

    def test_netboot(self):
        self.assertEqual(self.client().netboot()["image"], "netboot.xyz.iso")

    def test_type_and_key(self):
        c = self.client()
        c.type_text("hello")
        self.assertEqual(StubHandler.log[-1][2]["text"], "hello")
        c.key(["F12"])
        self.assertEqual(StubHandler.log[-1][2]["keys"], ["F12"])

    def test_power_sends_confirm(self):
        c = self.client()
        c.power("host", "wake")
        self.assertIs(StubHandler.log[-1][2]["confirm"], True)

    def test_no_origin_header_sent(self):
        # A CLI must not send Origin, so the server's same-origin check passes.
        self.client().eject()
        hdrs = StubHandler.log[-1][3]
        self.assertNotIn("Origin", hdrs)

    # --- argv wiring ---
    def test_main_boot_menu_alias(self):
        rc = kvmctl.main(["--url", self.url, "--pin", PIN, "boot-menu", "F12"])
        self.assertEqual(rc, 0)
        self.assertEqual(StubHandler.log[-1][1], "/api/key")
        self.assertEqual(StubHandler.log[-1][2]["keys"], ["F12"])

    def test_main_mount_rw_flag(self):
        rc = kvmctl.main(["--url", self.url, "--pin", PIN, "iso", "mount", "x.img", "--rw"])
        self.assertEqual(rc, 0)
        self.assertEqual(StubHandler.log[-1][2]["ro"], False)

    def test_main_missing_pin_exit1(self):
        rc = kvmctl.main(["--url", self.url, "iso", "eject"])
        self.assertEqual(rc, 1)

    def test_main_status_json(self):
        rc = kvmctl.main(["--url", self.url, "--json", "status"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
