import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection, QDBusMessage
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import autostart
import hidpp
import mice
import tray
import trayHost
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

    def testMainInstallsTheExceptionHooks(self):
        with mock.patch.object(sys, "excepthook", sys.excepthook), mock.patch.object(tray.threading, "excepthook", tray.threading.excepthook), mock.patch.object(tray, "raiseNiceness"), mock.patch.object(tray, "QApplication"), mock.patch.object(tray, "takeLock", return_value=(False, None)), mock.patch.object(tray, "Notifier"):
            self.assertEqual(tray.main(), 0)
            self.assertEqual((sys.excepthook, tray.threading.excepthook), (tray.printException, tray.printThreadException))

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
        with mock.patch.object(trayHost, "hostRegistered", return_value=hostUp):
            self.mouseApp = tray.MouseBatteryApp(app, self.bus, self.bus)
            self.mouseApp.start()
        return self.mouseApp

    def testAPanelColorErrorStillShowsTheIconInThePaletteColor(self):
        mouseApp = self.makeApp(False)
        errors = io.StringIO()
        with mock.patch.object(tray.panelColor, "baseColor", side_effect=ValueError("embedded null byte")), mock.patch.object(trayHost, "hostRegistered", return_value=True), contextlib.redirect_stderr(errors):
            mouseApp.trayHost.checkHost()
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

        with mock.patch.object(mouseApp, "updateTray", failingUpdate), mock.patch.object(trayHost, "hostRegistered", return_value=True), mock.patch.object(tray.sys, "excepthook", lambda *info: hooked.append(info[0])):
            mouseApp.trayHost.checkHost()
        self.assertTrue(mouseApp.tray.isVisible())
        self.assertEqual(hooked, [RuntimeError])

    def testMenuLinesAndTooltipFollowTheMice(self):
        mouseApp = self.makeApp(True)
        self.assertEqual([action.text() for action in mouseApp.statusActions], ["No Logitech mouse found"])
        mouseApp.mouseList.applyUPower(upower.UPowerMouse(PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        self.assertEqual([action.text() for action in mouseApp.statusActions], ["MX Master 3, 55%"])
        self.assertEqual(mouseApp.shownTooltip, "MX Master 3, 55%")

    def lowBatterySends(self, replies):
        mouseApp = self.makeApp(True)
        with mock.patch.object(mouseApp.notifier, "send", side_effect=replies) as send:
            for percent in (9, 8, 7):
                mouseApp.mouseList.applyUPower(upower.UPowerMouse(PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(percent, 0)))
        return [each.args[1] for each in send.call_args_list]

    def testALowBatteryNoticeThatFailsIsSentAgain(self):
        self.assertEqual(self.lowBatterySends([(None, "org.freedesktop.DBus.Error.ServiceUnknown"), (7, None), (7, None)]), ["MX Master 3 has 9% left.", "MX Master 3 has 8% left."])

    def testALowBatteryNoticeThatTimedOutIsntSentAgain(self):
        self.assertEqual(self.lowBatterySends([(None, "org.freedesktop.DBus.Error.NoReply"), (7, None), (7, None)]), ["MX Master 3 has 9% left."])

    def testThemeChangeRedrawsTheIcon(self):
        mouseApp = self.makeApp(True)
        mouseApp.mouseList.applyUPower(upower.UPowerMouse(PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(81, 0)))
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

    def testDontOpenAtLoginWritesTheOverrideAndQuits(self):
        mouseApp = self.makeApp(False)
        with mock.patch.object(mouseApp, "quitApp") as quitApp:
            mouseApp.onDontOpenAtLogin()
        self.assertFalse(autostart.isEnabled(self.temp.name))
        self.assertTrue(quitApp.called)

    def testTheTrayShowsOnceTheHostIsReady(self):
        mouseApp = self.makeApp(False)
        self.assertIsNone(mouseApp.tray)
        with mock.patch.object(trayHost, "hostRegistered", return_value=True):
            mouseApp.trayHost.checkHost()
        self.assertIsNotNone(mouseApp.tray)

    def testStopEndsTheHostTimers(self):
        mouseApp = self.makeApp(False)
        mouseApp.stop()
        self.assertEqual((mouseApp.trayHost.hostTimer.isActive(), mouseApp.trayHost.noticeTimer.isActive()), (False, False))

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
