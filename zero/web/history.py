"""Cumulative per-process network history per PC, kept in SQLite on the writable /data
partition (KVM_DATA_DIR, default /var/lib/kvm), outside the read-only overlay.

The helper sends per-interval byte deltas (type-8 "netdelta"); ingest() adds them up in
memory and flush() commits them (kvm-web calls it every 30 s and on shutdown), so the SD
card sees one small write batch per 30 s. If the directory is missing or unwritable the
feature is simply off: every call is a no-op and status shows nulls.

Boot erase files: `<hostname-or-pc_id>-erase.txt` on the FAT boot partition (any PC can
write it). scan_erase() wipes that PC once. Boot is never written; handled files are
recorded here as (name, mtime, size), so a new file (different mtime) runs again.
"""
import os, re, sqlite3, sys, threading, time

PC_ID_RE = re.compile(r"[0-9a-f]{12}")
ERASE_RE = re.compile(r"(.+)-erase\.txt", re.I)
DELTA_MAX = 1e12          # per interval, per process; larger is nonsense


def _mount_info(path):
    """(fstype, set(options)) of the mount exactly at path, or (None, set())."""
    path = os.path.realpath(path)
    try:
        with open("/proc/mounts") as f:
            for line in reversed(f.read().splitlines()):
                parts = line.split()
                if len(parts) >= 4 and parts[1].replace("\\040", " ") == path:
                    return parts[2], set(parts[3].split(","))
    except OSError:
        pass
    return None, set()


class Ambiguous(Exception):
    pass


class NotFound(Exception):
    pass


def log(msg):
    print(f"net-history: {msg}", file=sys.stderr, flush=True)


class NetHistory:
    def __init__(self, data_dir, now=time.time, flush_s=30.0, require_mount=False):
        self.path = os.path.join(data_dir, "history.sqlite3")
        self.data_dir, self._now, self.flush_s = data_dir, now, flush_s
        self._lock = threading.Lock()
        self._db = None
        self._pending = {}        # pc_id -> {"host","first","procs":{name:[rx,tx]}}
        self.require_mount = require_mount
        self.enabled = False

    def open(self):
        try:
            if not os.path.isdir(self.data_dir):
                raise OSError(f"{self.data_dir} missing (no kvmdata partition?)")
            if self.require_mount:
                # A bare dir, or an overlay over kvmdata, would take writes into RAM and lose them on reboot.
                fstype, opts = _mount_info(self.data_dir)
                if fstype != "ext4" or "rw" not in opts:
                    raise OSError(f"{self.data_dir} is not a writable ext4 mount (got {fstype or 'none'}; "
                                  "kvmdata not mounted, or overlayroot missing recurse=0?)")
            db = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pcs (pc_id TEXT PRIMARY KEY, pc_host TEXT, since REAL NOT NULL, last_seen REAL);
                CREATE TABLE IF NOT EXISTS net (pc_id TEXT, proc TEXT, rx INTEGER NOT NULL, tx INTEGER NOT NULL,
                                                PRIMARY KEY (pc_id, proc));
                CREATE TABLE IF NOT EXISTS erase_done (name TEXT, mtime REAL, size INTEGER, at REAL,
                                                       PRIMARY KEY (name, mtime, size));""")
            db.commit()
            self._db, self.enabled = db, True
        except (OSError, sqlite3.Error) as e:
            log(f"disabled: {e}")
            self._db, self.enabled = None, False
        return self.enabled

    def ingest(self, ext):
        """One validated type-8 dict. Memory only (called under the hostview lock)."""
        if not self.enabled or "pc_id" not in ext:
            return
        with self._lock:
            p = self._pending.setdefault(ext["pc_id"], {"host": None, "first": self._now(), "procs": {}})
            p["host"] = ext.get("pc_host") or p["host"]
            for name, rx, tx in ext.get("netdelta", ()):
                if not (0 <= rx <= DELTA_MAX and 0 <= tx <= DELTA_MAX):
                    continue      # negative = counter reset somewhere; never subtract
                a = p["procs"].setdefault(name, [0, 0])
                a[0] += int(rx); a[1] += int(tx)

    def flush(self):
        if not self.enabled:
            return
        with self._lock:
            pend, self._pending = self._pending, {}
            if not pend:
                return
            try:
                with self._db:
                    for pc, p in pend.items():
                        self._db.execute("INSERT OR IGNORE INTO pcs VALUES (?,?,?,?)", (pc, p["host"], p["first"], self._now()))
                        self._db.execute("UPDATE pcs SET pc_host=COALESCE(?,pc_host), last_seen=? WHERE pc_id=?",
                                         (p["host"], self._now(), pc))
                        for name, (rx, tx) in p["procs"].items():
                            self._db.execute("INSERT INTO net VALUES (?,?,?,?) ON CONFLICT(pc_id,proc) DO UPDATE "
                                             "SET rx=rx+excluded.rx, tx=tx+excluded.tx", (pc, name, rx, tx))
            except sqlite3.Error as e:
                log(f"flush failed, disabling: {e}")
                self.enabled = False

    def snapshot(self, pc_id, n=8):
        """(since unix seconds, [{"p","rx","tx"}] busiest first) incl. unflushed bytes; (None, None) if off/unknown."""
        if not self.enabled or not pc_id:
            return None, None
        with self._lock:
            try:
                row = self._db.execute("SELECT since FROM pcs WHERE pc_id=?", (pc_id,)).fetchone()
                tot = {r[0]: [r[1], r[2]] for r in self._db.execute("SELECT proc, rx, tx FROM net WHERE pc_id=?", (pc_id,))}
            except sqlite3.Error:
                return None, None
            p = self._pending.get(pc_id)
            if row is None and p is None:
                return None, None
            since = row[0] if row else p["first"]
            for name, (rx, tx) in (p["procs"].items() if p else ()):
                a = tot.setdefault(name, [0, 0]); a[0] += rx; a[1] += tx
        rows = sorted(tot.items(), key=lambda kv: kv[1][0] + kv[1][1], reverse=True)[:n]
        return int(since), [{"p": k, "rx": int(v[0]), "tx": int(v[1])} for k, v in rows]

    def resolve(self, pc):
        """pc_id for a pc_id or hostname. Raises NotFound / Ambiguous."""
        self.flush()
        with self._lock:
            if PC_ID_RE.fullmatch(pc) and self._db.execute("SELECT 1 FROM pcs WHERE pc_id=?", (pc,)).fetchone():
                return pc
            ids = [r[0] for r in self._db.execute("SELECT pc_id FROM pcs WHERE lower(pc_host)=lower(?)", (pc,))]
        if not ids:
            raise NotFound(pc)
        if len(ids) > 1:
            raise Ambiguous(ids)
        return ids[0]

    def reset(self, pc):
        """Wipe all saved history for `pc` (hostname or pc_id), since=now. Returns the pc_id."""
        if not self.enabled:
            raise OSError("history storage is off")
        pid = self.resolve(pc)
        with self._lock, self._db:
            self._pending.pop(pid, None)
            self._db.execute("DELETE FROM net WHERE pc_id=?", (pid,))
            self._db.execute("UPDATE pcs SET since=? WHERE pc_id=?", (self._now(), pid))
        log(f"reset {pid}")
        return pid

    def scan_erase(self, boot_dir):
        """Handle new `<host-or-id>-erase.txt` files in boot_dir. Returns [(file, pc_id or error)]."""
        if not self.enabled or not boot_dir or not os.path.isdir(boot_dir):
            return []
        done = []
        for name in sorted(os.listdir(boot_dir)):
            m = ERASE_RE.fullmatch(name)
            if not m:
                continue
            try:
                st = os.stat(os.path.join(boot_dir, name))
            except OSError:
                continue
            key = (name, st.st_mtime, st.st_size)
            with self._lock:
                if self._db.execute("SELECT 1 FROM erase_done WHERE name=? AND mtime=? AND size=?", key).fetchone():
                    continue
            target = m.group(1)
            try:
                res = self.reset(target)
                log(f"erase file {name}: wiped {res}")
            except Ambiguous as e:
                res = f"ambiguous hostname, use pc_id ({', '.join(e.args[0])})"
                log(f"erase file {name}: {res}")
            except (NotFound, OSError, sqlite3.Error) as e:
                res = f"no such PC ({e})"
                log(f"erase file {name}: {res}")
            with self._lock, self._db:
                self._db.execute("INSERT OR IGNORE INTO erase_done VALUES (?,?,?,?)", key + (self._now(),))
            done.append((name, res))
        return done

    def close(self):
        self.flush()
        if self._db is not None:
            self._db.close()


def boot_dir():
    env = os.environ.get("KVM_BOOT_DIR")
    if env:
        return env
    for d in ("/boot/firmware", "/boot"):
        if os.path.isdir(d):
            return d
    return None
