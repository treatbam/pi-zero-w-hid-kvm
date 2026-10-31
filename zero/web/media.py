"""Virtual Media / Mass Storage Manager for the Pi Zero KVM.

Controls dynamic mounting/ejection of ISO/IMG files via ConfigFS
(/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0/lun.0).
Allows switching between the default KVM-SETUP pairing drive, bootable ISOs
(CD-ROM mode), and raw disk images (Flash Drive mode) on the fly without
re-plugging the USB cable.
"""
import os
import shutil
import subprocess

LUN_DEFAULT = "/sys/kernel/config/usb_gadget/kvm/functions/mass_storage.usb0/lun.0"
SETUP_IMG_DEFAULT = "/opt/pi-zero-w-hid-kvm/zero/gadget/kvm-setup.img"

def find_images_dir():
    env = os.environ.get("KVM_IMAGES_DIR")
    if env:
        return env
    candidates = [
        "/home/pi/images",
        "/var/lib/kvm/images",
        "/opt/pi-zero-w-hid-kvm/images",
        os.path.join(os.path.dirname(__file__), "..", "..", "images"),
    ]
    for c in candidates:
        if os.path.isdir(c):
            return os.path.abspath(c)
    return candidates[0]

class MediaManager:
    def __init__(self, lun_path=None, images_dir=None, setup_img=None):
        self.lun_path = lun_path or os.environ.get("KVM_LUN_PATH", LUN_DEFAULT)
        self.setup_img = setup_img or os.environ.get("KVM_SETUP_IMG", SETUP_IMG_DEFAULT)
        self.images_dir = images_dir or find_images_dir()
        try:
            os.makedirs(self.images_dir, exist_ok=True)
        except OSError:
            pass

    def is_available(self):
        """Returns True if the ConfigFS mass storage LUN is active and configurable."""
        return os.path.exists(os.path.join(self.lun_path, "file"))

    def status(self):
        """Current mount state and available image catalog."""
        if not self.is_available():
            return {
                "available": False,
                "mounted": None,
                "cdrom": False,
                "ro": True,
                "images": self.list_images()
            }

        cur_file = ""
        try:
            with open(os.path.join(self.lun_path, "file")) as f:
                cur_file = f.read().strip()
        except OSError:
            pass

        cdrom = False
        try:
            with open(os.path.join(self.lun_path, "cdrom")) as f:
                cdrom = f.read().strip() == "1"
        except OSError:
            pass

        ro = True
        try:
            with open(os.path.join(self.lun_path, "ro")) as f:
                ro = f.read().strip() == "1"
        except OSError:
            pass

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
                "size_mb": round(size / (1024 * 1024), 1)
            }

        return {
            "available": True,
            "mounted": mounted_info,
            "cdrom": cdrom,
            "ro": ro,
            "images": self.list_images(cur_file)
        }

    def list_images(self, current_file=""):
        images = []
        cur_abs = os.path.abspath(current_file) if current_file else ""

        # Include setup image
        if os.path.exists(self.setup_img):
            sz = os.path.getsize(self.setup_img)
            images.append({
                "name": os.path.basename(self.setup_img),
                "path": self.setup_img,
                "size": sz,
                "size_mb": round(sz / (1024 * 1024), 1),
                "type": "setup",
                "mounted": (cur_abs == os.path.abspath(self.setup_img))
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
                        "mounted": (cur_abs == os.path.abspath(p))
                    })
        return images

    def _write_lun(self, attr, value):
        p = os.path.join(self.lun_path, attr)
        try:
            with open(p, "w") as f:
                f.write(value + "\n")
        except OSError as e:
            cmd = ["sudo", "-n", "sh", "-c", f'echo "{value}" > "{p}"']
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"failed to write {attr}='{value}': {res.stderr.strip() or e}")

    def eject(self):
        """Eject any current media from the LUN."""
        if not self.is_available():
            raise RuntimeError("mass_storage gadget is not active")
        self._write_lun("file", "")
        return True

    def mount(self, name_or_path, cdrom=None, ro=True):
        """Mount an image file onto the LUN."""
        if not self.is_available():
            raise RuntimeError("mass_storage gadget is not active")

        target = None
        if name_or_path in ("setup", "kvm-setup.img", os.path.basename(self.setup_img)):
            if not os.path.exists(self.setup_img):
                raise FileNotFoundError(f"setup image not found at {self.setup_img}")
            target = self.setup_img
            cdrom = False
            ro = True
        elif os.path.isabs(name_or_path) and os.path.isfile(name_or_path):
            target = name_or_path
        elif os.path.isfile(os.path.join(self.images_dir, name_or_path)):
            target = os.path.join(self.images_dir, name_or_path)
        else:
            raise FileNotFoundError(f"image not found: {name_or_path}")

        if cdrom is None:
            cdrom = target.lower().endswith(".iso")

        if cdrom:
            ro = True

        # Always eject first to signal a media change to the host
        self.eject()

        self._write_lun("cdrom", "1" if cdrom else "0")
        self._write_lun("ro", "1" if ro else "0")
        self._write_lun("file", target)

        sz = os.path.getsize(target) if os.path.exists(target) else 0
        return {
            "ok": True,
            "mounted": {
                "name": os.path.basename(target),
                "path": target,
                "is_setup": (target == self.setup_img),
                "size": sz,
                "size_mb": round(sz / (1024 * 1024), 1)
            },
            "cdrom": cdrom,
            "ro": ro
        }

    def delete(self, name):
        """Delete an image file from the images directory."""
        if name in ("setup", "kvm-setup.img", os.path.basename(self.setup_img)):
            raise ValueError("cannot delete the built-in setup image")
        safe_name = os.path.basename(name)
        p = os.path.join(self.images_dir, safe_name)
        if not os.path.isfile(p):
            raise FileNotFoundError(f"image not found: {name}")

        cur = self.status().get("mounted")
        if cur and os.path.abspath(cur.get("path", "")) == os.path.abspath(p):
            self.eject()

        try:
            os.remove(p)
        except OSError:
            cmd = ["sudo", "-n", "rm", "-f", p]
            subprocess.run(cmd, check=True)
        return True
