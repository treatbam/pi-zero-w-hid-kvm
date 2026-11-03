import os
import shutil
import tempfile
import unittest

import sys

# Works from the repo root (zero.web.media) and from zero/web (media).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hashlib  # noqa: E402
import io  # noqa: E402
import media  # noqa: E402
from media import MediaManager, expected_sha256, fetch_verified, Busy  # noqa: E402

class TestMediaManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lun = os.path.join(self.tmp, "lun.0")
        os.makedirs(self.lun, exist_ok=True)
        for attr in ("file", "cdrom", "ro", "removable"):
            with open(os.path.join(self.lun, attr), "w") as f:
                f.write("\n")

        self.img_dir = os.path.join(self.tmp, "images")
        os.makedirs(self.img_dir, exist_ok=True)

        self.setup_img = os.path.join(self.tmp, "kvm-setup.img")
        with open(self.setup_img, "wb") as f:
            f.write(b"SETUP_IMAGE_DATA_1234567890")

        self.boot = os.path.join(self.tmp, "boot-image")
        self.mgr = MediaManager(
            lun_path=self.lun, images_dir=self.img_dir, setup_img=self.setup_img,
            selection_path=self.boot)
        os.environ["KVM_LUN_SUDO"] = "0"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_initial_status(self):
        st = self.mgr.status()
        self.assertTrue(st["available"])
        self.assertIsNone(st["mounted"])
        self.assertFalse(st["cdrom"])
        self.assertEqual(len(st["images"]), 1)
        self.assertEqual(st["images"][0]["name"], "kvm-setup.img")

    def test_mount_setup_image(self):
        res = self.mgr.mount("setup")
        self.assertTrue(res["ok"])
        self.assertEqual(res["mounted"]["name"], "kvm-setup.img")
        self.assertFalse(res["cdrom"])
        self.assertTrue(res["ro"])

        with open(os.path.join(self.lun, "file")) as f:
            self.assertEqual(f.read().strip(), self.setup_img)
        with open(os.path.join(self.lun, "cdrom")) as f:
            self.assertEqual(f.read().strip(), "0")
        with open(os.path.join(self.lun, "ro")) as f:
            self.assertEqual(f.read().strip(), "1")

        st = self.mgr.status()
        self.assertIsNotNone(st["mounted"])
        self.assertTrue(st["mounted"]["is_setup"])

    def _iso(self, name, size=614400):
        path = os.path.join(self.img_dir, name)
        with open(path, "wb") as f:
            f.truncate(size)
        return path

    def test_mount_iso_auto_detects_cdrom(self):
        iso_path = self._iso("ubuntu.iso")

        res = self.mgr.mount("ubuntu.iso")
        self.assertTrue(res["ok"])
        self.assertTrue(res["cdrom"])
        self.assertTrue(res["ro"])

        with open(os.path.join(self.lun, "file")) as f:
            self.assertEqual(f.read().strip(), iso_path)
        with open(os.path.join(self.lun, "cdrom")) as f:
            self.assertEqual(f.read().strip(), "1")

    def test_eject(self):
        self.mgr.mount("setup")
        self.assertIsNotNone(self.mgr.status()["mounted"])

        self.mgr.eject()
        with open(os.path.join(self.lun, "file")) as f:
            self.assertEqual(f.read().strip(), "")
        self.assertIsNone(self.mgr.status()["mounted"])

    def test_delete_image(self):
        test_img = os.path.join(self.img_dir, "test.img")
        with open(test_img, "wb") as f:
            f.write(b"RAW_DATA")

        self.mgr.mount("test.img")
        self.assertEqual(self.mgr.status()["mounted"]["name"], "test.img")

        self.mgr.delete("test.img")
        self.assertFalse(os.path.exists(test_img))
        self.assertIsNone(self.mgr.status()["mounted"])

    def test_cannot_delete_setup_image(self):
        with self.assertRaises(ValueError):
            self.mgr.delete("kvm-setup.img")

    def test_boot_choice_survives_until_eject(self):
        iso = self._iso("debian-netinst.iso")
        res = self.mgr.mount("debian-netinst.iso")
        self.assertTrue(res["boot"])
        with open(self.boot) as f:
            self.assertEqual(f.read().strip(), iso)
        self.assertTrue(self.mgr.status()["boot"])
        self.mgr.eject_and_forget()
        self.assertFalse(os.path.exists(self.boot))
        self.assertEqual(self.mgr.status()["mounted"]["name"], "kvm-setup.img")
        self.assertFalse(self.mgr.status()["cdrom"])

    def test_rejects_paths_outside_the_image_dir(self):
        outside = os.path.join(self.tmp, "secret.iso")
        with open(outside, "wb") as f:
            f.truncate(614400)
        for name in (outside, "../secret.iso", "nested/debian.iso"):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.mgr.mount(name)
        self.assertIsNone(self.mgr.status()["mounted"])

    def test_rejects_iso_the_kernel_would_truncate_or_refuse(self):
        self._iso("tiny.iso", 1024)
        self._iso("huge.iso", 2359296000)
        with self.assertRaises(ValueError):
            self.mgr.mount("tiny.iso")
        with self.assertRaises(ValueError):
            self.mgr.mount("huge.iso")
        self.assertIsNone(self.mgr.status()["mounted"])

    def test_puts_the_previous_disc_back_when_the_switch_fails(self):
        self.mgr.mount("setup")
        self._iso("debian-netinst.iso")
        os.chmod(os.path.join(self.lun, "cdrom"), 0)
        try:
            with self.assertRaises(RuntimeError):
                self.mgr.mount("debian-netinst.iso")
        finally:
            os.chmod(os.path.join(self.lun, "cdrom"), 0o644)
        with open(os.path.join(self.lun, "file")) as f:
            self.assertEqual(f.read().strip(), self.setup_img)
        # The setup stick is the default, so mounting it leaves no saved boot image.
        self.assertEqual(self.mgr._saved_boot_path(), "")

    def test_forced_eject_clears_a_locked_disc(self):
        forced = os.path.join(self.lun, "forced_eject")
        with open(forced, "w") as f:
            f.write("\n")
        self.mgr.mount("setup")
        self.mgr.eject()
        with open(forced) as f:
            self.assertEqual(f.read().strip(), "1")
        with open(os.path.join(self.lun, "file")) as f:
            self.assertEqual(f.read().strip(), "")

    def test_writable_mount_refused_by_default(self):
        p = os.path.join(self.img_dir, "disk.img")
        with open(p, "wb") as f:
            f.write(b"\0" * 4096)
        os.environ.pop("KVM_ALLOW_RW", None)
        with self.assertRaises(ValueError):
            self.mgr.mount("disk.img", ro=False)

    def test_mounting_setup_clears_saved_boot(self):
        with open(self.boot, "w") as f:
            f.write("/somewhere/old.iso\n")
        self.mgr.mount("kvm-setup.img")
        self.assertEqual(self.mgr._saved_boot_path(), "")


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["KVM_DATA_REQUIRE_MOUNT"] = "0"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _opener(self, data):
        return lambda url: io.BytesIO(data)

    def _fetch(self, data, sha, **kw):
        return fetch_verified("https://github.com/x", sha, "nb.iso", self.tmp, 1 << 20,
                              opener=self._opener(data), **kw)

    def test_expected_sha256_parses_star_and_space_forms(self):
        h = "a" * 64
        self.assertEqual(expected_sha256(f"{h} *netboot.xyz.iso\n", "netboot.xyz.iso"), h)
        self.assertEqual(expected_sha256(f"{h}  netboot.xyz.iso\n", "netboot.xyz.iso"), h)
        self.assertIsNone(expected_sha256(f"{h} *other.iso\n", "netboot.xyz.iso"))

    def test_keeps_file_when_hash_matches(self):
        data = b"ISO" * 50000
        res = self._fetch(data, hashlib.sha256(data).hexdigest())
        self.assertEqual(res["size"], len(data))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["nb.iso"])

    def test_bad_hash_leaves_nothing(self):
        with self.assertRaises(RuntimeError):
            self._fetch(b"tampered", "0" * 64)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_oversize_is_refused_and_cleaned(self):
        data = b"x" * ((1 << 20) + 1)
        with self.assertRaises(RuntimeError):
            self._fetch(data, hashlib.sha256(data).hexdigest())
        self.assertEqual(os.listdir(self.tmp), [])

    def test_overall_deadline(self):
        t = iter(range(0, 10000, 100))
        with self.assertRaises(RuntimeError):
            self._fetch(b"y" * 300000, "0" * 64, deadline_s=150, clock=lambda: next(t))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_disk_full_cleans_temp(self):
        class Full(io.BytesIO):
            pass
        real = os.fdopen
        def fake_fdopen(fd, mode):
            f = real(fd, mode)
            def boom(_):
                raise OSError(28, "No space left on device")
            f.write = boom
            return f
        media.os.fdopen = fake_fdopen
        try:
            with self.assertRaises(OSError):
                self._fetch(b"z" * 10, "0" * 64)
        finally:
            media.os.fdopen = real
        self.assertEqual(os.listdir(self.tmp), [])

    def test_second_download_is_busy(self):
        media._DOWNLOAD_LOCK.acquire()
        try:
            with self.assertRaises(Busy):
                self._fetch(b"a", "0" * 64)
        finally:
            media._DOWNLOAD_LOCK.release()

    def test_rejects_http_and_foreign_hosts(self):
        for url in ("http://github.com/x", "https://evil.example/x"):
            with self.assertRaises(RuntimeError):
                media._check_url(url)
        media._check_url("https://release-assets.githubusercontent.com/a")

    def test_mount_check_wants_writable_ext4(self):
        os.environ["KVM_DATA_REQUIRE_MOUNT"] = "1"
        try:
            m = os.path.join(self.tmp, "mounts")
            with open(m, "w") as f:
                f.write("overlayroot / overlay rw 0 0\n")
            self.assertFalse(media._mount_ok("/var/lib/kvm/images", m))
            with open(m, "a") as f:
                f.write("/dev/mmcblk0p3 /var/lib/kvm ext4 rw,noatime 0 0\n")
            self.assertTrue(media._mount_ok("/var/lib/kvm/images", m))
        finally:
            os.environ["KVM_DATA_REQUIRE_MOUNT"] = "0"
