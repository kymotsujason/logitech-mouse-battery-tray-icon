import errno
import io
import json
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

import health
import hidpp
import history
import mice
import serviceClient
import serviceState
import version
from tests import testHistory, testReplay
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
        # long enough that three refusals and the switch to a good answer all land inside one refill wait on a loaded machine
        refillSeconds = 4
        with mock.patch.object(serviceState, "READ_ALL_REFILL_SECONDS", refillSeconds):
            self.startWatcher()
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 1))
            self.watcher.readAll()
            self.watcher.readAll()
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 3))
            QTest.qWait(200)
            self.assertTrue(self.watcher.readRetryTimer.isActive())
            self.setAnswer(None)
            # three refusals inside one refill wait come back as a single read
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 4, timeout=refillSeconds + 3.0))
            QTest.qWait(int(refillSeconds * 1000) + 500)
        self.assertEqual(self.privateBus.lines(log).count("ReadAll"), 4)
        self.assertEqual(self.errorLines(), [])

    def setMethodAnswer(self, method, answer):
        with open(self.stateFile + ".answer." + method, "w") as f:
            f.write(answer)

    def testAReadSilentReachesTheService(self):
        self.privateBus = PrivateBus()
        log = self.startFake()
        self.startWatcher()
        self.watcher.readSilent()
        self.assertTrue(waitUntil(lambda: "ReadSilent" in self.privateBus.lines(log)))

    def testAReadSilentTheServiceDoesntHaveGoesOutAsAReadAll(self):
        self.privateBus = PrivateBus()
        self.setMethodAnswer("ReadSilent", serviceState.UNKNOWN_METHOD)
        log = self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 1))
        self.watcher.readSilent()
        self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 2))
        self.assertEqual(self.errorLines(), [])

    def testAReadSilentTheBusTurnsAwayGoesOutAsAReadAll(self):
        self.privateBus = PrivateBus()
        self.setMethodAnswer("ReadSilent", serviceState.ACCESS_DENIED)
        log = self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 1))
        self.watcher.readSilent()
        self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 2))

    def testARefusedReadSilentIsAskedAgainAsAReadSilent(self):
        self.privateBus = PrivateBus()
        log = self.startFake()
        refillSeconds = 2
        with mock.patch.object(serviceState, "READ_ALL_REFILL_SECONDS", refillSeconds):
            self.startWatcher()
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 1))
            self.setMethodAnswer("ReadSilent", serviceState.READ_ALL_LIMITED)
            self.watcher.readSilent()
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadSilent") == 1))
            os.remove(self.stateFile + ".answer.ReadSilent")
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadSilent") == 2, timeout=refillSeconds + 3.0))
        self.assertEqual(self.privateBus.lines(log).count("ReadAll"), 1)

    def testARefusedReadAllWinsOverARefusedReadSilent(self):
        self.privateBus = PrivateBus()
        self.setAnswer(serviceState.READ_ALL_LIMITED)
        log = self.startFake()
        refillSeconds = 2
        with mock.patch.object(serviceState, "READ_ALL_REFILL_SECONDS", refillSeconds):
            self.startWatcher()
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 1))
            self.watcher.readSilent()
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadSilent") == 1))
            self.setAnswer(None)
            self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("ReadAll") == 2, timeout=refillSeconds + 3.0))
        self.assertEqual(self.privateBus.lines(log).count("ReadSilent"), 1)

    def testGetHistoryHandsOverTheDecodedSummary(self):
        self.privateBus = PrivateBus()
        with open(self.stateFile + ".history", "w") as f:
            f.write('{"version": 1, "key": "02bc524c", "sessions": []}')
        self.startFake()
        self.startWatcher()
        answers = []
        self.watcher.getHistory("02bc524c", lambda summary, error: answers.append((summary, error)))
        self.assertTrue(waitUntil(lambda: answers))
        self.assertEqual(answers, [({"version": 1, "key": "02bc524c", "sessions": []}, None)])

    def testGetHistoryHandsOverTheErrorName(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        answers = []
        self.watcher.getHistory("02bc524c", lambda summary, error: answers.append((summary, error)))
        self.assertTrue(waitUntil(lambda: answers))
        self.assertEqual(answers, [(None, serviceState.HISTORY_UNAVAILABLE)])

    def testAHistoryTheServiceNeverAnswersTimesOut(self):
        self.privateBus = PrivateBus()
        self.setMethodAnswer("GetHistory", "hang")
        self.startFake()
        self.startWatcher()
        answers = []
        with mock.patch.object(serviceClient, "HISTORY_TIMEOUT_MS", 300):
            self.watcher.getHistory("02bc524c", lambda summary, error: answers.append((summary, error)))
            self.assertTrue(waitUntil(lambda: answers, timeout=1.2))
        self.assertEqual(answers, [(None, "org.freedesktop.DBus.Error.NoReply")])

    def testLevelsTheServiceNeverAnswersTimeOut(self):
        self.privateBus = PrivateBus()
        self.setMethodAnswer("GetLevels", "hang")
        self.startFake()
        self.startWatcher()
        answers = []
        with mock.patch.object(serviceClient, "HISTORY_TIMEOUT_MS", 300):
            self.watcher.getLevels("02bc524c", "year", lambda levels, error: answers.append((levels, error)))
            self.assertTrue(waitUntil(lambda: answers, timeout=1.2))
        self.assertEqual(answers, [(None, "org.freedesktop.DBus.Error.NoReply")])

    def testAHistoryForAnotherKeyDoesntDecode(self):
        self.privateBus = PrivateBus()
        with open(self.stateFile + ".history", "w") as f:
            f.write('{"version": 1, "key": "ffffffff"}')
        self.startFake()
        self.startWatcher()
        answers = []
        self.watcher.getHistory("02bc524c", lambda summary, error: answers.append((summary, error)))
        self.assertTrue(waitUntil(lambda: answers))
        self.assertEqual(answers, [(None, serviceClient.UNDECODABLE)])

    def askLevels(self, text, rangeName="year"):
        self.privateBus = PrivateBus()
        if (text is not None):
            with open(self.stateFile + ".levels", "w") as f:
                f.write(text)
        self.startFake()
        self.startWatcher()
        answers = []
        self.watcher.getLevels("02bc524c", rangeName, lambda levels, error: answers.append((levels, error)))
        self.assertTrue(waitUntil(lambda: answers))
        return answers

    def testGetLevelsHandsOverTheDecodedSlots(self):
        answers = self.askLevels('{"version": 1, "key": "02bc524c", "range": "year", "slots": [[1, 20, 90, 2]]}')
        self.assertEqual(answers, [({"version": 1, "key": "02bc524c", "range": "year", "slots": [[1, 20, 90, 2]]}, None)])

    def testGetLevelsHandsOverTheErrorName(self):
        self.assertEqual(self.askLevels(None), [(None, serviceState.HISTORY_UNAVAILABLE)])

    def testLevelsForAnotherRangeDontDecode(self):
        self.assertEqual(self.askLevels('{"version": 1, "key": "02bc524c", "range": "all", "slots": []}'), [(None, serviceClient.UNDECODABLE)])

    def testLevelsForAnotherKeyDontDecode(self):
        self.assertEqual(self.askLevels('{"version": 1, "key": "ffffffff", "range": "year", "slots": []}'), [(None, serviceClient.UNDECODABLE)])

    def testLevelsWithoutASlotListDontDecode(self):
        self.assertEqual(self.askLevels('{"version": 1, "key": "02bc524c", "range": "year", "slots": 4}'), [(None, serviceClient.UNDECODABLE)])

    def testANewOwnerSaysTheServiceRestarted(self):
        self.privateBus = PrivateBus()
        self.startFake()
        self.startWatcher()
        restarts = []
        self.watcher.serviceRestarted.connect(lambda: restarts.append(1))
        self.assertTrue(waitUntil(lambda: self.goodStates()))
        self.stopFake()
        self.startFake()
        self.assertTrue(waitUntil(lambda: restarts))

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
        # a ReadAll that goes through ends the trouble, so the next refusal is printed again
        self.setAnswer(None)
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: not self.watcher.readProblems))
        self.setAnswer(serviceState.CALLER_UNKNOWN)
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: self.privateBus.lines(log).count("GetState") == before + 3))
        refused = "Couldn't ask the battery service for a fresh reading: " + serviceState.CALLER_UNKNOWN + ": refused by the fake"
        self.assertEqual(self.errorLines(), [refused, "Asked the battery service for a fresh reading again", refused])

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

    def testTheTimedReadDoesntStartAServiceThatFailedToStart(self):
        self.startActivatableBus()
        self.startWatcher()
        mouseList = mice.MouseList()
        self.addCleanup(mouseList.stop)
        # wired the way the tray wires it
        mouseList.readRequested.connect(self.watcher.readAll)
        self.assertTrue(waitUntil(lambda: None in self.states, timeout=15.0))
        mouseList.readTimer.start(50)
        self.assertTrue(waitUntil(lambda: self.watcher.readProblems, timeout=15.0))
        QTest.qWait(500)
        mouseList.readTimer.stop()
        # each 300 s read of every tray would start a new round of failed starts past the unit's start limit, so only one an hour may start it
        self.assertEqual(len(self.starts()), 1)
        self.assertEqual(self.watcher.readProblems, {"org.freedesktop.DBus.Error.NameHasNoOwner"})

    def testATimedReadStartsAFailedServiceOnceTheSlowWaitPasses(self):
        self.startActivatableBus()
        self.startWatcher()
        with mock.patch.object(mice, "TIMED_START_INTERVAL_S", 0.5):
            mouseList = mice.MouseList()
            self.addCleanup(mouseList.stop)
            mouseList.readRequested.connect(self.watcher.readAll)
            self.assertTrue(waitUntil(lambda: None in self.states, timeout=15.0))
            mouseList.readTimer.start(50)
            # a service that hit its start limit for a cause that passed comes back with no one opening the menu
            self.assertTrue(waitUntil(lambda: self.goodStates(), timeout=15.0))
            mouseList.readTimer.stop()
        self.assertEqual(len(self.starts()), 3)
        self.assertEqual(self.goodStates()[0], serviceState.decodeState(STATE))

    def testTheTraysOwnReadsDontStartTheService(self):
        self.privateBus = PrivateBus()
        self.setAnswer(serviceState.READ_ALL_LIMITED)
        self.startFake()
        calls = []
        realCall = serviceClient.ServiceWatcher.callService

        def recordCall(watcher, member, replySlot, errorSlot, activate=True):
            calls.append((member, activate))
            return realCall(watcher, member, replySlot, errorSlot, activate)

        with mock.patch.object(serviceState, "READ_ALL_REFILL_SECONDS", 0.3), mock.patch.object(serviceClient.ServiceWatcher, "callService", recordCall):
            self.startWatcher()
            self.assertTrue(waitUntil(lambda: [member for member, activate in calls].count("ReadAll") >= 2))
            self.watcher.stop()
        # the start's GetState may start the service, while the read after the first good state and the read after a refill wait are the tray's own
        self.assertEqual(calls[:3], [("GetState", True), ("ReadAll", False), ("ReadAll", False)])

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
        # the tray starts a stopped service on its own only once an hour, so an admin's stop holds until then or until someone asks for a reading
        QTest.qWait(500)
        self.assertEqual(len(self.starts()), 3)
        count = len(self.goodStates())
        self.writeState(LATER)
        self.watcher.readAll()
        self.assertTrue(waitUntil(lambda: len(self.goodStates()) > count, timeout=10.0))
        self.assertEqual(len(self.starts()), 4)
        self.assertEqual(self.goodStates()[-1], serviceState.decodeState(LATER))


class DecodeTests(testHistory.RecorderCase):
    def replyOf(self, payload):
        return QDBusMessage.createMethodCall("a.b", "/a", "a.b", "c").createReply([json.dumps(payload, sort_keys=True)])

    def levelsOf(self, rangeName, slots):
        return {"version": history.LEVELS_VERSION, "key": testHistory.KEY, "range": rangeName, "slots": slots}

    def testASlotOfTheWrongLengthDoesntDecode(self):
        fiveFields = [[testHistory.START, 50, None, None, False]]
        fourFields = [[testHistory.START, 20, 90, 1]]
        for rangeName in history.LEVEL_RANGES:
            right, wrong = (fourFields, fiveFields) if rangeName in history.SPAN_RANGES else (fiveFields, fourFields)
            self.assertIsNotNone(serviceClient.levelsFrom(self.replyOf(self.levelsOf(rangeName, right)), testHistory.KEY, rangeName), rangeName)
            self.assertIsNone(serviceClient.levelsFrom(self.replyOf(self.levelsOf(rangeName, wrong)), testHistory.KEY, rangeName), rangeName)
            self.assertIsNone(serviceClient.levelsFrom(self.replyOf(self.levelsOf(rangeName, right + [None])), testHistory.KEY, rangeName), rangeName)

    def testRealLevelsOfEveryRangeDecode(self):
        self.event(0, 62)
        self.event(3600, 60)
        for rangeName in history.LEVEL_RANGES:
            reply = self.replyOf(self.recorder.levels(testHistory.KEY, rangeName))
            self.assertIsNotNone(serviceClient.levelsFrom(reply, testHistory.KEY, rangeName), rangeName)

    def testARealSummaryDecodesAndIsAssessed(self):
        self.event(0, 62)
        self.event(3600, 60)
        decoded = serviceClient.historyFrom(self.replyOf(self.recorder.summary(testHistory.KEY)), testHistory.KEY)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded["version"], history.HISTORY_VERSION)
        self.assertTrue(health.assess(decoded, hidpp.BatteryReading(60, 0), testHistory.START + 3600).lines)

    def testADecodedSummaryWithSessionsIsAssessedLikeTheRawOne(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        with mock.patch.object(sys, "stderr", io.StringIO()):
            raw = testReplay.Replay(folder).run(14)
        self.assertGreater(len(raw["monthly"]), 0)
        decoded = serviceClient.historyFrom(self.replyOf(raw), testReplay.KEY)
        self.assertIsNotNone(decoded)
        now = testReplay.START + 14 * testReplay.DAY
        reading = hidpp.BatteryReading(60, 0)
        want = health.assess(raw, reading, now)
        got = health.assess(decoded, reading, now)
        self.assertGreater(want.cycles, 0)
        self.assertTrue(want.hoursPerCharge)
        self.assertEqual(got, want)

    def testAnotherHistoryVersionDoesntDecode(self):
        self.event(0, 62)
        summary = self.recorder.summary(testHistory.KEY)
        summary["version"] = history.HISTORY_VERSION + 1
        self.assertIsNone(serviceClient.historyFrom(self.replyOf(summary), testHistory.KEY))

    def testTheVersionsAreTheOnesTheServiceWrites(self):
        self.event(0, 62)
        with mock.patch.object(history, "HISTORY_VERSION", 2), mock.patch.object(history, "LEVELS_VERSION", 3):
            summary = self.recorder.summary(testHistory.KEY)
            levels = self.recorder.levels(testHistory.KEY, "week")
            self.assertEqual((summary["version"], levels["version"]), (2, 3))
            self.assertIsNotNone(serviceClient.historyFrom(self.replyOf(summary), testHistory.KEY))
            self.assertIsNotNone(serviceClient.levelsFrom(self.replyOf(levels), testHistory.KEY, "week"))

    def testAnotherLevelsVersionDoesntDecode(self):
        self.event(0, 62)
        levels = self.recorder.levels(testHistory.KEY, "week")
        levels["version"] = history.LEVELS_VERSION + 1
        self.assertIsNone(serviceClient.levelsFrom(self.replyOf(levels), testHistory.KEY, "week"))


if (__name__ == "__main__"):
    unittest.main()
