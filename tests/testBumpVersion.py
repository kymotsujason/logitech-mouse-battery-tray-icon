import datetime
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import version

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METAINFO = "packaging/io.github.kymotsujason.LogitechMouseBattery.metainfo.xml"
FILES = ["src/version.py", "README.md", "packaging/bumpVersion.py", "packaging/aur/PKGBUILD", "packaging/aur/.SRCINFO", METAINFO]


class BumpVersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name
        for name in FILES:
            os.makedirs(os.path.dirname(os.path.join(self.root, name)), exist_ok=True)
            shutil.copy(os.path.join(REPO, name), os.path.join(self.root, name))

    def read(self, name):
        with open(os.path.join(self.root, name)) as f:
            return f.read()

    def write(self, name, text):
        with open(os.path.join(self.root, name), "w") as f:
            f.write(text)

    def bump(self, *args):
        # the copy of the script finds the files next to it, thus it only ever edits the temporary copies
        return subprocess.run([sys.executable, "-B", os.path.join(self.root, "packaging", "bumpVersion.py"), *args], capture_output=True, text=True).returncode

    def testANewVersionReachesEveryFileThatCarriesIt(self):
        self.assertEqual(self.bump("9.8.7"), 0)
        self.assertIn('VERSION = "9.8.7"', self.read("src/version.py"))
        self.assertIn("\npkgver=9.8.7\n", self.read("packaging/aur/PKGBUILD"))
        srcinfo = self.read("packaging/aur/.SRCINFO")
        self.assertIn("\tpkgver = 9.8.7\n", srcinfo)
        self.assertIn("/releases/download/v9.8.7/logitech-mouse-battery-9.8.7.tar.gz", srcinfo)
        readme = self.read("README.md")
        for name in ("logitech-mouse-battery_9.8.7_all.deb", "logitech-mouse-battery-9.8.7-1.noarch.rpm", "logitech-mouse-battery-9.8.7-1-any.pkg.tar.zst"):
            self.assertIn(name, readme)
        self.assertNotIn(version.VERSION, readme)

    def testTheNewReleaseComesFirstAndTheOldOneStays(self):
        self.bump("9.8.7")
        metainfo = self.read(METAINFO)
        today = datetime.date.today().isoformat()
        self.assertIn('<releases>\n    <release version="9.8.7" date="' + today + '"/>\n    <release version="' + version.VERSION + '"', metainfo)

    def testThePackageReleaseStartsOverAtOne(self):
        self.write("packaging/aur/PKGBUILD", self.read("packaging/aur/PKGBUILD").replace("\npkgrel=1\n", "\npkgrel=3\n"))
        self.write("packaging/aur/.SRCINFO", self.read("packaging/aur/.SRCINFO").replace("\tpkgrel = 1\n", "\tpkgrel = 3\n"))
        self.bump("9.8.7")
        self.assertIn("\npkgrel=1\n", self.read("packaging/aur/PKGBUILD"))
        self.assertIn("\tpkgrel = 1\n", self.read("packaging/aur/.SRCINFO"))

    def testABadOrUnchangedVersionChangesNothing(self):
        before = {name: self.read(name) for name in FILES}
        for args in ((), ("9.8",), ("v9.8.7",), ("9.8.7", "1.0.0"), (version.VERSION,)):
            self.assertEqual(self.bump(*args), 1)
        self.assertEqual({name: self.read(name) for name in FILES}, before)

    def testAFileWithoutTheVersionChangesNothing(self):
        self.write("README.md", "# Mouse Battery\n")
        before = {name: self.read(name) for name in FILES}
        self.assertEqual(self.bump("9.8.7"), 1)
        self.assertEqual({name: self.read(name) for name in FILES}, before)


if (__name__ == "__main__"):
    unittest.main()
