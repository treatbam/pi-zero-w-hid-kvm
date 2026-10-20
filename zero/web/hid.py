"""HID report writers for /dev/hidg0 (keyboard) and /dev/hidg1 (mouse). Stdlib only."""
import errno, glob, os, select, struct, threading, time

KBD_DEV, MOUSE_DEV, SYS_DEV = "/dev/hidg0", "/dev/hidg1", "/dev/hidg2"
SYS_KEYS = {"power": 0x01, "sleep": 0x02, "wake": 0x04}   # System Control bits (see hid-gadget.sh)

MOD = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "gui": 0x08,
       "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rgui": 0x80}

# USB HID usage IDs (HID Usage Tables, Keyboard page 0x07). US layout assumed.
KEYS = {chr(ord('a') + i): 0x04 + i for i in range(26)}
KEYS.update({str(i): 0x1E + i - 1 for i in range(1, 10)}); KEYS["0"] = 0x27
KEYS.update({"enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
             "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31, ";": 0x33, "'": 0x34,
             "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38, "capslock": 0x39, "scrolllock": 0x47,
             "delete": 0x4C, "home": 0x4A, "end": 0x4D, "pageup": 0x4B, "pagedown": 0x4E,
             "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "numlock": 0x53})
KEYS.update({f"f{i}": 0x3A + i - 1 for i in range(1, 13)})
SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8',
           '(': '9', ')': '0', '_': '-', '+': '=', '{': '[', '}': ']', '|': '\\', ':': ';',
           '"': "'", '~': '`', '<': ',', '>': '.', '?': '/'}

WRITE_TIMEOUT = 0.5   # seconds; a host that stops reading (suspend/unplug) must not hang us
MAX_MOVE = 4096       # per-request |dx|,|dy| cap -> at most ~33 int8 reports
_leds_lock = threading.Lock()
_leds_byte = 0

class HIDTimeout(Exception):
    """Host did not accept the report within WRITE_TIMEOUT."""

def _leds_dict(b):
    return {"num": bool(b & 0x01), "caps": bool(b & 0x02), "scroll": bool(b & 0x04)}

def _drain_fd(rfd):
    global _leds_byte
    got = False
    while True:
        try:
            chunk = os.read(rfd, 64)
            if not chunk:
                break
            _leds_byte = chunk[-1]
            got = True
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                break
            break
    return got

def read_leds():
    """Non-blocking drain of 1-byte LED output reports from /dev/hidg0."""
    with _leds_lock:
        try:
            rfd = os.open(KBD_DEV, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return _leds_dict(_leds_byte)
        try:
            _drain_fd(rfd)
        finally:
            try: os.close(rfd)
            except OSError: pass
        return _leds_dict(_leds_byte)

def _write_fd(fd, dev, data):
    deadline = time.monotonic() + WRITE_TIMEOUT
    while data:
        left = deadline - time.monotonic()
        if left <= 0: raise HIDTimeout(f"{dev}: host not reading")
        _, w, _ = select.select([], [fd], [], left)
        if not w: continue
        try:
            n = os.write(fd, data); data = data[n:]
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK): raise

def _write(dev, data):
    fd = os.open(dev, os.O_WRONLY | os.O_NONBLOCK)
    try:
        _write_fd(fd, dev, data)
    finally:
        os.close(fd)

def _report(keys, mods=()):
    m = 0
    for x in mods: m |= MOD[x]
    codes = [KEYS[k] for k in keys][:6]
    return bytes([m, 0] + codes + [0] * (6 - len(codes)))

def key_combo(keys, mods=()):
    """Press keys (<=6) with modifiers, then release all."""
    press = _report(keys, mods)
    fd = os.open(KBD_DEV, os.O_WRONLY | os.O_NONBLOCK)
    try:
        _write_fd(fd, KBD_DEV, press)
        _write_fd(fd, KBD_DEV, bytes(8))
    finally:
        os.close(fd)

def ping_host(timeout=0.45):
    """Bounce CapsLock twice and measure the host's USB HID LED output report echo latency."""
    global _leds_byte
    with _leds_lock:
        try:
            rfd = os.open(KBD_DEV, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return {"alive": False, "latency_ms": None, "leds": _leds_dict(_leds_byte)}
        try:
            _drain_fd(rfd)
            t0 = time.monotonic()
            try:
                key_combo(["capslock"])
            except (HIDTimeout, OSError):
                return {"alive": False, "latency_ms": None, "leds": _leds_dict(_leds_byte)}
            alive = False
            latency_ms = None
            deadline = t0 + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                r, _, _ = select.select([rfd], [], [], left)
                if r and _drain_fd(rfd):
                    latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                    alive = True
                    break
            # Bounce back so CapsLock returns to its prior state.
            try:
                key_combo(["capslock"])
            except (HIDTimeout, OSError):
                pass
            if alive:
                deadline2 = time.monotonic() + min(timeout, 0.25)
                while True:
                    left = deadline2 - time.monotonic()
                    if left <= 0:
                        break
                    r, _, _ = select.select([rfd], [], [], left)
                    if r and _drain_fd(rfd):
                        break
            return {"alive": alive, "latency_ms": latency_ms, "leds": _leds_dict(_leds_byte)}
        finally:
            try: os.close(rfd)
            except OSError: pass

def _char_combo(ch):
    """(keys, mods) for one US-layout character, or None if it has no key."""
    if ch == " ": return (["space"], ())
    if ch == "\n": return (["enter"], ())
    if ch == "\t": return (["tab"], ())
    if ch in SHIFTED: return ([SHIFTED[ch]], ["shift"])
    if ch.isupper() and ch.lower() in KEYS: return ([ch.lower()], ["shift"])
    if ch in KEYS: return ([ch], ())
    return None    # silently skipped (TODO: report back)

def type_text(text):
    """Type US-layout text. The device is opened once for the whole string, not twice per
    character, and a key press is released before the next character starts."""
    fd = os.open(KBD_DEV, os.O_WRONLY | os.O_NONBLOCK)
    try:
        for ch in text:
            combo = _char_combo(ch)
            if combo:
                _write_fd(fd, KBD_DEV, _report(*combo))
                _write_fd(fd, KBD_DEV, bytes(8))
            time.sleep(0.005)
    finally:
        os.close(fd)

def _clamp(v): return max(-127, min(127, int(v)))

def mouse(dx=0, dy=0, wheel=0, buttons=0):
    """buttons bitmask: 1=left 2=right 4=middle. Large moves split into int8 chunks."""
    lim = lambda v: max(-MAX_MOVE, min(MAX_MOVE, int(v)))
    dx, dy = lim(dx), lim(dy)
    while True:
        sx, sy = _clamp(dx), _clamp(dy)
        _write(MOUSE_DEV, struct.pack("<Bbbb", buttons & 7, sx, sy, _clamp(wheel)))
        dx -= sx; dy -= sy; wheel = 0
        if dx == 0 and dy == 0: break

def system_key(name):
    """Tap a System Control key (power/sleep/wake) on the host."""
    _write(SYS_DEV, bytes([SYS_KEYS[name]])); time.sleep(0.1); _write(SYS_DEV, b"\x00")

def status():
    """hid_ready: device nodes exist. host_connected: UDC state == 'configured'
    (host enumerated us). Approximate: a powered-but-idle host may still read configured;
    a suspended host reports 'suspended'."""
    udcs = glob.glob("/sys/class/udc/*/state")
    state = open(udcs[0]).read().strip() if udcs else "no-udc"
    return {"hid_ready": os.path.exists(KBD_DEV) and os.path.exists(MOUSE_DEV),
            "power_keys": os.path.exists(SYS_DEV),
            "host_connected": state == "configured", "udc_state": state,
            "leds": read_leds()}
