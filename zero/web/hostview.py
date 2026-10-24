"""PC readings and desktop stills carried on the same serial stream as the terminal.

Envelope, little-endian:

    magic  FE FD 4B 56   ("\\xfe\\xfdKV")
    type   u8            1 = stats, 2 = frame, 3 = mode, 4 = net+cores, 5 = triage, 6 = clip,
                         7 = preview tiles, 8 = extended stats, 9 = preview quality ack
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
Type 7 is a preview frame of changed JPEG tiles (see host/kvm_preview.py), up to 1 MiB.
  The Zero checks the tile table and bounds and stores the JPEG bytes untouched.
Type 8 is extended stats as JSON (see host/kvm_stats.py), up to 4 KiB, version "v":1.
Type 9 payload is the preview quality ack: 1 byte, 0 = low, 1 = med, 2 = high, 3 = off.
Types 7-9 are parsed only while a fresh (<= MODE_ACK_TTL_S) helper-mode ack is held and
the web console has not asked for shell mode; otherwise they are terminal text, so shell
output can't fake a preview. A header whose payload hasn't arrived within PARTIAL_MAX_S is
released as text (a time cap, so steady shell output can't keep it alive).
Types 4, 5, 6 do not by themselves make the link LINKED. A stopped stats packet still
drops the board to KEYS even if other packets keep arriving.

Anything else is terminal text. A header that does not match those shapes is
terminal text too: the first byte is released and parsing resumes, so a stray
lookalike cannot swallow the shell.
"""
import base64
import json
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
NET_IFACE_LEN = 36          # NET_LEN + 16-byte NUL-padded interface name
TRIAGE_LEN = 40
CLIP_MAX = 1024
FRAME_LEN = 240 * 135 * 2
TYPE_PREVIEW = 7
TYPE_EXT = 8
TYPE_QUALITY = 9
GATED = (TYPE_PREVIEW, TYPE_EXT, TYPE_QUALITY)
PREVIEW_MAX = 1 << 20
EXT_MAX = 4096
QUALITY_NAMES = {0: "low", 1: "med", 2: "high", 3: "off"}
PARTIAL_MAX_S = 3.0
MODE_ACK_TTL_S = 5.0        # helper acks every 2 s; ~2x INTERVAL plus slack
PV_MAX_W, PV_MAX_H, PV_TILE = 1280, 720, 64
PV_MAX_BYTES = 2 << 20      # total stored JPEG bytes; over this the grid resets
FRESH_S = 6.0


def pack_stats(temp, load, mem):
    payload = struct.pack("<fff", float(temp), float(load), float(mem))
    return MAGIC + bytes([TYPE_STATS]) + struct.pack("<I", len(payload)) + payload


def pack_net(rx_bps, tx_bps, ncpu, cores=(), iface=""):
    cbytes = [
        min(100, max(0, int(round(c))))
        if (c is not None and math.isfinite(c) and 0 <= c <= 100)
        else 255
        for c in list(cores)[:8]
    ]
    cbytes += [255] * (8 - len(cbytes))
    payload = struct.pack("<fff8B", float(rx_bps), float(tx_bps), float(ncpu), *cbytes)
    if iface:
        payload += (iface.encode("utf-8")[:15]).ljust(16, b"\x00")
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
        self._ncpu_at = None
        self._iface = None          # PC's busiest interface name, from the helper
        self._iface_at = None
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
        self.requested_mode = None  # set by the server from /api/mode
        self.helper_returned = 0    # bumps on a helper ack after silence (StatsKeeper watches it)
        self._ext = None            # validated type-8 dict
        self._ext_at = None
        self._quality = None        # last quality the helper acked
        self._pv = {}               # (x, y) -> (zseq, w, h, jpeg bytes)
        self._pv_seq = 0
        self._pv_epoch = 0
        self._pv_size = None
        self._pv_at = None
        self._pv_bytes = 0
        self.pv_max_bytes = PV_MAX_BYTES
        self._partial_at = None

    def reset(self):
        """Drop a half-read envelope after the serial port closes. Readings stay until they go stale."""
        with self._lock:
            self._buf.clear(); self._partial_at = None

    def drop_incomplete(self):
        """Drop a header whose payload never arrived. A short magic prefix stays, so a pause
        in the shell does not eat the last few characters. Readings stay until they go stale."""
        with self._lock:
            if self._buf.find(MAGIC) != 0:
                return False
            if len(self._buf) < HDR:
                # MAGIC plus a partial type/length: the writer died mid-header.
                self._buf.clear()
                return True
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
                if not self._ok(typ, ln):
                    out.append(self._buf[0])
                    del self._buf[:1]; self._partial_at = None
                    continue
                # A fresh, fully valid header inside this packet's payload means the writer
                # restarted mid-packet. Drop the truncated packet, keep parsing.
                j = self._restart_at(HDR + ln)
                if j > 0:
                    del self._buf[:j]
                    continue
                if len(self._buf) < HDR + ln:
                    now = self._now()
                    if self._partial_at is None:
                        self._partial_at = now
                    elif now - self._partial_at > PARTIAL_MAX_S:
                        out.append(self._buf[0])      # too slow: not a real packet
                        del self._buf[:1]; self._partial_at = None
                        continue
                    break
                self._partial_at = None
                if typ == TYPE_PREVIEW:            # parse in place: one copy per tile
                    mv = memoryview(self._buf)
                    try:
                        self._accept_preview(mv[HDR:HDR + ln], self._now())
                    finally:
                        mv.release()
                    del self._buf[:HDR + ln]
                    continue
                payload = bytes(self._buf[HDR:HDR + ln])
                del self._buf[:HDR + ln]
                self._accept(typ, payload)
            return bytes(out)

    def _helper_live(self):
        return (self._mode == "helper" and self._mode_at is not None
                and self._now() - self._mode_at <= MODE_ACK_TTL_S
                and self.requested_mode != "shell")

    def _ok(self, typ, ln):
        if typ in GATED and not self._helper_live():
            return False
        return self._shape_ok(typ, ln)

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
                "pc_net_iface": None,
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
                if self._iface is not None and self._fresh(self._iface_at):
                    fields["pc_net_iface"] = self._iface
                if self._cores is not None and self._fresh(self._cores_at):
                    fields["pc_cores"] = list(self._cores)
                if fields["pc_ncpu"] is None and self._ncpu is not None and self._fresh(self._ncpu_at):
                    fields["pc_ncpu"] = self._ncpu
                if self._triage is not None and self._fresh(self._triage_at):
                    disk_pct, top_cpu_pct, lan_ip, top_proc = self._triage
                    if 0 <= disk_pct <= 100:
                        fields["pc_disk_pct"] = int(round(disk_pct))
                    if top_cpu_pct >= 0 and top_proc:
                        fields["pc_top_cpu_pct"] = round(top_cpu_pct, 1)
                    fields["pc_ip"] = lan_ip
                    fields["pc_top_proc"] = top_proc
            ext = self._ext if link == "LINKED" and self._fresh(self._ext_at) else {}
            temps = ext.get("temps", {})
            fields.update({"pc_cores_all": ext.get("cores"), "pc_gpu_pct": ext.get("gpu_pct"),
                           "pc_cpu_temp_c": temps.get("cpu"), "pc_gpu_temp_c": temps.get("gpu"),
                           "pc_nvme_temp_c": temps.get("nvme"), "pc_uptime_s": ext.get("uptime_s")})
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

    def _restart_at(self, limit):
        """Index (< limit) of a complete, fixed-length header inside the current packet's
        payload, or -1. Every such header is one of a few exact 9-byte constants, so a false
        match inside pixel data is vanishingly unlikely. Clipboard (variable length) is not
        used."""
        i = self._buf.find(MAGIC, 1)
        while 0 < i < limit:
            if len(self._buf) - i < HDR:
                return -1
            typ = self._buf[i + 4]
            ln = struct.unpack_from("<I", self._buf, i + 5)[0]
            if typ not in (TYPE_CLIP, TYPE_PREVIEW, TYPE_EXT) and self._ok(typ, ln):
                return i
            i = self._buf.find(MAGIC, i + 1)
        return -1

    @staticmethod
    def _shape_ok(typ, ln):
        if typ == TYPE_STATS:
            return ln == STATS_LEN
        if typ == TYPE_FRAME:
            return ln == FRAME_LEN
        if typ == TYPE_MODE:
            return ln == 1
        if typ == TYPE_NET:
            return ln in (NET_LEN, NET_LEGACY_LEN, NET_IFACE_LEN)
        if typ == TYPE_TRIAGE:
            return ln == TRIAGE_LEN
        if typ == TYPE_CLIP:
            return 0 <= ln <= CLIP_MAX
        if typ == TYPE_QUALITY:
            return ln == 1
        if typ == TYPE_PREVIEW:
            return 9 <= ln <= PREVIEW_MAX
        if typ == TYPE_EXT:
            return 2 <= ln <= EXT_MAX
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
            iface = None
            if len(payload) == NET_IFACE_LEN:
                rx, tx, ncpu, *cbytes = struct.unpack("<fff8B", payload[:NET_LEN])
                name = payload[NET_LEN:].split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
                iface = name if name and name.isprintable() else None
            elif len(payload) == NET_LEN:
                rx, tx, ncpu, *cbytes = struct.unpack("<fff8B", payload)
            else:
                rx, tx, ncpu = struct.unpack("<fff", payload)
                cbytes = []
            if not (math.isfinite(rx) and math.isfinite(tx) and math.isfinite(ncpu)) or ncpu < 1:
                return
            self._ncpu = int(round(ncpu))
            self._ncpu_at = now
            if iface:
                self._iface = iface
                self._iface_at = now
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
            if MODE_NAMES[payload[0]] == "helper" and (
                    self._mode_at is None or now - self._mode_at > MODE_ACK_TTL_S):
                self.helper_returned += 1          # first ack, or first after a gap (restart)
            if MODE_NAMES[payload[0]] == "helper" and self._mode == "shell":
                # bash exited on its own (Ctrl-D / exit): the shell request is over, so
                # types 7-9 are parsed again and the GEEK keeps its extended stats.
                self.requested_mode = None
            self._mode = MODE_NAMES[payload[0]]
            self._mode_at = now
        elif typ == TYPE_QUALITY and payload[0] in QUALITY_NAMES:
            self._quality = QUALITY_NAMES[payload[0]]
        elif typ == TYPE_EXT:
            ext = _parse_ext(payload)
            if ext is not None:
                self._ext, self._ext_at = ext, now

    def _accept_preview(self, payload, now):
        """payload is a memoryview into the read buffer; tiles are copied out once."""
        ver, flags, q, w, h, n = struct.unpack_from("<BBBHHH", payload, 0)
        if ver != 1 or q not in (0, 1, 2) or not (0 < w <= PV_MAX_W and 0 < h <= PV_MAX_H):
            return
        if n > ((w + PV_TILE - 1) // PV_TILE) * ((h + PV_TILE - 1) // PV_TILE):
            return
        table, off = [], 9
        for _ in range(n):
            if off + 12 > len(payload):
                return
            x, y, tw, th, ln = struct.unpack_from("<HHHHI", payload, off)
            off += 12
            if (off + ln > len(payload) or x % PV_TILE or y % PV_TILE or not 0 < tw <= PV_TILE
                    or not 0 < th <= PV_TILE or x + tw > w or y + th > h):
                return
            table.append((x, y, tw, th, off, ln))
            off += ln
        if off != len(payload):
            return
        self._pv_seq += 1
        if flags & 1 or self._pv_size != (w, h, q):
            self._pv_reset()
            self._pv_size = (w, h, q)
        for x, y, tw, th, o, ln in table:
            old = self._pv.get((x, y))
            self._pv_bytes += ln - (len(old[3]) if old else 0)
            self._pv[(x, y)] = (self._pv_seq, tw, th, bytes(payload[o:o + ln]))
        if self._pv_bytes > self.pv_max_bytes:       # runaway: keep nothing, wait for a key frame
            self._pv_reset()
        self._pv_at = now

    def _pv_reset(self):
        self._pv.clear(); self._pv_bytes = 0; self._pv_epoch = self._pv_seq

    def preview(self, since, host_connected):
        """Tiles newer than `since` (all of them, with reset=True, if `since` predates the
        current grid). None when the stream is stale or the USB host is gone."""
        with self._lock:
            if not host_connected or not self._fresh(self._pv_at) or self._pv_size is None:
                return None
            reset = since < self._pv_epoch or since > self._pv_seq
            tiles = [{"x": x, "y": y, "w": tw, "h": th, "jpg": base64.b64encode(jpg).decode()}
                     for (x, y), (zs, tw, th, jpg) in self._pv.items() if reset or zs > since]
            w, h, q = self._pv_size
            return {"seq": self._pv_seq, "w": w, "h": h, "quality": QUALITY_NAMES[q],
                    "reset": reset, "tiles": tiles}

    def quality(self):
        with self._lock:
            return self._quality

    def ext(self):
        with self._lock:
            return dict(self._ext) if self._ext and self._fresh(self._ext_at) else None

    def mode(self):
        """(acked mode or None, seconds since the ack or None)."""
        with self._lock:
            if self._mode_at is None:
                return None, None
            return self._mode, round(self._now() - self._mode_at, 1)


class StatsKeeper:
    """Decides when kvm-web sends KVMCTL:stats:on. Only after a helper ack (so a plain
    getty on the port never gets control bytes): the first one, and the first after a gap,
    which is what a helper restart looks like. At most once per `min_s`. A pending request
    that hits the rate limit is sent as soon as the limit allows. want=False (POST
    /api/stats {"on":false}) stops the automatic requests until it is turned back on."""

    def __init__(self, view, min_s=10.0, now=None):
        self.view, self.min_s, self._now = view, min_s, now or time.monotonic
        self.want = True
        self.seen = 0               # helper_returned value already answered
        self.last_sent = None

    def due(self):
        if not self.want or self.view.helper_returned == self.seen:
            return False
        if self.view.mode()[0] != "helper":
            return False
        return self.last_sent is None or self._now() - self.last_sent >= self.min_s

    def sent(self):
        self.seen = self.view.helper_returned
        self.last_sent = self._now()


def _num(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def _parse_ext(payload):
    """Validated type-8 dict, keeping only known, sane fields; None if unusable."""
    try:
        d = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    out = {}
    c = d.get("cores")
    if isinstance(c, list) and 0 < len(c) <= 64 and all(_num(x, 0, 100) for x in c):
        out["cores"] = [round(float(x), 1) for x in c]
    if _num(d.get("gpu_pct"), 0, 100):
        out["gpu_pct"] = round(float(d["gpu_pct"]), 1)
    t = d.get("temps")
    if isinstance(t, dict):
        tt = {k: round(float(t[k]), 1) for k in ("cpu", "gpu", "nvme") if _num(t.get(k), -50, 200)}
        if tt:
            out["temps"] = tt
    if _num(d.get("uptime_s"), 0, 10 ** 9):
        out["uptime_s"] = int(d["uptime_s"])
    return out


shared = HostView()
