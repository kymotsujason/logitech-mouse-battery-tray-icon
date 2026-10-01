import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtDBus import QDBusConnection
from PyQt6.QtGui import QAction, QColor, QPalette, QSessionManager
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

import autostart
import exceptionHooks
import hidpp
import installWatch
import mice
import notify
import panelColor
import serviceClient
import tray
import trayHost
import upower
from icon import IconState
from tests.fakes.recordingBus import RecordingBus

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


def waitUntil(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while (time.monotonic() < deadline):
        if (condition()):
            return True
        QTest.qWait(10)
    return condition()


class TrayTextTests(unittest.TestCase):
    def testIconStates(self):
        self.assertEqual(tray.iconStateFor(None), IconState(empty=True))
        self.assertEqual(tray.iconStateFor(makeMouse(None)), IconState(empty=True))
        self.assertEqual(tray.iconStateFor(makeMouse(hidpp.BatteryReading(81, 0))), IconState(percent=81))
        self.assertEqual(tray.iconStateFor(makeMouse(hidpp.BatteryReading(50, 2))), IconState(percent=50, charging=True))
        self.assertEqual(tray.iconStateFor(makeMouse(hidpp.BatteryReading(81, 0), asleep=True)), IconState(percent=81, asleep=True))

    def testMouseLines(self):
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(69, 0))), NAME + ", 69%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(82, hidpp.CHARGING_CHARGING))), NAME + ", charging, 82%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(82, hidpp.CHARGING_SLOW))), NAME + ", charging, 82%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(100, hidpp.CHARGING_FULL))), NAME + ", full, 100%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(50, hidpp.CHARGING_ERROR))), NAME + ", charging error, 50%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(50, hidpp.CHARGING_PENDING))), NAME + ", pending charge, 50%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(50, hidpp.CHARGING_UNKNOWN))), NAME + ", unknown state, 50%")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(hidpp.BatteryReading(None, 0, level="low"))), NAME + ", low")
        self.assertEqual(tray.mouseLine(NAME, makeMouse(None)), NAME + ", no reading yet")

    def testAnAsleepLineDatesAnOlderReading(self):
        local = time.localtime(LAST_READ)
        clock = time.strftime("%H:%M", local)
        day = mice.MONTH_NAMES[local.tm_mon - 1] + " " + str(local.tm_mday)
        asleep = makeMouse(hidpp.BatteryReading(69, 0), asleep=True)
        self.assertEqual(tray.mouseLine(NAME, asleep, now=LAST_READ), NAME + ", asleep, 69% at " + clock)
        self.assertEqual(tray.mouseLine(NAME, asleep, now=LAST_READ + 3 * 86400), NAME + ", asleep, 69% on " + day + " at " + clock)


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
        with mock.patch.object(tray.signal, "signal"), mock.patch.object(sys, "excepthook", sys.excepthook), mock.patch.object(exceptionHooks.threading, "excepthook", exceptionHooks.threading.excepthook), mock.patch.object(tray, "raiseNiceness"), mock.patch.object(tray, "QApplication"), mock.patch.object(tray, "takeLock", return_value=(False, None)), mock.patch.object(tray, "Notifier"):
            self.assertEqual(tray.main(), 0)
            self.assertEqual((sys.excepthook, exceptionHooks.threading.excepthook), (exceptionHooks.printException, exceptionHooks.printThreadException))

    def testMainLetsCtrlCEndItAndAsksNotToBeRestored(self):
        with mock.patch.object(tray.signal, "signal") as setSignal, mock.patch.object(sys, "excepthook", sys.excepthook), mock.patch.object(exceptionHooks.threading, "excepthook", exceptionHooks.threading.excepthook), mock.patch.object(tray, "raiseNiceness"), mock.patch.object(tray, "QApplication") as application, mock.patch.object(tray, "takeLock", return_value=(False, None)), mock.patch.object(tray, "Notifier"):
            tray.main()
        setSignal.assert_called_once_with(tray.signal.SIGINT, tray.signal.SIG_DFL)
        application.return_value.saveStateRequest.connect.assert_called_once_with(tray.neverRestart)

    def testNeverRestartSetsTheHint(self):
        manager = mock.Mock()
        tray.neverRestart(manager)
        manager.setRestartHint.assert_called_once_with(QSessionManager.RestartHint.RestartNever)

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
            fileWatcher = self.mouseApp.installWatcher.watcher
            fileWatcher.removePaths(fileWatcher.files() + fileWatcher.directories())
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
        self.assertTrue(waitUntil(lambda: [action.text() for action in mouseApp.statusActions] == ["Can't reach the battery service."]))
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
        with contextlib.redirect_stderr(io.StringIO()):
            sends = self.lowBatterySends([(None, "org.freedesktop.DBus.Error.ServiceUnknown"), (7, None), (7, None)])
        self.assertEqual(sends, ["MX Master 3 has 9% left.", "MX Master 3 has 8% left."])

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
            mouseApp.trayHost.onNoticeAction("dontOpen")
        self.assertFalse(autostart.isEnabled(self.temp.name))
        self.assertTrue(quitApp.called)

    def testARemovedInstallQuitsTheApp(self):
        mouseApp = self.makeApp(False)
        with mock.patch.object(mouseApp.app, "quit") as quit:
            mouseApp.installWatcher.removed.emit()
        self.assertTrue(quit.called)

    def notices(self, mouseApp):
        sent = []
        return sent, mock.patch.object(mouseApp.notifier, "send", lambda summary, body, actions=(), onAction=None: sent.append((body, list(actions))))

    def testDontOpenAtLoginThatCantWriteSaysSoAndKeepsRunning(self):
        mouseApp = self.makeApp(False)
        path = autostart.overridePath(self.temp.name)
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n")
        os.chmod(path, 0o400)
        sent, patch = self.notices(mouseApp)
        errors = io.StringIO()
        with patch, mock.patch.object(mouseApp, "quitApp") as quitApp, contextlib.redirect_stderr(errors):
            mouseApp.onDontOpenAtLogin()
        self.assertFalse(quitApp.called)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][0].startswith("Couldn't turn off Open at Login. [Errno 13] Permission denied"), sent[0][0])

    def testAFailedRestartKeepsTheMouseListRunning(self):
        mouseApp = self.makeApp(True)
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "tray.py"), "w") as f:
                f.write("")
            with open(os.path.join(folder, "version.py"), "w") as f:
                f.write("VERSION = \"9.9.9\"\n")
            errors = io.StringIO()
            with mock.patch.object(installWatch, "APP_FOLDER", folder), mock.patch.object(installWatch.subprocess, "run", return_value=mock.Mock(returncode=0)), mock.patch.object(installWatch.os, "execv", side_effect=OSError(8, "Exec format error")), contextlib.redirect_stderr(errors):
                mouseApp.installWatcher.onChanged()
        self.assertEqual((mouseApp.mouseList.stopped, mouseApp.mouseList.readTimer.isActive()), (False, True))

    def testARemovedStatusLineIsFreed(self):
        mouseApp = self.makeApp(True)
        other = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_1"
        mouseApp.mouseList.applyUPower(upower.UPowerMouse(PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        mouseApp.mouseList.applyUPower(upower.UPowerMouse(other, "56ef78ab", "MX Anywhere 3", hidpp.BatteryReading(60, 0)))
        before = len(mouseApp.menu.findChildren(QAction))
        mouseApp.mouseList.removeUPower(other)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.assertEqual(len(mouseApp.menu.findChildren(QAction)), before - 1)

    def testAnAmpersandIsDoubledOnlyInTheMenu(self):
        mouseApp = self.makeApp(True)
        mouseApp.mouseList.applyUPower(upower.UPowerMouse(PATH, "12ab34cd", "Tom & Jerry", hidpp.BatteryReading(55, 0)))
        self.assertEqual(([action.text() for action in mouseApp.statusActions], mouseApp.shownTooltip), (["Tom && Jerry, 55%"], "Tom & Jerry, 55%"))

    def testAboutOpensOneBox(self):
        mouseApp = self.makeApp(True)
        # the old modal about() would block the test, so it's stubbed for the run before the fix
        with mock.patch.object(tray.QMessageBox, "about"):
            mouseApp.showAbout()
            first = mouseApp.aboutBox
            mouseApp.showAbout()
        self.assertIs(mouseApp.aboutBox, first)
        self.assertTrue(first.isVisible())
        self.assertIn("Mouse Battery " + tray.VERSION, first.text())
        first.close()

    def testTheBaseColorIsCachedUntilTheReadTimerFires(self):
        mouseApp = self.makeApp(True)
        with mock.patch.object(tray.panelColor, "baseColor", return_value=QColor("#102030")) as baseColor:
            mouseApp.clearColor()
            mouseApp.updateTray()
            mouseApp.updateTray()
            self.assertEqual(baseColor.call_count, 1)
            mouseApp.mouseList.readTimer.start(10)
            QTest.qWait(100)
            mouseApp.mouseList.readTimer.stop()
        self.assertGreater(baseColor.call_count, 1)

    def testAPortalThatDoesntAnswerYetIsAskedOnceMore(self):
        mouseApp = self.makeApp(True)
        reads = [(0, notify.NO_REPLY), (1, None)]
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}), mock.patch.object(tray.panelColor, "readPortalColorScheme", side_effect=reads) as read:
            mouseApp.watchColors()
            self.assertTrue(mouseApp.portalTimer.isActive())
            mouseApp.cachedColor = QColor("#123456")
            mouseApp.portalTimer.stop()
            mouseApp.onPortalRetry()
        self.assertEqual((read.call_count, mouseApp.portalScheme), (2, 1))
        self.assertNotEqual(mouseApp.cachedColor, QColor("#123456"))

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

    def testThePortalSignalIsTakenOnlyFromThePortal(self):
        mouseApp = self.makeApp(True)
        bus = RecordingBus()
        mouseApp.sessionBus = bus
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}), mock.patch.object(tray.panelColor, "readPortalColorScheme", return_value=(0, None)):
            mouseApp.watchColors()
        self.assertIn((panelColor.PORTAL_SERVICE, panelColor.PORTAL_PATH, panelColor.SETTINGS_INTERFACE, "SettingChanged"), bus.subscriptions)

    def testAFailedLowBatteryNoticeIsLoggedOnce(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            self.lowBatterySends([(None, "org.freedesktop.DBus.Error.ServiceUnknown")] * 3)
        self.assertEqual(errors.getvalue(), "Couldn't send the low battery notice for MX Master 3: org.freedesktop.DBus.Error.ServiceUnknown\n")

    def testTheMenuALeftClickAndTheTimerEachAskTheServiceToRead(self):
        with mock.patch.object(serviceClient.ServiceWatcher, "readAll") as readAll:
            mouseApp = self.makeApp(True)
            mouseApp.onMenuShown()
            self.assertEqual(readAll.call_count, 1)
            mouseApp.onActivated(QSystemTrayIcon.ActivationReason.Trigger)
            self.assertEqual(readAll.call_count, 2)
            mouseApp.mouseList.readTimer.start(10)
            QTest.qWait(100)
            mouseApp.mouseList.readTimer.stop()
        self.assertGreaterEqual(readAll.call_count, 3)


class ConfigFolderTests(unittest.TestCase):
    def testAnAbsoluteConfigHomeIsUsed(self):
        self.assertEqual(tray.configFolder({"XDG_CONFIG_HOME": "/cfg"}), "/cfg")

    def testARelativeOrMissingConfigHomeFallsBackToDotConfig(self):
        expected = os.path.join(os.path.expanduser("~"), ".config")
        for env in ({"XDG_CONFIG_HOME": "cfg"}, {"XDG_CONFIG_HOME": ""}, {}):
            self.assertEqual(tray.configFolder(env), expected)


if (__name__ == "__main__"):
    unittest.main()
