import os
import shutil
import tempfile
import unittest

from zero.web.media import MediaManager, expected_sha256, fetch_netboot, NETBOOT_URL, NETBOOT_SUMS_URL

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
        with open(self.boot) as f:
            self.assertEqual(f.read().strip(), self.setup_img)

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



class TestNetboot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _getter(self, iso, sums):
        def get(url, limit):
            return {NETBOOT_URL: iso, NETBOOT_SUMS_URL: sums}[url]
        return get

    def test_expected_sha256_parses_star_and_space_forms(self):
        h = "a" * 64
        self.assertEqual(expected_sha256(f"{h} *netboot.xyz.iso\n", "netboot.xyz.iso"), h)
        self.assertEqual(expected_sha256(f"{h}  netboot.xyz.iso\n", "netboot.xyz.iso"), h)
        self.assertIsNone(expected_sha256(f"{h} *other.iso\n", "netboot.xyz.iso"))

    def test_fetch_writes_file_when_hash_matches(self):
        import hashlib
        iso = b"ISO" * 1000
        sums = f"{hashlib.sha256(iso).hexdigest()} *netboot.xyz.iso\n".encode()
        res = fetch_netboot(self.tmp, get=self._getter(iso, sums))
        self.assertTrue(res["ok"])
        with open(os.path.join(self.tmp, "netboot.xyz.iso"), "rb") as f:
            self.assertEqual(f.read(), iso)

    def test_fetch_refuses_bad_hash_and_leaves_nothing(self):
        sums = ("0" * 64 + " *netboot.xyz.iso\n").encode()
        with self.assertRaises(RuntimeError):
            fetch_netboot(self.tmp, get=self._getter(b"tampered", sums))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_fetch_refuses_missing_checksum(self):
        with self.assertRaises(RuntimeError):
            fetch_netboot(self.tmp, get=self._getter(b"x", b"nothing here\n"))


if __name__ == "__main__":
    unittest.main()
