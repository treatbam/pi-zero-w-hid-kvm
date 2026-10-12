"""HID report writers for /dev/hidg0 (keyboard) and /dev/hidg1 (mouse). Stdlib only."""
import errno, glob, os, select, struct, time

KBD_DEV, MOUSE_DEV = "/dev/hidg0", "/dev/hidg1"

MOD = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "gui": 0x08,
       "rctrl": 0x10, "rshift": 0x20, "ralt": 0x40, "rgui": 0x80}

# USB HID usage IDs (HID Usage Tables, Keyboard page 0x07). US layout assumed.
KEYS = {chr(ord('a') + i): 0x04 + i for i in range(26)}
KEYS.update({str(i): 0x1E + i - 1 for i in range(1, 10)}); KEYS["0"] = 0x27
KEYS.update({"enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
             "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31, ";": 0x33, "'": 0x34,
             "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38, "delete": 0x4C, "home": 0x4A,
             "end": 0x4D, "pageup": 0x4B, "pagedown": 0x4E, "right": 0x4F, "left": 0x50,
             "down": 0x51, "up": 0x52})
KEYS.update({f"f{i}": 0x3A + i - 1 for i in range(1, 13)})
SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8',
           '(': '9', ')': '0', '_': '-', '+': '=', '{': '[', '}': ']', '|': '\\', ':': ';',
           '"': "'", '~': '`', '<': ',', '>': '.', '?': '/'}

WRITE_TIMEOUT = 0.5   # seconds; a host that stops reading (suspend/unplug) must not hang us
MAX_MOVE = 4096       # per-request |dx|,|dy| cap -> at most ~33 int8 reports

class HIDTimeout(Exception):
    """Host did not accept the report within WRITE_TIMEOUT."""

def _write(dev, data):
    fd = os.open(dev, os.O_WRONLY | os.O_NONBLOCK)
    try:
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
    finally:
        os.close(fd)

def key_combo(keys, mods=()):
    """Press keys (<=6) with modifiers, then release all."""
    m = 0
    for x in mods: m |= MOD[x]
    codes = [KEYS[k] for k in keys][:6]
    _write(KBD_DEV, bytes([m, 0] + codes + [0] * (6 - len(codes))))
    _write(KBD_DEV, bytes(8))

def type_text(text):
    for ch in text:
        if ch == " ": key_combo(["space"])
        elif ch == "\n": key_combo(["enter"])
        elif ch in SHIFTED: key_combo([SHIFTED[ch]], ["shift"])
        elif ch.isupper(): key_combo([ch.lower()], ["shift"])
        elif ch in KEYS: key_combo([ch])
        # else: silently skip unsupported chars (TODO: report back)
        time.sleep(0.005)

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

def status():
    """hid_ready: device nodes exist. host_connected: UDC state == 'configured'
    (host enumerated us). Approximate: a powered-but-idle host may still read configured;
    a suspended host reports 'suspended'."""
    udcs = glob.glob("/sys/class/udc/*/state")
    state = open(udcs[0]).read().strip() if udcs else "no-udc"
    return {"hid_ready": os.path.exists(KBD_DEV) and os.path.exists(MOUSE_DEV),
            "host_connected": state == "configured", "udc_state": state}
