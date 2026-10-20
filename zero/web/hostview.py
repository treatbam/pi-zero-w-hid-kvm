"""PC readings and desktop stills carried on the same serial stream as the terminal.

Envelope, little-endian:

    magic  FE FD 4B 56   ("\\xfe\\xfdKV")
    type   u8            1 = stats, 2 = frame
    len    u32           payload bytes
    payload

Type 1 payload is three floats: temp C, load1, mem percent. Exactly 12 bytes.
Type 2 payload is 240*135 RGB565 little-endian pixels, top row first. Exactly 64800 bytes.

Anything else is terminal text. A header that does not match those two shapes is
terminal text too: the first byte is released and parsing resumes, so a stray
lookalike cannot swallow the shell.
"""
import math
import struct
import threading
import time

MAGIC = b"\xfe\xfdKV"
HDR = 9
TYPE_STATS = 1
TYPE_FRAME = 2
STATS_LEN = 12
FRAME_LEN = 240 * 135 * 2
FRESH_S = 6.0


def pack_stats(temp, load, mem):
    payload = struct.pack("<fff", float(temp), float(load), float(mem))
    return MAGIC + bytes([TYPE_STATS]) + struct.pack("<I", len(payload)) + payload


def pack_frame(rgb565):
    if len(rgb565) != FRAME_LEN:
        raise ValueError(f"frame must be {FRAME_LEN} bytes of rgb565le")
    return MAGIC + bytes([TYPE_FRAME]) + struct.pack("<I", FRAME_LEN) + rgb565


def _hold_len(buf):
    """How many trailing bytes are a proper prefix of MAGIC and must wait."""
    n = min(len(MAGIC) - 1, len(buf))
    for k in range(n, 0, -1):
        if MAGIC.startswith(buf[-k:]):
            return k
    return 0


class HostView:
    def __init__(self, now=None):
        self._now = now or time.monotonic
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._stats = None          # (temp, load, mem) floats
        self._stats_at = None
        self._frame = None
        self._frame_at = None

    def reset(self):
        """Drop a half-read envelope after the serial port closes. Readings stay until they go stale."""
        with self._lock:
            self._buf.clear()

    def drop_incomplete(self):
        """Drop a header whose payload never arrived. A short magic prefix stays, so a pause
        in the shell does not eat the last few characters. Readings stay until they go stale."""
        with self._lock:
            if len(self._buf) < HDR or self._buf.find(MAGIC) != 0:
                return False
            typ = self._buf[4]
            ln = struct.unpack_from("<I", self._buf, 5)[0]
            if not self._shape_ok(typ, ln) or len(self._buf) >= HDR + ln:
                return False
            self._buf.clear()
            return True

    def feed(self, data):
        """Parse data. Return the bytes that belong to the terminal."""
        with self._lock:
            self._buf.extend(data)
            out = bytearray()
            while True:
                i = self._buf.find(MAGIC)
                if i < 0:
                    hold = _hold_len(self._buf)
                    if hold:
                        out.extend(self._buf[:-hold])
                        del self._buf[:-hold]
                    else:
                        out.extend(self._buf)
                        self._buf.clear()
                    break
                out.extend(self._buf[:i])
                del self._buf[:i]
                if len(self._buf) < HDR:
                    break
                typ = self._buf[4]
                ln = struct.unpack_from("<I", self._buf, 5)[0]
                if not self._shape_ok(typ, ln):
                    out.append(self._buf[0])
                    del self._buf[:1]
                    continue
                if len(self._buf) < HDR + ln:
                    break
                payload = bytes(self._buf[HDR:HDR + ln])
                del self._buf[:HDR + ln]
                self._accept(typ, payload)
            return bytes(out)

    def status_fields(self, host_connected):
        with self._lock:
            link = self._link(host_connected)
            fresh_frame = bool(host_connected) and self._fresh(self._frame_at)
            fields = {
                "link": link,
                "frame_fresh": fresh_frame,
                "pc_temp_c": None,
                "pc_load1": None,
                "pc_mem_pct": None,
                "pc_age_s": None,
            }
            if link == "LINKED" and self._stats is not None:
                temp, load, mem = self._stats
                fields["pc_temp_c"] = round(temp, 1)
                fields["pc_load1"] = round(load, 2)
                fields["pc_mem_pct"] = int(round(mem))
                fields["pc_age_s"] = round(self._now() - self._stats_at, 1)
            return fields

    def frame_bytes(self, host_connected):
        """Latest still, or None when it is stale or the USB host is gone."""
        with self._lock:
            if not host_connected or not self._fresh(self._frame_at):
                return None
            return self._frame

    def _link(self, host_connected):
        if not host_connected:
            return "NO HOST"
        if self._stats is not None and self._fresh(self._stats_at):
            return "LINKED"
        return "KEYS"

    def _fresh(self, stamp):
        return stamp is not None and (self._now() - stamp) <= FRESH_S

    @staticmethod
    def _shape_ok(typ, ln):
        if typ == TYPE_STATS:
            return ln == STATS_LEN
        if typ == TYPE_FRAME:
            return ln == FRAME_LEN
        return False

    def _accept(self, typ, payload):
        now = self._now()
        if typ == TYPE_STATS:
            temp, load, mem = struct.unpack("<fff", payload)
            if not (math.isfinite(temp) and math.isfinite(load) and math.isfinite(mem)):
                return
            self._stats = (temp, load, mem)
            self._stats_at = now
        elif typ == TYPE_FRAME:
            self._frame = payload
            self._frame_at = now


shared = HostView()
