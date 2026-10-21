"""Extended PC stats for the helper (envelope type 8, JSON, version 1).

    {"v":1, "cores":[pct,...], "gpu_pct":n, "temps":{"cpu":c,"gpu":c,"nvme":c}}

Every key except "v" is optional and simply left out when this PC can't report it.
Sources, cheapest first: /proc/stat (per-core), psutil if installed, /sys/class/hwmon,
AMD gpu_busy_percent, nvidia-smi if on PATH. Types 1/4/5 (temp/load/mem, net + top-8 cores, triage) are still sent.
"""
import glob
import json
import math
import os
import shutil
import struct
import subprocess
import threading
import time

TYPE_STATS_EXT = 8
MAGIC = b"\xfe\xfdKV"

try:
    import psutil
except ImportError:
    psutil = None


def _ok(v, lo=-50.0, hi=200.0):
    return isinstance(v, (int, float)) and math.isfinite(v) and lo <= v <= hi


class ExtStats:
    def __init__(self, root="/", nvidia_smi=None):
        self.root = root
        self.prev = None
        self.nvsmi = nvidia_smi if nvidia_smi is not None else shutil.which("nvidia-smi")
        self._nv = (None, None)          # (pct, temp) from the background poller
        self._nv_thread = None

    def _p(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def cores(self):
        try:
            lines = [l.split() for l in open(self._p("/proc/stat")) if l.startswith("cpu") and l[3].isdigit()]
        except OSError:
            return None
        cur = [(sum(map(int, f[1:])), int(f[4]) + (int(f[5]) if len(f) > 5 else 0)) for f in lines]
        prev, self.prev = self.prev, cur
        if not prev or len(prev) != len(cur):
            return None
        out = []
        for (t1, i1), (t0, i0) in zip(cur, prev):
            dt = t1 - t0
            out.append(round(100.0 * (1 - (i1 - i0) / dt), 1) if dt > 0 else 0.0)
        return out

    def hwmon_temps(self):
        temps = {}
        if psutil and hasattr(psutil, "sensors_temperatures") and self.root == "/":
            try:
                for name, entries in psutil.sensors_temperatures().items():
                    self._classify(temps, name, [e.current for e in entries])
            except Exception:
                pass
        if temps:
            return temps
        for d in sorted(glob.glob(self._p("/sys/class/hwmon/hwmon*"))):
            try:
                name = open(os.path.join(d, "name")).read().strip()
            except OSError:
                continue
            vals = []
            for f in sorted(glob.glob(os.path.join(d, "temp*_input"))):
                try:
                    vals.append(int(open(f).read()) / 1000.0)
                except (OSError, ValueError):
                    pass
            self._classify(temps, name, vals)
        return temps

    @staticmethod
    def _classify(temps, name, vals):
        vals = [v for v in vals if _ok(v)]
        if not vals:
            return
        kind = {"coretemp": "cpu", "k10temp": "cpu", "zenpower": "cpu", "cpu_thermal": "cpu",
                "nvme": "nvme", "amdgpu": "gpu", "nouveau": "gpu", "radeon": "gpu"}.get(name)
        if kind:
            temps[kind] = round(max(temps.get(kind, -999), max(vals)), 1)

    def gpu(self):
        pct = temp = None
        for f in glob.glob(self._p("/sys/class/drm/card*/device/gpu_busy_percent")):
            try:
                v = float(open(f).read())
                if _ok(v, 0, 100):
                    pct = v if pct is None else max(pct, v)
            except (OSError, ValueError):
                pass
        if pct is None and self.nvsmi:
            if self._nv_thread is None:      # nvidia-smi can take ~100 ms+: poll off the main loop
                self._nv_thread = threading.Thread(target=self._nv_loop, name="nvidia-smi", daemon=True)
                self._nv_thread.start()
            pct, temp = self._nv
        return pct, temp

    def _nv_loop(self, every=10.0, max_backoff=300.0):
        """Poll nvidia-smi off the main loop. A missing binary stops it; any other failure
        (driver hiccup, timeout, odd output) backs off 20 s, 40 s, ... up to 5 min and retries."""
        backoff = every
        while self.nvsmi:
            try:
                out = subprocess.run([self.nvsmi, "--query-gpu=utilization.gpu,temperature.gpu",
                                      "--format=csv,noheader,nounits"], capture_output=True,
                                     text=True, timeout=5, check=False).stdout.splitlines()
                u, t = (float(x) for x in out[0].split(",")[:2])
                self._nv = ((u if _ok(u, 0, 100) else None), (t if _ok(t) else None))
                backoff = every
            except (FileNotFoundError, PermissionError):
                self._nv = (None, None); self.nvsmi = None
                return
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                self._nv = (None, None)
                backoff = min(max_backoff, backoff * 2)
            self.nv_backoff = backoff
            self._sleep(backoff)

    _sleep = staticmethod(time.sleep)

    def sample(self):
        d = {"v": 1}
        c = self.cores()
        if c:
            d["cores"] = c[:64]
        temps = self.hwmon_temps()
        pct, gtemp = self.gpu()
        if pct is not None:
            d["gpu_pct"] = round(pct, 1)
        if gtemp is not None and "gpu" not in temps:
            temps["gpu"] = round(gtemp, 1)
        if temps:
            d["temps"] = temps
        return d


def pack_ext(d):
    payload = json.dumps(d, separators=(",", ":")).encode()
    return MAGIC + bytes([TYPE_STATS_EXT]) + struct.pack("<I", len(payload)) + payload
