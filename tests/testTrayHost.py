import os
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection, QDBusMessage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import notify
import trayHost
from tests.fakes.recordingBus import RecordingBus

app = QApplication.instance() or QApplication([])


class TrayHostTests(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "XFCE"})]
        for patch in self.patches:
            patch.start()
        self.bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "trayHostTestBus")
        self.host = None
        self.readies = []
        self.leaves = []

    def tearDown(self):
        if (self.host is not None):
            self.host.stop()
        for patch in self.patches:
            patch.stop()
        QDBusConnection.disconnectFromBus("trayHostTestBus")

    def makeHost(self, hostUp):
        with mock.patch.object(trayHost, "hostRegistered", return_value=hostUp):
            self.host = trayHost.TrayHost(self.bus, notify.Notifier(self.bus))
            self.host.ready.connect(lambda: self.readies.append(1))
            self.host.dontOpenAtLogin.connect(lambda: self.leaves.append(1))
            self.host.start()
        return self.host

    def notices(self, host):
        sent = []
        return sent, mock.patch.object(host.notifier, "send", lambda summary, body, actions=(), onAction=None: sent.append((body, list(actions))))

    def testNotReadyUntilAHostIsRegistered(self):
        host = self.makeHost(False)
        self.assertEqual(self.readies, [])
        with mock.patch.object(trayHost, "hostRegistered", return_value=True):
            host.checkHost()
        self.assertEqual(self.readies, [1])

    def testReadyFiresOnlyOnce(self):
        host = self.makeHost(True)
        with mock.patch.object(trayHost, "hostRegistered", return_value=True):
            host.checkHost()
            host.onNoTray()
        self.assertEqual(self.readies, [1])

    def testAFailedHostQueryIsAskedAgain(self):
        with mock.patch.object(trayHost, "HOST_RETRY_MS", 10):
            self.makeHost(False)
        with mock.patch.object(trayHost, "hostRegistered", return_value=True):
            QTest.qWait(200)
        self.assertEqual(self.readies, [1])

    def testHostRetriesBackOffUpToACap(self):
        with mock.patch.object(trayHost, "HOST_RETRY_MS", 10), mock.patch.object(trayHost, "HOST_RETRY_MAX_MS", 40):
            host = self.makeHost(False)
            intervals = [host.hostTimer.interval()]
            with mock.patch.object(trayHost, "hostRegistered", return_value=False):
                for i in range(3):
                    host.checkHost()
                    intervals.append(host.hostTimer.interval())
        self.assertEqual(intervals, [10, 20, 40, 40])

    def testWatcherSignalsStartTheBackoffOver(self):
        with mock.patch.object(trayHost, "HOST_RETRY_MS", 10), mock.patch.object(trayHost, "HOST_RETRY_MAX_MS", 40):
            host = self.makeHost(False)
            with mock.patch.object(trayHost, "hostRegistered", return_value=False):
                for i in range(3):
                    host.checkHost()
                host.hostWatcher.serviceRegistered.emit(trayHost.WATCHER_SERVICE)
                afterWatcher = host.hostTimer.interval()
                for i in range(3):
                    host.checkHost()
                host.onHostRegistered(QDBusMessage())
                afterHost = host.hostTimer.interval()
        self.assertEqual((afterWatcher, afterHost), (10, 10))

    def testStopEndsTheHostAndNoticeTimers(self):
        host = self.makeHost(False)
        self.assertEqual((host.hostTimer.isActive(), host.noticeTimer.isActive()), (True, True))
        host.stop()
        self.assertEqual((host.hostTimer.isActive(), host.noticeTimer.isActive()), (False, False))

    def testTheNoTrayNoticeAsksForTheHostFirst(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "hostRegistered", return_value=True):
            host.onNoTray()
        self.assertEqual((self.readies, sent), ([1], []))

    def testNoTrayNoticeOnGnomeOffersTheExtension(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "nameHasOwner", return_value=True), mock.patch.object(trayHost, "installedExtension", return_value="ubuntu-appindicators@ubuntu.com"):
            host.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs the AppIndicator extension to show in the top bar.", [("turnOn", "Turn On"), ("dontOpen", "Don't Open at Login")])])

    def testNoTrayNoticeOnGnomeFlashbackOffersOnlyTheWayOut(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "nameHasOwner", return_value=True), mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME-Flashback:GNOME"}):
            host.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.", [("dontOpen", "Don't Open at Login")])])

    def testNoTrayNoticeWithoutTheExtensionLinksToIt(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "nameHasOwner", return_value=True), mock.patch.object(trayHost, "installedExtension", return_value=None):
            host.onNoTray()
        self.assertEqual(sent[0][1], [("getExtension", "Get Extension"), ("dontOpen", "Don't Open at Login")])

    def testNoTrayNoticeElsewhereOffersOnlyTheWayOut(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "nameHasOwner", return_value=False):
            host.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.", [("dontOpen", "Don't Open at Login")])])

    def testDontOpenAtLoginIsHandedToTheTray(self):
        host = self.makeHost(False)
        host.onNoticeAction("dontOpen")
        self.assertEqual(self.leaves, [1])

    def testTheHostSignalIsTakenOnlyFromTheWatcher(self):
        bus = RecordingBus()
        with mock.patch.object(trayHost, "QDBusServiceWatcher"), mock.patch.object(trayHost, "hostRegistered", return_value=False):
            host = trayHost.TrayHost(bus, mock.Mock())
            host.start()
        host.stop()
        self.assertEqual(bus.subscriptions, [(trayHost.WATCHER_SERVICE, trayHost.WATCHER_PATH, trayHost.WATCHER_SERVICE, "StatusNotifierHostRegistered")])


if (__name__ == "__main__"):
    unittest.main()
