#!/usr/bin/env python3
"""Linux helper for the Pi Zero W HID KVM.

Sends this PC's temperature, load, and memory, plus a 240x135 desktop still,
to the Zero over the USB serial port the gadget already exposes. The Zero
peels those packets off before the terminal page sees them.

Write-only. Stop any getty on this port first, or the login prompt and these
packets write over each other:

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
import re
import struct
import subprocess
import termios
import time

MAGIC = b"\xfe\xfdKV"
FRAME_LEN = 240 * 135 * 2
INTERVAL = 2.0


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
    fd = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attr = termios.tcgetattr(fd)
        attr[0] = 0
        attr[1] = 0
        attr[2] = (attr[2] & ~termios.CSIZE) | termios.CS8 | termios.CREAD | termios.CLOCAL
        attr[3] = 0
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


def main():
    dev = os.environ.get("KVM_HOST_DEV", "/dev/ttyACM0")
    display = os.environ.get("KVM_DISPLAY", os.environ.get("DISPLAY", "")).strip()
    fd = None
    announced = None
    while True:
        started = time.monotonic()
        try:
            if fd is None:
                fd = open_port(dev)
                print(f"helper writing {dev}", flush=True)
            blob = b""
            stats = read_stats()
            if stats:
                blob += pack_stats(*stats)
            frame = capture(display)
            if frame:
                blob += pack_frame(frame)
            if blob:
                write_all(fd, blob)
            state = ("stats" if stats else "no-stats") + ("+frame" if frame else "")
            if state != announced:
                print(state, flush=True)
                announced = state
        except (OSError, TimeoutError) as exc:
            print(f"{dev}: {exc}", flush=True)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
            announced = None
        time.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
