"""Terminal ring buffer: carriage returns and escape sequences across read boundaries."""
import os
import sys
import termios
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import term


def state():
    snap = term.snapshot()
    return snap["lines"], snap["partial"]


class FeedTest(unittest.TestCase):
    def setUp(self):
        term._lines.clear()
        term._partial = ""
        term._seq = 0
        term.reset_pending()

    def test_crlf_split_across_reads_keeps_the_line(self):
        term._feed("hello\r")
        term._feed("\nworld")
        self.assertEqual(state(), (["hello"], "world"))

    def test_double_cr_before_lf_keeps_the_line(self):
        term._feed("hello\r\r\nnext\r\n")
        self.assertEqual(state(), (["hello", "next"], ""))

    def test_lone_cr_still_overwrites_the_line(self):
        term._feed("10%\r50%\r100%")
        self.assertEqual(state(), ([], "100%"))

    def test_cr_then_text_in_next_read_overwrites(self):
        term._feed("old text\r")
        self.assertEqual(state(), ([], "old text"))
        term._feed("new")
        self.assertEqual(state(), ([], "new"))

    def test_csi_split_across_reads_is_removed(self):
        term._feed("a\x1b[3")
        term._feed("2mgreen\x1b[0m!")
        self.assertEqual(state(), ([], "agreen!"))

    def test_lone_escape_at_end_of_read_is_held(self):
        term._feed("x\x1b")
        self.assertEqual(state(), ([], "x"))
        term._feed("[1mbold")
        self.assertEqual(state(), ([], "xbold"))

    def test_charset_and_keypad_escapes_are_removed(self):
        term._feed("\x1b(Bprompt\x1b)0 \x1b=ok\x1b>\n")
        self.assertEqual(state(), (["prompt ok"], ""))

    def test_osc_title_is_removed(self):
        term._feed("\x1b]0;user@host: ~\x07$ ")
        self.assertEqual(state(), ([], "$ "))

    def test_reset_pending_drops_a_stale_escape(self):
        term._feed("a\x1b[")
        term.reset_pending()
        term._feed("b")
        self.assertEqual(state(), ([], "ab"))


class ReaderRetryTest(unittest.TestCase):
    def test_reader_retries_when_tty_is_not_ready(self):
        """tcgetattr raises termios.error (not OSError) while the gadget has no host.
        The reader has to catch that and open the port again, or the thread dies and
        the helper stays unpaired until kvm-web is restarted."""
        gets = {"n": 0}
        stop = {"die": False}
        opened = []
        real_open, real_close = os.open, os.close
        real_read, real_sleep = os.read, term.time.sleep
        real_getattr, real_setattr = termios.tcgetattr, termios.tcsetattr

        def fake_open(path, flags, mode=0o777):
            if stop["die"]:
                raise SystemExit
            r, w = os.pipe()
            real_close(w)
            opened.append(r)
            return r

        def fake_getattr(fd):
            gets["n"] += 1
            if gets["n"] == 1:
                raise termios.error(5, "Input/output error")
            return [0, 0, termios.CS8, 0, 0, 0, [0] * 32]

        def fake_read(fd, n):
            stop["die"] = True
            raise SystemExit

        try:
            os.open = fake_open
            os.read = fake_read
            term.time.sleep = lambda _s: None
            termios.tcgetattr = fake_getattr
            termios.tcsetattr = lambda fd, when, attr: None
            t = threading.Thread(target=term._reader, name="term-test", daemon=True)
            t.start()
            t.join(2)
            self.assertGreaterEqual(gets["n"], 2)
            self.assertFalse(t.is_alive())
        finally:
            stop["die"] = True
            os.open, os.read = real_open, real_read
            term.time.sleep = real_sleep
            termios.tcgetattr, termios.tcsetattr = real_getattr, real_setattr
            for fd in opened:
                try: real_close(fd)
                except OSError: pass
            term._fd = None
            term._wfd = None
            term._err = "not started"


if __name__ == "__main__":
    unittest.main()
