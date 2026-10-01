import os
import tempfile
import unittest

from PyQt6.QtDBus import QDBusVariant
from PyQt6.QtGui import QColor

import panelColor


class PortalBus:
    # answers ReadOne and Read with what a test gives, an error name or a value, and keeps what it was asked
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def call(self, message, mode=None, timeout=None):
        self.asked.append((message.member(), timeout))
        answer = self.answers[message.member()]
        if (isinstance(answer, str)):
            return message.createErrorReply(answer, "failed on purpose")
        return message.createReply([QDBusVariant(answer)])


class PortalReadTests(unittest.TestCase):
    def testReadOneAnswersWithTheDefaultTimeout(self):
        bus = PortalBus({"ReadOne": 1})
        self.assertEqual(panelColor.readPortalColorScheme(bus), (1, None))
        self.assertEqual(bus.asked, [("ReadOne", 2000)])

    def testAnOlderPortalFallsBackToRead(self):
        bus = PortalBus({"ReadOne": panelColor.UNKNOWN_METHOD, "Read": 2})
        self.assertEqual(panelColor.readPortalColorScheme(bus), (2, None))
        self.assertEqual([member for member, timeout in bus.asked], ["ReadOne", "Read"])

    def testAnyOtherErrorIsReturnedWithoutTryingRead(self):
        bus = PortalBus({"ReadOne": "org.freedesktop.DBus.Error.NoReply"})
        self.assertEqual(panelColor.readPortalColorScheme(bus), (0, "org.freedesktop.DBus.Error.NoReply"))
        self.assertEqual([member for member, timeout in bus.asked], ["ReadOne"])


class PanelColorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.configHome = os.path.join(self.temp.name, "config")
        self.dataHome = os.path.join(self.temp.name, "data")
        os.makedirs(self.configHome)
        self.palette = QColor(35, 38, 41)

    def tearDown(self):
        self.temp.cleanup()

    def writeTheme(self, name, colors=None):
        with open(os.path.join(self.configHome, "plasmarc"), "w") as f:
            f.write("[Theme]\nname=" + name + "\n")
        if (colors is not None):
            folder = os.path.join(self.dataHome, "plasma", "desktoptheme", name)
            os.makedirs(folder)
            with open(os.path.join(folder, "colors"), "w") as f:
                f.write("[Colors:Window]\nForegroundNormal=" + colors + "\n")

    def color(self, desktop, portalScheme=0):
        return panelColor.baseColor({"XDG_CURRENT_DESKTOP": desktop}, self.configHome, [self.dataHome], portalScheme, self.palette)

    def testPlasmaThemeWithAColorsFileWins(self):
        self.writeTheme("breeze-dark", "252,252,252")
        self.assertEqual(self.color("KDE"), QColor(252, 252, 252))

    def testPlasmaThemeWithoutAColorsFileUsesThePalette(self):
        self.writeTheme("default")
        self.assertEqual(self.color("KDE"), self.palette)

    def testMissingPlasmarcUsesThePalette(self):
        self.assertEqual(self.color("KDE"), self.palette)

    def testUndecodablePlasmarcFallsBackToThePalette(self):
        with open(os.path.join(self.configHome, "plasmarc"), "wb") as f:
            f.write(b"[Theme]\nname=\xe9\n")
        self.assertEqual(self.color("KDE"), self.palette)

    def testThemeNameWithANulByteFallsBackToThePalette(self):
        self.writeTheme("a\x00b")
        self.assertEqual(self.color("KDE"), self.palette)

    def testGnomeIsWhiteUnlessItPrefersLight(self):
        self.assertEqual(self.color("GNOME", 0), QColor(255, 255, 255))
        self.assertEqual(self.color("ubuntu:GNOME", 1), QColor(255, 255, 255))
        self.assertEqual(self.color("GNOME", 2), self.palette)

    def testGnomeClassicAndFlashbackUseThePalette(self):
        self.assertEqual(self.color("GNOME-Classic:GNOME", 0), self.palette)
        self.assertEqual(self.color("GNOME-Flashback:GNOME", 0), self.palette)

    def testOtherDesktopsUseThePalette(self):
        self.assertEqual(self.color("XFCE"), self.palette)

    def testDataDirs(self):
        self.assertEqual(panelColor.dataDirs({"XDG_DATA_HOME": "/h", "XDG_DATA_DIRS": "/a:/b"}), ["/h", "/a", "/b"])
        self.assertEqual(panelColor.dataDirs({})[1:], ["/usr/local/share", "/usr/share"])

    def testRelativeDataDirsAreIgnored(self):
        dirs = panelColor.dataDirs({"XDG_DATA_HOME": "h", "XDG_DATA_DIRS": "a:/b"})
        self.assertEqual(dirs, [os.path.join(os.path.expanduser("~"), ".local", "share"), "/b"])


if (__name__ == "__main__"):
    unittest.main()
