import os
import unittest
import xml.etree.ElementTree as ElementTree

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtGui import QColor, QIcon, QImageReader
from PyQt6.QtWidgets import QApplication

import autostart
import icon
import tray
import version

app = QApplication.instance() or QApplication([])

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(REPO, "packaging")


def entryKeys(path):
    with open(path) as f:
        lines = f.read().splitlines()
    return (lines[0], dict(line.split("=", 1) for line in lines[1:] if "=" in line))


class DesktopEntryTests(unittest.TestCase):
    def testTheMenuEntryIsNamedForThePortalAppId(self):
        # the xdg portal finds the app by this file name, thus a renamed id without a renamed file brings its warning back
        firstLine, keys = entryKeys(os.path.join(PACKAGING, tray.APP_ID + ".desktop"))
        self.assertEqual(firstLine, "[Desktop Entry]")
        self.assertEqual((keys["Name"], keys["Exec"], keys["Icon"]), ("Mouse Battery", "logitech-mouse-battery", "logitech-mouse-battery"))
        self.assertNotIn("NoDisplay", keys)
        self.assertNotIn("Hidden", keys)

    def testTheLoginEntryIsNamedForTheOverride(self):
        # a user override only replaces the system entry when both carry the same file name
        firstLine, keys = entryKeys(os.path.join(PACKAGING, "autostart", autostart.FILE_NAME))
        self.assertEqual(firstLine, "[Desktop Entry]")
        self.assertEqual((keys["Exec"], keys["TryExec"]), ("logitech-mouse-battery", "logitech-mouse-battery"))
        self.assertNotIn("Hidden", keys)


RULE_FILE = "70-logitech-mouse-battery.rules"
RULE = 'ACTION!="remove", SUBSYSTEM=="hidraw", KERNELS=="0003:046D:*", DRIVERS=="hid-generic", PROGRAM="/usr/bin/python3 -I -B /usr/share/logitech-mouse-battery/isHidpp.py %S%p", TAG+="uaccess"'


class UdevRuleTests(unittest.TestCase):
    def readRule(self):
        with open(os.path.join(PACKAGING, RULE_FILE)) as f:
            return [line for line in f.read().splitlines() if line and not line.startswith("#")]

    def testTheRuleIsTheSpecsLine(self):
        self.assertEqual(self.readRule(), [RULE])

    def testTheRuleRunsTheCheckFromTheLaunchersFolder(self):
        with open(os.path.join(PACKAGING, "logitech-mouse-battery")) as f:
            appDir = next(line.split("=", 1)[1] for line in f.read().splitlines() if line.startswith("APP_DIR="))
        self.assertIn(" " + appDir + "/isHidpp.py ", self.readRule()[0])
        self.assertTrue(os.path.exists(os.path.join(REPO, "isHidpp.py")))


SVG = "{http://www.w3.org/2000/svg}"
METAINFO = "io.github.kymotsujason.LogitechMouseBattery.metainfo.xml"
RAW_MAIN = "https://raw.githubusercontent.com/kymotsujason/logitech-mouse-battery-tray-icon/main/"


class AppIconTests(unittest.TestCase):
    def setUp(self):
        self.root = ElementTree.parse(os.path.join(PACKAGING, "logitech-mouse-battery.svg")).getroot()

    def testTheIconUsesDesignGsTwentyTwoUnitGrid(self):
        self.assertEqual(self.root.get("viewBox"), "0 0 22 22")
        bodies = [rect for rect in self.root.iter(SVG + "rect") if (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height"), rect.get("rx")) == ("4", "1", "14", "20", "7")]
        self.assertEqual(len(bodies), 1)

    def testTheFillIsTheTrayGreen(self):
        fills = [element for element in self.root.iter() if element.get("fill") == icon.GREEN]
        self.assertEqual(len(fills), 1)

    def testTheIconUsesNoClipOrMask(self):
        # Qt's SVG renderer ignores clipPath and mask
        tags = [element.tag for element in self.root.iter()]
        self.assertNotIn(SVG + "clipPath", tags)
        self.assertNotIn(SVG + "mask", tags)

    def testQtDrawsTheFillAtThreeQuarters(self):
        formats = [bytes(f) for f in QImageReader.supportedImageFormats()]
        if (b"svg" not in formats):
            self.skipTest("this Qt has no SVG plugin")
        image = QIcon(os.path.join(PACKAGING, "logitech-mouse-battery.svg")).pixmap(220, 220).toImage()
        self.assertNotEqual(image.pixelColor(70, 40), QColor(icon.GREEN))
        self.assertEqual(image.pixelColor(110, 150), QColor(icon.GREEN))


class MetainfoTests(unittest.TestCase):
    def setUp(self):
        self.root = ElementTree.parse(os.path.join(PACKAGING, METAINFO)).getroot()

    def testItNamesTheAppAndItsMenuEntry(self):
        self.assertEqual(self.root.findtext("id"), "io.github.kymotsujason.LogitechMouseBattery")
        self.assertEqual(self.root.findtext("name"), "Mouse Battery")
        self.assertEqual(self.root.findtext("launchable"), tray.APP_ID + ".desktop")
        self.assertEqual(self.root.findtext("project_license"), "GPL-3.0-or-later")

    def testTheNewestReleaseIsTheAppsVersion(self):
        self.assertEqual(self.root.find("releases/release").get("version"), version.VERSION)

    def testTheScreenshotIsInTheRepo(self):
        url = self.root.findtext("screenshots/screenshot/image")
        self.assertTrue(url.startswith(RAW_MAIN))
        self.assertTrue(os.path.exists(os.path.join(REPO, url[len(RAW_MAIN):])))


def appFiles():
    # every Python module at the repo root is part of the app, thus a new one has to be packaged as well
    modules = sorted(name for name in os.listdir(REPO) if name.endswith(".py"))
    return modules + ["NotoSans-Medium.ttf", "OFL.txt"]


class NfpmTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PACKAGING, "nfpm.yaml")) as f:
            self.text = f.read()

    def testEveryAppFileGoesToTheAppFolder(self):
        for name in appFiles():
            self.assertIn("  - src: " + name + "\n    dst: /usr/share/logitech-mouse-battery/" + name + "\n", self.text)

    def testTheVersionComesFromTheBuild(self):
        self.assertIn("\nversion: ${VERSION}\n", self.text)

    def testDebianAsksForPythonThreeElevenOrLater(self):
        self.assertIn("      - python3 (>= 3.11)\n", self.text)

    def testBothScriptsReloadUdev(self):
        self.assertIn("scripts:\n  postinstall: packaging/reloadUdev.sh\n  postremove: packaging/reloadUdev.sh\n", self.text)

    def testTheLicenseGoesToEachDistrosPlace(self):
        self.assertIn("  - src: LICENSE\n    dst: /usr/share/doc/logitech-mouse-battery/copyright\n    packager: deb\n", self.text)
        self.assertIn("  - src: LICENSE\n    dst: /usr/share/licenses/logitech-mouse-battery/LICENSE\n    packager: rpm\n", self.text)


class PkgbuildTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PACKAGING, "aur", "PKGBUILD")) as f:
            self.text = f.read()

    def testTheVersionIsTheAppsVersion(self):
        self.assertIn("\npkgver=" + version.VERSION + "\n", self.text)

    def testEveryAppFileGoesToTheAppFolder(self):
        line = next(line for line in self.text.splitlines() if '-t "$pkgdir/usr/share/$pkgname"' in line)
        self.assertEqual(line.split('-t "$pkgdir/usr/share/$pkgname"', 1)[1].split(), appFiles())

    def testTheSrcinfoMatchesTheVersion(self):
        with open(os.path.join(PACKAGING, "aur", ".SRCINFO")) as f:
            self.assertIn("\tpkgver = " + version.VERSION + "\n", f.read())


if (__name__ == "__main__"):
    unittest.main()
