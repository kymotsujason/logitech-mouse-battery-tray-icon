import contextlib
import errno
import io
import os
import sys
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QObject
from PyQt6.QtDBus import QDBusConnection, QDBusMessage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import dbusCalls
import notify
import trayHost
from tests.fakes.recordingBus import RecordingBus

app = QApplication.instance() or QApplication([])


class BrokenStream(io.StringIO):
    # stderr once the journal's end of it has gone away
    def write(self, text):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


def answering(value):
    return lambda bus, owner, onAnswer: onAnswer(value)


def ownerAnswering(value):
    return lambda bus, owner, name, onAnswer: onAnswer(value)


class RunCommandTests(unittest.TestCase):
    def setUp(self):
        self.owner = QObject()
        self.results = []

    def runAndWait(self, arguments, timeoutMs=5000):
        started = time.monotonic()
        trayHost.runCommand(self.owner, arguments, timeoutMs, lambda code, why: self.results.append((code, why)))
        returned = time.monotonic() - started
        for i in range(400):
            if (self.results):
                break
            QTest.qWait(20)
        return returned

    def testACommandRunsBesideTheTrayAndHandsBackItsExitCode(self):
        returned = self.runAndWait([sys.executable, "-c", "import time, sys; time.sleep(0.5); sys.exit(3)"])
        self.assertLess(returned, 0.3)
        self.assertEqual(self.results, [(3, None)])

    def testACommandPastItsTimeIsStopped(self):
        self.runAndWait([sys.executable, "-c", "import time; time.sleep(30)"], timeoutMs=300)
        self.assertEqual(self.results, [(None, "it didn't finish in time")])

    def testACommandThatCantStartSaysWhy(self):
        self.runAndWait(["/nonexistent/gnome-extensions", "info", "x"])
        self.assertEqual(len(self.results), 1)
        self.assertIsNone(self.results[0][0])
        self.assertTrue(self.results[0][1])


class TrayHostTests(unittest.TestCase):
    def setUp(self):
        # the no tray notice also goes to stderr, which would fill the test output
        self.patches = [mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "XFCE"}), mock.patch.object(sys, "stderr", io.StringIO())]
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
        with mock.patch.object(trayHost, "askHostRegistered", answering(hostUp)):
            self.host = trayHost.TrayHost(self.bus, notify.Notifier(self.bus))
            self.host.ready.connect(lambda: self.readies.append(1))
            self.host.dontOpenAtLogin.connect(lambda: self.leaves.append(1))
            self.host.start()
        return self.host

    def testAnExtensionCheckThatTimesOutMovesOnToTheNext(self):
        answers = [(None, "it didn't finish in time"), (0, None)]
        asked = []
        found = []

        def run(owner, arguments, timeoutMs, onDone):
            asked.append((arguments, timeoutMs))
            onDone(*answers.pop(0))

        with mock.patch.object(trayHost.shutil, "which", return_value="/usr/bin/gnome-extensions"), mock.patch.object(trayHost, "runCommand", run):
            trayHost.findInstalledExtension(QObject(), found.append)
        self.assertEqual(found, [trayHost.EXTENSION_UUIDS[1]])
        self.assertEqual(asked, [(["gnome-extensions", "info", uuid], trayHost.EXTENSION_CHECK_MS) for uuid in trayHost.EXTENSION_UUIDS])

    def testNoGnomeExtensionsCommandFindsNoExtension(self):
        found = []
        with mock.patch.object(trayHost.shutil, "which", return_value=None), mock.patch.object(trayHost, "runCommand") as run:
            trayHost.findInstalledExtension(QObject(), found.append)
        self.assertEqual((found, run.called), ([None], False))

    def testTurnOnThatFailsSaysWhy(self):
        host = self.makeHost(False)
        host.extensionUuid = "ubuntu-appindicators@ubuntu.com"
        errors = io.StringIO()
        with mock.patch.object(trayHost, "runCommand", lambda owner, arguments, timeoutMs, onDone: onDone(2, None)), contextlib.redirect_stderr(errors):
            host.onNoticeAction("turnOn")
        self.assertEqual(errors.getvalue(), "Couldn't turn on ubuntu-appindicators@ubuntu.com: gnome-extensions enable exited with 2\n")

    def testTurnOnThatCantRunSaysWhy(self):
        host = self.makeHost(False)
        host.extensionUuid = "ubuntu-appindicators@ubuntu.com"
        errors = io.StringIO()
        with mock.patch.object(trayHost, "runCommand", lambda owner, arguments, timeoutMs, onDone: onDone(None, "it didn't finish in time")), contextlib.redirect_stderr(errors):
            host.onNoticeAction("turnOn")
        self.assertEqual(errors.getvalue(), "Couldn't turn on ubuntu-appindicators@ubuntu.com: it didn't finish in time\n")

    def notices(self, host):
        sent = []
        return sent, mock.patch.object(host.notifier, "send", lambda summary, body, actions=(), onAction=None, onSent=None: sent.append((body, list(actions))))

    def testNotReadyUntilAHostIsRegistered(self):
        host = self.makeHost(False)
        self.assertEqual(self.readies, [])
        with mock.patch.object(trayHost, "askHostRegistered", answering(True)):
            host.checkHost()
        self.assertEqual(self.readies, [1])

    def testReadyFiresOnlyOnce(self):
        host = self.makeHost(True)
        with mock.patch.object(trayHost, "askHostRegistered", answering(True)):
            host.checkHost()
            host.onNoTray()
        self.assertEqual(self.readies, [1])

    def testAFailedHostQueryIsAskedAgain(self):
        with mock.patch.object(trayHost, "HOST_RETRY_MS", 10):
            self.makeHost(False)
        with mock.patch.object(trayHost, "askHostRegistered", answering(True)):
            QTest.qWait(200)
        self.assertEqual(self.readies, [1])

    def testHostRetriesBackOffUpToACap(self):
        with mock.patch.object(trayHost, "HOST_RETRY_MS", 10), mock.patch.object(trayHost, "HOST_RETRY_MAX_MS", 40):
            host = self.makeHost(False)
            intervals = [host.hostTimer.interval()]
            with mock.patch.object(trayHost, "askHostRegistered", answering(False)):
                for i in range(3):
                    host.checkHost()
                    intervals.append(host.hostTimer.interval())
        self.assertEqual(intervals, [10, 20, 40, 40])

    def testWatcherSignalsStartTheBackoffOver(self):
        with mock.patch.object(trayHost, "HOST_RETRY_MS", 10), mock.patch.object(trayHost, "HOST_RETRY_MAX_MS", 40):
            host = self.makeHost(False)
            with mock.patch.object(trayHost, "askHostRegistered", answering(False)):
                for i in range(3):
                    host.checkHost()
                host.hostWatcher.serviceRegistered.emit(trayHost.WATCHER_SERVICE)
                afterWatcher = host.hostTimer.interval()
                for i in range(3):
                    host.checkHost()
                host.onHostRegistered(QDBusMessage())
                afterHost = host.hostTimer.interval()
        self.assertEqual((afterWatcher, afterHost), (10, 10))

    def testASignalDuringACheckAsksOnceMore(self):
        held = []
        with mock.patch.object(trayHost, "askHostRegistered", lambda bus, owner, onAnswer: held.append(onAnswer)):
            host = trayHost.TrayHost(self.bus, notify.Notifier(self.bus))
            self.host = host
            host.ready.connect(lambda: self.readies.append(1))
            host.checkHost()
            host.checkHost()
            host.onHostRegistered(QDBusMessage())
            self.assertEqual(len(held), 1)
            held[0](False)
            self.assertEqual(len(held), 2)
            held[1](True)
        self.assertEqual(self.readies, [1])

    def testStopEndsTheHostAndNoticeTimers(self):
        host = self.makeHost(False)
        self.assertEqual((host.hostTimer.isActive(), host.noticeTimer.isActive()), (True, True))
        host.stop()
        self.assertEqual((host.hostTimer.isActive(), host.noticeTimer.isActive()), (False, False))

    def testTheNoTrayNoticeAsksForTheHostFirst(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "askHostRegistered", answering(True)):
            host.onNoTray()
        self.assertEqual((self.readies, sent), ([1], []))

    def testNoTrayNoticeOnGnomeOffersTheExtension(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "askNameHasOwner", ownerAnswering(True)), mock.patch.object(trayHost, "findInstalledExtension", lambda owner, onFound: onFound("ubuntu-appindicators@ubuntu.com")), mock.patch.object(trayHost, "askHostRegistered", answering(False)):
            host.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs the AppIndicator extension to show in the top bar.", [("turnOn", "Turn On"), ("dontOpen", "Don't Open at Login")])])

    def testNoTrayNoticeOnGnomeFlashbackOffersOnlyTheWayOut(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "askNameHasOwner", ownerAnswering(True)), mock.patch.object(trayHost, "askHostRegistered", answering(False)), mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME-Flashback:GNOME"}):
            host.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.", [("dontOpen", "Don't Open at Login")])])

    def testNoTrayNoticeWithoutTheExtensionLinksToIt(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "askNameHasOwner", ownerAnswering(True)), mock.patch.object(trayHost, "findInstalledExtension", lambda owner, onFound: onFound(None)), mock.patch.object(trayHost, "askHostRegistered", answering(False)):
            host.onNoTray()
        self.assertEqual(sent[0][1], [("getExtension", "Get Extension"), ("dontOpen", "Don't Open at Login")])

    def testNoTrayNoticeElsewhereOffersOnlyTheWayOut(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "askNameHasOwner", ownerAnswering(False)), mock.patch.object(trayHost, "askHostRegistered", answering(False)):
            host.onNoTray()
        self.assertEqual(sent, [("Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.", [("dontOpen", "Don't Open at Login")])])

    def testAHostThatShowsUpWhileTheExtensionIsLookedForStopsTheNotice(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        found = []
        with patch, mock.patch.object(trayHost, "askNameHasOwner", ownerAnswering(True)), mock.patch.object(trayHost, "findInstalledExtension", lambda owner, onFound: found.append(onFound)), mock.patch.object(trayHost, "askHostRegistered", answering(False)):
            host.onNoTray()
        with mock.patch.object(trayHost, "askHostRegistered", answering(True)):
            host.checkHost()
        found[0]("ubuntu-appindicators@ubuntu.com")
        self.assertEqual((self.readies, sent), ([1], []))

    def testTheNoTrayNoticeIsLoggedAndSoIsItsFailure(self):
        host = self.makeHost(False)
        errors = io.StringIO()

        def failingSend(summary, body, actions=(), onAction=None, onSent=None):
            onSent(None, "org.freedesktop.DBus.Error.ServiceUnknown")
        with mock.patch.object(host.notifier, "send", failingSend), mock.patch.object(trayHost, "askNameHasOwner", ownerAnswering(False)), mock.patch.object(trayHost, "askHostRegistered", answering(False)), contextlib.redirect_stderr(errors):
            host.onNoTray()
        self.assertEqual(errors.getvalue(), "No system tray found: Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.\nCouldn't send the no tray notice: org.freedesktop.DBus.Error.ServiceUnknown\n")

    def testTheNoTrayNoticeGoesOutWhenItsPrintFails(self):
        host = self.makeHost(False)
        sent, patch = self.notices(host)
        with patch, mock.patch.object(trayHost, "askNameHasOwner", ownerAnswering(False)), mock.patch.object(trayHost, "askHostRegistered", answering(False)), mock.patch.object(sys, "stderr", BrokenStream()):
            host.onNoTray()
        self.assertEqual(len(sent), 1)

    def testDontOpenAtLoginIsHandedToTheTray(self):
        host = self.makeHost(False)
        host.onNoticeAction("dontOpen")
        self.assertEqual(self.leaves, [1])

    def testTheHostCheckAndTheNoTrayNoticeMakeNoBlockingCall(self):
        blocking = []
        realCall = dbusCalls.call
        with mock.patch.object(dbusCalls, "call", lambda bus, message, *args: blocking.append(message.member()) or realCall(bus, message, *args)), mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}):
            self.host = trayHost.TrayHost(self.bus, notify.Notifier(self.bus))
            self.host.start()
            self.host.onNoTray()
            QTest.qWait(100)
        self.assertEqual(blocking, [])

    def testTheHostSignalIsTakenOnlyFromTheWatcher(self):
        bus = RecordingBus()
        with mock.patch.object(trayHost, "QDBusServiceWatcher"), mock.patch.object(trayHost, "askHostRegistered", answering(False)):
            host = trayHost.TrayHost(bus, mock.Mock())
            host.start()
        host.stop()
        self.assertEqual(bus.subscriptions, [(trayHost.WATCHER_SERVICE, trayHost.WATCHER_PATH, trayHost.WATCHER_SERVICE, "StatusNotifierHostRegistered")])


if (__name__ == "__main__"):
    unittest.main()
