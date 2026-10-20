"""PC readings and desktop stills carried on the same serial stream as the terminal.

Envelope, little-endian:

    magic  FE FD 4B 56   ("\\xfe\\xfdKV")
    type   u8            1 = stats, 2 = frame, 3 = mode, 4 = net+cores, 5 = triage, 6 = clip
    len    u32           payload bytes
    payload

Type 1 payload is three floats: temp C, load1, mem percent. Exactly 12 bytes.
Type 2 payload is 240*135 RGB565 little-endian pixels, top row first. Exactly 64800 bytes.
Type 3 payload is the helper's serial mode ack: 1 byte, 0 = helper, 1 = shell.
Type 4 payload is three floats (down bytes/s, up bytes/s, CPU count) plus 8 u8 core busy
percentages sorted hottest-to-coolest (0..100, 255 = unused). 20 bytes (12-byte legacy accepted).
Type 5 payload is headless bench triage: disk %, top process CPU %, 16B LAN IP, 16B top process
name (`<ff16s16s`, 40 bytes).
Type 6 payload is UTF-8 host clipboard bytes (0..1024 bytes).
Types 4, 5, 6 do not by themselves make the link LINKED. A stopped stats packet still
drops the board to KEYS even if other packets keep arriving.

Anything else is terminal text. A header that does not match those shapes is
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
TYPE_MODE = 3
TYPE_NET = 4
TYPE_TRIAGE = 5
TYPE_CLIP = 6
MODE_NAMES = {0: "helper", 1: "shell"}
STATS_LEN = 12
NET_LEN = 20
NET_LEGACY_LEN = 12
TRIAGE_LEN = 40
CLIP_MAX = 1024
FRAME_LEN = 240 * 135 * 2
FRESH_S = 6.0


def pack_stats(temp, load, mem):
    payload = struct.pack("<fff", float(temp), float(load), float(mem))
    return MAGIC + bytes([TYPE_STATS]) + struct.pack("<I", len(payload)) + payload


def pack_net(rx_bps, tx_bps, ncpu, cores=()):
    cbytes = [
        min(100, max(0, int(round(c))))
        if (c is not None and math.isfinite(c) and 0 <= c <= 100)
        else 255
        for c in list(cores)[:8]
    ]
    cbytes += [255] * (8 - len(cbytes))
    payload = struct.pack("<fff8B", float(rx_bps), float(tx_bps), float(ncpu), *cbytes)
    return MAGIC + bytes([TYPE_NET]) + struct.pack("<I", len(payload)) + payload


def pack_triage(disk_pct, top_cpu_pct, lan_ip="", top_proc=""):
    ip_b = (lan_ip or "").encode("utf-8")[:15].ljust(16, b"\x00")
    proc_b = (top_proc or "").encode("utf-8")[:15].ljust(16, b"\x00")
    payload = struct.pack("<ff16s16s", float(disk_pct), float(top_cpu_pct), ip_b, proc_b)
    return MAGIC + bytes([TYPE_TRIAGE]) + struct.pack("<I", len(payload)) + payload


def pack_clip(text):
    raw = (text.encode("utf-8") if isinstance(text, str) else bytes(text))[:CLIP_MAX]
    return MAGIC + bytes([TYPE_CLIP]) + struct.pack("<I", len(raw)) + raw


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
        self._net = None            # (down bytes/s, up bytes/s, ncpu)
        self._net_at = None
        self._ncpu = None
        self._cores = None          # [int 0..100, ...] sorted hottest-to-coolest (max 8)
        self._cores_at = None
        self._triage = None         # (disk_pct, top_cpu_pct, lan_ip, top_proc)
        self._triage_at = None
        self._clip = None           # str
        self._clip_at = None
        self._clip_seq = 0
        self._frame = None
        self._frame_at = None
        self._frame_seq = 0
        self._mode = None           # last mode the helper acked ("helper"/"shell")
        self._mode_at = None

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
                "pc_rx_bps": None,
                "pc_tx_bps": None,
                "pc_ncpu": None,
                "pc_cores": None,
                "pc_ip": None,
                "pc_disk_pct": None,
                "pc_top_proc": None,
                "pc_top_cpu_pct": None,
            }
            if link == "LINKED" and self._stats is not None:
                temp, load, mem = self._stats
                fields["pc_temp_c"] = round(temp, 1)
                fields["pc_load1"] = round(load, 2)
                fields["pc_mem_pct"] = int(round(mem))
                fields["pc_age_s"] = round(self._now() - self._stats_at, 1)
                if self._net is not None and self._fresh(self._net_at):
                    rx, tx, ncpu = self._net
                    fields["pc_rx_bps"] = round(rx, 1)
                    fields["pc_tx_bps"] = round(tx, 1)
                    fields["pc_ncpu"] = int(round(ncpu))
                if self._cores is not None and self._fresh(self._cores_at):
                    fields["pc_cores"] = list(self._cores)
                    if fields["pc_ncpu"] is None and self._ncpu is not None:
                        fields["pc_ncpu"] = self._ncpu
                if self._triage is not None and self._fresh(self._triage_at):
                    disk_pct, top_cpu_pct, lan_ip, top_proc = self._triage
                    if 0 <= disk_pct <= 100:
                        fields["pc_disk_pct"] = int(round(disk_pct))
                    if top_cpu_pct >= 0 and top_proc:
                        fields["pc_top_cpu_pct"] = round(top_cpu_pct, 1)
                    fields["pc_ip"] = lan_ip
                    fields["pc_top_proc"] = top_proc
            return fields

    def frame_bytes(self, host_connected):
        """Latest still, or None when it is stale or the USB host is gone."""
        with self._lock:
            if not host_connected or not self._fresh(self._frame_at):
                return None
            return self._frame

    def frame_seq(self):
        with self._lock:
            return self._frame_seq

    def clipboard(self):
        """(latest clipboard text or None, age in seconds or None, sequence counter)."""
        with self._lock:
            if self._clip_at is None:
                return None, None, self._clip_seq
            return self._clip, round(self._now() - self._clip_at, 1), self._clip_seq

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
        if typ == TYPE_MODE:
            return ln == 1
        if typ == TYPE_NET:
            return ln in (NET_LEN, NET_LEGACY_LEN)
        if typ == TYPE_TRIAGE:
            return ln == TRIAGE_LEN
        if typ == TYPE_CLIP:
            return 0 <= ln <= CLIP_MAX
        return False

    def _accept(self, typ, payload):
        now = self._now()
        if typ == TYPE_STATS:
            temp, load, mem = struct.unpack("<fff", payload)
            if not (math.isfinite(temp) and math.isfinite(load) and math.isfinite(mem)):
                return
            self._stats = (temp, load, mem)
            self._stats_at = now
        elif typ == TYPE_NET:
            if len(payload) == NET_LEN:
                rx, tx, ncpu, *cbytes = struct.unpack("<fff8B", payload)
            else:
                rx, tx, ncpu = struct.unpack("<fff", payload)
                cbytes = []
            if not (math.isfinite(rx) and math.isfinite(tx) and math.isfinite(ncpu)) or ncpu < 1:
                return
            self._ncpu = int(round(ncpu))
            cores = [b for b in cbytes if b <= 100]
            if cores:
                self._cores = cores
                self._cores_at = now
            if rx >= 0 and tx >= 0:
                self._net = (rx, tx, ncpu)
                self._net_at = now
        elif typ == TYPE_TRIAGE:
            disk_pct, top_cpu_pct, ip_b, proc_b = struct.unpack("<ff16s16s", payload)
            if not (math.isfinite(disk_pct) and math.isfinite(top_cpu_pct)):
                return
            if disk_pct < -1 or disk_pct > 100 or top_cpu_pct < -1:
                return
            lan_ip = ip_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip() or None
            top_proc = proc_b.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip() or None
            self._triage = (disk_pct, top_cpu_pct, lan_ip, top_proc)
            self._triage_at = now
        elif typ == TYPE_CLIP:
            self._clip = payload.decode("utf-8", errors="replace")
            self._clip_at = now
            self._clip_seq += 1
        elif typ == TYPE_FRAME:
            self._frame = payload
            self._frame_at = now
            self._frame_seq += 1
        elif typ == TYPE_MODE and payload[0] in MODE_NAMES:
            self._mode = MODE_NAMES[payload[0]]
            self._mode_at = now

    def mode(self):
        """(acked mode or None, seconds since the ack or None)."""
        with self._lock:
            if self._mode_at is None:
                return None, None
            return self._mode, round(self._now() - self._mode_at, 1)


shared = HostView()
