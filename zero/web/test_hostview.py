"""Wire format between the host helper and the Zero.

A stats record or a 240x135 still may sit in the same byte stream as the
host terminal. Terminal bytes come back out. Frame bytes do not.
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hostview

FRAME_LEN = 240 * 135 * 2


def packet(typ, payload):
    return b"\xfe\xfdKV" + bytes([typ]) + struct.pack("<I", len(payload)) + payload


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class HostViewTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.view = hostview.HostView(now=self.clock)

    def _pkt(self, typ, payload):
        return b"\xfe\xfdKV" + struct.pack("<BI", typ, len(payload)) + payload

    def test_bash_exit_clears_shell_request_and_reopens_types_8_9(self):
        v = self.view
        v.requested_mode = "shell"
        self.assertEqual(v.feed(self._pkt(hostview.TYPE_MODE, b"\x01")), b"")
        ext = self._pkt(hostview.TYPE_EXT, b'{"v":1,"gpu_pct":5}')
        self.assertEqual(v.feed(ext), ext)               # shell: type 8 is terminal text
        self.clock.t = 1.0
        v.feed(self._pkt(hostview.TYPE_MODE, b"\x00"))   # bash hit Ctrl-D, helper acks
        self.assertIsNone(v.requested_mode)
        self.assertEqual(v.feed(ext), b"")
        self.assertEqual(v.ext()["gpu_pct"], 5.0)
        self.assertEqual(v.feed(self._pkt(hostview.TYPE_QUALITY, b"\x02")), b"")
        self.assertEqual(v.quality(), "high")

    def test_helper_ack_without_shell_keeps_request(self):
        self.view.requested_mode = "shell"
        self.view.feed(self._pkt(hostview.TYPE_MODE, b"\x00"))   # stale helper ack, shell not up yet
        self.assertEqual(self.view.requested_mode, "shell")

    def test_plain_terminal_bytes_pass_through(self):
        self.assertEqual(self.view.feed(b"login: "), b"login: ")

    def test_stats_record_is_removed_and_arms_linked(self):
        raw = packet(1, struct.pack("<fff", 62.0, 1.4, 71.0))
        out = self.view.feed(b"hi\n" + raw + b"yo")
        self.assertEqual(out, b"hi\nyo")
        st = self.view.status_fields(True)
        self.assertEqual(st["link"], "LINKED")
        self.assertEqual(st["pc_temp_c"], 62.0)
        self.assertEqual(st["pc_load1"], 1.4)
        self.assertEqual(st["pc_mem_pct"], 71)
        self.assertEqual(st["pc_age_s"], 0.0)
        self.assertFalse(st["frame_fresh"])

    def test_frame_is_removed_and_stats_still_pass(self):
        frame = bytes(i % 256 for i in range(FRAME_LEN))
        text = self.view.feed(b"abc" + packet(2, frame) + b"def")
        self.assertEqual(text, b"abcdef")
        self.assertEqual(self.view.frame_bytes(True), frame)
        self.assertTrue(self.view.status_fields(True)["frame_fresh"])
        # A picture alone is not a PC reading.
        self.assertEqual(self.view.status_fields(True)["link"], "KEYS")

    def test_packet_split_across_reads_stays_out_of_the_terminal(self):
        raw = packet(1, struct.pack("<fff", 40.0, 0.2, 10.0))
        acc = b""
        for i in range(0, len(raw), 5):
            acc += self.view.feed(raw[i:i + 5])
        self.assertEqual(acc, b"")
        self.assertEqual(self.view.status_fields(True)["link"], "LINKED")

    def test_partial_magic_is_held_until_it_fails(self):
        magic = b"\xfe\xfdKV"
        self.assertEqual(self.view.feed(b"ok" + magic[:3]), b"ok")
        self.assertEqual(self.view.feed(b"Zrest"), magic[:3] + b"Zrest")

    def test_bad_length_is_not_eaten(self):
        bad = b"\xfe\xfdKV" + bytes([1]) + struct.pack("<I", 99) + b"HELLO"
        self.assertEqual(self.view.feed(bad), bad)
        self.assertEqual(self.view.status_fields(True)["link"], "KEYS")
        self.assertIsNone(self.view.status_fields(True)["pc_temp_c"])

    def test_non_finite_stats_do_not_count_as_a_reading(self):
        raw = packet(1, struct.pack("<fff", float("nan"), 1.0, 1.0))
        self.assertEqual(self.view.feed(raw), b"")
        self.assertEqual(self.view.status_fields(True)["link"], "KEYS")
        self.assertIsNone(self.view.status_fields(True)["pc_temp_c"])

    def test_stale_reading_drops_linked_to_keys(self):
        self.view.feed(packet(1, struct.pack("<fff", 50.0, 0.1, 20.0)))
        self.clock.t = 6.0
        self.assertEqual(self.view.status_fields(True)["link"], "LINKED")
        self.clock.t = 6.01
        st = self.view.status_fields(True)
        self.assertEqual(st["link"], "KEYS")
        self.assertIsNone(st["pc_temp_c"])

    def test_stale_frame_is_dropped(self):
        frame = b"\x00" * FRAME_LEN
        self.view.feed(packet(2, frame))
        self.assertEqual(self.view.frame_bytes(True), frame)
        self.clock.t = 6.01
        self.assertIsNone(self.view.frame_bytes(True))
        self.assertFalse(self.view.status_fields(True)["frame_fresh"])

    def test_no_host_hides_a_fresh_reading(self):
        self.view.feed(packet(1, struct.pack("<fff", 50.0, 0.1, 20.0)))
        self.view.feed(packet(2, b"\x11" * FRAME_LEN))
        st = self.view.status_fields(False)
        self.assertEqual(st["link"], "NO HOST")
        self.assertIsNone(st["pc_temp_c"])
        self.assertFalse(st["frame_fresh"])
        self.assertIsNone(self.view.frame_bytes(False))

    def test_drop_incomplete_forgets_a_partial_frame(self):
        frame = packet(2, b"\xab" * FRAME_LEN)
        self.assertEqual(self.view.feed(frame[:20]), b"")
        self.assertTrue(self.view.drop_incomplete())
        stats = packet(1, struct.pack("<fff", 55.0, 0.3, 12.0))
        self.assertEqual(self.view.feed(stats), b"")
        self.assertEqual(self.view.status_fields(True)["pc_temp_c"], 55.0)
        self.assertIsNone(self.view.frame_bytes(True))

    def test_drop_incomplete_keeps_a_short_magic_prefix(self):
        self.assertEqual(self.view.feed(b"ok\xfe\xfd"), b"ok")
        self.assertFalse(self.view.drop_incomplete())
        self.assertEqual(self.view.feed(b"Zrest"), b"\xfe\xfdZrest")

    def test_drop_incomplete_clears_a_partial_header(self):
        # MAGIC plus 1..4 more bytes is a header that will never finish.
        for n in range(4, 9):
            self.view.reset()
            stats = packet(1, struct.pack("<fff", 1.0, 2.0, 3.0))
            self.assertEqual(self.view.feed(stats[:n]), b"")
            self.assertTrue(self.view.drop_incomplete(), n)
            self.assertEqual(self.view.feed(b"$ "), b"$ ")

    def test_restart_inside_a_frame_resyncs_without_terminal_noise(self):
        # Helper dies 1000 bytes into a still and comes back with stats + a new still
        # inside one second, so drop_incomplete() never runs.
        frame = packet(2, b"\x5a" * FRAME_LEN)
        stats = packet(1, struct.pack("<fff", 55.0, 0.3, 12.0))
        out = self.view.feed(frame[:1009])
        out += self.view.feed(stats + frame)
        self.assertEqual(out, b"")
        self.assertEqual(self.view.status_fields(True)["pc_temp_c"], 55.0)
        self.assertEqual(self.view.frame_bytes(True), b"\x5a" * FRAME_LEN)
        self.assertEqual(self.view.feed(b"login: "), b"login: ")

    def test_restart_header_split_across_reads_still_resyncs(self):
        frame = packet(2, b"\x5a" * FRAME_LEN)
        stats = packet(1, struct.pack("<fff", 40.0, 0.1, 9.0))
        out = self.view.feed(frame[:500] + stats[:6])
        out += self.view.feed(stats[6:] + b"ok")
        self.assertEqual(out, b"ok")
        self.assertEqual(self.view.status_fields(True)["pc_temp_c"], 40.0)

    def test_helper_packets_match_the_parser(self):
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), "..", "..", "host", "kvm_helper.py")
        spec = importlib.util.spec_from_file_location("kvm_helper", os.path.abspath(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        text = self.view.feed(mod.pack_stats(48.2, 0.5, 33))
        self.assertEqual(text, b"")
        self.assertEqual(self.view.status_fields(True)["pc_temp_c"], 48.2)
        self.assertEqual(self.view.status_fields(True)["pc_mem_pct"], 33)
        frame = bytes(FRAME_LEN)
        self.assertEqual(self.view.frame_seq(), 0)
        self.assertEqual(self.view.feed(mod.pack_frame(frame)), b"")
        self.assertEqual(self.view.frame_bytes(True), frame)
        self.assertEqual(self.view.frame_seq(), 1)
        rates, prev = mod.step_net(None, ("eth0", 1000, 400, 8), now=10)
        self.assertIsNone(rates)
        rates, prev = mod.step_net(prev, ("eth0", 5000, 1400, 8), now=12)
        self.assertEqual(rates, (2000.0, 500.0, 8))
        self.assertIsNone(mod.step_net(prev, ("eth0", 10, 10, 8), now=14)[0])
        cores, cprev = mod.step_cores(None, {"cpu0": (100, 200), "cpu1": (50, 200)})
        self.assertEqual(cores, [])
        cores, cprev = mod.step_cores(cprev, {"cpu0": (120, 300), "cpu1": (130, 300)})
        self.assertEqual(cores, [80, 20])
        self.assertEqual(self.view.feed(mod.pack_net(rates[0], rates[1], rates[2], cores)), b"")
        (top_proc, top_cpu), pprev = mod.step_top_proc(None, {1: ("systemd", 5), 10: ("python3", 100)}, now=10, clk_tck=100)
        # Tie-breaker when dticks == 0 picks higher cumulative ticks (python3 over systemd)
        (idle_proc, idle_cpu), _ = mod.step_top_proc(pprev, {1: ("systemd", 5), 10: ("python3", 100)}, now=11, clk_tck=100)
        self.assertEqual(idle_proc, "python3")
        self.assertEqual(idle_cpu, 0.0)
        (top_proc, top_cpu), pprev = mod.step_top_proc(pprev, {1: ("systemd", 5), 10: ("python3", 150), 20: ("idle", 10)}, now=12, clk_tck=100)
        self.assertEqual(top_proc, "python3")
        self.assertAlmostEqual(top_cpu, 25.0)
        self.assertEqual(self.view.feed(mod.pack_triage(42.4, top_cpu, "192.168.1.50", top_proc)), b"")
        st = self.view.status_fields(True)
        self.assertEqual(st["pc_rx_bps"], 2000.0)
        self.assertEqual(st["pc_tx_bps"], 500.0)
        self.assertEqual(st["pc_ncpu"], 8)
        self.assertEqual(st["pc_cores"], [80, 20])
        self.assertEqual(st["pc_disk_pct"], 42)
        self.assertEqual(st["pc_ip"], "192.168.1.50")
        self.assertEqual(st["pc_top_proc"], "python3")
        self.assertEqual(st["pc_top_cpu_pct"], 25.0)
        self.assertEqual(self.view.feed(mod.pack_clip("hello Ω")), b"")
        self.assertEqual(self.view.clipboard(), ("hello Ω", 0.0, 1))

    def test_net_and_triage_alone_do_not_link(self):
        self.assertEqual(self.view.feed(hostview.pack_net(1000, 20, 4, [50, 10])), b"")
        self.assertEqual(self.view.feed(hostview.pack_triage(55.0, 12.0, "10.0.0.2", "gcc")), b"")
        st = self.view.status_fields(True)
        self.assertEqual(st["link"], "KEYS")
        self.assertIsNone(st["pc_rx_bps"])
        self.assertIsNone(st["pc_cores"])
        self.assertIsNone(st["pc_ip"])

    def test_cores_without_net_rates_still_populate(self):
        self.view.feed(hostview.pack_stats(45.0, 0.8, 30.0))
        self.view.feed(hostview.pack_net(-1.0, -1.0, 4, [90, 60, 30, 10]))
        st = self.view.status_fields(True)
        self.assertIsNone(st["pc_rx_bps"])
        self.assertEqual(st["pc_ncpu"], 4)
        self.assertEqual(st["pc_cores"], [90, 60, 30, 10])

    def test_non_finite_net_and_triage_are_ignored(self):
        self.view.feed(packet(4, struct.pack("<fff", float("nan"), 1.0, 4.0)))
        self.view.feed(packet(5, struct.pack("<ff16s16s", float("nan"), 1.0, b"1.2.3.4", b"sh")))
        self.view.feed(packet(1, struct.pack("<fff", 40.0, 0.2, 10.0)))
        st = self.view.status_fields(True)
        self.assertIsNone(st["pc_rx_bps"])
        self.assertIsNone(st["pc_disk_pct"])
        self.assertEqual(st["link"], "LINKED")

    def test_decorate_frame_reticle_and_delta_brackets(self):
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), "..", "..", "host", "kvm_helper.py")
        spec = importlib.util.spec_from_file_location("kvm_helper", os.path.abspath(path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        prev = bytes(FRAME_LEN)
        cur = bytearray(FRAME_LEN)
        # Change a box from (20, 30) to (40, 50)
        for y in (30, 50):
            for x in (20, 40):
                idx = (y * 240 + x) * 2
                cur[idx] = 0x12; cur[idx + 1] = 0x34
        out = mod.decorate_frame(bytes(cur), prev_raw=prev, cursor_xy=(100, 60))
        self.assertEqual(len(out), FRAME_LEN)
        def px(x, y):
            i = (y * 240 + x) * 2
            return out[i] | (out[i + 1] << 8)
        # Reticle center and arms are white (0xFFFF), outline is black (0x0000)
        self.assertEqual(px(100, 60), 0xFFFF)
        self.assertEqual(px(102, 60), 0xFFFF)
        self.assertEqual(px(100, 58), 0xFFFF)
        self.assertEqual(px(103, 60), 0x0000)
        self.assertEqual(px(101, 61), 0x0000)
        # Delta corner brackets at (20, 30) and (40, 50) are cyan (0x06FF)
        self.assertEqual(px(20, 30), 0x06FF)
        self.assertEqual(px(23, 30), 0x06FF)
        self.assertEqual(px(20, 33), 0x06FF)
        self.assertEqual(px(40, 50), 0x06FF)
        self.assertEqual(px(37, 50), 0x06FF)

    def test_hid_leds_and_ping_host(self):
        import hid, threading
        m, s = os.openpty()
        orig_kbd = hid.KBD_DEV
        orig_kc = hid.key_combo
        try:
            hid.KBD_DEV = os.ttyname(s)
            import tty
            tty.setraw(m); tty.setraw(s)
            # Simulate host sending NumLock + CapsLock LED report (0x03)
            os.write(m, b"\x03")
            leds = hid.read_leds()
            self.assertEqual(leds, {"num": True, "caps": True, "scroll": False})
            # Simulate responsive host echoing LED reports when CapsLock is tapped
            def responder():
                r, _, _ = select.select([m], [], [], 0.5)
                if r:
                    os.read(m, 64)
                    os.write(m, b"\x01")  # CapsLock off
                r, _, _ = select.select([m], [], [], 0.5)
                if r:
                    os.read(m, 64)
                    os.write(m, b"\x03")  # CapsLock restored
            import select
            t = threading.Thread(target=responder, daemon=True)
            t.start()
            res = hid.ping_host(timeout=0.3)
            t.join(timeout=1.0)
            self.assertTrue(res["alive"])
            self.assertIsNotNone(res["latency_ms"])
            self.assertEqual(res["leds"], {"num": True, "caps": True, "scroll": False})
            # Drain any leftover keyboard reports on m, then test frozen host (no LED echo)
            while select.select([m], [], [], 0)[0]:
                os.read(m, 64)
            res_dead = hid.ping_host(timeout=0.08)
            self.assertFalse(res_dead["alive"])
            self.assertIsNone(res_dead["latency_ms"])
            # Test USB IN write timeout (HIDTimeout) returns alive=False instead of raising
            def raise_timeout(*a, **k):
                raise hid.HIDTimeout("simulated frozen USB IN endpoint")
            hid.key_combo = raise_timeout
            res_to = hid.ping_host(timeout=0.05)
            self.assertFalse(res_to["alive"])
            self.assertIsNone(res_to["latency_ms"])
        finally:
            hid.key_combo = orig_kc
            hid.KBD_DEV = orig_kbd
            os.close(m); os.close(s)

    def test_type_text_sends_tab_skips_unknown_and_releases_each_key(self):
        import hid, select, time, tty
        m, s = os.openpty()
        orig_kbd = hid.KBD_DEV
        try:
            hid.KBD_DEV = os.ttyname(s)
            tty.setraw(m); tty.setraw(s)
            hid.type_text("A\tb\u00c9\u00e9")        # last two have no US key and are skipped
            time.sleep(0.05)
            data = b""
            while select.select([m], [], [], 0.05)[0]:
                data += os.read(m, 256)
            reports = [data[i:i + 8] for i in range(0, len(data), 8)]
            press = lambda mod, code: bytes([mod, 0, code, 0, 0, 0, 0, 0])
            self.assertEqual(reports, [
                press(0x02, 0x04), bytes(8),          # shift + a
                press(0x00, 0x2B), bytes(8),          # tab
                press(0x00, 0x05), bytes(8),          # b
            ])
        finally:
            hid.KBD_DEV = orig_kbd
            os.close(m); os.close(s)

if __name__ == "__main__":
    unittest.main()


class NetIfaceTest(unittest.TestCase):
    def test_iface_reaches_status(self):
        v = hostview.HostView()
        v.feed(hostview.pack_stats(50, 1, 30))
        self.assertEqual(v.feed(hostview.pack_net(1000, 20, 4, [50], "wlp3s0")), b"")
        self.assertEqual(v.status_fields(True)["pc_net_iface"], "wlp3s0")


class ProcsExtTest(unittest.TestCase):
    def test_procs_validated(self):
        d = hostview._parse_ext(b'{"v":1,"procs":[["firefox",42.5],["bad",-1],[1,2],["x\\n",3]]}')
        self.assertEqual(d["procs"], [["firefox", 42.5]])


class MemDiskExtTest(unittest.TestCase):
    def test_mem_disk(self):
        d = hostview._parse_ext(b'{"v":1,"mem":[4,16],"disk":[100,500,"nvme0n1p2"]}')
        self.assertEqual(d["mem"], [4, 16])
        self.assertEqual(d["disk"], [100, 500, "nvme0n1p2"])
        self.assertNotIn("mem", hostview._parse_ext(b'{"v":1,"mem":[20,16]}'))
