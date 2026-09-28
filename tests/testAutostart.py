import errno
import os
import stat
import tempfile
import unittest
from unittest import mock

import autostart


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.configHome = self.temp.name
        self.path = autostart.overridePath(self.configHome)

    def tearDown(self):
        self.temp.cleanup()

    def read(self):
        with open(self.path) as f:
            return f.read()

    def testEnabledWithoutAnOverride(self):
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOffWritesTheOverrideAndMakesTheFolder(self):
        autostart.setEnabled(self.configHome, False)
        self.assertEqual(self.read(), "[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\n")
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testTurningItBackOnDeletesTheAppsOwnOverride(self):
        autostart.setEnabled(self.configHome, False)
        autostart.setEnabled(self.configHome, True)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOnKeepsKeysAnotherToolWrote(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\nX-GNOME-Autostart-enabled=false\nExec=logitech-mouse-battery\n")
        autostart.setEnabled(self.configHome, True)
        self.assertIn("Hidden=false", self.read())
        self.assertIn("X-GNOME-Autostart-enabled=false", self.read())
        self.assertNotIn("Hidden=true", self.read())
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOffKeepsKeysAnotherToolWrote(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nX-Custom=1\n")
        autostart.setEnabled(self.configHome, False)
        self.assertIn("X-Custom=1", self.read())
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testTurningItOnDeletesAnOverrideWithoutExec(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\nX-GNOME-Autostart-enabled=false\n")
        autostart.setEnabled(self.configHome, True)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOnDeletesAnEmptyOverride(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("")
        autostart.setEnabled(self.configHome, True)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testFolderAtOverridePathReadsAsEnabled(self):
        os.makedirs(self.path)
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testInvalidUtf8SurvivesTurningItOff(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "wb") as f:
            f.write(b"[Desktop Entry]\nType=Application\nName=Mouse Battery\nX-Custom=\xff\xfe\n")
        autostart.setEnabled(self.configHome, False)
        with open(self.path, "rb") as f:
            content = f.read()
        self.assertIn(b"\xff\xfe", content)
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testSetEnabledRaisesWhenAutostartFolderIsAFile(self):
        with open(os.path.dirname(self.path), "w") as f:
            f.write("not a folder")
        with self.assertRaises(OSError):
            autostart.setEnabled(self.configHome, False)

    def testTurningItOnWithNoOverrideCreatesNothing(self):
        autostart.setEnabled(self.configHome, True)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))

    def testAFailedWriteLeavesTheOldOverrideWhole(self):
        os.makedirs(os.path.dirname(self.path))
        original = "[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n"
        with open(self.path, "w") as f:
            f.write(original)
        with mock.patch.object(autostart.os, "replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                autostart.setEnabled(self.configHome, False)
        self.assertEqual(self.read(), original)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [autostart.FILE_NAME])

    def testAWriteLeavesNoTemporaryFile(self):
        autostart.setEnabled(self.configHome, False)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [autostart.FILE_NAME])

    def testAFailedCreateLeavesNoOverrideOrTemporaryFile(self):
        with mock.patch.object(autostart.os, "replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                autostart.setEnabled(self.configHome, False)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [])

    def testASymlinkedOverrideStaysALinkAfterWritingThroughIt(self):
        os.makedirs(os.path.dirname(self.path))
        with tempfile.TemporaryDirectory() as other:
            target = os.path.join(other, "real.desktop")
            with open(target, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n")
            os.symlink(target, self.path)
            autostart.setEnabled(self.configHome, False)
            self.assertTrue(os.path.islink(self.path))
            self.assertEqual(os.path.realpath(self.path), target)
            with open(target) as f:
                self.assertIn("Hidden=true", f.read())

    def testA0400OverrideRaisesPermissionErrorAndIsLeftUnchanged(self):
        os.makedirs(os.path.dirname(self.path))
        original = "[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n"
        with open(self.path, "w") as f:
            f.write(original)
        os.chmod(self.path, 0o400)
        with self.assertRaises(PermissionError):
            autostart.setEnabled(self.configHome, False)
        self.assertEqual(self.read(), original)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [autostart.FILE_NAME])

    def testA0600OverrideKeepsItsMode(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n")
        os.chmod(self.path, 0o600)
        autostart.setEnabled(self.configHome, False)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


if (__name__ == "__main__"):
    unittest.main()
