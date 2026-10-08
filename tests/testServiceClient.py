import errno
import io
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection, QDBusMessage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import serviceClient
import serviceState
import version
from tests.fakes.privateBus import FAKES, PrivateBus, requireTools

app = QApplication.instance() or QApplication([])
STATE = '{"version": 1, "busy": false, "searching": false, "nodes": ["/dev/hidraw4"], "deniedPaths": [], "mice": [{"key": "02bc524c", "pathKey": false, "name": "PRO X3 SUPERSTRIKE", "source": "/dev/hidraw4", "percent": 53, "charging": 0, "millivolts": null, "level": null, "asleep": false, "lastRead": 1790000000.0, "wokeAt": 1789999000.0}]}'
LATER = STATE.replace('"percent": 53', '"percent": 52').replace("1790000000.0", "1790000060.0")


class BrokenStream(io.StringIO):
    # stderr once the journal's end of it has gone away
    def write(self, text):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


def waitUntil(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while (time.monotonic() < deadline):
        if (condition()):
            return True
        QTest.qWait(10)
    return condition()


class ServiceWatcherTests(unittest.TestCase):
    def setUp(self):
        requireTools(self)
        self.patches = [mock.patch.object(serviceClient, "RETRY_MS", 100), mock.patch.object(serviceClient, "RETRY_MAX_MS", 400)]
        for patch in self.patches:
            patch.start()
        self.work = tempfile.mkdtemp()
        self.stateFile = os.path.join(self.work, "state")
        self.startsFile = os.path.join(self.work, "starts")
        self.writeState(STATE)
        self.privateBus = None
        self.names = []
        self.states = []
        self.watcher = None
        self.captureErrors()

    def tearDown(self):
        if (self.watcher is not None):
            self.watcher.stop()
        for name in self.names:
            QDBusConnection.disconnectFromBus(name)
        if (self.privateBus is not None):
            self.privateBus.close()
        if (os.path.exists(self.startsFile)):
            with open(self.startsFile) as f:
                for line in f.read().splitlines():
                    try:
                        os.kill(int(line), signal.SIGTERM)
                    except (ProcessLookupError, ValueError):
                        pass
        shutil.rmtree(self.work, ignore_errors=True)
        for patch in self.patches:
            patch.stop()

    def writeState(self, text):
        # one rename, so the fake never reads half a file
        with open(self.stateFile + ".new", "w") as f:
            f.write(text)
        os.replace(self.stateFile + ".new", self.stateFile)

    def setAnswer(self, answer):
        # the fake reads this on every call, so a test can turn ReadAll down or stop answering at any point
        if (answer is None):
            os.remove(self.stateFile + ".answer")
            return
        with open(self.stateFile + ".answer", "w") as f:
            f.write(answer)

    def connectTo(self, name):
        self.names.append(name + str(id(self)))
        return QDBusConnection.connectToBus(self.privateBus.address, self.names[-1])

    def startWatcher(self):
        self.watcher = serviceClient.ServiceWatcher(self.connectTo("tray"))
        self.watcher.stateChanged.connect(self.states.append)
        self.watcher.start()

    def startFake(self):
        log = self.privateBus.startFake("fakeService.py", self.stateFile)
        self.assertTrue(self.privateBus.waitForLine(log, "owns the service name"))
        return log

    def stopFake(self):
        process = self.privateBus.fakes[-1][0]
        process.terminate()
        process.wait(5)

    def goodStates(self):
        return [state for state in self.states if state is not None]

    def testTheFirstGetStateReachesTheTray(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.assertEqual(self.goodStates()[0], serviceState.decodeState(STATE))

    def testAStateChangeReachesTheTray(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.writeState(LATER)
        self.assertTrue(waitUntil(lambda: self.goodStates()[-1] == serviceState.decodeState(LATER)))

    def testASignalFromAnotherConnectionIsDropped(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        forged = QDBusMessage.createSignal(serviceState.OBJECT_PATH, serviceState.INTERFACE, "StateChanged")
        forged.setArguments([LATER])
        self.assertTrue(self.connectTo("forger").send(forged))
        QTest.qWait(300)
        self.assertNotIn(serviceState.decodeState(LATER), self.states)

    def testTheServiceGoingAwaySendsNone(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.stopFake()
        self.assertTrue(waitUntil(lambda: self.states[-1] is None))

    def testANewOwnerIsAskedAtOnceAndFollowed(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.stopFake()
        self.writeState(LATER)
        self.startFake()
        self.assertTrue(waitUntil(lambda: self.goodStates()[-1] == serviceState.decodeState(LATER)))
        self.writeState(STATE)
        self.assertTrue(waitUntil(lambda: self.goodStates()[-1] == serviceState.decodeState(STATE)))

    def testAServiceThatStartsAfterTheTraySubscribedIsFollowed(self):
        self.privateBus = PrivateBus()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: None in self.states))
        self.startFake()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.writeState(LATER)
        self.assertTrue(waitUntil(lambda: self.goodStates()[-1] == serviceState.decodeState(LATER)))

    def testOneReadAllFollowsTheFirstGoodStateOnly(self):
        self.privateBus = PrivateBus()
        first = self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.privateBus.lines(first).count("ReadAll") == 1))
        self.stopFake()
        # the second fake starts a fresh log at the same path
        second = self.startFake()
        count = len(self.goodStates())
        self.assertTrue(waitUntil(lambda: len(self.goodStates()) > count))
        QTest.qWait(300)
        self.assertEqual(self.privateBus.lines(second).count("ReadAll"), 0)

    def testARefusedReadAllIsAskedAgainOnceAfterTheRefill(self):
        self.privateBus = PrivateBus()
        self.setAnswer(serviceState.READ_ALL_LIMITED)
        log = self.startFake()
        with mock.patch.object(serviceState, "READ_ALL_REFILL_SECONDS", 2):
            self.startWatcher()
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 1))
            self.watcher.readAll()
            self.watcher.readAll()
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 3))
            QTest.qWait(200)
            self.setAnswer(None)
            # three refusals inside one refill wait come back as a single read
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 4, timeout=5.0))
            QTest.qWait(2500)
        self.assertEqual(self.privateBus.lines(log).count("ReadAll"), 4)
        self.assertEqual(self.errorLines(), [])

    def testAReadAllTheServiceDoesntAnswerSendsNone(self):
        self.privateBus = PrivateBus()
        self.startFake()
        with mock.patch.object(serviceClient, "CALL_TIMEOUT_MS", 300):
            self.startWatcher()
            self.assertTrue(waitUntil(lambda: self.goodStates()))
            self.setAnswer("hang")
            self.watcher.readAll()
            self.assertTrue(waitUntil(lambda: self.states[-1] is None))
        self.assertIn("Couldn't ask the battery service for a fresh reading: org.freedesktop.DBus.Error.NoReply", self.errors.getvalue())

    def testAReadAllTurnedDownForAnotherReasonIsPrintedOnceAndTheStateAskedFor(self):
        self.privateBus = PrivateBus()
        log = self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.setAnswer(serviceState.CALLER_UNKNOWN)
        before = self.privateBus.lines(log).count("GetState")
        self.watcher.readAll()
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("GetState") == before + 2))
        self.assertEqual(self.errorLines(), ["Couldn't ask the battery service for a fresh reading: " + serviceState.CALLER_UNKNOWN + ": refused by the fake"])
        self.assertIsNotNone(self.states[-1])

    def testAReadAllThatCantBeSentCountsAsAFailedCall(self):
        self.names.append("deadBus" + str(id(self)))
        self.watcher = serviceClient.ServiceWatcher(QDBusConnection.connectToBus("unix:path=/nonexistent/bus", self.names[-1]))
        self.watcher.stateChanged.connect(self.states.append)
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: self.states == [None], timeout=1.0))
        self.assertTrue(self.watcher.retryTimer.isActive())

    def percents(self):
        return [entry.reading.percent for state in self.goodStates() for entry in state.mice if entry.reading is not None]

    def serviceHolds(self, bus, percent):
        reply = bus.call(QDBusMessage.createMethodCall(serviceState.SERVICE_NAME, serviceState.OBJECT_PATH, serviceState.INTERFACE, "GetState"))
        arguments = reply.arguments()
        state = serviceState.decodeState(arguments[0]) if (arguments and isinstance(arguments[0], str)) else None
        return state is not None and any(entry.reading is not None and entry.reading.percent == percent for entry in state.mice)

    def testTheRealServiceSendsTheTrayAReadsNewPercent(self):
        self.privateBus = PrivateBus()
        percentFile = os.path.join(self.work, "percent")
        with open(percentFile, "w") as f:
            f.write("60")
        with mock.patch.dict(os.environ, {"LIVE_PERCENT_FILE": percentFile}):
            log = self.privateBus.startFake("runService.py")
        self.assertTrue(self.privateBus.waitForLine(log, "Mouse Battery service " + version.VERSION + " started", timeout=15.0))
        # the service has read the mouse before the tray starts, so the first percent comes in GetState's reply and only the second needs the signal
        probe = self.connectTo("probe")
        self.assertTrue(waitUntil(lambda: self.serviceHolds(probe, 60), timeout=15.0))
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: 60 in self.percents()))
        with open(percentFile, "w") as f:
            f.write("42")
        # ReadAll's reply is empty, so the new percent can only reach the tray through StateChanged
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: 42 in self.percents(), timeout=15.0))

    def testAStateThatDoesntDecodeSendsNone(self):
        self.privateBus = PrivateBus()
        self.writeState("not json")
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: None in self.states))
        self.assertEqual(self.goodStates(), [])

    def errorLines(self):
        return self.errors.getvalue().splitlines()

    def captureErrors(self):
        self.errors = io.StringIO()
        patch = mock.patch.object(sys, "stderr", self.errors)
        patch.start()
        self.patches.append(patch)

    def testAnUnreachableServiceIsPrintedOnceAndItsReturnOnce(self):
        self.privateBus = PrivateBus()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.states.count(None) >= 3))
        self.startFake()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        lines = self.errorLines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("Couldn't reach the battery service: org.freedesktop.DBus.Error.ServiceUnknown"), lines[0])
        self.assertEqual(lines[1], "Reached the battery service again")

    def brokenStderr(self):
        hooked = []
        patches = [mock.patch.object(sys, "stderr", BrokenStream()), mock.patch.object(sys, "excepthook", lambda *info: hooked.append(info[1]))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return hooked

    def testAnUnreachableServiceStillSendsNoneAndRetriesWhenItsPrintFails(self):
        self.privateBus = PrivateBus()
        hooked = self.brokenStderr()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.states.count(None) >= 2))
        self.assertEqual(hooked, [])

    def testAServiceThatComesBackReachesTheTrayWhenItsPrintFails(self):
        self.privateBus = PrivateBus()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: None in self.states))
        hooked = self.brokenStderr()
        self.startFake()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.assertEqual((hooked, self.watcher.retryMs), ([], serviceClient.RETRY_MS))

    def testAServiceThatGoesAwaySendsNoneWhenItsPrintFails(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        hooked = self.brokenStderr()
        self.stopFake()
        self.assertTrue(waitUntil(lambda: self.states[-1] is None))
        self.assertEqual(hooked, [])

    def testAStateThatDoesntDecodeSendsNoneAndRetriesWhenItsPrintFails(self):
        self.privateBus = PrivateBus()
        self.writeState("not json")
        self.startFake()
        hooked = self.brokenStderr()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.states.count(None) >= 2))
        self.assertEqual(hooked, [])

    def testAReadAllTurnedDownStillAsksForTheStateWhenItsPrintFails(self):
        self.privateBus = PrivateBus()
        log = self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.setAnswer(serviceState.CALLER_UNKNOWN)
        before = self.privateBus.lines(log).count("GetState")
        hooked = self.brokenStderr()
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("GetState") == before + 1))
        self.assertEqual(hooked, [])

    def testAServiceThatGoesAwayIsPrinted(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.stopFake()
        self.assertTrue(waitUntil(lambda: self.states[-1] is None))
        count = len(self.goodStates())
        self.startFake()
        self.assertTrue(waitUntil(lambda: len(self.goodStates()) > count))
        self.assertEqual(self.errorLines(), ["The battery service went away", "Reached the battery service again"])

    def testAStateThatDoesntDecodeIsPrintedOnce(self):
        self.privateBus = PrivateBus()
        self.writeState("not json")
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.states.count(None) >= 3))
        self.assertEqual(self.errorLines(), ["The battery service sent a state this version can't read"])

    def testACallThatCantBeSentCountsAsAFailedCall(self):
        self.names.append("deadBus" + str(id(self)))
        self.watcher = serviceClient.ServiceWatcher(QDBusConnection.connectToBus("unix:path=/nonexistent/bus", self.names[-1]))
        self.watcher.stateChanged.connect(self.states.append)
        self.watcher.getState()
        self.assertTrue(waitUntil(lambda: self.states == [None], timeout=1.0))
        self.assertTrue(self.watcher.retryTimer.isActive())

    def startActivatableBus(self):
        # the bus starts the fake on the first call to its name, and the fake fails its first two starts
        serviceDir = os.path.join(self.work, "services")
        os.mkdir(serviceDir)
        with open(os.path.join(serviceDir, serviceState.SERVICE_NAME + ".service"), "w") as f:
            f.write("[D-BUS Service]\nName=" + serviceState.SERVICE_NAME + "\nExec=" + sys.executable + " " + os.path.join(FAKES, "fakeService.py") + " starter " + self.stateFile + " " + self.startsFile + "\n")
        self.privateBus = PrivateBus(serviceDir)

    def starts(self):
        with open(self.startsFile) as f:
            return f.read().splitlines()

    def testARetryDoesntStartAServiceThatFailedToStart(self):
        self.startActivatableBus()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.states.count(None) >= 4, timeout=15.0))
        # systemd restarts a failed start on its own up to the unit's limit, and a retry that started it again would go on past that limit for as long as a tray runs
        self.assertEqual(len(self.starts()), 1)
        lines = self.errorLines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("Couldn't reach the battery service: org.freedesktop.DBus.Error.Spawn."), lines[0])
        self.assertTrue(lines[1].startswith("Couldn't reach the battery service: org.freedesktop.DBus.Error.NameHasNoOwner"), lines[1])

    def readUntilTheThirdStart(self):
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: None in self.states, timeout=15.0))
        self.watcher.readAll()
        # the second start writes its pid before it fails, and a read sent before the bus sees it fail joins that start instead of making a third
        self.assertTrue(waitUntil(lambda: self.watcher.readProblems, timeout=15.0))
        self.assertEqual(len(self.starts()), 2)
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: self.goodStates(), timeout=15.0))

    def testAReadStartsAServiceThatFailedToStartAndTheTrayFollowsIt(self):
        self.startActivatableBus()
        self.readUntilTheThirdStart()
        self.assertEqual(len(self.starts()), 3)
        self.assertEqual(self.goodStates()[0], serviceState.decodeState(STATE))

    def testARefusedReadAllWaitingWhenTheServiceGoesAwayIsDropped(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.setAnswer(serviceState.READ_ALL_LIMITED)
        self.watcher.readAll()
        self.assertTrue(waitUntil(self.watcher.readRetryTimer.isActive))
        self.stopFake()
        self.assertTrue(waitUntil(lambda: self.states[-1] is None))
        # the read it waited for would start the stopped service again through activation
        self.assertFalse(self.watcher.readRetryTimer.isActive())

    def testTheNextReadStartsAServiceThatStoppedCleanly(self):
        self.startActivatableBus()
        self.readUntilTheThirdStart()
        # systemd stops the service with SIGTERM, and Restart=on-failure leaves a clean stop down
        os.kill(int(self.starts()[-1]), signal.SIGTERM)
        self.assertTrue(waitUntil(lambda: self.states[-1] is None))
        # the tray doesn't start a stopped service on its own, so an admin's stop holds until someone asks for a reading
        QTest.qWait(500)
        self.assertEqual(len(self.starts()), 3)
        count = len(self.goodStates())
        self.writeState(LATER)
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: len(self.goodStates()) > count, timeout=10.0))
        self.assertEqual(len(self.starts()), 4)
        self.assertEqual(self.goodStates()[-1], serviceState.decodeState(LATER))


if (__name__ == "__main__"):
    unittest.main()
