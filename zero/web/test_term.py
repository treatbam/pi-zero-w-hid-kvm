"""Terminal ring buffer: carriage returns and escape sequences across read boundaries."""
import os
import sys
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


if __name__ == "__main__":
    unittest.main()
