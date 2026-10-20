#!/usr/bin/env python3
"""Linux helper for the Pi Zero W HID KVM.

Sends this PC's temperature, load, and memory, plus a 240x135 desktop still,
to the Zero over the USB serial port the gadget already exposes. The Zero
peels those packets off before the terminal page sees them.

Two modes, switched from the web console (POST /api/mode on the Zero):

  helper  (default) stats + stills, as before. Input from the Zero is ignored
          except for mode-control packets.
  shell   stats/stills pause; a `bash -l` runs on a pty as the user running this
          helper (never root: shell mode is refused when euid is 0) and is bridged
          both ways to the serial port. A "helper" packet or the shell exiting
          kills/reaps bash and resumes helper mode.

Control packet, Zero -> host (never valid UTF-8 text a person types):
    1E 1F "KVMCTL:" <mode> 0A        mode = b"shell" | b"helper"
Ack, host -> Zero: the normal envelope, type 3, 1-byte payload (0 helper, 1 shell).
Sent on every mode change and every 2 s in helper mode.

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
import glob
import math
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time

MAGIC = b"\xfe\xfdKV"
FRAME_LEN = 240 * 135 * 2
INTERVAL = 2.0
CTL = b"\x1e\x1fKVMCTL:"
CTL_MAX = len(CTL) + 8          # longest valid packet body before the newline
MODES = {b"helper": 0, b"shell": 1}


def pack_mode(mode):
    return MAGIC + bytes([3]) + struct.pack("<I", 1) + bytes([MODES[mode]])


class CtlParser:
    """Split Zero->host input into (data bytes, [modes]). A partial prefix is held
    back; a prefix followed by garbage is released as data."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf.extend(data)
        out, modes = bytearray(), []
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
                if len(self.buf) > CTL_MAX:
                    out.append(self.buf[0]); del self.buf[:1]
                    continue
                break
            mode = bytes(self.buf[len(CTL):nl])
            if mode in MODES:
                modes.append(mode)
                del self.buf[:nl + 1]
            else:
                out.append(self.buf[0]); del self.buf[:1]
        return bytes(out), modes


def pack_stats(temp, load, mem):
    payload = struct.pack("<fff", float(temp), float(load), float(mem))
    return MAGIC + bytes([1]) + struct.pack("<I", len(payload)) + payload


def pack_frame(rgb565):
    raw = bytes(rgb565)
    if len(raw) != FRAME_LEN:
        raise ValueError("frame must be 240x135 rgb565le")
    return MAGIC + bytes([2]) + struct.pack("<I", FRAME_LEN) + raw


def read_temp():
    """Package temperature in C, or None when the kernel exposes no zone."""
    preferred = None
    rest = []
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            kind = open(os.path.join(zone, "type")).read().strip()
            temp = int(open(os.path.join(zone, "temp")).read()) / 1000.0
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
        load = float(open("/proc/loadavg").read().split()[0])
        info = {}
        for line in open("/proc/meminfo"):
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


def capture(display):
    """One 240x135 RGB565-LE still, or None."""
    if not display:
        return None
    size = screen_size(display)
    if not size:
        return None
    w, h = size
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-f", "x11grab", "-video_size", f"{w}x{h}", "-i", display,
        "-frames:v", "1", "-vf", "scale=240:135",
        "-pix_fmt", "rgb565le", "-f", "rawvideo", "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1.5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or len(proc.stdout) != FRAME_LEN:
        return None
    return proc.stdout


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


def send_helper_packets(fd, display, announced):
    blob = b""
    stats = read_stats()
    if stats:
        blob += pack_stats(*stats)
    frame = capture(display)
    if frame:
        blob += pack_frame(frame)
    blob += pack_mode(b"helper")
    write_all(fd, blob)
    state = ("stats" if stats else "no-stats") + ("+frame" if frame else "")
    if state != announced:
        print(state, flush=True)
    return state


def run(fd, display):
    """Serve one open port until an OSError. Returns on error (caller reopens)."""
    parser = CtlParser()
    shell = None
    announced = None
    next_tick = 0.0
    was_shell = requested_back = False
    try:
        write_all(fd, pack_mode(b"helper"))
        while True:
            now = time.monotonic()
            if shell is None and now >= next_tick:
                announced = send_helper_packets(fd, display, announced)
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
                if data:
                    text, modes = parser.feed(data)
                    for mode in modes:
                        if mode == b"shell" and shell is None:
                            text = b""        # bytes before the packet were helper-mode noise
                            try:
                                shell = Shell()
                                print("shell mode: bash -l pid", shell.pid, flush=True)
                                write_all(fd, pack_mode(b"shell"))
                            except (PermissionError, OSError) as exc:
                                print("shell refused:", exc, flush=True)
                                write_all(fd, pack_mode(b"helper"))
                        elif mode == b"helper" and shell is not None:
                            if text:
                                write_all(shell.master, text)
                                text = b""
                            shell.close(); shell = None
                            print("helper mode (requested)", flush=True)
                            write_all(fd, pack_mode(b"helper")); next_tick = 0.0
                            requested_back = True
                        else:
                            write_all(fd, pack_mode(b"shell" if shell else b"helper"))
                    if shell and text:
                        write_all(shell.master, text)
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
