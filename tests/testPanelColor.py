import os
import tempfile
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QObject
from PyQt6.QtDBus import QDBusPendingCall, QDBusVariant
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import panelColor

app = QApplication.instance() or QApplication([])
# the suite runs as root in the containers, so the tests act as a non root account and root's exemptions can't pass a test on their own
ACCOUNT = os.getuid() or 1000


class PortalBus:
    # answers ReadOne and Read with what a test gives, an error name or a value, keeps what it was asked, and takes no blocking call
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def asyncCall(self, message, timeout=None):
        self.asked.append((message.member(), timeout))
        answer = self.answers[message.member()]
        if (isinstance(answer, str)):
            return QDBusPendingCall.fromCompletedCall(message.createErrorReply(answer, "failed on purpose"))
        return QDBusPendingCall.fromCompletedCall(message.createReply([QDBusVariant(answer)]))


class PortalSettingTests(unittest.TestCase):
    def testAnyAppearanceKeyCanBeAskedFor(self):
        bus = PortalBus({"ReadOne": 1})
        sent = []
        realCall = bus.asyncCall

        def recording(message, timeout=None):
            sent.append(message.arguments())
            return realCall(message, timeout)
        bus.asyncCall = recording
        answers = []
        owner = QObject()
        panelColor.askPortalSetting(bus, owner, "reduced-motion", lambda value, error: answers.append((value, error)))
        for i in range(50):
            if (answers):
                break
            QTest.qWait(20)
        self.assertEqual((answers, sent), ([(1, None)], [[panelColor.APPEARANCE, "reduced-motion"]]))


class PortalReadTests(unittest.TestCase):
    def ask(self, bus):
        answers = []
        owner = QObject()
        panelColor.askPortalColorScheme(bus, owner, lambda scheme, error: answers.append((scheme, error)))
        for i in range(50):
            if (answers):
                break
            QTest.qWait(20)
        return answers

    def testReadOneAnswersWithTheDefaultTimeout(self):
        bus = PortalBus({"ReadOne": 1})
        self.assertEqual(self.ask(bus), [(1, None)])
        self.assertEqual(bus.asked, [("ReadOne", 2000)])

    def testAnOlderPortalFallsBackToRead(self):
        bus = PortalBus({"ReadOne": panelColor.UNKNOWN_METHOD, "Read": 2})
        self.assertEqual(self.ask(bus), [(2, None)])
        self.assertEqual([member for member, timeout in bus.asked], ["ReadOne", "Read"])

    def testAnyOtherErrorIsReturnedWithoutTryingRead(self):
        bus = PortalBus({"ReadOne": "org.freedesktop.DBus.Error.NoReply"})
        self.assertEqual(self.ask(bus), [(0, "org.freedesktop.DBus.Error.NoReply")])
        self.assertEqual([member for member, timeout in bus.asked], ["ReadOne"])

    def testAValueThatIsntANumberCountsAsNoPreference(self):
        bus = PortalBus({})
        bus.asyncCall = lambda message, timeout=None: QDBusPendingCall.fromCompletedCall(message.createReply([QDBusVariant("dark")]))
        self.assertEqual(self.ask(bus), [(0, None)])


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



class PlantedIniTests(unittest.TestCase):
    # a run as root with another account's XDG_CONFIG_HOME reads their kdeglobals and plasmarc, so each file is told apart by the owner it reports
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = os.path.join(self.temp.name, "target")
        with open(self.target, "w") as f:
            f.write("[KDE]\nAnimationDurationFactor=0.5\n")
        self.path = os.path.join(self.temp.name, "kdeglobals")
        self.owners = {}
        realUid = os.getuid()
        realFstat = os.fstat

        def ownerFor(fd, *args, **kwargs):
            info = realFstat(fd, *args, **kwargs)
            fields = list(info[:10])
            fields[4] = self.owners.get(os.readlink("/proc/self/fd/" + str(fd)), ACCOUNT if info.st_uid == realUid else info.st_uid)
            return os.stat_result(fields)

        for patch in (mock.patch.object(os, "getuid", return_value=ACCOUNT), mock.patch.object(os, "fstat", ownerFor)):
            patch.start()
            self.addCleanup(patch.stop)

    def testALinkAnotherAccountMadeIsntFollowed(self):
        os.symlink(self.target, self.path)
        self.owners[self.path] = ACCOUNT + 1
        self.assertIsNone(panelColor.readIni(self.path))

    def testALinkThisAccountMadeIsFollowed(self):
        os.symlink(self.target, self.path)
        self.assertEqual(panelColor.readIni(self.path), {"": {}, "KDE": {"AnimationDurationFactor": "0.5"}})

    def testAFifoAtTheNameDoesntHang(self):
        os.mkfifo(self.path)
        self.assertIsNone(panelColor.readIni(self.path))


if (__name__ == "__main__"):
    unittest.main()
