#!/usr/bin/env python3
"""Preview (type 7), extended stats (type 8), quality ack (type 9), Zero-side guards,
the non-blocking Outbox, nvidia-smi backoff and the stdlib-only helper run."""
import io, json, os, select, shutil, struct, subprocess, sys, tempfile, threading, time, tty, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "zero", "web"))
import kvm_helper, hostview, kvm_preview, kvm_stats
try:
    from PIL import Image
except ImportError:
    Image = None
needs_pil = unittest.skipUnless(Image is not None, "Pillow not installed (preview tests skipped)")
CTL = kvm_helper.CTL

class Tiles(unittest.TestCase):
    def frame(self, enc, fill=0):
        return bytearray([fill]) * (enc.w * enc.h * 3)

    def payload(self, pkt):
        self.assertEqual(pkt[:4], kvm_preview.MAGIC); self.assertEqual(pkt[4], 7)
        ln = struct.unpack_from("<I", pkt, 5)[0]; self.assertEqual(len(pkt), 9 + ln)
        return pkt[9:]

    def test_diff_only_changed_tiles(self):
        enc = kvm_preview.TileEncoder("low")
        f0 = self.frame(enc)
        p = self.payload(enc.encode(bytes(f0)))
        ver, flags, q, w, h, n = struct.unpack_from("<BBBHHH", p)
        self.assertEqual((ver, flags & 1, q, w, h, n), (1, 1, 0, 640, 360, 10 * 6))
        self.assertEqual(struct.unpack_from("<BBBHHH", self.payload(enc.encode(bytes(f0), bytes(f0))))[5], 0)
        f1 = bytearray(f0); off = (70 * 640 + 130) * 3; f1[off:off + 3] = b"\xff\xff\xff"
        p = self.payload(enc.encode(bytes(f1), bytes(f0)))
        self.assertEqual(struct.unpack_from("<BBBHHH", p)[1:6:4], (0, 1))
        self.assertEqual(struct.unpack_from("<HHHH", p, 9), (128, 64, 64, 64))

    def test_budget_leftovers_sent_next_tick(self):
        enc = kvm_preview.TileEncoder("low"); enc.budget = 1
        noise = os.urandom(640 * 360 * 3)
        n1 = struct.unpack_from("<BBBHHH", self.payload(enc.encode(noise)))[5]
        self.assertEqual(n1, 1)
        total = n1
        for _ in range(80):
            total += struct.unpack_from("<BBBHHH", self.payload(enc.encode(noise, noise)))[5]
        self.assertEqual(total, 60)

    def test_round_trip_through_zero_parser(self):
        enc = kvm_preview.TileEncoder("med")
        f = bytes(self.frame(enc, 0x80))
        hv = hostview.HostView(); hv.feed(kvm_helper.pack_mode(b"helper"))
        self.assertEqual(hv.feed(b"pre" + enc.encode(f) + b"post"), b"prepost")
        pv = hv.preview(0, True)
        self.assertTrue(pv["reset"]); self.assertEqual((pv["w"], pv["h"], pv["quality"]), (960, 540, "med"))
        self.assertEqual(len(pv["tiles"]), 15 * 9)
        import base64
        t = pv["tiles"][-1]
        im = Image.open(io.BytesIO(base64.b64decode(t["jpg"])))
        self.assertEqual(im.size, (t["w"], t["h"]))
        self.assertEqual(hv.preview(pv["seq"], True)["tiles"], [])
        self.assertIsNone(hv.preview(0, False))
        # a corrupt tile table is dropped whole
        bad = bytearray(kvm_preview.TileEncoder("med").encode(f)); bad[9 + 9 + 8] ^= 0xFF
        seq = hv.preview(0, True)["seq"]; hv.feed(bytes(bad)); self.assertEqual(hv.preview(0, True)["seq"], seq)

    def test_quality_switch_resets(self):
        enc = kvm_preview.TileEncoder("high"); hv = hostview.HostView(); hv.feed(kvm_helper.pack_mode(b"helper"))
        hv.feed(enc.encode(bytes(enc.w * enc.h * 3)))
        enc.set_quality("low"); hv.feed(enc.encode(bytes(enc.w * enc.h * 3)) + kvm_preview.pack_quality("low"))
        pv = hv.preview(5, True)
        self.assertTrue(pv["reset"]); self.assertEqual((pv["w"], pv["quality"]), (640, "low"))
        self.assertEqual(hv.quality(), "low")
        p = kvm_helper.CtlParser()
        self.assertEqual(p.feed(CTL + b"q:hi"), []); self.assertEqual(p.feed(b"gh\n"), [("ctl", b"q:high")])
        self.assertEqual(p.feed(CTL + b"q:ultra\n")[0][0], "data")


class ZeroGuards(unittest.TestCase):
    """Fix 1 and 2: the Zero's parser in shell mode, the partial-payload time cap, and bounds."""
    def clock(self):
        self.t = 0.0
        return hostview.HostView(now=lambda: self.t)

    def ack(self, hv, mode):
        hv.feed(kvm_helper.pack_mode(mode))

    def test_shell_mode_ignores_types_7_to_9(self):
        hv = self.clock(); self.ack(hv, b"shell")
        fake = kvm_preview.envelope(7, struct.pack("<BBBHHH", 1, 1, 0, 64, 64, 0))
        fake6 = kvm_preview.pack_quality("high")
        out = hv.feed(b"ls\r\n" + fake + fake6 + b"$ ")
        self.assertEqual(out, b"ls\r\n" + fake + fake6 + b"$ ")
        self.assertIsNone(hv.preview(0, True)); self.assertIsNone(hv.quality())
        # a fake big type-4 header can't hold the terminal either
        out = hv.feed(kvm_preview.MAGIC + b"\x07" + struct.pack("<I", 500000) + b"more text")
        self.assertTrue(out.endswith(b"more text"))
        self.ack(hv, b"helper"); hv.feed(fake6); self.assertEqual(hv.quality(), "high")

    def test_partial_payload_released_after_time_cap(self):
        hv = self.clock(); self.ack(hv, b"helper")
        hdr = kvm_preview.MAGIC + b"\x02" + struct.pack("<I", 240 * 135 * 2)
        self.assertEqual(hv.feed(hdr + b"abc"), b"")          # held: could be a real still
        for i in range(10):                                      # steady text, but time passes
            self.t += 0.5
            out = hv.feed(b"x" * 10)
            if out:
                break
        self.assertLessEqual(self.t, hostview.PARTIAL_MAX_S + 0.5)
        self.assertTrue(out.startswith(hdr[:1]) or out.endswith(b"x"), out)
        rest = out + hv.feed(b"\n")
        self.assertIn(b"abc", rest)

    def test_preview_bounds(self):
        def frame(w, h, tiles, key=1):
            body = b"".join(struct.pack("<HHHHI", x, y, tw, th, len(j)) + j for x, y, tw, th, j in tiles)
            return kvm_preview.envelope(7, struct.pack("<BBBHHH", 1, key, 0, w, h, len(tiles)) + body)
        hv = self.clock(); self.ack(hv, b"helper")
        hv.feed(frame(640, 360, [(0, 0, 64, 64, b"J" * 10)]))
        good = hv.preview(0, True)["seq"]
        for bad in (frame(640, 360, [(10, 0, 64, 64, b"J")]),        # x not a multiple of 64
                    frame(640, 360, [(0, 0, 128, 64, b"J")]),        # tile wider than 64
                    frame(1920, 1080, [(0, 0, 64, 64, b"J")]),       # bigger than 1280x720
                    frame(640, 360, [(640, 0, 64, 64, b"J")])):      # outside the frame
            hv.feed(bad)
            self.assertEqual(hv.preview(0, True)["seq"], good)
        hv.pv_max_bytes = 25
        hv.feed(frame(640, 360, [(64, 0, 64, 64, b"K" * 10)], key=0))
        self.assertEqual(len(hv.preview(0, True)["tiles"]), 2)
        hv.feed(frame(640, 360, [(128, 0, 64, 64, b"L" * 10)], key=0))     # 30 > 25: reset
        self.assertEqual(hv.preview(0, True)["tiles"], []); self.assertEqual(hv._pv_bytes, 0)


class ReviewNits(unittest.TestCase):
    def test_helper_ack_expires_and_shell_request_blocks(self):
        t = [0.0]; hv = hostview.HostView(now=lambda: t[0])
        q9 = kvm_preview.pack_quality("high")
        hv.feed(kvm_helper.pack_mode(b"helper")); hv.feed(q9); self.assertEqual(hv.quality(), "high")
        t[0] += hostview.MODE_ACK_TTL_S + 1                     # helper went quiet / shell faked once
        self.assertEqual(hv.feed(kvm_preview.pack_quality("low")), kvm_preview.pack_quality("low"))
        self.assertEqual(hv.quality(), "high")
        hv.feed(kvm_helper.pack_mode(b"helper")); hv.requested_mode = "shell"   # /api/mode asked for shell
        self.assertEqual(hv.feed(q9), q9)                        # faked helper ack can't re-open 7-9
        hv.requested_mode = "helper"; hv.feed(kvm_preview.pack_quality("low"))
        self.assertEqual(hv.quality(), "low")

    def test_nvidia_smi_backoff_then_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            n, smi = f"{d}/n", f"{d}/nvidia-smi"
            open(n, "w").write("0")
            open(smi, "w").write(f"""#!/bin/sh
c=$(cat {n}); echo $((c+1)) > {n}
if [ "$c" -lt 2 ]; then echo garbage; exit 1; fi
echo "33, 61"
""")
            os.chmod(smi, 0o755)
            e = kvm_stats.ExtStats(root=d, nvidia_smi=smi)
            sleeps = []

            def fake_sleep(x):
                sleeps.append((x, e._nv))
                if len(sleeps) == 3:
                    os.remove(smi)                               # binary gone -> loop ends
            e._sleep = fake_sleep
            e._nv_loop(every=10.0)
        self.assertEqual([x for x, _ in sleeps], [20.0, 40.0, 10.0])
        self.assertEqual(sleeps[2][1], (33.0, 61.0))
        self.assertIsNone(e.nvsmi)

    def test_outbox_queues_big_frame_without_timeout(self):
        m, s = os.openpty(); tty.setraw(m); tty.setraw(s)
        os.set_blocking(s, False)
        try:
            ob = kvm_helper.Outbox(s, stall_s=5.0)
            blob = os.urandom(300_000)
            t0 = time.monotonic(); ob.send(blob)
            self.assertLess(time.monotonic() - t0, 0.5, "send blocked")
            self.assertGreater(ob.pending(), 0)
            got = bytearray()
            end = time.monotonic() + 10
            while len(got) < len(blob) and time.monotonic() < end:
                if select.select([m], [], [], 0.05)[0]:
                    got += os.read(m, 65536)
                ob.pump()
            self.assertEqual(bytes(got), blob)
        finally:
            os.close(m); os.close(s)
        m, s = os.pipe(); os.set_blocking(s, False)
        try:                                   # nobody reads: only a real stall raises
            ob2 = kvm_helper.Outbox(s, stall_s=0.2); ob2.send(os.urandom(300_000))
            time.sleep(0.3)
            with self.assertRaises(TimeoutError):
                ob2.pump()
        finally:
            os.close(m); os.close(s)


class StdlibOnly(unittest.TestCase):
    """Derek's SBC case: no Pillow/psutil/numpy, no ffmpeg/xrandr/nvidia-smi, no X."""
    def test_helper_runs_on_stdlib(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/bin")
            os.symlink(shutil.which("bash"), f"{d}/bin/bash")
            open(f"{d}/block.py", "w").write(
                "import sys, runpy\n"
                "class B:\n"
                "  def find_spec(self, name, path=None, target=None):\n"
                "    if name.split('.')[0] in ('PIL', 'psutil', 'numpy'): raise ImportError('blocked ' + name)\n"
                "sys.meta_path.insert(0, B())\n"
                f"sys.argv = [{os.path.join(HERE, 'kvm_helper.py')!r}]\n"
                "runpy.run_path(sys.argv[0], run_name='__main__')\n")
            m, s = os.openpty(); tty.setraw(m); tty.setraw(s)
            env = {"PATH": f"{d}/bin", "HOME": os.environ.get("HOME", "/tmp"), "KVM_HOST_DEV": os.ttyname(s)}
            p = subprocess.Popen([sys.executable, f"{d}/block.py"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            hv, text = hostview.HostView(), [b""]

            def pump(until, t=8):
                end = time.monotonic() + t
                while time.monotonic() < end:
                    if select.select([m], [], [], 0.1)[0]: text[0] += hv.feed(os.read(m, 65536))
                    if until(): return True
                return False
            try:
                self.assertTrue(pump(lambda: hv.mode()[0] == "helper"), "no helper ack")
                os.write(m, CTL + b"q:high\n")
                self.assertTrue(pump(lambda: hv.quality() == "off"), "preview should ack off")
                self.assertIsNone(hv.preview(0, True))
                os.write(m, CTL + b"shell\n")
                self.assertTrue(pump(lambda: hv.mode()[0] == "shell"))
                os.write(m, b"echo std$((6*7))\r")
                self.assertTrue(pump(lambda: b"std42" in text[0]), text[0][-200:])
                os.write(m, b"exit\r")
                self.assertTrue(pump(lambda: hv.mode()[0] == "helper"))
            finally:
                p.kill(); out = p.communicate()[0].decode(); os.close(m); os.close(s)
            self.assertIn("optional: missing pillow, ffmpeg, xrandr, psutil, nvidia-smi, display", out)
            self.assertEqual(out.count("optional:"), 1)
            self.assertNotIn("Traceback", out)


class Stats(unittest.TestCase):
    def test_missing_everything(self):
        with tempfile.TemporaryDirectory() as d:
            e = kvm_stats.ExtStats(root=d, nvidia_smi="")
            self.assertEqual(e.sample(), {"v": 1})
        hv = hostview.HostView()
        hv.feed(kvm_helper.pack_mode(b"helper") + kvm_helper.pack_stats(40, 1, 50) + kvm_stats.pack_ext({"v": 1}))
        f = hv.status_fields(True)
        self.assertEqual(f["link"], "LINKED")
        for k in ("pc_cores_all", "pc_gpu_pct", "pc_cpu_temp_c", "pc_gpu_temp_c", "pc_nvme_temp_c"):
            self.assertIsNone(f[k])

    def test_fake_sysfs(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(f"{d}/proc")
            for name, t in (("coretemp", 51000), ("nvme", 38500), ("acpitz", 99000)):
                h = f"{d}/sys/class/hwmon/hwmon_{name}"; os.makedirs(h)
                open(f"{h}/name", "w").write(name + "\n"); open(f"{h}/temp1_input", "w").write(str(t))
            g = f"{d}/sys/class/drm/card0/device"; os.makedirs(g); open(f"{g}/gpu_busy_percent", "w").write("17\n")
            open(f"{d}/proc/stat", "w").write("cpu  1 1 1 1\ncpu0 10 0 10 80 0\ncpu1 0 0 0 100 0\n")
            e = kvm_stats.ExtStats(root=d, nvidia_smi="")
            e.sample()
            open(f"{d}/proc/stat", "w").write("cpu  1 1 1 1\ncpu0 60 0 10 130 0\ncpu1 0 0 0 200 0\n")
            s = e.sample()
        self.assertEqual(s, {"v": 1, "cores": [50.0, 0.0], "gpu_pct": 17.0, "temps": {"cpu": 51.0, "nvme": 38.5}})
        hv = hostview.HostView()
        hv.feed(kvm_helper.pack_mode(b"helper") + kvm_helper.pack_stats(40, 1, 50) + kvm_stats.pack_ext(s))
        f = hv.status_fields(True)
        self.assertEqual((f["pc_cores_all"], f["pc_gpu_pct"], f["pc_cpu_temp_c"], f["pc_nvme_temp_c"], f["pc_gpu_temp_c"]),
                         ([50.0, 0.0], 17.0, 51.0, 38.5, None))

    def test_bad_ext_rejected(self):
        hv = hostview.HostView(); hv.feed(kvm_helper.pack_mode(b"helper"))
        for bad in (b"not json", json.dumps({"v": 2, "gpu_pct": 5}).encode(),
                    json.dumps({"v": 1, "cores": ["x"], "gpu_pct": 500, "temps": {"cpu": "hot"}}).encode()):
            hv.feed(kvm_helper.pack_stats(40, 1, 50) + kvm_preview.envelope(8, bad))
            f = hv.status_fields(True)
            self.assertIsNone(f["pc_cores_all"]); self.assertIsNone(f["pc_gpu_pct"]); self.assertIsNone(f["pc_cpu_temp_c"])



class PreviewLoopback(unittest.TestCase):
    def setUp(self):
        self.m, s = os.openpty()
        tty.setraw(self.m); tty.setraw(s)
        self.slave_name = os.ttyname(s)
        self.s = s  # keep open so the pty doesn't hang up between helper reopens
        env = dict(os.environ, KVM_HOST_DEV=self.slave_name, KVM_DISPLAY="", KVM_PREVIEW_SOURCE="test")
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "kvm_helper.py")], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.hv = hostview.HostView(); self.text = b""

    def tearDown(self):
        self.p.kill(); self.p.wait(); print(self.p.stdout.read().decode(), file=sys.stderr)
        os.close(self.m); os.close(self.s)

    def pump(self, until, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([self.m], [], [], 0.1)
            if r: self.text += self.hv.feed(os.read(self.m, 65536))
            if until(): return True
        return False

    def mode(self): return self.hv.mode()[0]


    def test_nothing_7_to_9_until_asked_then_off(self):
        self.assertTrue(self.pump(lambda: self.mode() == "helper"))
        self.pump(lambda: False, timeout=3)
        self.assertIsNone(self.hv.quality()); self.assertIsNone(self.hv.preview(0, True))
        self.assertIsNone(self.hv._ext, "extended stats sent before q:")
        os.write(self.m, CTL + b"q:low\n")
        self.assertTrue(self.pump(lambda: (self.hv.preview(0, True) or {}).get("w") == 640))
        self.assertTrue(self.pump(lambda: self.hv._ext is not None), "no ext stats after q:")
        os.write(self.m, CTL + b"q:off\n")
        self.assertTrue(self.pump(lambda: self.hv.quality() == "off"))
        self.pump(lambda: False, timeout=0.5); s1 = self.hv._pv_seq; e1 = self.hv._ext_at
        self.pump(lambda: False, timeout=2.5)
        self.assertEqual(self.hv._pv_seq, s1, "preview still running after q:off")
        self.assertEqual(self.hv._ext_at, e1, "ext stats still sent after q:off")

    @needs_pil
    def test_preview_quality_and_shell_pause(self):
        os.write(self.m, CTL + b"q:med\n")
        self.assertTrue(self.pump(lambda: (self.hv.preview(0, True) or {}).get("w") == 960), "no med preview")
        self.assertEqual(self.hv.quality(), "med")
        os.write(self.m, CTL + b"q:low\n")
        self.assertTrue(self.pump(lambda: self.hv.quality() == "low" and
                                  (self.hv.preview(0, True) or {}).get("w") == 640), "quality switch failed")
        s1 = self.hv.preview(0, True)["seq"]
        self.assertTrue(self.pump(lambda: self.hv.preview(0, True)["seq"] > s1 + 1), "preview not moving")
        os.write(self.m, CTL + b"shell\n")
        self.assertTrue(self.pump(lambda: self.mode() == "shell"))
        self.pump(lambda: False, timeout=0.5)
        s2 = self.hv._pv_seq
        self.pump(lambda: False, timeout=2.5)
        self.assertEqual(self.hv._pv_seq, s2, "preview kept running in shell mode")
        os.write(self.m, CTL + b"helper\n")
        self.assertTrue(self.pump(lambda: self.mode() == "helper" and self.hv._pv_seq > s2), "preview did not resume")
        self.assertEqual(self.hv.preview(0, True)["w"], 640)



if __name__ == "__main__":
    unittest.main(verbosity=2)
