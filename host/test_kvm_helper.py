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
        self.assertEqual(p.feed(b"ls\r" + CTL[:4]), (b"ls\r", []))
        self.assertEqual(p.feed(CTL[4:] + b"helper\nok"), (b"ok", [b"helper"]))

    def test_garbage_released(self):
        p = kvm_helper.CtlParser()
        out, modes = p.feed(CTL + b"rootme\nx")
        self.assertEqual((out, modes), (CTL + b"rootme\nx", []))
        out, modes = p.feed(CTL + b"waytoolongmodename")
        self.assertEqual(modes, []); self.assertTrue(out.startswith(CTL))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
