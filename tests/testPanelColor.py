import os
import tempfile
import unittest

from PyQt6.QtGui import QColor

import panelColor


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
