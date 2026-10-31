"""Virtual Media / Mass Storage Manager for the Pi Zero KVM.

Switches the one mass-storage slot the gadget already has
(/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0/lun.0)
between the KVM-SETUP stick and a boot image. An ISO under the kernel's
CD limit is presented as a CD so a PC can boot it. The chosen path is
saved on the writable data partition and put back in the slot at boot
by hid-gadget.sh.

Images live on the kvmdata partition (default /var/lib/kvm/images). Anywhere
else under the read-only root overlay is RAM and disappears on reboot.
kvmdata defaults to 64 MB: enough for netboot.xyz, not for a full installer ISO
(re-run tools/setup-kvmdata.sh with a larger --size-mb for those).
"""
import os
import re
import subprocess
import tempfile
import threading
import functools

LUN_DEFAULT = "/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0/lun.0"
SETUP_IMG_DEFAULT = "/opt/pi-zero-w-hid-kvm/zero/gadget/kvm-setup.img"
IMAGES_DIR_DEFAULT = "/var/lib/kvm/images"  # on kvmdata, so images survive the read-only root
BOOT_IMAGE_DEFAULT = "/var/lib/kvm/boot-image"

# f_mass_storage CD emulation: at least 300 sectors of 2048 bytes, and the
# kernel keeps only the first (256*60*75 - 1) sectors of anything larger.
CDROM_MIN_BYTES = 300 * 2048
CDROM_MAX_BYTES = 256 * 60 * 75 * 2048

# netboot.xyz: a ~2.4 MB boot CD that pulls installers over the host's own network.
# Pinned release, hash stored here, so a hacked mirror can't swap the file.
# To bump: change the version, download the ISO, and paste its sha256sum below.
NETBOOT_VERSION = "3.0.3"
NETBOOT_URL = f"https://github.com/netbootxyz/netboot.xyz/releases/download/{NETBOOT_VERSION}/netboot.xyz.iso"
NETBOOT_SHA256 = "2206b05c1c7a8ec7a7a4356dd5f811e19c8081d7d5e2bc856f1f3f4059c5fed1"
NETBOOT_NAME = "netboot.xyz.iso"
NETBOOT_MAX_BYTES = 8 * 1024 * 1024
# GitHub serves release files from its asset CDN after a redirect.
DOWNLOAD_HOSTS = ("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com")
DOWNLOAD_DEADLINE_S = 120

_DOWNLOAD_LOCK = threading.Lock()


class Busy(RuntimeError):
    """Another download is already running."""


def _check_url(url):
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme != "https" or u.hostname not in DOWNLOAD_HOSTS:
        raise RuntimeError(f"refusing download from {url!r}: not https on an allowed host")


def _http_stream(url, timeout=30):
    """Open url; every redirect hop must stay https on an allowed host."""
    import urllib.request

    class _Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _check_url(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    _check_url(url)
    opener = urllib.request.build_opener(_Redirect)
    r = opener.open(urllib.request.Request(url, headers={"User-Agent": "pi-zero-w-hid-kvm"}), timeout=timeout)
    _check_url(r.geturl())
    return r


def expected_sha256(sums_text, name):
    """Hash for name from a sha256sum-style list ("<hex> *name" or "<hex>  name")."""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def _mount_ok(path, mounts="/proc/mounts"):
    """True if path sits on a real writable mount (kvmdata), not the RAM overlay."""
    if os.environ.get("KVM_DATA_REQUIRE_MOUNT", "1") == "0":
        return True
    path = os.path.realpath(path)
    best, info = "", (None, set())
    try:
        with open(mounts) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (path == mnt or path.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                    best, info = mnt, (parts[2], set(parts[3].split(",")))
    except OSError:
        return False
    return info[0] == "ext4" and "rw" in info[1]


def fetch_verified(url, sha256, name, images_dir, max_bytes, opener=_http_stream,
                   deadline_s=DOWNLOAD_DEADLINE_S, clock=None):
    """Stream url into images_dir/name, keeping it only if its SHA256 matches.

    The file goes to a private temp file in images_dir and is renamed into place
    after the hash checks out; any failure removes the temp file.
    """
    import hashlib
    import time
    clock = clock or time.monotonic
    if not _DOWNLOAD_LOCK.acquire(blocking=False):
        raise Busy("another download is already running")
    try:
        os.makedirs(images_dir, exist_ok=True)
        if not _mount_ok(images_dir):
            raise RuntimeError(f"{images_dir} is not on the kvmdata partition; "
                               "a download there would live in RAM and vanish on reboot")
        start = clock()
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(prefix="." + name + ".", suffix=".part", dir=images_dir)
        try:
            with os.fdopen(fd, "wb") as f, opener(url) as r:
                while True:
                    if clock() - start > deadline_s:
                        raise RuntimeError(f"download took longer than {deadline_s} s")
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise RuntimeError(f"{name} is larger than {max_bytes} bytes")
                    h.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            got = h.hexdigest()
            if got != sha256.lower():
                raise RuntimeError(f"{name} SHA256 mismatch: got {got}, expected {sha256.lower()}")
            dest = os.path.join(images_dir, name)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return {"ok": True, "name": name, "path": dest, "size": size, "sha256": got}
    finally:
        _DOWNLOAD_LOCK.release()


def fetch_netboot(images_dir, **kw):
    """Download the pinned netboot.xyz release, checked against the hash stored in this file."""
    return fetch_verified(NETBOOT_URL, NETBOOT_SHA256, NETBOOT_NAME, images_dir, NETBOOT_MAX_BYTES, **kw)


def find_images_dir():
    env = os.environ.get("KVM_IMAGES_DIR")
    if env:
        return env
    return IMAGES_DIR_DEFAULT


def find_boot_image_path():
    env = os.environ.get("KVM_BOOT_IMAGE")
    if env:
        return env
    if os.path.isdir(os.path.dirname(BOOT_IMAGE_DEFAULT)):
        return BOOT_IMAGE_DEFAULT
    return ""


def _locked(fn):
    """Serialize LUN changes so two requests can't leave the drive half-configured."""
    @functools.wraps(fn)
    def wrap(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrap


class MediaManager:
    def __init__(self, lun_path=None, images_dir=None, setup_img=None, selection_path=None):
        self.lun_path = lun_path or os.environ.get("KVM_LUN_PATH", LUN_DEFAULT)
        self.setup_img = setup_img or os.environ.get("KVM_SETUP_IMG", SETUP_IMG_DEFAULT)
        self.images_dir = images_dir or find_images_dir()
        if selection_path is None:
            selection_path = find_boot_image_path()
        self.selection_path = selection_path or None
        self._lock = threading.RLock()
        try:
            os.makedirs(self.images_dir, exist_ok=True)
        except OSError:
            pass

    def is_available(self):
        """Returns True if the ConfigFS mass storage LUN is active and configurable."""
        return os.path.exists(os.path.join(self.lun_path, "file"))

    def _read_attr(self, attr):
        try:
            with open(os.path.join(self.lun_path, attr)) as f:
                return f.read().strip()
        except OSError:
            return ""

    def _saved_boot_path(self):
        if not self.selection_path:
            return ""
        try:
            with open(self.selection_path) as f:
                return f.readline().strip()
        except OSError:
            return ""

    def status(self):
        """Current mount state and available image catalog."""
        images = self.list_images()
        if not self.is_available():
            return {
                "available": False,
                "mounted": None,
                "cdrom": False,
                "ro": True,
                "boot": False,
                "images": images,
            }

        cur_file = self._read_attr("file")
        cdrom = self._read_attr("cdrom") == "1"
        ro_raw = self._read_attr("ro")
        ro = True if ro_raw == "" else ro_raw == "1"

        mounted_info = None
        if cur_file:
            is_setup = (os.path.exists(self.setup_img) and
                        os.path.abspath(cur_file) == os.path.abspath(self.setup_img))
            size = os.path.getsize(cur_file) if os.path.exists(cur_file) else 0
            mounted_info = {
                "path": cur_file,
                "name": os.path.basename(cur_file),
                "is_setup": is_setup,
                "size": size,
                "size_mb": round(size / (1024 * 1024), 1),
            }

        saved = self._saved_boot_path()
        return {
            "available": True,
            "mounted": mounted_info,
            "cdrom": cdrom,
            "ro": ro,
            "boot": bool(cur_file) and os.path.abspath(saved) == os.path.abspath(cur_file) if saved and cur_file else False,
            "images": self.list_images(cur_file),
        }

    def list_images(self, current_file=""):
        images = []
        cur_abs = os.path.abspath(current_file) if current_file else ""

        if os.path.exists(self.setup_img):
            sz = os.path.getsize(self.setup_img)
            images.append({
                "name": os.path.basename(self.setup_img),
                "path": self.setup_img,
                "size": sz,
                "size_mb": round(sz / (1024 * 1024), 1),
                "type": "setup",
                "mounted": (cur_abs == os.path.abspath(self.setup_img)),
            })

        if os.path.isdir(self.images_dir):
            for entry in sorted(os.listdir(self.images_dir)):
                p = os.path.join(self.images_dir, entry)
                if not os.path.isfile(p):
                    continue
                ext = os.path.splitext(entry)[1].lower()
                if ext in (".iso", ".img", ".bin", ".raw"):
                    sz = os.path.getsize(p)
                    images.append({
                        "name": entry,
                        "path": p,
                        "size": sz,
                        "size_mb": round(sz / (1024 * 1024), 1),
                        "type": "iso" if ext == ".iso" else "disk",
                        "mounted": (cur_abs == os.path.abspath(p)),
                    })
        return images

    def _write_lun(self, attr, value):
        p = os.path.join(self.lun_path, attr)
        data = (value + "\n").encode()
        try:
            with open(p, "wb") as f:
                f.write(data)
            return
        except OSError as e:
            err = e
        if os.environ.get("KVM_LUN_SUDO", "1") == "0" or not isinstance(err, PermissionError):
            raise RuntimeError(f"failed to write {attr}: {err}")
        res = subprocess.run(
            ["sudo", "-n", "tee", p],
            input=data, capture_output=True,
        )
        if res.returncode != 0:
            detail = res.stderr.decode(errors="replace").strip() or str(err)
            raise RuntimeError(f"failed to write {attr}: {detail}")

    def _snapshot(self):
        return {
            "file": self._read_attr("file"),
            "cdrom": self._read_attr("cdrom") or "0",
            "ro": self._read_attr("ro") or "1",
        }

    def _restore(self, prev):
        try:
            self.eject()
        except Exception:
            pass
        for attr, fallback in (("cdrom", "0"), ("ro", "1")):
            try:
                self._write_lun(attr, prev.get(attr) or fallback)
            except Exception:
                pass
        if prev.get("file"):
            try:
                self._write_lun("file", prev["file"])
            except Exception:
                pass

    def eject(self):
        """Eject any current media from the LUN."""
        if not self.is_available():
            raise RuntimeError("mass_storage gadget is not active")
        forced = os.path.join(self.lun_path, "forced_eject")
        if os.path.exists(forced):
            self._write_lun("forced_eject", "1")
        self._write_lun("file", "")
        return True

    def _clear_boot(self):
        if not self.selection_path:
            return
        try:
            os.remove(self.selection_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _save_boot(self, path):
        if not self.selection_path:
            return False
        parent = os.path.dirname(self.selection_path) or "."
        fd = None
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=parent, prefix=".boot-image.")
            os.write(fd, path.encode() + b"\n")
            os.close(fd)
            fd = None
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.selection_path)
            tmp = None
            return True
        except OSError:
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _resolve(self, name_or_path):
        """Return (path, is_setup) or raise. Names stay inside the image dir."""
        if name_or_path in ("setup", "kvm-setup.img", os.path.basename(self.setup_img)):
            if not os.path.isfile(self.setup_img):
                raise FileNotFoundError(f"setup image not found at {self.setup_img}")
            return os.path.realpath(self.setup_img), True
        if os.path.isabs(name_or_path) or "/" in name_or_path or "\\" in name_or_path:
            raise ValueError("image name must be a file in the images directory")
        base = os.path.basename(name_or_path)
        candidate = os.path.join(self.images_dir, base)
        if not os.path.isfile(candidate):
            raise FileNotFoundError(f"image not found: {name_or_path}")
        root = os.path.realpath(self.images_dir)
        real = os.path.realpath(candidate)
        if os.path.commonpath([root, real]) != root:
            raise ValueError("image name must be a file in the images directory")
        return real, False

    @_locked
    def mount(self, name_or_path, cdrom=None, ro=True):
        """Mount an image file onto the LUN."""
        if not self.is_available():
            raise RuntimeError("mass_storage gadget is not active")

        target, is_setup = self._resolve(name_or_path)
        if not ro and os.environ.get("KVM_ALLOW_RW", "0") != "1":
            raise ValueError("writable mounts are off; set KVM_ALLOW_RW=1 to let the host write to images")
        if is_setup:
            cdrom = False
            ro = True
        if cdrom is None:
            cdrom = target.lower().endswith(".iso")
        if cdrom:
            ro = True
            size = os.path.getsize(target)
            if size < CDROM_MIN_BYTES:
                raise ValueError(
                    f"{os.path.basename(target)} is too small to boot as a CD"
                )
            if size >= CDROM_MAX_BYTES:
                raise ValueError(
                    f"{os.path.basename(target)} is over 2.2 GB, so the kernel would "
                    "cut the end off. Mount it as a disk, or use a smaller image"
                )

        prev = self._snapshot()
        ejected = False
        try:
            self.eject()
            ejected = True
            self._write_lun("cdrom", "1" if cdrom else "0")
            self._write_lun("ro", "1" if ro else "0")
            inquiry = "DIY      KVM-SETUP" if is_setup else ("KVM      BOOT-CD" if cdrom else "KVM      BOOT-DISK")
            try:
                self._write_lun("inquiry_string", inquiry)
            except RuntimeError:
                pass
            self._write_lun("file", target)
        except Exception:
            if ejected:
                self._restore(prev)
            raise

        if is_setup:
            self._clear_boot()
            saved = False
        else:
            saved = self._save_boot(target)
        sz = os.path.getsize(target) if os.path.exists(target) else 0
        return {
            "ok": True,
            "mounted": {
                "name": os.path.basename(target),
                "path": target,
                "is_setup": is_setup,
                "size": sz,
                "size_mb": round(sz / (1024 * 1024), 1),
            },
            "cdrom": cdrom,
            "ro": ro,
            "boot": saved,
        }

    @_locked
    def delete(self, name):
        """Delete an image file from the images directory."""
        if name in ("setup", "kvm-setup.img", os.path.basename(self.setup_img)):
            raise ValueError("cannot delete the built-in setup image")
        if os.path.isabs(name) or "/" in name or "\\" in name:
            raise ValueError("image name must be a file in the images directory")
        safe_name = os.path.basename(name)
        p = os.path.join(self.images_dir, safe_name)
        if not os.path.isfile(p):
            raise FileNotFoundError(f"image not found: {name}")

        cur = self.status().get("mounted")
        if cur and os.path.abspath(cur.get("path", "")) == os.path.abspath(p):
            self.eject()
            self._clear_boot()

        try:
            os.remove(p)
        except OSError:
            if os.environ.get("KVM_LUN_SUDO", "1") == "0":
                raise
            subprocess.run(["sudo", "-n", "rm", "-f", "--", p], check=True)
        return True

    @_locked
    def eject_and_forget(self):
        """Drop the boot disc and put the setup stick back in the slot."""
        self.eject()
        self._clear_boot()
        if os.path.isfile(self.setup_img) and self.is_available():
            try:
                self._write_lun("cdrom", "0")
                self._write_lun("ro", "1")
                try:
                    self._write_lun("inquiry_string", "DIY      KVM-SETUP")
                except RuntimeError:
                    pass
                self._write_lun("file", os.path.realpath(self.setup_img))
            except Exception:
                return True
        return True
