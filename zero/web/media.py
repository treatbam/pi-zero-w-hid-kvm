"""Virtual Media / Mass Storage Manager for the Pi Zero KVM.

Switches the one mass-storage slot the gadget already has
(/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0/lun.0)
between the KVM-SETUP stick and a boot image. An ISO under the kernel's
CD limit is presented as a CD so a PC can boot it. The chosen path is
saved on the writable data partition and put back in the slot at boot
by hid-gadget.sh.

Images belong on the SD card (default /opt/pi-zero-w-hid-kvm/images).
/home/pi/images sits on the read-only-root overlay and disappears on reboot,
and it is too small for an installer.
"""
import os
import subprocess
import tempfile

LUN_DEFAULT = "/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0/lun.0"
SETUP_IMG_DEFAULT = "/opt/pi-zero-w-hid-kvm/zero/gadget/kvm-setup.img"
IMAGES_DIR_DEFAULT = "/opt/pi-zero-w-hid-kvm/images"
BOOT_IMAGE_DEFAULT = "/var/lib/kvm/boot-image"

# f_mass_storage CD emulation: at least 300 sectors of 2048 bytes, and the
# kernel keeps only the first (256*60*75 - 1) sectors of anything larger.
CDROM_MIN_BYTES = 300 * 2048
CDROM_MAX_BYTES = 256 * 60 * 75 * 2048


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


class MediaManager:
    def __init__(self, lun_path=None, images_dir=None, setup_img=None, selection_path=None):
        self.lun_path = lun_path or os.environ.get("KVM_LUN_PATH", LUN_DEFAULT)
        self.setup_img = setup_img or os.environ.get("KVM_SETUP_IMG", SETUP_IMG_DEFAULT)
        self.images_dir = images_dir or find_images_dir()
        if selection_path is None:
            selection_path = find_boot_image_path()
        self.selection_path = selection_path or None
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

    def mount(self, name_or_path, cdrom=None, ro=True):
        """Mount an image file onto the LUN."""
        if not self.is_available():
            raise RuntimeError("mass_storage gadget is not active")

        target, is_setup = self._resolve(name_or_path)
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
