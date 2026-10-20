"""Host terminal over the gadget's CDC ACM port (/dev/ttyGS0).

A background thread reads the port into a ring buffer of the last MAX_LINES lines
(ANSI escapes and control chars stripped). The host side runs a getty or a script on
ttyACM0 / COMx. The port is put in raw mode with echo OFF: an echoing tty here would
bounce the host getty's output straight back to it and loop.

The same port also carries helper packets (PC stats and a desktop still). Those are
removed before the ring buffer, so the terminal page does not print them.
"""
import codecs, collections, os, re, termios, threading, time
import hostview

DEV = os.environ.get("KVM_TERM_DEV", "/dev/ttyGS0")
MAX_LINES, MAX_LINE = 200, 400
MAX_ESC = 64          # longest escape sequence we will wait for across two reads
# CSI, OSC, charset / keypad / save-cursor style escapes (ESC ( B, ESC =, ESC 7, ...) and the
# original two-character forms. ESC [ and ESC ] are only taken as CSI / OSC starts.
ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[ -/]+[0-~]|[0-Z\\-~])")

_lock = threading.Lock()
_wlock = threading.Lock()
_lines = collections.deque(maxlen=MAX_LINES)
_partial = ""
_pend = ""           # an escape sequence cut off by the end of a read
_cr = False          # a carriage return is waiting to overwrite the line
_seq = 0            # total completed lines ever (lets clients spot new output)
_fd = None
_wfd = None         # separate O_NONBLOCK fd so a stalled host can't hang a web request
_err = "not started"

def _raw(fd):
    a = termios.tcgetattr(fd)
    a[0] = 0                                   # iflag: no CR/NL mapping, no XON/XOFF
    a[1] = 0                                   # oflag: no post-processing
    a[2] = (a[2] & ~termios.CSIZE) | termios.CS8 | termios.CREAD | termios.CLOCAL
    a[3] = 0                                   # lflag: no ECHO, no ICANON, no ISIG
    a[6][termios.VMIN] = 1; a[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, a)

def _split_pending(text):
    """Hold back an escape sequence that the read ended in the middle of."""
    i = text.rfind("\x1b")
    if i < 0 or ANSI.match(text, i) or len(text) - i > MAX_ESC:
        return text, ""
    return text[:i], text[i:]

def reset_pending():
    """Forget a half-received escape and a pending carriage return (port closed / long gap)."""
    global _pend, _cr
    with _lock:
        _pend = ""; _cr = False

def _feed(text):
    global _partial, _seq, _pend, _cr
    with _lock:
        text, new_pend = _split_pending(_pend + text)
        _pend = new_pend
        text = ANSI.sub("", text)
        for ch in text:
            if ch == "\n":
                _lines.append(_partial); _partial = ""; _seq += 1; _cr = False
                continue
            if ch == "\r":
                _cr = True                         # overwrite the line on the next character
                continue
            if _cr and (ch >= " " or ch in "\b\x7f\t"):
                _partial = ""; _cr = False
            if ch in "\b\x7f":
                _partial = _partial[:-1]
            elif ch == "\t":
                _partial += " " * (8 - len(_partial) % 8)
            elif ch >= " ":
                _partial += ch
            if len(_partial) >= MAX_LINE:
                _lines.append(_partial); _partial = ""; _seq += 1

def _reader():
    global _fd, _wfd, _err
    while True:
        fd = wfd = None
        try:
            fd = os.open(DEV, os.O_RDWR | os.O_NOCTTY)
            _raw(fd); wfd = os.open(DEV, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
            with _wlock:
                _fd, _wfd, _err = fd, wfd, None
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            last_data = time.monotonic()
            while True:
                b = os.read(fd, 4096)
                now = time.monotonic()
                # A helper that dies mid-frame leaves a header waiting for 64800 bytes.
                # The next open blocks, so the gap shows up on the following read.
                # An empty read is the host closing the port. Either way, drop the
                # half packet before the next stats record is swallowed as pixels.
                if not b or now - last_data > 1.0:
                    hostview.shared.drop_incomplete()
                    dec.reset()
                    reset_pending()
                if not b:
                    time.sleep(0.2); continue
                last_data = now
                text = hostview.shared.feed(b)
                if text:
                    s = dec.decode(text)
                    if s: _feed(s)
        except OSError as e:
            hostview.shared.reset()
            reset_pending()
            _err = f"{DEV}: {e.strerror or e}"
            with _wlock:
                # fd/_fd and wfd/_wfd are the same descriptors: close each once, or a
                # second close could hit a number the kernel has already handed out again.
                for f in {fd, wfd, _fd, _wfd}:
                    if f is not None:
                        try: os.close(f)
                        except OSError: pass
                _fd = _wfd = None
            time.sleep(2)

def start():
    threading.Thread(target=_reader, name="term", daemon=True).start()

def snapshot(n=MAX_LINES):
    n = max(1, min(int(n), MAX_LINES))
    with _lock:
        return {"seq": _seq, "lines": list(_lines)[-n:], "partial": _partial,
                "open": _fd is not None, "error": _err}

import base64

CTL = b"\x1e\x1fKVMCTL:"     # mode-control prefix the host helper watches for

def send_mode(mode):
    """Ask the host helper to switch: 1E 1F "KVMCTL:" <helper|shell> LF."""
    if mode not in ("helper", "shell"): raise ValueError("bad mode")
    return _write(CTL + mode.encode() + b"\n")

def send_ctl(cmd):
    """Send a short control command ('frame', 'loupe', 'cget') to kvm_helper."""
    if cmd not in ("frame", "loupe", "cget"): raise ValueError("bad ctl")
    return _write(CTL + cmd.encode() + b"\n")

def send_clipboard(text):
    """Push UTF-8 clipboard text to the host helper as base64."""
    raw = (text.encode("utf-8") if isinstance(text, str) else bytes(text))[:1024]
    b64 = base64.b64encode(raw)
    return _write(CTL + b"cset:" + b64 + b"\n")

def send(text, newline=True):
    """Write keystrokes to the host terminal. Returns False if the port isn't open."""
    return _write((text + ("\r" if newline else "")).encode())

def _write(data):
    with _wlock:
        fd = _wfd
        if fd is None: return False
        # If the host isn't reading, the port buffer fills; give up after 1 s.
        end = time.monotonic() + 1.0
        while data:
            try:
                n = os.write(fd, data); data = data[n:]
            except BlockingIOError:
                if time.monotonic() > end: raise TimeoutError("host not reading the serial port")
                time.sleep(0.02)
        return True
