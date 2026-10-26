"""Per-PC cumulative network history on /data (history.py) and its type-8 fields."""
import json, os, shutil, stat, struct, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import history, hostview

A, B = "aaaaaaaaaaaa", "bbbbbbbbbbbb"


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def ext(pc, host, *rows):
    return {"pc_id": pc, "pc_host": host, "netdelta": [list(r) for r in rows]}


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = Clock()
        self.h = history.NetHistory(self.dir, now=self.clock)
        self.assertTrue(self.h.open())

    def tearDown(self):
        self.h.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_accumulates_across_flushes_and_restarts(self):
        self.h.ingest(ext(A, "pc1", ("firefox", 100, 10), ("ssh", 1, 2)))
        self.h.flush()
        self.h.ingest(ext(A, "pc1", ("firefox", 50, 5)))          # pending, not flushed
        since, rows = self.h.snapshot(A)
        self.assertEqual(since, 1000)
        self.assertEqual(rows, [{"p": "firefox", "rx": 150, "tx": 15}, {"p": "ssh", "rx": 1, "tx": 2}])
        self.h.close()
        h2 = history.NetHistory(self.dir, now=self.clock); h2.open()
        self.assertEqual(h2.snapshot(A)[1][0], {"p": "firefox", "rx": 150, "tx": 15})
        h2.close()

    def test_top8_busiest_first(self):
        self.h.ingest(ext(A, "pc1", *[(f"p{i}", i, i) for i in range(12)]))
        rows = self.h.snapshot(A)[1]
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]["p"], "p11")

    def test_counter_reset_never_subtracts(self):
        self.h.ingest(ext(A, "pc1", ("x", 100, 100)))
        self.h.ingest(ext(A, "pc1", ("x", -50, -1)))
        self.assertEqual(self.h.snapshot(A)[1], [{"p": "x", "rx": 100, "tx": 100}])

    def test_reset_by_host_and_id(self):
        self.h.ingest(ext(A, "pc1", ("x", 5, 5)))
        self.h.ingest(ext(B, "pc2", ("y", 7, 7)))
        self.clock.t = 2000
        self.assertEqual(self.h.reset("PC1"), A)
        self.assertEqual(self.h.snapshot(A), (2000, []))
        self.assertEqual(self.h.snapshot(B)[1], [{"p": "y", "rx": 7, "tx": 7}])
        self.assertEqual(self.h.reset(B), B)
        self.assertEqual(self.h.snapshot(B), (2000, []))
        with self.assertRaises(history.NotFound):
            self.h.reset("nope")

    def test_ambiguous_host(self):
        self.h.ingest(ext(A, "same", ("x", 1, 1)))
        self.h.ingest(ext(B, "same", ("x", 1, 1)))
        with self.assertRaises(history.Ambiguous) as cm:
            self.h.reset("same")
        self.assertEqual(sorted(cm.exception.args[0]), [A, B])

    def test_erase_file_runs_once_per_file(self):
        boot = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, boot)
        f = os.path.join(boot, "pc1-erase.txt")
        open(f, "w").close()
        open(os.path.join(boot, "config.txt"), "w").close()
        self.h.ingest(ext(A, "pc1", ("x", 5, 5)))
        self.assertEqual(self.h.scan_erase(boot), [("pc1-erase.txt", A)])
        self.assertEqual(self.h.snapshot(A)[1], [])
        self.h.ingest(ext(A, "pc1", ("x", 3, 3)))
        self.assertEqual(self.h.scan_erase(boot), [])             # same file again: skipped
        self.assertEqual(self.h.snapshot(A)[1], [{"p": "x", "rx": 3, "tx": 3}])
        os.utime(f, (5000, 5000))                                  # a new file for the same PC
        self.assertEqual(self.h.scan_erase(boot), [("pc1-erase.txt", A)])
        self.assertEqual(self.h.snapshot(A)[1], [])
        self.assertEqual(sorted(os.listdir(boot)), ["config.txt", "pc1-erase.txt"])   # boot untouched

    def test_erase_file_by_pc_id(self):
        boot = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, boot)
        open(os.path.join(boot, f"{B}-erase.txt"), "w").close()
        self.h.ingest(ext(B, "pc2", ("y", 1, 1)))
        self.assertEqual(self.h.scan_erase(boot), [(f"{B}-erase.txt", B)])

    def test_missing_or_unwritable_dir_is_off(self):
        h = history.NetHistory(os.path.join(self.dir, "missing"))
        self.assertFalse(h.open())
        h.ingest(ext(A, "pc1", ("x", 1, 1))); h.flush()
        self.assertEqual(h.snapshot(A), (None, None))
        self.assertEqual(h.scan_erase(self.dir), [])
        with self.assertRaises(OSError):
            h.reset(A)
        if os.geteuid() != 0:                                      # root ignores modes
            ro = os.path.join(self.dir, "ro"); os.mkdir(ro); os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
            self.assertFalse(history.NetHistory(ro).open())


class ExtFieldsTest(unittest.TestCase):
    def parse(self, **kw):
        return hostview._parse_ext(json.dumps({"v": 1, **kw}).encode())

    def test_valid(self):
        d = self.parse(pc_id=A, pc_host="workbenchpc", netdelta=[["firefox", 10, 2]])
        self.assertEqual((d["pc_id"], d["pc_host"], d["netdelta"]), (A, "workbenchpc", [["firefox", 10, 2]]))

    def test_rejects_junk(self):
        d = self.parse(pc_id="XYZ", pc_host="a b", netdelta=[["x", -1, 2], ["y", 1], [3, 1, 1]])
        self.assertEqual(d, {})
        self.assertNotIn("netdelta", self.parse(netdelta=[["x", 1, 1]] * 17))

    def test_hook_and_status(self):
        got = []
        v = hostview.HostView(); v.on_ext = got.append
        p = json.dumps({"v": 1, "pc_id": A, "pc_host": "h", "netdelta": [["x", 1, 1]]}).encode()
        pkt = lambda t, b: b"\xfe\xfdKV" + bytes([t]) + struct.pack("<I", len(b)) + b
        v.feed(pkt(hostview.TYPE_MODE, b"\x00"))                 # helper ack opens type 8
        v.feed(b"\xfe\xfdKV" + bytes([8]) + struct.pack("<I", len(p)) + p)
        self.assertEqual(got[0]["pc_id"], A)


class RequireMountTest(unittest.TestCase):
    def test_plain_dir_refused_when_mount_required(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(history.NetHistory(d, require_mount=True).open())
            self.assertTrue(history.NetHistory(d).open())


if __name__ == "__main__":
    unittest.main()
