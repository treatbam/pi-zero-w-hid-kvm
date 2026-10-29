"""Tests for zero-boot auto-helper detection, stream-on-boot bundle, FAT12 KVM-SETUP,
HTTP/1.1 keep-alive, RFC 6455 /api/ws HID WebSocket, /api/theme, and S3 wake."""
import base64
import hashlib
import http.client
import json
import os
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tty
import unittest
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "host"))
sys.path.insert(0, os.path.join(ROOT, "zero", "gadget"))

import autolaunch  # noqa: E402
import build_setup_img  # noqa: E402
import hid  # noqa: E402
import hostview  # noqa: E402
import kvm_stub  # noqa: E402
import ws  # noqa: E402


def _mask_ws_text(text):
    raw = text.encode("utf-8")
    mask = b"\x37\xfa\x21\x3d"
    masked = bytes(raw[i] ^ mask[i & 3] for i in range(len(raw)))
    n = len(raw)
    hdr = bytearray([0x81])
    if n < 126:
        hdr.append(0x80 | n)
    else:
        hdr.append(0x80 | 126)
        hdr += struct.pack("!H", n)
    return bytes(hdr) + mask + masked


class AutoLaunchUnitTest(unittest.TestCase):
    def test_hello_envelope_parses_without_terminal_leak(self):
        hv = hostview.HostView()
        out = hv.feed(b"login: " + hostview.pack_hello("linux", "aarch64", "stream") + b"ready\n")
        self.assertEqual(out, b"login: ready\n")
        info, age, seq = hv.hello()
        self.assertEqual(seq, 1)
        self.assertEqual(info["os"], "linux")
        self.assertEqual(info["arch"], "aarch64")
        self.assertEqual(info["want"], "stream")
        self.assertLessEqual(age, 1.0)

        # Unknown OS in hello envelope is ignored safely
        bad = b"\xfe\xfdKV\x0a" + struct.pack("<I", 22) + b'{"v":1,"os":"plan9"}  '
        hv.feed(bad)
        self.assertEqual(hv.hello()[2], 1)

    def test_state_machine_probe_missing_and_stream_on_hello(self):
        t = [100.0]
        hv = hostview.HostView(now=lambda: t[0])
        ctls, boots = [], []
        al = autolaunch.AutoLauncher(
            hv,
            send_ctl=lambda c: (ctls.append(c), True)[1],
            send_boot_frame=lambda pfx, os_tag, sha, b64: (boots.append((pfx, os_tag, sha, len(b64))), True)[1],
            now=lambda: t[0],
            setup_link="/nonexistent/mass_storage.usb0",
        )
        self.assertIsNone(al.step(False))
        self.assertEqual(al.status_fields()["helper_state"], "no_host")

        # Plug into unpaired host -> watching -> probing -> missing
        self.assertIsNone(al.step(True))
        self.assertEqual(al.status_fields()["helper_state"], "watching")
        t[0] += 3.6
        self.assertEqual(al.step(True), "probe")
        self.assertEqual(ctls, ["probe"])
        self.assertEqual(al.status_fields()["helper_state"], "probing")
        t[0] += 4.1
        self.assertIsNone(al.step(True))
        self.assertEqual(al.status_fields()["helper_state"], "missing")

        # Host stub starts and sends TYPE_HELLO (windows) -> streams KVMBOOT:windows
        hv.feed(hostview.pack_hello("windows", "arm64", "stream"))
        res = al.step(True)
        self.assertEqual(res, "stream:windows")
        self.assertEqual(boots[-1][0], "KVMBOOT")
        self.assertEqual(boots[-1][1], "windows")
        self.assertEqual(al.status_fields()["helper_state"], "streaming")
        self.assertEqual(al.status_fields()["helper_os"], "windows")

        # Stats arrive -> linked
        hv.feed(hostview.pack_stats(48.0, 0.8, 42.0))
        al.step(True)
        self.assertEqual(al.status_fields()["helper_state"], "linked")

        # Install request sends KVMINST
        hv.feed(hostview.pack_stats(48.0, 0.8, 42.0))
        t[0] += 10.0  # let stats go stale so link drops to KEYS
        hv.feed(hostview.pack_hello("linux", "x86_64", "install"))
        self.assertEqual(al.step(True), "install:linux")
        self.assertEqual(boots[-1][0], "KVMINST")

    def test_single_file_bundle_unpacks_and_runs_once_over_pty(self):
        builder = autolaunch.PayloadBuilder()
        os_tag, sha, b64 = builder.stream_payload("linux")
        self.assertEqual(os_tag, "linux")
        line = f"{os_tag}:{sha}:{b64}".encode("ascii")
        code = kvm_stub.unpack_boot_line(line)
        self.assertIsNotNone(code)
        self.assertEqual(hashlib.sha256(code).hexdigest(), sha)

        # Corrupted sha must be rejected by kvm_stub
        bad_line = f"linux:{'0' * 64}:{b64}".encode("ascii")
        self.assertIsNone(kvm_stub.unpack_boot_line(bad_line))

        with tempfile.TemporaryDirectory() as d:
            bundle_py = os.path.join(d, "kvm-helper-live.py")
            with open(bundle_py, "wb") as f:
                f.write(code)
            m, s = os.openpty()
            tty.setraw(m)
            tty.setraw(s)
            env = dict(os.environ, KVM_HOST_DEV=os.ttyname(s), KVM_DISPLAY="")
            proc = subprocess.Popen(
                [sys.executable, bundle_py, "--once"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            hv = hostview.HostView()
            try:
                end = time.monotonic() + 8.0
                linked = False
                while time.monotonic() < end:
                    if select.select([m], [], [], 0.1)[0]:
                        hv.feed(os.read(m, 65536))
                    if hv.status_fields(True)["link"] == "LINKED":
                        linked = True
                        break
                self.assertTrue(linked, "streamed single-file bundle never reached LINKED")
                # Probe command answered immediately
                os.write(m, b"\x1e\x1fKVMCTL:probe\n")
                time.sleep(0.15)
                while select.select([m], [], [], 0.05)[0]:
                    hv.feed(os.read(m, 65536))
                self.assertEqual(hv.mode()[0], "helper")
            finally:
                os.close(m)
                os.close(s)
                try:
                    out = proc.communicate(timeout=8)[0].decode("utf-8", errors="replace")
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out = proc.communicate()[0].decode("utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, out)
            self.assertNotIn("Traceback", out)

    def test_fat12_setup_image_roundtrips_all_files(self):
        img = build_setup_img.build_image_bytes()
        self.assertEqual(len(img), 512 * 1024)
        self.assertEqual(img[510:512], b"\x55\xaa")
        self.assertEqual(img[43:54], b"KVM-SETUP  ")
        files = build_setup_img.list_fat12_files(img)
        self.assertEqual(set(files.keys()), {"install-linux.sh", "install-windows.ps1", "kvm_stub.py", "README.txt"})
        host_dir = os.path.join(ROOT, "host")
        for fname in ("install-linux.sh", "install-windows.ps1", "kvm_stub.py"):
            with open(os.path.join(host_dir, fname), "rb") as f:
                self.assertEqual(files[fname], f.read())


class ServerWsAndThemeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pin = "87654321"
        pin_file = os.path.join(self.tmp, "pin")
        with open(pin_file, "w") as f:
            f.write(self.pin + "\n")
        self.udc_file = os.path.join(self.tmp, "udc_state")
        with open(self.udc_file, "w") as f:
            f.write("configured\n")

        self.kbd_file = os.path.join(self.tmp, "hidg0")
        self.mouse_file = os.path.join(self.tmp, "hidg1")
        self.sys_file = os.path.join(self.tmp, "hidg2")
        for p in (self.kbd_file, self.mouse_file, self.sys_file):
            open(p, "wb").close()

        self._orig_env = os.environ.get("KVM_UDC_GLOB")
        os.environ["KVM_UDC_GLOB"] = self.udc_file
        self._orig_devs = (hid.KBD_DEV, hid.MOUSE_DEV, hid.SYS_DEV)
        hid.KBD_DEV, hid.MOUSE_DEV, hid.SYS_DEV = self.kbd_file, self.mouse_file, self.sys_file

        import server
        self.srv_mod = server
        self._orig_pin = getattr(server, "PIN", "")
        server.PIN = self.pin
        server.FAILS.clear()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        hid.KBD_DEV, hid.MOUSE_DEV, hid.SYS_DEV = self._orig_devs
        if self._orig_env is None:
            os.environ.pop("KVM_UDC_GLOB", None)
        else:
            os.environ["KVM_UDC_GLOB"] = self._orig_env
        self.srv_mod.PIN = self._orig_pin
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_http11_keepalive_theme_setup_and_s3_wake(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            # 1. Theme change over keep-alive connection
            hdrs = {"Content-Type": "application/json", "X-KVM-PIN": self.pin}
            conn.request("POST", "/api/theme", json.dumps({"theme": "braun"}), hdrs)
            r1 = conn.getresponse()
            self.assertEqual(r1.status, 200)
            self.assertEqual(json.loads(r1.read())["ui_theme"], "braun")
            sock1 = conn.sock

            # 2. Second request reuses exact same TCP socket (HTTP/1.1 keep-alive!)
            conn.request("GET", "/api/status", headers={"X-KVM-PIN": self.pin})
            r2 = conn.getresponse()
            self.assertEqual(r2.status, 200)
            st = json.loads(r2.read())
            self.assertIs(conn.sock, sock1)
            self.assertEqual(st["ui_theme"], "braun")
            self.assertIn("helper_state", st)

            # 3. /setup/install-linux.sh download preserves explicit Cache-Control: no-store
            conn.request("GET", "/setup/install-linux.sh")
            r3 = conn.getresponse()
            self.assertEqual(r3.status, 200)
            self.assertEqual(r3.getheader("Cache-Control"), "no-store")
            self.assertIn(b"kvm_stub.py", r3.read())

            # 3b. Static index.html + app.js emit Cache-Control: no-cache over same keep-alive socket
            conn.request("GET", "/")
            r_html = conn.getresponse()
            html_bytes = r_html.read()
            self.assertEqual(r_html.status, 200)
            self.assertEqual(r_html.getheader("Cache-Control"), "no-cache")
            self.assertIs(conn.sock, sock1)
            self.assertIn(b'id="tablet-btn"', html_bytes)
            self.assertIn(b'id="stats-dash"', html_bytes)
            self.assertIn(b'id="st-net-hist-tbody"', html_bytes)

            conn.request("GET", "/app.js?v=20261028b")
            r_js = conn.getresponse()
            js_bytes = r_js.read()
            self.assertEqual(r_js.status, 200)
            self.assertEqual(r_js.getheader("Cache-Control"), "no-cache")
            self.assertIn(b"computeResolvedLayout", js_bytes)

            # 4. S3 suspended state: mouse returns 409, but host wake succeeds with 200!
            with open(self.udc_file, "w") as f:
                f.write("suspended\n")
            conn.request("POST", "/api/mouse", json.dumps({"dx": 5, "dy": 0}), hdrs)
            r4 = conn.getresponse()
            r4.read()
            self.assertEqual(r4.status, 409)

            conn.request("POST", "/api/power", json.dumps({"target": "host", "action": "wake", "confirm": True}), hdrs)
            r5 = conn.getresponse()
            r5.read()
            self.assertEqual(r5.status, 200)
        finally:
            conn.close()

    def test_geek_rssi_round_trips_through_status(self):
        self.srv_mod.GEEK_RSSI.update(dbm=None, at=0.0)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

        def status(hdrs):
            conn.request("GET", "/api/status", headers=hdrs)
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            return json.loads(r.read())

        try:
            self.assertIsNone(status({"X-KVM-PIN": self.pin})["geek_rssi"])
            self.assertEqual(status({"X-KVM-PIN": self.pin, "X-Geek-RSSI": "-58"})["geek_rssi"], -58)
            # The console polls without the header and still sees the GEEK's last value.
            self.assertEqual(status({"X-KVM-PIN": self.pin})["geek_rssi"], -58)
            self.assertEqual(status({"X-KVM-PIN": self.pin, "X-Geek-RSSI": "junk"})["geek_rssi"], -58)
            # Without the PIN the header is ignored and the field is not sent.
            st = status({"X-Geek-RSSI": "-40"})
            self.assertNotIn("geek_rssi", st)
            self.assertEqual(status({"X-KVM-PIN": self.pin})["geek_rssi"], -58)
            self.srv_mod.GEEK_RSSI["at"] -= self.srv_mod.GEEK_RSSI_TTL + 1
            self.assertIsNone(status({"X-KVM-PIN": self.pin})["geek_rssi"])
        finally:
            conn.close()

    def test_websocket_auth_ping_and_mouse_dispatch(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            key = base64.b64encode(b"0123456789abcdef").decode("ascii")
            req = (
                f"GET /api/ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n\r\n"
            ).encode("ascii")
            s.sendall(req)
            resp = b""
            while b"\r\n\r\n" not in resp:
                resp += s.recv(1024)
            self.assertIn(b"101 Switching Protocols", resp)
            self.assertIn(ws.accept_key(key).encode("ascii"), resp)

            # First frame authenticates PIN
            s.sendall(_mask_ws_text(json.dumps({"op": "auth", "pin": self.pin})))
            hdr = s.recv(2)
            ln = hdr[1] & 0x7F
            hello = json.loads(s.recv(ln).decode("utf-8"))
            self.assertEqual(hello, {"ok": True, "op": "hello"})

            # Ping frame echoes timestamp
            s.sendall(_mask_ws_text(json.dumps({"op": "p", "t": 12345})))
            hdr = s.recv(2)
            pong = json.loads(s.recv(hdr[1] & 0x7F).decode("utf-8"))
            self.assertEqual(pong, {"op": "p", "t": 12345})

            # Relative mouse frame writes 4-byte HID report
            s.sendall(_mask_ws_text(json.dumps({"op": "m", "dx": 9, "dy": -4, "w": 1, "b": 1})))
            deadline = time.monotonic() + 2.0
            report = b""
            while time.monotonic() < deadline:
                report = open(self.mouse_file, "rb").read()
                if len(report) == 4:
                    break
                time.sleep(0.02)
            self.assertEqual(report, struct.pack("<Bbbb", 1, 9, -4, 1))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
