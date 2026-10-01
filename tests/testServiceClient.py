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
from tests.fakes.privateBus import FAKES, PrivateBus, requireTools

app = QApplication.instance() or QApplication([])
STATE = '{"version": 1, "busy": false, "searching": false, "nodes": ["/dev/hidraw4"], "deniedPaths": [], "mice": [{"key": "02bc524c", "pathKey": false, "name": "PRO X3 SUPERSTRIKE", "source": "/dev/hidraw4", "percent": 53, "charging": 0, "millivolts": null, "level": null, "asleep": false, "lastRead": 1790000000.0, "wokeAt": 1789999000.0}]}'
LATER = STATE.replace('"percent": 53', '"percent": 52').replace("1790000000.0", "1790000060.0")


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

    def testAStateThatDoesntDecodeSendsNone(self):
        self.privateBus = PrivateBus()
        self.writeState("not json")
        self.startFake()
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: None in self.states))
        self.assertEqual(self.goodStates(), [])

    def testACallThatCantBeSentCountsAsAFailedCall(self):
        self.names.append("deadBus" + str(id(self)))
        self.watcher = serviceClient.ServiceWatcher(QDBusConnection.connectToBus("unix:path=/nonexistent/bus", self.names[-1]))
        self.watcher.stateChanged.connect(self.states.append)
        self.watcher.getState()
        self.assertTrue(waitUntil(lambda: self.states == [None], timeout=1.0))
        self.assertTrue(self.watcher.retryTimer.isActive())

    def testTheRetryBackoffReachesAServiceThatStartsOnTheThirdTry(self):
        serviceDir = os.path.join(self.work, "services")
        os.mkdir(serviceDir)
        with open(os.path.join(serviceDir, serviceState.SERVICE_NAME + ".service"), "w") as f:
            f.write("[D-BUS Service]\nName=" + serviceState.SERVICE_NAME + "\nExec=" + sys.executable + " " + os.path.join(FAKES, "fakeService.py") + " starter " + self.stateFile + " " + self.startsFile + "\n")
        self.privateBus = PrivateBus(serviceDir)
        self.startWatcher()
        self.assertTrue(waitUntil(lambda: self.goodStates(), timeout=15.0))
        with open(self.startsFile) as f:
            self.assertEqual(len(f.read().splitlines()), 3)
        self.assertEqual(self.states[:2], [None, None])
        self.assertEqual(self.goodStates()[0], serviceState.decodeState(STATE))


if (__name__ == "__main__"):
    unittest.main()
