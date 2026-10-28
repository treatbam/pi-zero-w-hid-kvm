#!/usr/bin/env python3
"""Lightweight USB-serial watcher stub for Pi Zero W HID KVM (Linux & macOS).

Installed once (~3 KB, Python 3 stdlib only). Waits for the Zero's composite USB
CDC ACM serial port, announces OS/runtime via TYPE_HELLO (type 10), receives the
Zero's compressed + SHA-256-verified helper bundle (\x1e\x1fKVMBOOT:...), writes it
to a 0700 user runtime file in tmpfs, and executes it with --once. Whenever the
Zero reboots or is updated, the next session automatically streams the fresh helper.
"""
import base64
import fcntl
import glob
import gzip
import hashlib
import json
import os
import platform
import select
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time

MAGIC = b"\xfe\xfdKV"
TYPE_HELLO = 10
CTL_PROBE = b"\x1e\x1fKVMCTL:probe\n"
BOOT_PFX = b"\x1e\x1fKVMBOOT:"
BUF_MAX = 512 * 1024


def discover_port():
    env_dev = os.environ.get("KVM_HOST_DEV", "").strip()
    if env_dev:
        return env_dev
    for pat in ("/dev/serial/by-id/usb-DIY_PiZeroW_HID_KVM_*", "/dev/cu.usbmodem*"):
        matches = sorted(glob.glob(pat))
        if matches:
            return matches[0]
    if sys.platform == "darwin":
        return "/dev/cu.usbmodem0"
    return "/dev/serial/by-id/usb-DIY_PiZeroW_HID_KVM_0001-if03"


def open_raw(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OSError(f"{path} already in use")
        t = termios.tcgetattr(fd)
        t[0] = 0
        t[1] = 0
        t[2] = (t[2] & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)) | termios.CS8 | termios.CREAD | termios.CLOCAL
        t[3] = 0
        t[4] = termios.B115200
        t[5] = termios.B115200
        t[6][termios.VMIN] = 1
        t[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, t)
        return fd
    except Exception:
        os.close(fd)
        raise


def make_hello(want="stream"):
    os_name = "darwin" if sys.platform == "darwin" else "linux"
    body = json.dumps({
        "v": 1,
        "os": os_name,
        "arch": platform.machine() or "unknown",
        "py": f"{sys.version_info[0]}.{sys.version_info[1]}",
        "host": socket.gethostname()[:64],
        "want": want,
    }, separators=(",", ":")).encode()
    return MAGIC + struct.pack("<BI", TYPE_HELLO, len(body)) + body


def write_runtime_helper(code_bytes):
    rdir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not rdir or not os.path.isdir(rdir):
        rdir = os.path.join(tempfile.gettempdir(), f"kvm-helper-{os.getuid()}")
    os.makedirs(rdir, mode=0o700, exist_ok=True)
    dst = os.path.join(rdir, "kvm-helper-live.py")
    tmp = dst + f".{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    try:
        os.write(fd, code_bytes)
    finally:
        os.close(fd)
    os.replace(tmp, dst)
    return dst


def unpack_boot_line(line):
    """Parse <os>:<sha256>:<b64_gzip> and return verified uncompressed bytes, or None."""
    parts = line.split(b":", 2)
    if len(parts) != 3:
        return None
    os_tag, want_sha, b64_payload = parts
    if os_tag not in (b"linux", b"darwin"):
        return None
    try:
        gz = base64.b64decode(b64_payload, validate=True)
        code = gzip.decompress(gz)
    except Exception as exc:
        print(f"stub: bad boot payload: {exc}", flush=True)
        return None
    got_sha = hashlib.sha256(code).hexdigest().encode("ascii")
    if got_sha != want_sha.lower():
        print("stub: sha256 mismatch on boot payload", flush=True)
        return None
    return code


def wait_for_boot(fd, fallback_s=12.0):
    hello = make_hello("stream")
    buf = bytearray()
    last_hello = 0.0
    started = time.monotonic()
    while True:
        now = time.monotonic()
        if now - last_hello >= 2.5:
            os.write(fd, hello)
            last_hello = now
        if fallback_s > 0 and (now - started) >= fallback_s:
            return b""
        r, _, _ = select.select([fd], [], [], 0.5)
        if not r:
            continue
        try:
            chunk = os.read(fd, 16384)
        except BlockingIOError:
            continue
        if chunk == b"":
            raise OSError("serial port closed")
        buf += chunk
        if CTL_PROBE in buf:
            idx = buf.rfind(CTL_PROBE)
            del buf[:idx + len(CTL_PROBE)]
            os.write(fd, hello)
            last_hello = time.monotonic()
        while BOOT_PFX in buf:
            i = buf.index(BOOT_PFX)
            if i > 0:
                del buf[:i]
            nl = buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(buf[len(BOOT_PFX):nl]).rstrip(b"\r")
            del buf[:nl + 1]
            code = unpack_boot_line(line)
            if code is not None:
                return code
        if len(buf) > BUF_MAX:
            del buf[:-65536]


def run_once():
    dev = discover_port()
    sibling = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kvm_helper.py")
    fb_default = "12" if os.path.isfile(sibling) else "0"
    fallback_s = float(os.environ.get("KVM_STUB_FALLBACK_S", fb_default))
    fd = open_raw(dev)
    code = None
    try:
        print(f"stub on {dev}: announcing TYPE_HELLO", flush=True)
        code = wait_for_boot(fd, fallback_s=fallback_s)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if code:
        target = write_runtime_helper(code)
        print(f"stub: verified {len(code)} B helper -> {target}", flush=True)
    elif code == b"" and os.path.isfile(sibling):
        target = sibling
        print(f"stub: no stream after {fallback_s:.0f}s, using local {target}", flush=True)
    else:
        return False
    env = dict(os.environ, KVM_HOST_DEV=dev)
    subprocess.run([sys.executable, target, "--once"], env=env)
    return True


def main():
    once = "--once" in sys.argv[1:]
    while True:
        try:
            ran = run_once()
            if once and ran:
                return
        except OSError as exc:
            if once:
                raise
            time.sleep(2.0)
            continue
        time.sleep(1.0)


if __name__ == "__main__":
    main()
