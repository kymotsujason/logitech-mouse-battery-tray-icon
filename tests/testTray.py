import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import autostart
import hidpp
import mice
import tray
import upower
from icon import IconState

app = QApplication.instance() or QApplication([])
NAME = "PRO X3 SUPERSTRIKE"
LAST_READ = 1000000.0
PATH = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"


def makeMouse(reading, asleep=False):
    mouse = mice.Mouse("02bc524c")
    mouse.name = NAME
    mouse.reading = reading
    mouse.asleep = asleep
    mouse.lastRead = LAST_READ
    return mouse


class TrayTextTests(unittest.TestCase):
    def testIconStates(self):
        self.assertEqual(tray.iconStateFor(None), IconState(empty=True))
        self.assertEqual(tray.iconStateFor(makeMouse(None)), IconState(empty=True))
        self.assertEqual(tray.iconStateFor(makeMouse(hidpp.BatteryReading(81, 0))), IconState(percent=81))
        self.assertEqual(tray.iconStateFor(makeMouse(hidpp.BatteryReading(50, 2))), IconState(percent=50, charging=True))
        self.assertEqual(tray.iconStateFor(makeMouse(hidpp.BatteryReading(81, 0), asleep=True)), IconState(percent=81, asleep=True))

    def testMouseLines(self):
        stamp = time.strftime("%H:%M", time.localtime(LAST_READ))
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(69, 0))), NAME + ", 69%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(82, 1))), NAME + ", charging, 82%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(100, 3))), NAME + ", full, 100%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(69, 0), asleep=True)), NAME + ", asleep, 69% at " + stamp)
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(None, 0, level="low"))), NAME + ", low")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(None)), NAME + ", no reading yet")


class ProcessTests(unittest.TestCase):
    def testTheLockLetsOneCopyRun(self):
        with tempfile.TemporaryDirectory() as runtimeDir:
            first = tray.takeLock(runtimeDir)
            self.assertTrue(first[0])
            self.assertEqual(tray.takeLock(runtimeDir), (False, None))
            first[1].close()
            again = tray.takeLock(runtimeDir)
            self.assertTrue(again[0])
            again[1].close()

    def testNoRuntimeFolderMeansNoLock(self):
        self.assertEqual(tray.takeLock(None), (True, None))
        self.assertEqual(tray.takeLock(""), (True, None))

    def testReadVersion(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(tray.readVersion(folder))
            with open(os.path.join(folder, "version.py"), "w") as f:
                f.write("VERSION = \"1.2.3\"\n")
            self.assertEqual(tray.readVersion(folder), "1.2.3")

    def testNicenessIsOnlyToppedUp(self):
        for current, expected in ((0, [mock.call(0), mock.call(10)]), (10, [mock.call(0)]), (12, [mock.call(0)])):
            with mock.patch.object(tray.os, "nice", return_value=current) as nice:
                tray.raiseNiceness()
            self.assertEqual(nice.call_args_list, expected)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(hidpp, "findHidppNodes", lambda *args, **kwargs: []),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.temp.name, "XDG_CURRENT_DESKTOP": "XFCE"}),
        ]
        for patch in self.patches:
            patch.start()
        self.bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "trayTestBus")
        self.mouseApp = None

    def tearDown(self):
        if (self.mouseApp is not None):
            self.mouseApp.stop()
            if (self.mouseApp.tray is not None):
                self.mouseApp.tray.hide()
        for patch in self.patches:
            patch.stop()
        QDBusConnection.disconnectFromBus("trayTestBus")
        self.temp.cleanup()

    def makeApp(self, hostUp):
        with mock.patch.object(tray, "hostRegistered", return_value=hostUp):
            self.mouseApp = tray.MouseBatteryApp(app, self.bus, self.bus)
            self.mouseApp.start()
        return self.mouseApp

    def notices(self, mouseApp):
        sent = []
        return sent, mock.patch.object(mouseApp.notifier, "send", lambda summary, body, actions=(), onAction=None: sent.append((body, list(actions))))

    def testNoTrayIconUntilAHostIsRegistered(self):
        mouseApp = self.makeApp(False)
        self.assertIsNone(mouseApp.tray)
        with mock.patch.object(tray, "hostRegistered", return_value=True):
            mouseApp.checkHost()
        self.assertIsNotNone(mouseApp.tray)

    def testAFailedHostQueryIsAskedAgain(self):
        with mock.patch.object(tray, "HOST_RETRY_MS", 10):
            mouseApp = self.makeApp(False)
        with mock.patch.object(tray, "hostRegistered", return_value=True):
            QTest.qWait(200)
        self.assertIsNotNone(mouseApp.tray)

    def testHostRetriesBackOffUpToACap(self):
        with mock.patch.object(tray, "HOST_RETRY_MS", 10), mock.patch.object(tray, "HOST_RETRY_MAX_MS", 40):
            mouseApp = self.makeApp(False)
            intervals = [mouseApp.hostTimer.interval()]
            with mock.patch.object(tray, "hostRegistered", return_value=False):
                for i in range(3):
                    mouseApp.checkHost()
                    intervals.append(mouseApp.hostTimer.interval())
        self.assertEqual(intervals, [10, 20, 40, 40])

    def testTheNoTrayNoticeAsksForTheHostFirst(self):
        mouseApp = self.makeApp(False)
        sent, patch = self.notices(mouseApp)
        with patch, mock.patch.object(tray, "hostRegistered", return_value=True):
            mouseApp.onNoTray()
        self.assertIsNotNone(mouseApp.tray)
        self.assertEqual(sent, [])

    def testAPanelColorErrorStillShowsTheIconInThePaletteColor(self):
        mouseApp = self.makeApp(False)
        errors = io.StringIO()
        with mock.patch.object(tray.panelColor, "baseColor", side_effect=ValueError("embedded null byte")), mock.patch.object(tray, "hostRegistered", return_value=True), contextlib.redirect_stderr(errors):
            mouseApp.checkHost()
            mouseApp.onColorsChanged()
        self.assertTrue(mouseApp.tray.isVisible())
        self.assertEqual(mouseApp.shownColor, app.palette().color(QPalette.ColorRole.WindowText))
        self.assertEqual(errors.getvalue(), "Couldn't read the panel color: embedded null byte\n")

    def testAFailedFirstDrawStillShowsTheIconAndKeepsRunning(self):
        mouseApp = self.makeApp(False)
        realUpdate = mouseApp.updateTray
        hooked = []

        def failingUpdate(*args):
            realUpdate()
            raise RuntimeError("update failed")

        with mock.patch.object(mouseApp, "updateTray", failingUpdate), mock.patch.object(tray, "hostRegistered", return_value=True), mock.patch.object(tray.sys, "excepthook", lambda *info: hooked.append(info[0])):
            mouseApp.checkHost()
        self.assertTrue(mouseApp.tray.isVisible())
        self.assertEqual(hooked, [RuntimeError])

    def testMenuLinesAndTooltipFollowTheMice(self):
        mouseApp = self.makeApp(True)
        self.assertEqual([action.text() for action in mouseApp.statusActions], ["No Logitech mouse found"])
        mouseApp.mice.applyUPower(upower.UPowerMouse(PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        self.assertEqual([action.text() for action in mouseApp.statusActions], ["MX Master 3, 55%"])
        self.assertEqual(mouseApp.shownTooltip, "MX Master 3, 55%")

    def lowBatterySends(self, replies):
        mouseApp = self.makeApp(True)
        with mock.patch.object(mouseApp.notifier, "send", side_effect=replies) as send:
            for percent in (9, 8, 7):
                mouseApp.mice.applyUPower(upower.UPowerMouse(PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(percent, 0)))
        return [each.args[1] for each in send.call_args_list]

    def testALowBatteryNoticeThatFailsIsSentAgain(self):
        self.assertEqual(self.lowBatterySends([(None, "org.freedesktop.DBus.Error.ServiceUnknown"), (7, None), (7, None)]), ["MX Master 3 has 9% left.", "MX Master 3 has 8% left."])

    def testALowBatteryNoticeThatTimedOutIsntSentAgain(self):
        self.assertEqual(self.lowBatterySends([(None, "org.freedesktop.DBus.Error.NoReply"), (7, None), (7, None)]), ["MX Master 3 has 9% left."])

    def testThemeChangeRedrawsTheIcon(self):
        mouseApp = self.makeApp(True)
        mouseApp.mice.applyUPower(upower.UPowerMouse(PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(81, 0)))
        original = QPalette(app.palette())
        self.addCleanup(app.setPalette, original)
        palette = QPalette(original)
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#102030"))
        app.setPalette(palette)
        QTest.qWait(20)
        # the fill at 81% is drawn in the base color
        color = mouseApp.tray.icon().pixmap(22, 22).toImage().pixelColor(11, 19)
        self.assertEqual((color.red(), color.green(), color.blue(), color.alpha()), (0x10, 0x20, 0x30, 255))

    def testOpenAtLoginToggles(self):
        mouseApp = self.makeApp(True)
        self.assertTrue(mouseApp.loginAction.isChecked())
        mouseApp.onLoginToggled(False)
        self.assertFalse(autostart.isEnabled(self.temp.name))
        self.assertFalse(mouseApp.loginAction.isChecked())

    def testNoTrayNoticeOnGnomeOffersTheExtension(self):
        mouseApp = self.makeApp(False)
        sent, patch = self.notices(mouseApp)
        with patch, mock.patch.object(tray, "nameHasOwner", return_value=True), mock.patch.object(tray, "installedExtension", return_value="ubuntu-appindicators@ubuntu.com"):
            mouseApp.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs the AppIndicator extension to show in the top bar.", [("turnOn", "Turn On"), ("dontOpen", "Don't Open at Login")])])

    def testNoTrayNoticeOnGnomeFlashbackOffersOnlyTheWayOut(self):
        mouseApp = self.makeApp(False)
        sent, patch = self.notices(mouseApp)
        with patch, mock.patch.object(tray, "nameHasOwner", return_value=True), mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME-Flashback:GNOME"}):
            mouseApp.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.", [("dontOpen", "Don't Open at Login")])])

    def testNoTrayNoticeWithoutTheExtensionLinksToIt(self):
        mouseApp = self.makeApp(False)
        sent, patch = self.notices(mouseApp)
        with patch, mock.patch.object(tray, "nameHasOwner", return_value=True), mock.patch.object(tray, "installedExtension", return_value=None):
            mouseApp.onNoTray()
        self.assertEqual(sent[0][1], [("getExtension", "Get Extension"), ("dontOpen", "Don't Open at Login")])

    def testNoTrayNoticeElsewhereOffersOnlyTheWayOut(self):
        mouseApp = self.makeApp(False)
        sent, patch = self.notices(mouseApp)
        with patch, mock.patch.object(tray, "nameHasOwner", return_value=False):
            mouseApp.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.", [("dontOpen", "Don't Open at Login")])])

    def testDontOpenAtLoginWritesTheOverrideAndQuits(self):
        mouseApp = self.makeApp(False)
        with mock.patch.object(mouseApp, "quitApp") as quitApp:
            mouseApp.onNoticeAction("dontOpen")
        self.assertFalse(autostart.isEnabled(self.temp.name))
        self.assertTrue(quitApp.called)

    def testRemovedFilesQuitTheApp(self):
        mouseApp = self.makeApp(True)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(tray, "APP_FOLDER", folder), mock.patch.object(mouseApp, "quitApp") as quitApp:
            mouseApp.onInstallChanged()
        self.assertTrue(quitApp.called)

    def installedCopy(self, folder, version):
        with open(os.path.join(folder, "tray.py"), "w") as f:
            f.write("")
        with open(os.path.join(folder, "version.py"), "w") as f:
            f.write("VERSION = \"" + version + "\"\n")

    def testANewVersionThatImportsRestartsTheApp(self):
        mouseApp = self.makeApp(True)
        with tempfile.TemporaryDirectory() as folder:
            self.installedCopy(folder, "9.9.9")
            errors = io.StringIO()
            with mock.patch.object(tray, "APP_FOLDER", folder), mock.patch.object(tray.subprocess, "run", return_value=mock.Mock(returncode=0)) as run, mock.patch.object(tray.os, "execv") as execv, contextlib.redirect_stderr(errors):
                mouseApp.onInstallChanged()
        self.assertEqual(run.call_args.args[0], [sys.executable, "-B", "-c", "import tray"])
        self.assertEqual(execv.call_args.args[1], [sys.executable, "-B", os.path.join(folder, "tray.py")])
        self.assertEqual(errors.getvalue(), "")

    def testANewVersionThatDoesntImportKeepsRunning(self):
        mouseApp = self.makeApp(True)
        with tempfile.TemporaryDirectory() as folder:
            self.installedCopy(folder, "9.9.9")
            errors = io.StringIO()
            with mock.patch.object(tray, "APP_FOLDER", folder), mock.patch.object(tray.subprocess, "run", return_value=mock.Mock(returncode=1)), mock.patch.object(tray.os, "execv") as execv, contextlib.redirect_stderr(errors):
                mouseApp.onInstallChanged()
        self.assertFalse(execv.called)
        self.assertEqual(errors.getvalue(), "Version 9.9.9 didn't import, thus " + tray.VERSION + " keeps running\n")

    def testAVersionCheckThatCantFinishKeepsRunning(self):
        mouseApp = self.makeApp(True)
        with tempfile.TemporaryDirectory() as folder:
            self.installedCopy(folder, "9.9.9")
            for error in (tray.subprocess.TimeoutExpired([sys.executable], 60), OSError("exec failed")):
                with self.subTest(error=type(error).__name__):
                    errors = io.StringIO()
                    with mock.patch.object(tray, "APP_FOLDER", folder), mock.patch.object(tray.subprocess, "run", side_effect=error), mock.patch.object(tray.os, "execv") as execv, contextlib.redirect_stderr(errors):
                        mouseApp.onInstallChanged()
                    self.assertFalse(execv.called)
                    self.assertEqual(errors.getvalue(), "Version 9.9.9 couldn't be checked (" + str(error) + "), thus " + tray.VERSION + " keeps running\n")

    def testTheSameVersionDoesNothing(self):
        mouseApp = self.makeApp(True)
        with tempfile.TemporaryDirectory() as folder:
            self.installedCopy(folder, tray.VERSION)
            with mock.patch.object(tray, "APP_FOLDER", folder), mock.patch.object(tray.subprocess, "run") as run:
                mouseApp.onInstallChanged()
        self.assertFalse(run.called)

    def testUPowersOwnerIsWatched(self):
        mouseApp = self.makeApp(True)
        self.assertEqual(mouseApp.upower.ownerWatcher.watchedServices(), [upower.SERVICE])


class ConfigFolderTests(unittest.TestCase):
    def testAnAbsoluteConfigHomeIsUsed(self):
        self.assertEqual(tray.configFolder({"XDG_CONFIG_HOME": "/cfg"}), "/cfg")

    def testARelativeOrMissingConfigHomeFallsBackToDotConfig(self):
        expected = os.path.join(os.path.expanduser("~"), ".config")
        for env in ({"XDG_CONFIG_HOME": "cfg"}, {"XDG_CONFIG_HOME": ""}, {}):
            self.assertEqual(tray.configFolder(env), expected)


if (__name__ == "__main__"):
    unittest.main()
