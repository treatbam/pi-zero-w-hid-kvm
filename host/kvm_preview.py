"""Desktop preview for the KVM helper: changed-tile JPEG frames over the serial port.

Envelope type 7 (PREVIEW), payload little-endian:
    u8 version (1)  u8 flags (bit0 = key: drop all old tiles)  u8 quality (0 low, 1 med, 2 high)
    u16 width  u16 height  u16 ntiles
    ntiles x { u16 x  u16 y  u16 w  u16 h  u32 len  <len bytes of baseline JPEG> }
A frame with ntiles = 0 is a heartbeat ("still live, nothing changed").

Tiles are TILE x TILE (edge tiles smaller). A tile's reference copy only moves forward
when it is actually sent, so tiles left over when a frame hits the byte budget are
sent on the next tick. The Zero passes the JPEGs through untouched; the browser
composites them.

Optional: needs Pillow (python3-pil) for JPEG and ffmpeg for the grab. Without either,
or with no X display, the preview is off and only the legacy 240x135 still is sent.
Nothing here is required for the helper to run on a stock Python 3.
"""
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time

try:
    from PIL import Image
except ImportError:          # preview disabled, helper still works
    Image = None

TYPE_PREVIEW = 7
TYPE_QUALITY = 9
VERSION = 1
TILE = 64
KEY_EVERY_S = 60.0
# name: (index, width, height, jpeg quality, interval s, byte budget per frame)
QUALITY = {
    "low":  (0, 640, 360, 45, 1.0, 48_000),
    "med":  (1, 960, 540, 60, 0.5, 96_000),
    "high": (2, 1280, 720, 75, 0.25, 160_000),
}
QNAMES = {v[0]: k for k, v in QUALITY.items()}
QIDX = {"low": 0, "med": 1, "high": 2, "off": 3}   # type-6 ack values
MAGIC = b"\xfe\xfdKV"


def envelope(typ, payload):
    return MAGIC + bytes([typ]) + struct.pack("<I", len(payload)) + payload


def pack_quality(name):
    return envelope(TYPE_QUALITY, bytes([QIDX[name]]))


def tiles_of(w, h, tile=TILE):
    return [(x, y, min(tile, w - x), min(tile, h - y))
            for y in range(0, h, tile) for x in range(0, w, tile)]


class TileEncoder:
    """Diff rgb24 frames tile by tile and JPEG-encode the changed ones."""

    def __init__(self, quality="med"):
        self.set_quality(quality)

    def set_quality(self, name):
        if name not in QUALITY:
            raise ValueError(name)
        self.quality = name
        _, self.w, self.h, self.jpeg_q, self.interval, self.budget = QUALITY[name]
        self.force_key()

    def force_key(self):
        self.ref = {}            # (x, y) -> bytes last sent for that tile
        self.pending = set()     # tiles that changed but did not fit the last budget
        self.key = True

    def _tile_bytes(self, frame, x, y, w, h):
        stride, bx = self.w * 3, x * 3
        return b"".join(frame[r * stride + bx: r * stride + bx + w * 3] for r in range(y, y + h))

    def changed(self, frame, prev):
        """Tiles whose pixels differ from what was last sent. Unchanged bands of TILE rows
        are skipped with one compare."""
        stride, out = self.w * 3, []
        for y in range(0, self.h, TILE):
            h = min(TILE, self.h - y)
            band = slice(y * stride, (y + h) * stride)
            if prev is not None and frame[band] == prev[band] and not any(
                    py == y for _, py in self.pending):
                continue
            for x in range(0, self.w, TILE):
                w = min(TILE, self.w - x)
                tb = self._tile_bytes(frame, x, y, w, h)
                if self.ref.get((x, y)) != tb:
                    out.append((x, y, w, h, tb))
        return out

    def encode(self, frame, prev=None):
        """One type-4 envelope for this rgb24 frame (len w*h*3)."""
        if len(frame) != self.w * self.h * 3:
            raise ValueError("frame size does not match quality")
        if Image is None:
            raise RuntimeError("Pillow not installed")
        flags = 1 if self.key else 0
        if self.key:
            prev = None
        body, used, n = bytearray(), 0, 0
        todo = self.changed(frame, prev)
        self.pending = set()
        for x, y, w, h, tb in todo:
            if used >= self.budget:
                self.pending.add((x, y))     # stays dirty for the next tick
                continue
            buf = io.BytesIO()
            Image.frombytes("RGB", (w, h), tb).save(buf, "JPEG", quality=self.jpeg_q)
            jpg = buf.getvalue()
            body += struct.pack("<HHHHI", x, y, w, h, len(jpg)) + jpg
            self.ref[(x, y)] = tb
            used += len(jpg); n += 1
        self.key = False
        head = struct.pack("<BBBHHH", VERSION, flags, QUALITY[self.quality][0], self.w, self.h, n)
        return envelope(TYPE_PREVIEW, head + bytes(body))


def rgb565_still(frame, w, h):
    """240x135 RGB565-LE still for the GEEK from an rgb24 preview frame."""
    img = Image.frombytes("RGB", (w, h), bytes(frame)).resize((240, 135))
    try:
        import numpy as np
        a = np.frombuffer(img.tobytes(), dtype=np.uint8).reshape(-1, 3).astype(np.uint16)
        v = ((a[:, 0] >> 3) << 11) | ((a[:, 1] >> 2) << 5) | (a[:, 2] >> 3)
        return v.astype("<u2").tobytes()
    except ImportError:
        raw = img.tobytes()
        return b"".join(struct.pack("<H", ((raw[i] >> 3) << 11) | ((raw[i + 1] >> 2) << 5) | (raw[i + 2] >> 3))
                        for i in range(0, len(raw), 3))


class Grabber:
    """Persistent ffmpeg x11grab -> rgb24 frames at the quality's size and rate.
    KVM_PREVIEW_SOURCE=test swaps in a synthetic moving square (for tests)."""

    def __init__(self, display, quality, screen_size):
        self.display, self.quality, self.screen_size = display, quality, screen_size
        self.proc, self.buf, self.latest, self.retry_at = None, bytearray(), None, 0.0
        self.test = os.environ.get("KVM_PREVIEW_SOURCE") == "test"
        self.wayland = not display and bool(os.environ.get("WAYLAND_DISPLAY")) and bool(shutil.which("grim"))
        self.macos = not display and sys.platform == "darwin" and bool(shutil.which("screencapture"))
        self.t = 0

    @property
    def frame_len(self):
        _, w, h, *_ = QUALITY[self.quality]
        return w * h * 3

    def _fit_rgb24(self, img):
        _, w, h, *_ = QUALITY[self.quality]
        img = img.convert("RGB")
        iw, ih = img.size
        if iw <= 0 or ih <= 0:
            return None
        scale = min(w / iw, h / ih)
        nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
        resized = img.resize((nw, nh))
        if (nw, nh) == (w, h):
            return resized.tobytes()
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(resized, ((w - nw) // 2, (h - nh) // 2))
        return canvas.tobytes()

    def _grab_alt_rgb24(self):
        if Image is None or time.monotonic() < self.retry_at:
            return None
        if self.wayland:
            try:
                r = subprocess.run(["grim", "-t", "ppm", "-"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=1.5, check=False)
                if r.returncode == 0 and r.stdout:
                    return self._fit_rgb24(Image.open(io.BytesIO(r.stdout)))
            except Exception:
                pass
            self.retry_at = time.monotonic() + 5
        elif self.macos:
            fd, tmp = tempfile.mkstemp(suffix=".jpg", prefix="kvm-pv-")
            os.close(fd)
            try:
                r = subprocess.run(["screencapture", "-x", "-t", "jpg", tmp],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5, check=False)
                if r.returncode == 0 and os.path.getsize(tmp) > 0:
                    with Image.open(tmp) as im:
                        return self._fit_rgb24(im)
            except Exception:
                pass
            finally:
                try: os.unlink(tmp)
                except OSError: pass
            self.retry_at = time.monotonic() + 5
        return None

    def start(self):
        if self.test or self.proc or time.monotonic() < self.retry_at or not self.display:
            return
        size = self.screen_size(self.display)
        if not size:
            self.retry_at = time.monotonic() + 5; return
        _, w, h, _, interval, _ = QUALITY[self.quality]
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "x11grab",
               "-framerate", f"{1 / interval:g}", "-video_size", f"{size[0]}x{size[1]}",
               "-i", self.display, "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "-pix_fmt", "rgb24",
               "-f", "rawvideo", "pipe:1"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         env={**os.environ, "DISPLAY": self.display})
            os.set_blocking(self.proc.stdout.fileno(), False)
        except OSError:
            self.proc = None; self.retry_at = time.monotonic() + 5

    def stop(self):
        if self.proc:
            self.proc.kill(); self.proc.wait()
            self.proc.stdout.close()
        self.proc, self.buf, self.latest = None, bytearray(), None

    def set_quality(self, q):
        if q != self.quality:
            self.stop(); self.quality = q

    def fileno(self):
        return self.proc.stdout.fileno() if self.proc else None

    def pump(self):
        """Read what ffmpeg has written; keep only the newest complete frame."""
        if not self.proc:
            return
        try:
            while True:
                chunk = os.read(self.proc.stdout.fileno(), 1 << 20)
                if not chunk:
                    self.stop(); self.retry_at = time.monotonic() + 5; return
                self.buf += chunk
        except BlockingIOError:
            pass
        n = self.frame_len
        while len(self.buf) >= n:
            self.latest = bytes(self.buf[:n]); del self.buf[:n]

    def take(self):
        if self.test:
            _, w, h, *_ = QUALITY[self.quality]
            f = bytearray(w * h * 3)
            sx = (self.t * 16) % max(1, w - 32)
            for r in range(10, 42):
                f[(r * w + sx) * 3:(r * w + sx + 32) * 3] = b"\xff\x40\x10" * 32
            self.t += 1
            return bytes(f)
        if not self.display and (self.wayland or self.macos):
            return self._grab_alt_rgb24()
        f, self.latest = self.latest, None
        return f
