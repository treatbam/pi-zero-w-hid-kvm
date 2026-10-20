#!/usr/bin/env python3
"""Loopback test: run kvm_helper.py against one end of a pty pair and play the Zero
on the other end (packets parsed with zero/web/hostview.py). Needs a non-root user."""
import os, select, subprocess, sys, termios, time, tty, unittest
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

    def test_shell_echo_exit_and_helper_packet(self):
        self.assertTrue(self.pump(lambda: self.mode() == "helper"), "no helper ack")
        os.write(self.m, CTL + b"shell\n")
        self.assertTrue(self.pump(lambda: self.mode() == "shell"), "no shell ack")
        os.write(self.m, b"echo hi$((40+2))\r")
        self.assertTrue(self.pump(lambda: b"hi42" in self.text), self.text[-300:])
        os.write(self.m, b"exit\r")
        self.assertTrue(self.pump(lambda: self.mode() == "helper"), "no return after exit")
        # second round: leave shell with a helper packet instead
        self.text = b""
        os.write(self.m, CTL + b"shell\n")
        self.assertTrue(self.pump(lambda: self.mode() == "shell"))
        os.write(self.m, b"echo $$ > /tmp/kvm_test_pid\r")
        self.assertTrue(self.pump(lambda: os.path.exists("/tmp/kvm_test_pid") and open("/tmp/kvm_test_pid").read().strip()))
        pid = int(open("/tmp/kvm_test_pid").read()); os.remove("/tmp/kvm_test_pid")
        os.write(self.m, CTL + b"helper\n")
        self.assertTrue(self.pump(lambda: self.mode() == "helper"), "no return after helper packet")
        time.sleep(0.3)
        self.assertFalse(os.path.exists(f"/proc/{pid}") and "Z" not in open(f"/proc/{pid}/stat").read().split()[2],
                         "bash still running")
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
