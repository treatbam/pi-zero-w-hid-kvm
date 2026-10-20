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
        self.assertEqual(self.view.feed(mod.pack_frame(frame)), b"")
        self.assertEqual(self.view.frame_bytes(True), frame)


if __name__ == "__main__":
    unittest.main()
