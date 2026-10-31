import os
import shutil
import tempfile
import unittest

from zero.web.media import MediaManager

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

        self.mgr = MediaManager(lun_path=self.lun, images_dir=self.img_dir, setup_img=self.setup_img)

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

    def test_mount_iso_auto_detects_cdrom(self):
        iso_path = os.path.join(self.img_dir, "ubuntu.iso")
        with open(iso_path, "wb") as f:
            f.write(b"MOCK_ISO_IMAGE")

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

if __name__ == "__main__":
    unittest.main()
