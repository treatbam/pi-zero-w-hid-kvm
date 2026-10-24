#!/usr/bin/env python3
"""Loopback test: run kvm_helper.py against one end of a pty pair and play the Zero
on the other end (packets parsed with zero/web/hostview.py). Needs a non-root user."""
import os, struct, select, subprocess, sys, termios, time, tty, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "zero", "web"))
import kvm_helper, hostview

CTL = kvm_helper.CTL


class Parser(unittest.TestCase):
    def test_split_and_partial(self):
        p = kvm_helper.CtlParser()
        self.assertEqual(p.feed(b"ls\r" + CTL[:4]), [("data", b"ls\r")])
        self.assertEqual(p.feed(CTL[4:] + b"helper\nok"), [("mode", b"helper"), ("data", b"ok")])

    def test_prefix_split_byte_by_byte(self):
        p = kvm_helper.CtlParser(); segs = []
        for ch in CTL + b"shell\n":
            segs += p.feed(bytes([ch]))
        self.assertEqual(segs, [("mode", b"shell")])

    def test_order_around_packets(self):
        p = kvm_helper.CtlParser()
        self.assertEqual(p.feed(b"a" + CTL + b"shell\nb" + CTL + b"helper\nc"),
                         [("data", b"a"), ("mode", b"shell"), ("data", b"b"), ("mode", b"helper"), ("data", b"c")])

    def test_garbage_released(self):
        p = kvm_helper.CtlParser()
        self.assertEqual(p.feed(CTL + b"rootme\nx"), [("data", CTL + b"rootme\nx")])
        segs = p.feed(CTL + b"waytoolongmodename")
        self.assertTrue(all(k == "data" for k, _ in segs)); self.assertTrue(segs[0][1].startswith(CTL))

    def test_ctl_commands_and_clipboard(self):
        import base64
        p = kvm_helper.CtlParser()
        b64 = base64.b64encode("clip-test-✓".encode("utf-8"))
        stream = CTL + b"frame\n" + CTL + b"loupe\n" + CTL + b"cget\n" + CTL + b"cset:" + b64 + b"\n"
        self.assertEqual(p.feed(stream), [
            ("ctl", b"frame"),
            ("ctl", b"loupe"),
            ("ctl", b"cget"),
            ("ctl", b"cset:" + b64),
        ])
        # Invalid base64 in cset is released as data
        bad = CTL + b"cset:not valid b64!\n"
        self.assertEqual(p.feed(bad), [("data", bad)])


class Sensors(unittest.TestCase):
    """read_temp(): a CPU sensor wins; hwmon-only hosts still report a temperature."""

    def _temp(self, zones, hwmon):
        import unittest.mock as mock
        reads = dict(zones)   # thermal zone dir -> (type, millidegrees)

        def fake_glob(pattern):
            return sorted(reads) if "thermal_zone" in pattern else []

        real_open = open

        def fake_open(path, *a, **k):
            for zone, (kind, milli) in reads.items():
                if path == os.path.join(zone, "type"):
                    return mock.mock_open(read_data=kind + "\n")()
                if path == os.path.join(zone, "temp"):
                    return mock.mock_open(read_data=str(milli))()
            return real_open(path, *a, **k)

        with mock.patch.object(kvm_helper.glob, "glob", fake_glob), \
             mock.patch("builtins.open", fake_open), \
             mock.patch.object(kvm_helper, "_hwmon_temps", lambda: hwmon):
            return kvm_helper.read_temp()

    def test_hwmon_only_host_has_a_temperature(self):
        self.assertEqual(self._temp({}, [("k10temp", 57.5), ("nvme", 41.0)]), 57.5)

    def test_cpu_sensor_beats_a_hotter_board_sensor(self):
        zones = {"/z0": ("acpitz", 90000)}
        self.assertEqual(self._temp(zones, [("k10temp", 60.0)]), 60.0)

    def test_falls_back_to_hottest_other_sensor(self):
        self.assertEqual(self._temp({}, [("nvme", 41.0), ("acpitz", 47.0)]), 47.0)

    def test_no_sensor_at_all(self):
        self.assertIsNone(self._temp({}, []))

    def test_net_packet_always_carries_the_cpu_count(self):
        import unittest.mock as mock
        sent = []
        with mock.patch.object(kvm_helper, "write_all", lambda fd, blob: sent.append(blob)), \
             mock.patch.object(kvm_helper, "read_net_counters", lambda: None):
            kvm_helper.send_helper_packets(-1, "", None, want_frame=False)
        view = hostview.HostView()
        view.feed(sent[0])
        self.assertIsNotNone(view._ncpu)
        self.assertGreaterEqual(view._ncpu, 1)


class Loopback(unittest.TestCase):
    def setUp(self):
        self.m, s = os.openpty()
        tty.setraw(self.m); tty.setraw(s)
        self.slave_name = os.ttyname(s)
        self.s = s  # keep open so the pty doesn't hang up between helper reopens
        env = dict(os.environ, KVM_HOST_DEV=self.slave_name, KVM_DISPLAY="")
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "kvm_helper.py")], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.hv = hostview.HostView(); self.text = b""

    def tearDown(self):
        self.p.kill(); self.p.wait()
        try:
            print(self.p.stdout.read().decode(), file=sys.stderr)
        finally:
            self.p.stdout.close()
        os.close(self.m); os.close(self.s)

    def pump(self, until, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r, _, _ = select.select([self.m], [], [], 0.1)
            if r: self.text += self.hv.feed(os.read(self.m, 65536))
            if until(): return True
        return False

    def mode(self): return self.hv.mode()[0]

    def test_shell_echo_exit_and_helper_packet(self):
        self.assertTrue(self.pump(lambda: self.mode() == "helper"), "no helper ack")
        # Verify clipboard push + pull (cset -> cget) round-trip in helper mode
        import base64
        b64 = base64.b64encode(b"bench-clip-123")
        os.write(self.m, CTL + b"cset:" + b64 + b"\n")
        self.assertTrue(self.pump(lambda: self.hv.clipboard()[0] == "bench-clip-123"), "no clip cset ack")
        seq_after_set = self.hv.clipboard()[2]
        os.write(self.m, CTL + b"cget\n")
        self.assertTrue(self.pump(lambda: self.hv.clipboard()[2] > seq_after_set), "no clip cget ack")
        self.assertEqual(self.hv.clipboard()[0], "bench-clip-123")
        os.write(self.m, CTL + b"shell\n")
        self.assertTrue(self.pump(lambda: self.mode() == "shell"), "no shell ack")
        # Control packets arriving during shell mode must be stripped and ignored without leaking into bash
        os.write(self.m, CTL + b"frame\n" + CTL + b"loupe\n" + CTL + b"cget\n" + b"echo hi$((40+2))\r")
        self.assertTrue(self.pump(lambda: b"hi42" in self.text), self.text[-300:])
        self.assertNotIn(b"KVMCTL", self.text)
        os.write(self.m, b"exit\r")
        self.assertTrue(self.pump(lambda: self.mode() == "helper"), "no return after exit")
        # second round: leave shell with a helper packet instead
        self.text = b""
        os.write(self.m, CTL + b"shell\n")
        self.assertTrue(self.pump(lambda: self.mode() == "shell"))
        os.write(self.m, b"echo $$ > /tmp/kvm_test_pid\r")
        def _has_pid():
            if not os.path.exists("/tmp/kvm_test_pid"):
                return False
            with open("/tmp/kvm_test_pid") as pf:
                return bool(pf.read().strip())
        self.assertTrue(self.pump(_has_pid))
        with open("/tmp/kvm_test_pid") as pf:
            pid = int(pf.read())
        os.remove("/tmp/kvm_test_pid")
        os.write(self.m, CTL + b"helper\n")
        self.assertTrue(self.pump(lambda: self.mode() == "helper"), "no return after helper packet")
        time.sleep(0.3)
        def _proc_running(p):
            if not os.path.exists(f"/proc/{p}"):
                return False
            with open(f"/proc/{p}/stat") as sf:
                return "Z" not in sf.read().split()[2]
        self.assertFalse(_proc_running(pid), "bash still running")
        self.assertFalse(CTL in self.text)

    def test_trailing_bytes_route_on_both_switches(self):
        self.assertTrue(self.pump(lambda: self.mode() == "helper"))
        # helper -> shell: bytes before the packet are dropped, bytes after go to bash
        os.write(self.m, b"echo DROP$((1+1))\r" + CTL + b"shell\necho KEEP$((1+1))\r")
        self.assertTrue(self.pump(lambda: b"KEEP2" in self.text), self.text[-300:])
        self.assertNotIn(b"DROP2", self.text)
        # shell -> helper: bytes before go to bash, bytes after are dropped
        f = "/tmp/kvm_test_route"
        for x in (f, f + "2"):
            if os.path.exists(x): os.remove(x)
        os.write(self.m, f"echo x > {f}\r".encode() + CTL + b"helper\n" + f"echo y > {f}2\r".encode())
        self.assertTrue(self.pump(lambda: self.mode() == "helper"))
        time.sleep(0.5)
        self.assertTrue(os.path.exists(f), "bytes before helper packet did not reach bash")
        self.assertFalse(os.path.exists(f + "2"), "bytes after helper packet reached a shell")
        os.remove(f)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class NetPickerTest(unittest.TestCase):
    def test_follows_busiest_with_hold(self):
        p = kvm_helper.NetPicker()
        self.assertEqual(p.choose({"eth0": (0, 0), "wlan0": (0, 0)}, "eth0"), "eth0")
        c = {"eth0": (100, 0), "wlan0": (0, 0)}
        self.assertEqual(p.choose(c, "eth0"), "eth0")
        for i, want in enumerate(["eth0", "eth0", "wlan0"], 1):
            c = {"eth0": (100, 0), "wlan0": (10000 * i, 0)}
            self.assertEqual(p.choose(c, "eth0"), want)
        # idle: keep the current pick
        self.assertEqual(p.choose(c, "eth0"), "wlan0")

    def test_packs_iface_into_net(self):
        pkt = kvm_helper.pack_net(1.0, 2.0, 4, [50], "enp5s0")
        self.assertEqual(struct.unpack("<I", pkt[5:9])[0], 36)
        self.assertEqual(len(kvm_helper.pack_net(1.0, 2.0, 4, [50])) - 9, 20)


class TopProcsTest(unittest.TestCase):
    def test_top_procs_merged_and_sorted(self):
        prev = (10, {1: ("a", 0), 2: ("b", 0), 3: ("b", 0), 4: ("c", 0)})
        now = {1: ("a", 50), 2: ("b", 40), 3: ("b", 40), 4: ("c", 0), 5: ("new", 9)}
        self.assertEqual(kvm_helper.top_procs(prev, now, 11, clk_tck=100), [["b", 80.0], ["a", 50.0]])
        self.assertEqual(kvm_helper.top_procs(None, now, 11), [])
