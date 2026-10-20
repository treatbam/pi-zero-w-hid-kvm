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
ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[@-Z\\-_])")

_lock = threading.Lock()
_wlock = threading.Lock()
_lines = collections.deque(maxlen=MAX_LINES)
_partial = ""
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

def _feed(text):
    global _partial, _seq
    text = ANSI.sub("", text).replace("\r\n", "\n")
    with _lock:
        for ch in text:
            if ch == "\n":
                _lines.append(_partial); _partial = ""; _seq += 1
            elif ch == "\r":
                _partial = ""                      # carriage return: overwrite line
            elif ch in "\b\x7f":
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
                if not b:
                    time.sleep(0.2); continue
                last_data = now
                text = hostview.shared.feed(b)
                if text:
                    s = dec.decode(text)
                    if s: _feed(s)
        except OSError as e:
            hostview.shared.reset()
            _err = f"{DEV}: {e.strerror or e}"
            with _wlock:
                for f in (fd, wfd, _fd, _wfd):
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

def send(text, newline=True):
    """Write keystrokes to the host terminal. Returns False if the port isn't open."""
    data = (text + ("\r" if newline else "")).encode()
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
