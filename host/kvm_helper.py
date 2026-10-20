#!/usr/bin/env python3
"""Linux helper for the Pi Zero W HID KVM.

Sends this PC's temperature, load, and memory, its default-route up/down
rates and top-8 per-core CPU busy %, headless bench triage (LAN IP, root disk %,
top CPU process), plus a 240x135 desktop still (full-screen or 1:1 cursor loupe
with high-contrast cursor reticle and delta brackets), to the Zero over the USB
serial port the gadget already exposes. The Zero peels those packets off before
the terminal page sees them.

Two modes, switched from the web console (POST /api/mode on the Zero):

  helper  (default) stats + stills + control commands. Input from the Zero is
          ignored except for KVMCTL control packets.
  shell   stats/stills pause; a `bash -l` runs on a pty as the user running this
          helper (never root: shell mode is refused when euid is 0) and is bridged
          both ways to the serial port. A "helper" packet or the shell exiting
          kills/reaps bash and resumes helper mode.

Control packets, Zero -> host (never valid UTF-8 text a person types):
    1E 1F "KVMCTL:" <cmd> 0A
      cmd = b"shell" | b"helper"      switch serial mode
          | b"frame"                  request full-screen 240x135 still lease
          | b"loupe"                  request 1:1 cursor-crop 240x135 still lease
          | b"cget"                   read host clipboard -> sends type 6 envelope
          | b"cset:" <base64>         write decoded UTF-8 bytes to host clipboard

This program owns the port. Stop any getty on this port first:

    sudo systemctl disable --now serial-getty@ttyACM0.service
    python3 kvm_helper.py

The keyboard, mouse, and Pi scoreboard work with this program not running.
A still is sent only when a display is available (DISPLAY or KVM_DISPLAY) and
ffmpeg can grab it. Stats are sent either way.

Env:
  KVM_HOST_DEV   serial device, default /dev/ttyACM0
  KVM_DISPLAY    X display to grab; default $DISPLAY; empty skips the still
"""
import base64
import binascii
import ctypes
import glob
import math
import os
import pty
import re
import select
import signal
import socket
import struct
import subprocess
import termios
import time

MAGIC = b"\xfe\xfdKV"
FRAME_LEN = 240 * 135 * 2
INTERVAL = 2.0
FRAME_LEASE_S = 8.0
CTL = b"\x1e\x1fKVMCTL:"
CTL_MAX = len(CTL) + 8          # longest valid non-clipboard packet body before newline
CTL_CLIP_MAX = len(CTL) + 1500  # longest valid cset:<base64> packet before newline
MODES = {b"helper": 0, b"shell": 1}
SIMPLE_CTLS = {b"frame", b"loupe", b"cget"}
B64_RE = re.compile(rb"^[A-Za-z0-9+/=]*$")
CLIP_MAX = 1024


def pack_mode(mode):
    return MAGIC + bytes([3]) + struct.pack("<I", 1) + bytes([MODES[mode]])


def _valid_ctl(cmd):
    if cmd in SIMPLE_CTLS:
        return True
    if cmd.startswith(b"cset:"):
        b64 = cmd[5:]
        return len(b64) <= 1400 and bool(B64_RE.match(b64))
    return False


class CtlParser:
    """Split Zero->host input into an ordered list of segments:
    ("data", bytes), ("mode", b"shell"|b"helper"), or ("ctl", bytes). A partial
    prefix is held back until the next feed; a prefix followed by garbage is
    released as data."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf.extend(data)
        segs, out = [], bytearray()

        def flush():
            if out:
                segs.append(("data", bytes(out)))
                out.clear()

        while True:
            i = self.buf.find(CTL)
            if i < 0:
                hold = 0
                for k in range(min(len(CTL) - 1, len(self.buf)), 0, -1):
                    if CTL.startswith(self.buf[-k:]):
                        hold = k
                        break
                out.extend(self.buf[:len(self.buf) - hold])
                del self.buf[:len(self.buf) - hold]
                break
            out.extend(self.buf[:i])
            del self.buf[:i]
            nl = self.buf.find(b"\n", len(CTL))
            if nl < 0:
                tail = bytes(self.buf[len(CTL):])
                is_cset = (tail.startswith(b"cset:") and bool(B64_RE.match(tail[5:]))) or b"cset:".startswith(tail)
                limit = CTL_CLIP_MAX if is_cset else CTL_MAX
                if len(self.buf) > limit:
                    out.append(self.buf[0]); del self.buf[:1]
                    continue
                break
            cmd = bytes(self.buf[len(CTL):nl])
            if cmd in MODES:
                flush()
                segs.append(("mode", cmd))
                del self.buf[:nl + 1]
            elif _valid_ctl(cmd):
                flush()
                segs.append(("ctl", cmd))
                del self.buf[:nl + 1]
            else:
                out.append(self.buf[0]); del self.buf[:1]
        flush()
        return segs


def pack_stats(temp, load, mem):
    payload = struct.pack("<fff", float(temp), float(load), float(mem))
    return MAGIC + bytes([1]) + struct.pack("<I", len(payload)) + payload


def pack_net(rx_bps, tx_bps, ncpu, cores=()):
    cbytes = [
        min(100, max(0, int(round(c))))
        if (c is not None and math.isfinite(c) and 0 <= c <= 100)
        else 255
        for c in list(cores)[:8]
    ]
    cbytes += [255] * (8 - len(cbytes))
    payload = struct.pack("<fff8B", float(rx_bps), float(tx_bps), float(ncpu), *cbytes)
    return MAGIC + bytes([4]) + struct.pack("<I", len(payload)) + payload


def pack_triage(disk_pct, top_cpu_pct, lan_ip="", top_proc=""):
    ip_b = (lan_ip or "").encode("utf-8")[:15].ljust(16, b"\x00")
    proc_b = (top_proc or "").encode("utf-8")[:15].ljust(16, b"\x00")
    payload = struct.pack("<ff16s16s", float(disk_pct), float(top_cpu_pct), ip_b, proc_b)
    return MAGIC + bytes([5]) + struct.pack("<I", len(payload)) + payload


def pack_clip(text):
    raw = (text.encode("utf-8") if isinstance(text, str) else bytes(text))[:CLIP_MAX]
    return MAGIC + bytes([6]) + struct.pack("<I", len(raw)) + raw


def pack_frame(rgb565):
    raw = bytes(rgb565)
    if len(raw) != FRAME_LEN:
        raise ValueError("frame must be 240x135 rgb565le")
    return MAGIC + bytes([2]) + struct.pack("<I", FRAME_LEN) + raw


def default_iface():
    """Name of the interface that holds the default route, or None."""
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000" and parts[0] != "Iface":
                    return parts[0]
    except OSError:
        return None
    return None


def read_net_counters():
    """(iface, rx_bytes, tx_bytes, ncpu) for the default route, or None."""
    iface = default_iface()
    if not iface:
        return None
    try:
        with open("/proc/net/dev") as f:
            rows = f.readlines()
    except OSError:
        return None
    rx = tx = None
    for line in rows:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        if name.strip() != iface:
            continue
        cols = rest.split()
        if len(cols) < 9:
            return None
        rx, tx = int(cols[0]), int(cols[8])
        break
    if rx is None:
        return None
    return iface, rx, tx, float(os.cpu_count() or 1)


def step_net(prev, sample, now):
    """(rates_or_None, new_prev). rates is (down bytes/s, up bytes/s, ncpu).

    The first sample, an interface change, and a counter reset return no rates.
    """
    if sample is None:
        return None, None
    iface, rx, tx, ncpu = sample
    new = (now, iface, rx, tx, ncpu)
    if prev is None or prev[1] != iface or now <= prev[0]:
        return None, new
    drx, dtx = rx - prev[2], tx - prev[3]
    if drx < 0 or dtx < 0:
        return None, new
    dt = now - prev[0]
    return (drx / dt, dtx / dt, ncpu), new


def read_cpu_times():
    """Return {cpuN: (busy_ticks, total_ticks)} from /proc/stat, or None."""
    out = {}
    try:
        with open("/proc/stat") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 5 or not parts[0].startswith("cpu") or not parts[0][3:].isdigit():
                    continue
                vals = [int(x) for x in parts[1:9]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                total = sum(vals)
                out[parts[0]] = (total - idle, total)
    except (OSError, ValueError):
        return None
    return out or None


def step_cores(prev, sample):
    """(top_8_sorted_busy_pcts, new_prev). Sorts cores hottest-to-coolest."""
    if not sample:
        return [], None
    if not prev:
        return [], sample
    pcts = []
    for k, (busy, total) in sample.items():
        if k not in prev:
            continue
        p_busy, p_total = prev[k]
        db, dt = busy - p_busy, total - p_total
        if dt > 0 and db >= 0:
            pcts.append(min(100, max(0, int(round(100.0 * db / dt)))))
    pcts.sort(reverse=True)
    return pcts[:8], sample


def read_lan_ip():
    """Outbound LAN IPv4 address of this host, or ''."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return "" if ip.startswith("127.") or ip == "0.0.0.0" else ip
    except OSError:
        return ""


def read_disk_pct(path="/"):
    """Used percentage on `path` (0..100), or -1.0 if unavailable."""
    try:
        st = os.statvfs(path)
        if st.f_blocks <= 0:
            return -1.0
        used = st.f_blocks - st.f_bavail
        return min(100.0, max(0.0, 100.0 * used / st.f_blocks))
    except OSError:
        return -1.0


def read_proc_ticks():
    """Return {pid: (comm, utime_plus_stime_ticks)} across /proc/[pid]/stat."""
    procs = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return procs
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                s = f.read()
            lp = s.find("(")
            rp = s.rfind(")")
            if lp < 0 or rp <= lp:
                continue
            comm = s[lp + 1:rp][:15]
            rest = s[rp + 2:].split()
            ticks = int(rest[11]) + int(rest[12])
            procs[int(name)] = (comm, ticks)
        except (OSError, ValueError, IndexError):
            continue
    return procs


def step_top_proc(prev, sample, now, clk_tck=None):
    """Return ((top_comm, top_cpu_pct), new_prev)."""
    if not sample:
        return ("", 0.0), None
    if clk_tck is None:
        try:
            clk_tck = int(os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError):
            clk_tck = 100
    new = (now, sample)
    if prev is None or now <= prev[0] or clk_tck <= 0:
        best = max(sample.values(), key=lambda x: x[1], default=("", 0))
        return (best[0], 0.0), new
    dt = now - prev[0]
    prev_map = prev[1]
    best_comm, best_dticks, best_ticks = "", -1, -1
    for pid, (comm, ticks) in sample.items():
        old = prev_map.get(pid)
        if old is None:
            continue
        dticks = ticks - old[1]
        if (dticks, ticks) > (best_dticks, best_ticks):
            best_dticks = dticks
            best_ticks = ticks
            best_comm = comm
    if best_dticks < 0:
        return ("", 0.0), new
    pct = max(0.0, 100.0 * (best_dticks / clk_tck) / dt)
    return (best_comm, pct), new


def read_temp():
    """Package temperature in C, or None when the kernel exposes no zone."""
    preferred = None
    rest = []
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            with open(os.path.join(zone, "type")) as f:
                kind = f.read().strip()
            with open(os.path.join(zone, "temp")) as f:
                temp = int(f.read()) / 1000.0
        except (OSError, ValueError):
            continue
        if not math.isfinite(temp):
            continue
        if kind in ("x86_pkg_temp", "cpu-thermal", "soc_thermal"):
            preferred = temp if preferred is None else max(preferred, temp)
        else:
            rest.append(temp)
    if preferred is not None:
        return preferred
    return max(rest) if rest else None


def read_stats():
    """(temp C, load1, mem percent) or None when any of the three is missing."""
    try:
        with open("/proc/loadavg") as f:
            load = float(f.read().split()[0])
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, val = line.split(":", 1)
                info[key] = int(val.split()[0])
        total, avail = info["MemTotal"], info["MemAvailable"]
        mem = 100.0 * (total - avail) / total
        temp = read_temp()
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
        return None
    if temp is None or not (math.isfinite(load) and math.isfinite(mem)):
        return None
    return temp, load, mem


def screen_size(display):
    try:
        out = subprocess.run(
            ["xrandr", "--current"],
            env={**os.environ, "DISPLAY": display},
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"current (\d+) x (\d+)", out.stdout or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def pointer_pos(display):
    """Return (x, y) pointer coordinates on `display`, or None."""
    if not display:
        return None
    try:
        x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XQueryPointer.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XQueryPointer.restype = ctypes.c_int
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        dpy = x11.XOpenDisplay(display.encode("utf-8"))
        if dpy:
            try:
                root = x11.XDefaultRootWindow(dpy)
                rw, cw = ctypes.c_ulong(), ctypes.c_ulong()
                rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
                mask = ctypes.c_uint()
                if x11.XQueryPointer(dpy, root, ctypes.byref(rw), ctypes.byref(cw),
                                     ctypes.byref(rx), ctypes.byref(ry),
                                     ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask)):
                    return int(rx.value), int(ry.value)
            finally:
                x11.XCloseDisplay(dpy)
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["xdotool", "getmouselocation", "--shell"],
            env={**os.environ, "DISPLAY": display},
            capture_output=True, text=True, timeout=0.6, check=False,
        )
        if out.returncode == 0:
            mx = re.search(r"^X=(\d+)", out.stdout or "", re.M)
            my = re.search(r"^Y=(\d+)", out.stdout or "", re.M)
            if mx and my:
                return int(mx.group(1)), int(my.group(1))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def _put_px(buf, x, y, col):
    if 0 <= x < 240 and 0 <= y < 135:
        idx = (y * 240 + x) * 2
        buf[idx] = col & 0xFF
        buf[idx + 1] = (col >> 8) & 0xFF


def decorate_frame(raw, prev_raw=None, cursor_xy=None):
    """Stamp 4px cyan delta corner brackets and a 5x5 high-contrast cursor reticle
    onto a 240x135 RGB565-LE frame."""
    if len(raw) != FRAME_LEN:
        raise ValueError("frame must be 240x135 rgb565le")
    buf = bytearray(raw)
    if prev_raw is not None and len(prev_raw) == FRAME_LEN and prev_raw != raw:
        min_x, min_y, max_x, max_y = 240, 135, -1, -1
        for i in range(0, FRAME_LEN, 2):
            if raw[i] != prev_raw[i] or raw[i + 1] != prev_raw[i + 1]:
                p = i // 2
                y, x = divmod(p, 240)
                if x < min_x: min_x = x
                if x > max_x: max_x = x
                if y < min_y: min_y = y
                if y > max_y: max_y = y
        if max_x >= min_x and max_y >= min_y:
            cyan = 0x06FF  # rgb565(0, 220, 255)
            for d in range(4):
                _put_px(buf, min_x + d, min_y, cyan)
                _put_px(buf, min_x, min_y + d, cyan)
                _put_px(buf, max_x - d, min_y, cyan)
                _put_px(buf, max_x, min_y + d, cyan)
                _put_px(buf, min_x + d, max_y, cyan)
                _put_px(buf, min_x, max_y - d, cyan)
                _put_px(buf, max_x - d, max_y, cyan)
                _put_px(buf, max_x, max_y - d, cyan)
    if cursor_xy is not None:
        cx = max(0, min(239, int(cursor_xy[0])))
        cy = max(0, min(134, int(cursor_xy[1])))
        # 1px black outline around the 5x5 crosshair
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                if abs(dx) <= 3 and abs(dy) <= 1 or abs(dy) <= 3 and abs(dx) <= 1:
                    _put_px(buf, cx + dx, cy + dy, 0x0000)
        # 5x5 white crosshair
        for d in range(-2, 3):
            _put_px(buf, cx + d, cy, 0xFFFF)
            _put_px(buf, cx, cy + d, 0xFFFF)
    return bytes(buf)


def capture_with_state(display, crop="full", prev_raw=None):
    """Return (decorated_rgb565, raw_rgb565) or (None, prev_raw)."""
    if not display:
        return None, prev_raw
    size = screen_size(display)
    if not size:
        return None, prev_raw
    w, h = size
    if w <= 0 or h <= 0:
        return None, prev_raw
    pt = pointer_pos(display)
    if crop == "loupe" and w >= 240 and h >= 135:
        px, py = pt if pt is not None else (w // 2, h // 2)
        ox = max(0, min(w - 240, px - 120))
        oy = max(0, min(h - 135, py - 67))
        grab_in = f"{display}+{ox},{oy}"
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "x11grab", "-video_size", "240x135", "-i", grab_in,
            "-frames:v", "1", "-pix_fmt", "rgb565le", "-f", "rawvideo", "pipe:1",
        ]
        cxy = (px - ox, py - oy) if pt is not None else (120, 67)
    else:
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-probesize", "32", "-analyzeduration", "0",
            "-f", "x11grab", "-video_size", f"{w}x{h}", "-i", display,
            "-frames:v", "1", "-vf", "scale=240:135",
            "-pix_fmt", "rgb565le", "-f", "rawvideo", "pipe:1",
        ]
        cxy = (int(pt[0] * 240 / w), int(pt[1] * 135 / h)) if pt is not None else None
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1.5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, prev_raw
    if proc.returncode != 0 or len(proc.stdout) != FRAME_LEN:
        return None, prev_raw
    raw = proc.stdout
    return decorate_frame(raw, prev_raw=prev_raw, cursor_xy=cxy), raw


def capture(display, crop="full", prev_raw=None):
    """One 240x135 RGB565-LE still, or None."""
    frame, _ = capture_with_state(display, crop=crop, prev_raw=prev_raw)
    return frame


def _clip_env(display):
    if not display and not os.environ.get("WAYLAND_DISPLAY"):
        return None
    if "KVM_DISPLAY" in os.environ and not os.environ["KVM_DISPLAY"].strip():
        return None
    env = dict(os.environ)
    if display:
        env["DISPLAY"] = display
    return env


def get_clipboard(display=""):
    """Read up to 1024 bytes of UTF-8 text from the host clipboard, or None."""
    env = _clip_env(display)
    if env is None:
        return None
    cmds = (
        ["wl-paste", "-n"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    )
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=0.8, check=False)
            if proc.returncode == 0 and proc.stdout is not None:
                return proc.stdout[:CLIP_MAX]
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def set_clipboard(data, display=""):
    """Write UTF-8 bytes to the host clipboard via wl-copy, xclip, or xsel."""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    env = _clip_env(display)
    if env is None:
        return False
    cmds = (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard", "-i"],
        ["xsel", "--clipboard", "--input"],
    )
    for cmd in cmds:
        try:
            proc = subprocess.run(cmd, env=env, input=raw, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=0.8, check=False)
            if proc.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def open_port(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attr = termios.tcgetattr(fd)
        attr[0] = 0
        attr[1] = 0
        attr[2] = (attr[2] & ~termios.CSIZE) | termios.CS8 | termios.CREAD | termios.CLOCAL
        attr[3] = 0
        attr[6][termios.VMIN] = 1
        attr[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attr)
    except termios.error:
        pass
    return fd


def write_all(fd, data):
    """Blocking write with a 1 s stall limit. The loop is single-threaded, so a
    host-side reader that stops draining (or a full pty) pauses everything for up to
    1 s before TimeoutError makes main() reopen the port."""
    end = time.monotonic() + 1.0
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
            view = view[n:]
        except BlockingIOError:
            if time.monotonic() > end:
                raise TimeoutError("serial port not accepting data")
            time.sleep(0.02)


class Shell:
    """bash -l on a pty, as the current (non-root) user."""

    def __init__(self):
        if os.geteuid() == 0:
            raise PermissionError("refusing to start a shell as root; run the helper as a normal user")
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.chdir(os.path.expanduser("~"))
            except OSError:
                pass
            env = dict(os.environ, TERM="dumb")
            os.execvpe("bash", ["bash", "-l"], env)
        self.pid, self.master = pid, master
        os.set_blocking(master, False)

    def alive(self):
        if self.pid is None:
            return False
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            done = self.pid
        if done:
            self.pid = None
        return self.pid is not None

    def drain(self, port_fd, quiet=0.3, limit=1.5):
        """Forward pty output to the port until it is quiet for `quiet` s (max `limit` s)."""
        end, last = time.monotonic() + limit, time.monotonic()
        while time.monotonic() < end and time.monotonic() - last < quiet:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                out = os.read(self.master, 4096)
            except BlockingIOError:
                continue
            except OSError:
                return
            if not out:
                return
            write_all(port_fd, out); last = time.monotonic()

    def close(self):
        if self.pid is not None:
            for sig, wait in ((signal.SIGHUP, 1.0), (signal.SIGKILL, 2.0)):
                try:
                    os.killpg(self.pid, sig)
                except (ProcessLookupError, PermissionError):
                    try:
                        os.kill(self.pid, sig)
                    except ProcessLookupError:
                        pass
                end = time.monotonic() + wait
                while time.monotonic() < end and self.alive():
                    time.sleep(0.02)
                if self.pid is None:
                    break
            if self.pid is not None:
                try:
                    os.waitpid(self.pid, 0)
                except ChildProcessError:
                    pass
                self.pid = None
        try:
            os.close(self.master)
        except OSError:
            pass


class HelperState:
    def __init__(self):
        self.net_prev = None
        self.cores_prev = None
        self.proc_prev = None
        self.prev_raw = None
        self.crop_mode = "full"
        self.frame_until = time.monotonic() + FRAME_LEASE_S
        self.last_frame_at = 0.0
        self.clip_buf = b""


def send_helper_packets(fd, display, announced, state=None, want_frame=True):
    if state is None:
        state = HelperState()
    now = time.monotonic()
    blob = b""
    stats = read_stats()
    if stats:
        blob += pack_stats(*stats)
    net, state.net_prev = step_net(state.net_prev, read_net_counters(), now)
    cores, state.cores_prev = step_cores(state.cores_prev, read_cpu_times())
    ncpu = float(os.cpu_count() or 1)
    if net:
        blob += pack_net(net[0], net[1], net[2], cores)
    elif cores:
        blob += pack_net(-1.0, -1.0, ncpu, cores)
    disk_pct = read_disk_pct("/")
    (top_proc, top_cpu), state.proc_prev = step_top_proc(state.proc_prev, read_proc_ticks(), now)
    lan_ip = read_lan_ip()
    if disk_pct >= 0 or lan_ip or top_proc:
        blob += pack_triage(disk_pct, top_cpu, lan_ip, top_proc)
    frame = None
    if want_frame and display and (now - state.last_frame_at >= 1.0):
        frame, state.prev_raw = capture_with_state(display, crop=state.crop_mode, prev_raw=state.prev_raw)
        if frame:
            blob += pack_frame(frame)
            state.last_frame_at = time.monotonic()
    blob += pack_mode(b"helper")
    write_all(fd, blob)
    has_frame = bool(frame) or (want_frame and bool(display) and state.last_frame_at > 0)
    st_str = ("stats" if stats else "no-stats") + ("+net" if net else "") + ("+frame" if has_frame else "")
    if st_str != announced:
        print(st_str, flush=True)
    return st_str


def run(fd, display):
    """Serve one open port until an OSError. Returns on error (caller reopens)."""
    parser = CtlParser()
    shell = None
    announced = None
    next_tick = 0.0
    was_shell = requested_back = False
    hstate = HelperState()
    try:
        write_all(fd, pack_mode(b"helper"))
        while True:
            now = time.monotonic()
            if shell is None and now >= next_tick:
                want_frame = bool(display) and (now <= hstate.frame_until)
                announced = send_helper_packets(fd, display, announced, state=hstate, want_frame=want_frame)
                next_tick = time.monotonic() + INTERVAL
            rlist = [fd] + ([shell.master] if shell else [])
            timeout = 0.5 if shell else max(0.0, next_tick - time.monotonic())
            ready, _, _ = select.select(rlist, [], [], timeout)
            if fd in ready:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    data = None
                if data == b"":
                    raise OSError("serial port closed")
                for kind, val in parser.feed(data or b""):
                    if kind == "data":
                        if shell:
                            write_all(shell.master, val)
                        # helper mode: stray input from the Zero is dropped
                    elif kind == "ctl":
                        if shell is not None:
                            continue
                        if val in (b"frame", b"loupe"):
                            new_crop = "loupe" if val == b"loupe" else "full"
                            crop_changed = new_crop != hstate.crop_mode
                            if crop_changed:
                                hstate.prev_raw = None
                            hstate.crop_mode = new_crop
                            hstate.frame_until = time.monotonic() + FRAME_LEASE_S
                            if display and (crop_changed or time.monotonic() - hstate.last_frame_at >= 0.8):
                                frame, hstate.prev_raw = capture_with_state(display, crop=hstate.crop_mode, prev_raw=hstate.prev_raw)
                                if frame:
                                    write_all(fd, pack_frame(frame))
                                    hstate.last_frame_at = time.monotonic()
                        elif val == b"cget":
                            clip = get_clipboard(display)
                            if clip is not None:
                                hstate.clip_buf = clip
                            write_all(fd, pack_clip(clip if clip is not None else hstate.clip_buf))
                        elif val.startswith(b"cset:"):
                            try:
                                raw_clip = base64.b64decode(val[5:], validate=True)[:CLIP_MAX]
                            except binascii.Error:
                                continue
                            hstate.clip_buf = raw_clip
                            set_clipboard(raw_clip, display)
                            write_all(fd, pack_clip(raw_clip))
                    elif val == b"shell" and shell is None:
                        try:
                            shell = Shell()
                            print("shell mode: bash -l pid", shell.pid, flush=True)
                            write_all(fd, pack_mode(b"shell"))
                        except (PermissionError, OSError) as exc:
                            print("shell refused:", exc, flush=True)
                            write_all(fd, pack_mode(b"helper"))
                    elif val == b"helper" and shell is not None:
                        shell.drain(fd)          # let bash act on bytes sent just before
                        shell.close(); shell = None
                        print("helper mode (requested)", flush=True)
                        write_all(fd, pack_mode(b"helper")); next_tick = 0.0
                        requested_back = True
                    else:
                        write_all(fd, pack_mode(b"shell" if shell else b"helper"))
            if shell and shell.master in ready:
                try:
                    out = os.read(shell.master, 4096)
                except BlockingIOError:
                    out = None
                except OSError:          # EIO: bash exited, slave closed
                    out = b""
                if out:
                    write_all(fd, out)
                elif out == b"":
                    shell.close(); shell = None
            if shell and not shell.alive():
                try:                     # flush what bash printed last
                    while True:
                        out = os.read(shell.master, 4096)
                        if not out:
                            break
                        write_all(fd, out)
                except OSError:
                    pass
                shell.close(); shell = None
            if shell is None and was_shell and not requested_back:
                print("helper mode (shell exited)", flush=True)
                write_all(fd, pack_mode(b"helper")); next_tick = 0.0
            was_shell, requested_back = shell is not None, False
    finally:
        if shell:
            shell.close()


def main():
    dev = os.environ.get("KVM_HOST_DEV", "/dev/ttyACM0")
    display = os.environ.get("KVM_DISPLAY", os.environ.get("DISPLAY", "")).strip()
    while True:
        fd = None
        try:
            fd = open_port(dev)
            print(f"helper on {dev}", flush=True)
            run(fd, display)
        except (OSError, TimeoutError) as exc:
            print(f"{dev}: {exc}", flush=True)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        time.sleep(2)


if __name__ == "__main__":
    main()
