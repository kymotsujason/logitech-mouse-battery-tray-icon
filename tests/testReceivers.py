import errno
import io
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import hidpp
import history
import receivers
from tests.fakes.fakeDevice import NAME, FakeDevice, FakeNodes, mouseHandler, reply

app = QApplication.instance() or QApplication([])
FEATURE = hidpp.BatteryFeature(1, hidpp.FEATURE_UNIFIED_BATTERY, 5, True)
PATH = "/dev/hidraw18"


class BrokenStream(io.StringIO):
    # stderr once the journal's end of it has gone away
    def write(self, text):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


def nextMessage(messages, kind, timeout=2.0):
    deadline = time.monotonic() + timeout
    while (time.monotonic() < deadline):
        try:
            message = messages.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            break
        if (message[0] == kind):
            return message
    return None


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.messages = queue.Queue()
        self.device = None
        self.worker = None
        self.patch = mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.1)
        self.patch.start()

    def tearDown(self):
        if (self.worker is not None):
            self.worker.stop()
        self.patch.stop()
        if (self.device is not None):
            self.device.close()

    def startWorker(self, handler):
        self.device = FakeDevice(handler)
        self.worker = receivers.NodeWorker(PATH, self.device.node(), self.messages.put)
        self.worker.start()

    def testQueuedWorkRunsWhileTheDeviceIsSilent(self):
        self.startWorker(mouseHandler())
        time.sleep(0.2)
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        message = nextMessage(self.messages, "read")
        self.assertIs(message[1], self.worker)
        self.assertEqual(message[2:], (1, (hidpp.ANSWER, hidpp.BatteryReading(81, 0, power=0)), 0.0))

    def testSearchResultIsPosted(self):
        self.startWorker(mouseHandler())
        self.worker.queueWork(("search", frozenset(), 0.0))
        self.assertEqual(nextMessage(self.messages, "searched")[2].mice[0].unitId, "02bc524c")

    def testIdentifyResultIsPosted(self):
        self.startWorker(mouseHandler())
        self.worker.queueWork(("identify", 1, 0.0))
        self.assertEqual(nextMessage(self.messages, "identified")[3][0], hidpp.FOUND)

    def testReportsArePosted(self):
        self.startWorker(mouseHandler())
        event = bytes([0x11, 1, 5, 0x00, 42, 0x04, 0, 0]).ljust(20, b"\x00")
        self.device.send(event)
        self.assertEqual(nextMessage(self.messages, "report")[:3], ("report", self.worker, event))

    def testRepliesAndShortReportsStayOnTheWorker(self):
        self.startWorker(mouseHandler())
        # software id 0x0A, so it answers some other program's request
        answer = bytearray(OTHER_REPORT)
        answer[3] = 0x0A
        event = bytes([0x11, 1, 5, 0x00, 42, 0x04, 0, 0]).ljust(20, b"\x00")
        self.worker.onEvent(bytes([0x10, 1, 0x41]))
        self.device.send(bytes(answer))
        self.device.send(event)
        self.assertEqual(nextMessage(self.messages, "report")[:3], ("report", self.worker, event))

    def testExceptionInAWorkItemKeepsTheThreadRunning(self):
        self.startWorker(mouseHandler())
        with mock.patch.object(receivers.traceback, "print_exc") as printed, mock.patch.object(sys, "stderr", io.StringIO()):
            self.worker.queueWork(("bogus",))
            self.assertEqual(nextMessage(self.messages, "failed"), ("failed", self.worker, ("bogus",)))
        self.assertTrue(printed.called)
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.assertIsNotNone(nextMessage(self.messages, "read"))

    def testAFailedItemPrintsItsKindAndNodeBeforeTheTraceback(self):
        self.startWorker(mouseHandler())
        errors = io.StringIO()
        with mock.patch.object(sys, "stderr", errors):
            self.worker.queueWork(("bogus",))
            self.assertIsNotNone(nextMessage(self.messages, "failed"))
        lines = errors.getvalue().splitlines()
        self.assertEqual(lines[0], "Error running bogus on " + PATH + ":")
        self.assertIn("ValueError: unknown work item 'bogus'", lines)

    def testUnplugPostsLost(self):
        self.startWorker(mouseHandler())
        self.device.unplug()
        self.assertEqual(nextMessage(self.messages, "lost")[:2], ("lost", self.worker))

    def failWrites(self, count, code):
        realWrite = self.worker.node.writeReport
        failed = []

        def flakyWrite(*args):
            if (len(failed) < count):
                failed.append(1)
                raise OSError(code, os.strerror(code))
            return realWrite(*args)
        self.worker.node.writeReport = flakyWrite

    def testAWriteTurnedAwayFailsOnlyItsItem(self):
        self.startWorker(mouseHandler())
        self.failWrites(1, errno.ETIMEDOUT)
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        message = nextMessage(self.messages, "failed")
        self.assertEqual((message[2], message[3].errno), (("read", 1, FEATURE, 0.0), errno.ETIMEDOUT))
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.assertEqual(nextMessage(self.messages, "read")[3], (hidpp.ANSWER, hidpp.BatteryReading(81, 0, power=0)))

    def testWritesTurnedAwayThreeTimesInARowLoseTheNode(self):
        self.startWorker(mouseHandler())
        self.failWrites(3, errno.EPIPE)
        for i in range(3):
            self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.assertEqual(nextMessage(self.messages, "lost")[2].errno, errno.EPIPE)

    def testAnItemThatWorksStartsTheCountOver(self):
        self.startWorker(mouseHandler())
        realWrite = self.worker.node.writeReport
        # each read is one write, so this fails two reads, lets one through, and fails two more
        outcomes = [False, False, True, False, False, True]

        def patternedWrite(*args):
            if (not outcomes.pop(0)):
                raise OSError(errno.EPIPE, "Broken pipe")
            return realWrite(*args)
        self.worker.node.writeReport = patternedWrite
        kinds = []
        for i in range(6):
            self.worker.queueWork(("read", 1, FEATURE, 0.0))
            kinds.append(self.messages.get(timeout=2.0)[0])
        self.assertEqual(kinds, ["failed", "failed", "read", "failed", "failed", "read"])

    def testAWriteToAGoneDeviceLosesTheNodeAtOnce(self):
        self.startWorker(mouseHandler())
        self.failWrites(1, errno.ENODEV)
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.assertEqual(nextMessage(self.messages, "lost")[2].errno, errno.ENODEV)

    def testStopEndsTheThreadAndClosesThePipe(self):
        self.startWorker(mouseHandler())
        self.worker.stop()
        self.assertFalse(self.worker.is_alive())
        self.assertIsNone(self.worker.wakeRead)
        self.worker = None

    def testAnUnexpectedErrorInTheLoopPostsLost(self):
        self.startWorker(mouseHandler())
        errors = io.StringIO()
        with mock.patch.object(sys, "stderr", errors), mock.patch.object(self.worker.node, "readReport", side_effect=ValueError("bad")):
            self.device.send(bytes([0x11, 1, 9, 0x00, 1, 2, 3]).ljust(20, b"\x00"))
            message = nextMessage(self.messages, "lost")
        self.assertEqual((message[1], type(message[2])), (self.worker, ValueError))
        lines = errors.getvalue().splitlines()
        self.assertEqual((lines[0], lines[-1]), ("Error reading " + PATH + ":", "ValueError: bad"))

    def testALoopErrorWithABrokenStderrStillPostsLost(self):
        self.startWorker(mouseHandler())
        with mock.patch.object(sys, "stderr", BrokenStream()), mock.patch.object(self.worker.node, "readReport", side_effect=ValueError("bad")):
            self.device.send(OTHER_REPORT)
            message = nextMessage(self.messages, "lost")
        self.assertIsNotNone(message)

    def testAStopThatGivesUpWaitingStillClosesThePipeLater(self):
        release = threading.Event()
        entered = threading.Event()

        # a read stuck where no stop reaches it
        def stuckRead(node, feature, deadline=None):
            entered.set()
            release.wait(2.0)
            return (hidpp.TIMEOUT, None)

        with mock.patch.object(hidpp, "readBattery", stuckRead):
            self.startWorker(silent)
            self.worker.queueWork(("read", 1, FEATURE, 0.0))
            self.assertTrue(entered.wait(2.0))
            self.worker.stop(timeout=0.01)
            self.assertTrue(self.worker.is_alive())
            self.worker.wake()
            release.set()
            self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertIsNone(self.worker.wakeRead)
        # a wake after the thread closed its pipe does nothing
        self.worker.wake()
        self.worker = None

    def testAStopCutsShortARequestInFlight(self):
        with mock.patch.object(hidpp, "REQUEST_TIMEOUT", 3.0):
            self.startWorker(pingOnly)
            self.worker.queueWork(("search", frozenset(), 0.0))
            # the ping was answered, so the search now waits on the slot's first identify request
            self.assertTrue(waitUntil(lambda: any(sent[2] == 0 and (sent[3] >> 4) == 0 for sent in list(self.device.requests))))
            started = time.monotonic()
            self.worker.stop(timeout=5.0)
            took = time.monotonic() - started
        self.assertFalse(self.worker.is_alive())
        self.assertLess(took, 0.5)

    def testAStopCutsShortThePingWait(self):
        with mock.patch.object(hidpp, "PING_TIMEOUT", 3.0):
            self.startWorker(silent)
            self.worker.queueWork(("search", frozenset(), 0.0))
            self.assertTrue(waitUntil(lambda: len(self.device.requests) == len(hidpp.DEVICE_SLOTS)))
            started = time.monotonic()
            self.worker.stop(timeout=5.0)
            took = time.monotonic() - started
        self.assertFalse(self.worker.is_alive())
        self.assertLess(took, 0.5)

    def testWorkQueuedDuringARequestLetsItFinish(self):
        def slowBattery(request):
            if (request[2] == 5):
                time.sleep(0.2)
            return mouseHandler()(request)

        with mock.patch.object(hidpp, "REQUEST_TIMEOUT", 1.0):
            self.startWorker(slowBattery)
            self.worker.queueWork(("read", 1, FEATURE, 0.0))
            # the second read's wake lands while the first read waits for its answer
            self.assertTrue(waitUntil(lambda: batteryReads(self.device) == 1))
            self.worker.queueWork(("read", 1, FEATURE, 1.0))
            first = nextMessage(self.messages, "read")
            second = nextMessage(self.messages, "read")
        self.assertEqual((first[3], second[3]), ((hidpp.ANSWER, hidpp.BatteryReading(81, 0, power=0)),) * 2)

    def testAReportTakenByARequestDoesntStallTheNextWork(self):
        self.device = FakeDevice(mouseHandler())
        self.worker = receivers.NodeWorker(PATH, self.device.node(), self.messages.put)
        # the report and the wake both wait at the first select, and the queued read takes the report off the node before the loop reads it
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.device.send(OTHER_REPORT)
        self.worker.start()
        self.assertIsNotNone(nextMessage(self.messages, "read"))
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.assertIsNotNone(nextMessage(self.messages, "read"))


class NodeSignatureTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.path = os.path.join(self.folder, "hidraw4")
        open(self.path, "w").close()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def testNewTimesKeepTheSignature(self):
        before = receivers.nodeSignature(self.path)
        os.utime(self.path, (1, 1))
        self.assertEqual(receivers.nodeSignature(self.path), before)

    def testANewModeOrANewNodeChangesIt(self):
        before = receivers.nodeSignature(self.path)
        os.chmod(self.path, 0o600)
        changedMode = receivers.nodeSignature(self.path)
        # a second file holds the old inode, so the replacement can't reuse its number
        os.rename(self.path, self.path + ".old")
        open(self.path, "w").close()
        self.assertNotIn(before, (changedMode, receivers.nodeSignature(self.path)))
        self.assertNotEqual(changedMode, receivers.nodeSignature(self.path))

    def testAMissingNodeHasNone(self):
        self.assertIsNone(receivers.nodeSignature(self.path + "x"))


RECEIVER = "/dev/hidraw18"
CABLE = "/dev/hidraw30"
OTHER_REPORT = bytes([0x11, 1, 9, 0x00, 1, 2, 3]).ljust(20, b"\x00")
LINK_UP = bytes([0x10, 1, 0x41, 0x10, 0x00, 0x00, 0x00])


def silent(request):
    return []


def pingOnly(request):
    if (request[1] == 1 and request[2] == 0 and (request[3] >> 4) == 1):
        return [reply(request, [4, 2, request[6]])]
    return []


def waitUntil(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while (time.monotonic() < deadline):
        if (condition()):
            return True
        QTest.qWait(10)
    return condition()


def pingsTo(device, slot):
    return sum(1 for sent in list(device.requests) if sent[1] == slot and sent[2] == 0 and (sent[3] >> 4) == 1)


def batteryReads(device, slot=1, featureIndex=5):
    return sum(1 for sent in list(device.requests) if sent[1] == slot and sent[2] == featureIndex and (sent[3] >> 4) == 1)


class ReceiverListTests(unittest.TestCase):
    def setUp(self):
        self.nodes = FakeNodes()
        self.errors = io.StringIO()
        self.patches = [
            mock.patch.object(hidpp, "findHidppNodes", self.nodes.find),
            mock.patch.object(hidpp.HidppNode, "open", self.nodes.open),
            mock.patch.object(receivers, "nodeSignature", self.nodes.signature),
            mock.patch.object(hidpp, "PING_TIMEOUT", 0.2),
            mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.1),
            mock.patch.object(receivers, "WAKE_RETRY_MS", 300),
            mock.patch.object(receivers, "SETTLE_MS", 10),
            mock.patch.object(receivers, "DENIED_RETRY_MS", 100),
            mock.patch.object(receivers, "READ_ALL_GAP_MS", 300),
            mock.patch.object(sys, "stderr", self.errors),
        ]
        for patch in self.patches:
            patch.start()
        self.mice = receivers.ReceiverList()

    def tearDown(self):
        self.mice.stop()
        for patch in self.patches:
            patch.stop()
        self.nodes.closeAll()
        # a list left to the cyclic GC is freed with every other one in a single pass, and that pause can outlast a later test's ping window
        sip.delete(self.mice)

    def keys(self):
        return [mouse.key for mouse in self.mice.mice]

    def mouse(self):
        return self.mice.mice[0] if self.mice.mice else None

    def byKey(self, key):
        return next(mouse for mouse in self.mice.mice if mouse.key == key)

    def testFindsTheMouseAtStartup(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        mouse = self.mouse()
        self.assertEqual((mouse.name, mouse.nodePath, mouse.slot, mouse.reading), (NAME.decode(), RECEIVER, 1, hidpp.BatteryReading(81, 0, power=0)))
        self.assertFalse(mouse.asleep)

    def testAMouseOnASecondReceiverBehindAKeyboard(self):
        self.nodes.add("/dev/hidraw10", mouseHandler(kind=0))
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and self.mice.nodes["/dev/hidraw10"].skipped == {1}))

    def testASkippedKeyboardsReportsDontSearch(self):
        keyboard = self.nodes.add(RECEIVER, mouseHandler(kind=0))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.mice.nodes[RECEIVER].skipped == {1} and not self.mice.nodes[RECEIVER].searching))
        before = pingsTo(keyboard, 2)
        for i in range(5):
            keyboard.send(OTHER_REPORT)
        QTest.qWait(300)
        self.assertEqual(pingsTo(keyboard, 2), before)

    def testOneSearchPerNodeWithOneMoreQueued(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        before = pingsTo(receiver, 2)
        for i in range(5):
            self.mice.requestSearch(RECEIVER)
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        QTest.qWait(200)
        self.assertEqual(pingsTo(receiver, 2) - before, 2)

    def testATimeoutLeavesTheSlotUnknownUntilItsNextReport(self):
        receiver = self.nodes.add(RECEIVER, pingOnly)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        self.assertEqual((self.keys(), self.mice.nodes[RECEIVER].skipped), ([], set()))
        receiver.handler = mouseHandler()
        receiver.send(OTHER_REPORT)
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))

    def testASearchFromAnUnknownSlotRunsOnceMore(self):
        receiver = self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        receiver.send(OTHER_REPORT)
        QTest.qWait(100)
        receiver.handler = mouseHandler()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"], timeout=2.0))

    def testAReportingSlotStillRetriesWhenAnotherSlotIsFound(self):
        first = mouseHandler(unitId=bytes.fromhex("aaaaaaaa"))
        second = [silent]

        def handler(request):
            return first(request) + second[0](request)

        receiver = self.nodes.add(RECEIVER, handler)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["aaaaaaaa"] and not self.mice.nodes[RECEIVER].searching))
        report = bytearray(OTHER_REPORT)
        report[1] = 2
        receiver.send(bytes(report))
        # the search the report starts has to finish against the silent slot before the handler changes, or the retry never gets exercised
        self.assertTrue(waitUntil(lambda: self.mice.nodes[RECEIVER].searching))
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        second[0] = mouseHandler(slot=2, unitId=bytes.fromhex("bbbbbbbb"))
        self.assertTrue(waitUntil(lambda: set(self.keys()) == {"aaaaaaaa", "bbbbbbbb"}, timeout=2.0))

    def testASilentSlotThatKeepsReportingCostsOneSearchPerRetryWait(self):
        with mock.patch.object(receivers, "WAKE_RETRY_MS", 5000):
            receiver = self.nodes.add(RECEIVER, silent)
            self.mice.start()
            self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
            before = pingsTo(receiver, 1)
            for i in range(10):
                receiver.send(OTHER_REPORT)
                QTest.qWait(100)
            self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        self.assertEqual(pingsTo(receiver, 1) - before, 1)

    def testASlotTheSearchSkippedIsntRetried(self):
        self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        state = self.mice.nodes[RECEIVER]
        report = bytearray(OTHER_REPORT)
        report[1] = 2
        self.mice.onReport(state, bytes(report), time.monotonic())
        self.mice.onSearched(state, hidpp.SearchResult((), frozenset(), frozenset({2})), 0.0)
        self.assertTrue(state.retryTimer.isActive())
        state.retryTimer.stop()
        self.mice.onReport(state, OTHER_REPORT, time.monotonic())
        self.mice.onSearched(state, hidpp.SearchResult((), frozenset({1}), frozenset()), 0.0)
        self.assertFalse(state.retryTimer.isActive())

    def testASearchThatSkipsAKnownMousesSlotDropsTheMouse(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        # a keyboard paired into the mouse's slot, and a search sees it before any identify does
        receiver.handler = mouseHandler(kind=0)
        self.mice.requestSearch(RECEIVER)
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))
        self.assertEqual((self.keys(), self.mice.nodes[RECEIVER].skipped), ([], {1}))

    def testReadingAnAsleepMouseWakesIt(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        receiver.handler = silent
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.mouse().asleep))
        receiver.handler = mouseHandler()
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: not self.mouse().asleep))

    def testAWakeSignReadsNowAndOnceMore(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        self.putToSleep(receiver)
        self.wakeOnceAndRetry(receiver, self.mouse())
        self.assertFalse(self.mouse().asleep)

    def wakeOnceAndRetry(self, receiver, mouse):
        before = batteryReads(receiver)
        # a retry that can't fire on its own, so a loaded runner can't spend it on the silent handler, and the test fires it once the handler answers
        with mock.patch.object(receivers, "WAKE_RETRY_MS", 60000):
            receiver.send(OTHER_REPORT)
            self.assertTrue(waitUntil(lambda: batteryReads(receiver) > before and not self.mice.nodes[RECEIVER].pendingReads))
        self.assertTrue(mouse.asleep)
        self.assertTrue(mouse.wakeTimer is not None and mouse.wakeTimer.isActive())
        receiver.handler = mouseHandler()
        mouse.wakeTimer.start(0)
        waitUntil(lambda: not mouse.asleep)

    def testADevChangeFindsANewNodeAndDropsAGoneOne(self):
        self.mice.start()
        self.assertEqual(self.keys(), [])
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.onDevChanged("/dev")
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.nodes.hide(RECEIVER)
        self.mice.onDevChanged("/dev")
        self.assertTrue(waitUntil(lambda: self.keys() == [] and self.mice.nodes == {}))

    def testAScanEmitsOnlyWhenItOpensOrDropsANode(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        changes = []
        self.mice.changed.connect(lambda: changes.append(1))
        self.mice.scanNodes()
        self.assertEqual(changes, [])
        self.nodes.add(CABLE, silent)
        self.mice.scanNodes()
        self.assertEqual(changes, [1])

    def testSteadyDevChurnStillScansWithinTheCeiling(self):
        scans = []

        def countingFind(*args):
            scans.append(1)
            return self.nodes.find()

        with mock.patch.object(receivers, "SETTLE_MS", 300), mock.patch.object(receivers, "SETTLE_CEILING_MS", 600), mock.patch.object(hidpp, "findHidppNodes", countingFind):
            self.mice.settleTimer.setInterval(300)
            self.mice.start()
            scans.clear()
            # a change every 100 ms would restart a plain 300 ms wait forever
            for i in range(20):
                self.mice.onDevChanged("/dev")
                QTest.qWait(100)
        self.assertGreaterEqual(len(scans), 2)

    def testAReplyFromAnUnknownSlotStartsNoSearch(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        before = pingsTo(receiver, 2)
        report = bytearray(OTHER_REPORT)
        report[1] = 2
        # software id 0x0A, so it answers some other program's request
        report[3] = 0x0A
        receiver.send(bytes(report))
        QTest.qWait(300)
        self.assertEqual(pingsTo(receiver, 2), before)

    def testARegisterReplyToAnotherProgramStartsNoSearch(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        before = pingsTo(receiver, 0xFF)
        # another program reading the receiver's register 0x00 and getting an error back on register 0xF0
        receiver.send(bytes([0x10, 0xFF, 0x81, 0x00, 0x00, 0x09, 0x00]))
        receiver.send(bytes([0x10, 0xFF, 0x8F, 0x81, 0xF0, 0x02, 0x00]))
        QTest.qWait(300)
        self.assertEqual(pingsTo(receiver, 0xFF), before)

    def testAReportFromASlotHidppNeverPingsStartsNoSearch(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        before = pingsTo(receiver, 2)
        report = bytearray(OTHER_REPORT)
        report[1] = 9
        for i in range(5):
            receiver.send(bytes(report))
        QTest.qWait(300)
        self.assertEqual(pingsTo(receiver, 2), before)

    def testTenWakeSignsCostOneReadBeforeTheRetry(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        receiver.handler = silent
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.mouse().asleep and not self.mice.nodes[RECEIVER].pendingReads))
        before = batteryReads(receiver)
        # a retry that can't fire during the test, so a slow runner can't count it as a read the signs caused
        with mock.patch.object(receivers, "WAKE_RETRY_MS", 60000):
            for i in range(10):
                receiver.send(OTHER_REPORT)
            self.assertTrue(waitUntil(lambda: batteryReads(receiver) > before and not self.mice.nodes[RECEIVER].pendingReads))
            QTest.qWait(300)
        self.assertEqual(batteryReads(receiver) - before, 1)

    def testABatteryEventUpdatesTheReading(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        receiver.send(bytes([0x11, 1, 5, 0x00, 64, 0x04, 1, 1]).ljust(20, b"\x00"))
        self.assertTrue(waitUntil(lambda: self.mouse().reading == hidpp.BatteryReading(64, 1, power=1)))

    def testALinkUpWithANewUnitIdReplacesTheEntry(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        receiver.handler = mouseHandler(unitId=bytes.fromhex("11223344"))
        receiver.send(LINK_UP)
        self.assertTrue(waitUntil(lambda: self.keys() == ["11223344"]))

    def testTheSameUnitIdOnTheCableIsOneMouse(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        receiver.handler = silent
        self.nodes.add(CABLE, mouseHandler(slot=0xFF))
        self.mice.scanNodes()
        self.assertTrue(waitUntil(lambda: self.mouse().nodePath == CABLE))
        self.assertEqual((self.keys(), self.mouse().slot), (["02bc524c"], 0xFF))

    def testLosingANodeSearchesTheOthers(self):
        receiver = self.nodes.add(RECEIVER, silent)
        self.nodes.add(CABLE, mouseHandler(slot=0xFF))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.mouse() is not None and self.mouse().nodePath == CABLE))
        receiver.handler = mouseHandler()
        self.nodes.remove(CABLE)
        self.assertTrue(waitUntil(lambda: self.mouse() is not None and self.mouse().nodePath == RECEIVER))

    def testLosingTheOnlyNodeDropsItsMouse(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.nodes.remove(RECEIVER)
        self.assertTrue(waitUntil(lambda: self.keys() == [] and self.mice.nodes == {}))

    def testANodeLostToAnErrorIsTriedAgain(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        lostState = self.mice.nodes[RECEIVER]
        fresh = self.nodes.replace(RECEIVER, mouseHandler())
        self.assertTrue(waitUntil(lambda: self.mice.nodes.get(RECEIVER) not in (None, lostState) and self.keys() == ["02bc524c"], timeout=2.0))
        self.assertGreater(pingsTo(fresh, 1), 0)
        self.assertEqual(self.mice.denied, set())

    def testALostNodeFreesItsTimers(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.mice.onWake(self.mouse())
        before = len(self.mice.findChildren(QTimer))
        self.nodes.remove(RECEIVER)
        self.assertTrue(waitUntil(lambda: self.keys() == [] and self.mice.nodes == {}))
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        # the node's retry and identify timers, and the mouse's wake timer
        self.assertEqual(len(self.mice.findChildren(QTimer)), before - 3)

    def testAMouseFoundAgainAfterAReplugGetsAWorkingWakeRetry(self):
        hooked = []
        with mock.patch.object(sys, "excepthook", lambda *info: hooked.append(info[1])):
            self.nodes.add(RECEIVER, mouseHandler())
            self.mice.start()
            self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
            self.mice.onWake(self.mouse())
            lostState = self.mice.nodes[RECEIVER]
            receiver = self.nodes.replace(RECEIVER, mouseHandler())
            self.assertTrue(waitUntil(lambda: self.mice.nodes.get(RECEIVER) not in (None, lostState) and self.keys() == ["02bc524c"], timeout=2.0))
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            found = self.mouse()
            self.putToSleep(receiver)
            self.wakeOnceAndRetry(receiver, found)
        self.assertEqual((hooked, found.asleep), ([], False))

    def testANodeThatFailsToOpenIsTriedAgain(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.nodes.failOnce(RECEIVER)
        self.mice.start()
        self.assertEqual((self.mice.nodes, self.mice.denied, self.mice.isSearching()), ({}, {RECEIVER}, False))
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"], timeout=2.0))
        self.assertEqual(self.mice.denied, set())

    def testANodeWhoseWorkerCantStartIsClosedAndTriedAgain(self):
        first = "/dev/hidraw10"
        self.nodes.add(first, mouseHandler(unitId=bytes.fromhex("aaaaaaaa")))
        self.nodes.add(RECEIVER, mouseHandler(unitId=bytes.fromhex("bbbbbbbb")))
        realOpen = self.nodes.open
        realStart = receivers.NodeWorker.start
        opened = []
        failed = []

        def recordingOpen(path, onEvent=None):
            node = realOpen(path, onEvent)
            opened.append(node)
            return node

        def failingStart(worker):
            if (worker.path == first and not failed):
                failed.append(worker)
                raise RuntimeError("can't start new thread")
            realStart(worker)

        with mock.patch.object(hidpp.HidppNode, "open", recordingOpen), mock.patch.object(receivers.NodeWorker, "start", failingStart):
            self.mice.start()
            self.assertEqual((sorted(self.mice.nodes), self.mice.denied), ([RECEIVER], {first}))
            self.assertEqual((opened[0].fd, failed[0].wakeRead, failed[0].wakeWrite), (None, None, None))
            self.assertIn("Couldn't start " + first + ": can't start new thread\n", self.errors.getvalue())
            self.assertTrue(waitUntil(lambda: sorted(self.keys()) == ["aaaaaaaa", "bbbbbbbb"], timeout=2.0))

    def testALateLostMessageAfterStopDoesNothing(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        worker = self.mice.nodes[RECEIVER].worker
        self.mice.stop()
        self.mice.onMessage(("lost", worker, OSError("late")))
        self.assertEqual(self.mice.denied, set())
        self.assertFalse(self.mice.deniedTimer.isActive())

    def testAStoppedListIgnoresDevChanges(self):
        self.mice.start()
        self.mice.stop()
        self.mice.devWatcher.directoryChanged.emit("/dev")
        self.assertFalse(self.mice.settleTimer.isActive())

    def testADeniedNodeIsTriedAgain(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.assertEqual((self.mice.nodes, self.mice.denied), ({}, {RECEIVER}))
        self.nodes.allow(RECEIVER, mouseHandler())
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.assertEqual(self.mice.denied, set())

    def testAZeroUnitIdFallsBackToNodeAndSlot(self):
        self.nodes.add(RECEIVER, mouseHandler(unitId=bytes(4)))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == [RECEIVER + "#1"]))

    def testAFailedSearchLetsTheNextOneRun(self):
        self.nodes.add(RECEIVER, mouseHandler())
        realSearch = hidpp.searchNode
        calls = []

        def flakySearch(node, skipped=frozenset(), pingTimeout=None):
            calls.append(skipped)
            if (len(calls) == 1):
                raise ValueError("boom")
            return realSearch(node, skipped, pingTimeout)

        with mock.patch.object(hidpp, "searchNode", flakySearch), mock.patch.object(receivers.traceback, "print_exc"):
            self.mice.start()
            self.assertTrue(waitUntil(lambda: calls and not self.mice.nodes[RECEIVER].searching))
            self.mice.requestSearch(RECEIVER)
            self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))

    def testASearchThatRefindsAnAwakeMouseKeepsItsWokeAt(self):
        first = self.nodes.add("/dev/hidraw10", mouseHandler(unitId=bytes.fromhex("aaaaaaaa")))
        self.nodes.add(RECEIVER, mouseHandler(unitId=bytes.fromhex("bbbbbbbb")))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: len(self.mice.mice) == 2))
        first.handler = silent
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.byKey("aaaaaaaa").asleep))
        first.handler = mouseHandler(unitId=bytes.fromhex("aaaaaaaa"))
        first.send(OTHER_REPORT)
        self.assertTrue(waitUntil(lambda: not self.byKey("aaaaaaaa").asleep))
        wokeAt = self.byKey("bbbbbbbb").wokeAt
        self.mice.requestSearch(RECEIVER)
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        self.assertEqual(self.byKey("bbbbbbbb").wokeAt, wokeAt)
        self.assertLess(wokeAt, self.byKey("aaaaaaaa").wokeAt)

    def testABurstOfReadsCostsAtMostOneMore(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        before = batteryReads(receiver)
        for i in range(10):
            self.mice.readAll()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].pendingReads))
        QTest.qWait(100)
        self.assertEqual(batteryReads(receiver) - before, 2)

    def testALinkUpDuringAPendingReadStillIdentifies(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        receiver.handler = silent
        self.mice.readAll()
        self.assertIn(1, self.mice.nodes[RECEIVER].pendingReads)
        receiver.handler = mouseHandler(unitId=bytes.fromhex("11223344"))
        receiver.send(LINK_UP)
        self.assertTrue(waitUntil(lambda: self.keys() == ["11223344"]))

    def testASecondLinkUpDuringAnIdentifyGetsItsOwn(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        state = self.mice.nodes[RECEIVER]
        with mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.5):
            receiver.handler = silent
            receiver.send(LINK_UP)
            self.assertTrue(waitUntil(lambda: 1 in state.pendingIdentifies))
            receiver.send(LINK_UP)
            self.assertTrue(waitUntil(lambda: 1 in state.identifyAgain))
            receiver.handler = mouseHandler(unitId=bytes.fromhex("11223344"))
            self.assertTrue(waitUntil(lambda: self.keys() == ["11223344"], timeout=4.0))

    def testAStreamOfLinkUpNoticesCostsOneIdentifyPerGap(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        realIdentify = hidpp.identifySlot
        identifies = []

        def countingIdentify(node, slot, *args):
            identifies.append(slot)
            return realIdentify(node, slot, *args)

        with mock.patch.object(hidpp, "identifySlot", countingIdentify):
            # each notice comes after the last identify finished, so only the gap can hold them back
            for i in range(5):
                receiver.send(LINK_UP)
                QTest.qWait(60)
            during = len(identifies)
            QTest.qWait(receivers.IDENTIFY_GAP_MS + 300)
        self.assertEqual((during, len(identifies)), (1, 2))

    def testAFailedReadDoesntBlockTheNextOne(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        realRead = hidpp.readBattery
        calls = []

        def flakyRead(node, feature):
            calls.append(feature)
            if (len(calls) == 1):
                raise ValueError("boom")
            return realRead(node, feature)

        with mock.patch.object(hidpp, "readBattery", flakyRead), mock.patch.object(receivers.traceback, "print_exc"):
            self.mice.readAll()
            self.assertTrue(waitUntil(lambda: calls and not self.mice.nodes[RECEIVER].pendingReads))
            self.mice.readAll()
            self.assertTrue(waitUntil(lambda: len(calls) == 2))

    def testABatteryEventDuringATimedOutReadKeepsTheMouseAwake(self):
        event = bytes([0x11, 1, 5, 0x00, 42, 0x04, 0, 0]).ljust(20, b"\x00")
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))

        def eventThenSilence(request):
            if (request[2] == 5 and (request[3] >> 4) == 1):
                return [event]
            return []

        receiver.handler = eventThenSilence
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].pendingReads))
        self.assertEqual((self.mouse().reading, self.mouse().asleep), (hidpp.BatteryReading(42, 0, power=0), False))

    def eventDuringTheName(self, receiver):
        # the battery reply comes first, and a battery event the mouse sends while its name is read is the newer level
        event = bytes([0x11, 1, 5, 0x00, 42, 0x04, 0, 0]).ljust(20, b"\x00")
        mouse = mouseHandler()

        def handler(request):
            if (request[1] == 1 and request[2] == 2 and (request[3] >> 4) == 0):
                return [event] + mouse(request)
            return mouse(request)

        receiver.handler = handler

    def testABatteryEventDuringASearchsNameReadWinsOverItsReading(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        self.eventDuringTheName(receiver)
        self.mice.requestSearch(RECEIVER)
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))
        mouse = self.mouse()
        self.assertEqual((mouse.reading, mouse.asleep, mouse.name), (hidpp.BatteryReading(42, 0, power=0), False, NAME.decode()))

    def testABatteryEventDuringAnIdentifysNameReadWinsOverItsReading(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        self.eventDuringTheName(receiver)
        identified = []
        self.mice.message.connect(lambda message: identified.append(message) if message[0] == "identified" else None)
        receiver.send(LINK_UP)
        self.assertTrue(waitUntil(lambda: identified and not self.mice.isBusy()))
        self.assertEqual(self.mouse().reading, hidpp.BatteryReading(42, 0, power=0))

    def testAnErrorOnANewMousesFirstReadReadsItAgainSoon(self):
        answers = {(5, 0): [0x0F, 0x02]}
        self.nodes.add(RECEIVER, mouseHandler(answers=answers))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        mouse = self.mouse()
        self.assertEqual((mouse.reading, mouse.asleep), (None, False))
        self.assertTrue(mouse.wakeTimer is not None and mouse.wakeTimer.isActive())
        answers[(5, 1)] = [77, 0x04, 0, 0]
        self.assertTrue(waitUntil(lambda: self.mouse().reading == hidpp.BatteryReading(77, 0, power=0), timeout=2.0))

    def testAMouseThatKeepsAnsweringWithAnErrorIsReadAFewTimesThenWaits(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler(answers={(5, 0): [0x0F, 0x02]}))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: batteryReads(receiver) == 4))
        # longer than four more retries at the patched WAKE_RETRY_MS of 300
        QTest.qWait(1500)
        self.assertEqual(batteryReads(receiver), 4)
        self.assertFalse(self.mouse().wakeTimer.isActive())
        receiver.send(bytes([0x11, 1, 5, 0x00, 64, 0x04, 0, 0]).ljust(20, b"\x00"))
        self.assertTrue(waitUntil(lambda: self.mouse().reading == hidpp.BatteryReading(64, 0, power=0)))

    def holdTwoBusyNodes(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.nodes.add(CABLE, mouseHandler(slot=0xFF, unitId=bytes.fromhex("aaaaaaaa")))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: len(self.keys()) == 2 and not self.mice.isBusy()))
        workers = [state.worker for state in self.mice.nodes.values()]
        release = threading.Event()
        entered = []

        # a read stuck where no stop reaches it, so only the shared wait can bound the close
        def stuckRead(node, feature, deadline=None):
            entered.append(1)
            release.wait(3.0)
            return (hidpp.TIMEOUT, None)

        def releaseWorkers():
            release.set()
            for worker in workers:
                worker.join(2)

        self.addCleanup(releaseWorkers)
        patch = mock.patch.object(hidpp, "readBattery", stuckRead)
        patch.start()
        self.addCleanup(patch.stop)
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: len(entered) == 2))
        return releaseWorkers

    def testStoppingWaitsForBusyNodesTogether(self):
        releaseWorkers = self.holdTwoBusyNodes()
        started = time.monotonic()
        self.mice.stop()
        took = time.monotonic() - started
        releaseWorkers()
        # one shared second, where each busy node used to get a second of its own
        self.assertLess(took, 1.5)

    def testNodesDiscoveryDropsTogetherShareOneWait(self):
        releaseWorkers = self.holdTwoBusyNodes()
        for path in (RECEIVER, CABLE):
            self.nodes.hide(path)
        started = time.monotonic()
        self.mice.scanNodes()
        took = time.monotonic() - started
        releaseWorkers()
        self.assertEqual((self.mice.nodes, self.keys()), ({}, []))
        self.assertLess(took, 1.5)

    def failFirstWrite(self):
        realWrite = hidpp.HidppNode.writeReport
        failed = []

        def flakyWrite(node, *args):
            if (not failed):
                failed.append(1)
                raise OSError(errno.ETIMEDOUT, "Connection timed out")
            return realWrite(node, *args)
        return mock.patch.object(hidpp.HidppNode, "writeReport", flakyWrite)

    def testASearchTurnedAwayIsTriedAgain(self):
        self.nodes.add(RECEIVER, mouseHandler())
        with self.failFirstWrite():
            self.mice.start()
            self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))

    def testAnIdentifyTurnedAwayReadsTheKnownMouseAgain(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        self.putToSleep(receiver)
        receiver.handler = mouseHandler()
        with self.failFirstWrite():
            receiver.send(LINK_UP)
            self.assertTrue(waitUntil(lambda: not self.mouse().asleep, timeout=2.0))

    def testANoticeDuringASearchGetsItsSearchRightAfter(self):
        first = mouseHandler()
        second = mouseHandler(slot=2, unitId=bytes.fromhex("bbbbbbbb"))
        paired = [False]

        def handler(request):
            if (request[1] != 2):
                return first(request)
            if (not paired[0]):
                # the new mouse pairs while the first search pings, so its notice lands during that search
                paired[0] = True
                return [bytes([0x10, 2, 0x41, 0x10, 0x00, 0x00, 0x00])]
            return second(request)

        with mock.patch.object(receivers, "WAKE_RETRY_MS", 5000):
            self.nodes.add(RECEIVER, handler)
            self.mice.start()
            self.assertTrue(waitUntil(lambda: set(self.keys()) == {"02bc524c", "bbbbbbbb"}, timeout=2.0))

    def testAReportDuringARetrySearchRidesOnTheRetry(self):
        self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        state = self.mice.nodes[RECEIVER]
        # the last search a report started was long ago, so only the retry's own search can count toward the gap
        state.reportSearchAt = time.monotonic() - 60
        state.retryTimer.start(0)
        self.assertTrue(waitUntil(lambda: state.searching))
        self.mice.onReport(state, OTHER_REPORT, time.monotonic())
        self.assertFalse(state.searchAgain)

    def testARetryDuringARunningSearchQueuesNoOther(self):
        self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        state = self.mice.nodes[RECEIVER]
        # a ping wait far past the check below, so the search is still running when it looks
        with mock.patch.object(hidpp, "PING_TIMEOUT", 3.0):
            self.mice.requestSearch(RECEIVER)
            # a search that outlasts the retry wait would otherwise get another one queued behind it every time
            state.retryTimer.start(0)
            QTest.qWait(30)
        self.assertEqual((state.searching, state.searchAgain), (True, False))

    def testAReportDuringARetrySearchLongerThanTheWaitStillRidesOnTheRetry(self):
        with mock.patch.object(receivers, "WAKE_RETRY_MS", 100):
            self.nodes.add(RECEIVER, silent)
            self.mice.start()
            self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
            state = self.mice.nodes[RECEIVER]
            state.retryTimer.stop()
            # the retry's ping wait runs far past the check below, so a loaded runner still finds it searching
            with mock.patch.object(hidpp, "PING_TIMEOUT", 3.0):
                state.retryTimer.start(0)
                self.assertTrue(waitUntil(lambda: state.searching))
                # past the retry wait, while the retry's own search still runs
                QTest.qWait(200)
                self.assertTrue(state.searching)
                self.mice.onReport(state, OTHER_REPORT, time.monotonic())
        self.assertFalse(state.searchAgain)

    def testASearchThatRaisesWithASlotWaitingArmsTheRetry(self):
        self.nodes.add(RECEIVER, silent)
        realSearch = hidpp.searchNode
        calls = []

        def failingSecond(node, skipped=frozenset(), pingTimeout=None):
            calls.append(1)
            if (len(calls) == 2):
                raise ValueError("boom")
            return realSearch(node, skipped, pingTimeout)

        with mock.patch.object(hidpp, "searchNode", failingSecond), mock.patch.object(receivers.traceback, "print_exc"):
            self.mice.start()
            self.assertTrue(waitUntil(lambda: calls and not self.mice.nodes[RECEIVER].searching))
            state = self.mice.nodes[RECEIVER]
            state.retryTimer.stop()
            state.retrySlots.add(2)
            self.mice.requestSearch(RECEIVER)
            self.assertTrue(waitUntil(lambda: len(calls) == 2 and not state.searching))
        self.assertTrue(state.retryTimer.isActive())

    def testANodeThatServedAWhilePullsTheSharedRetryIn(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.nodes.deny(CABLE)
        self.mice.start()
        self.mice.onLost(self.mice.nodes[RECEIVER], OSError(errno.EIO, "Input/output error"))
        self.mice.retryDenied()
        self.assertIn(RECEIVER, self.mice.nodes)
        self.holdDeniedRetry()
        state = self.mice.nodes[RECEIVER]
        state.openedAt -= receivers.STABLE_MS / 1000
        self.mice.onLost(state, OSError(errno.EIO, "Input/output error"))
        self.assertEqual(self.mice.deniedTimer.interval(), 100)

    def testANewDenialPullsTheRetryInButNeverPushesItOut(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.mice.deniedTimer.start(30)
        self.nodes.deny(CABLE)
        self.mice.scanNodes()
        # Qt can report a millisecond or two of rounding, while a push out would read about 100
        self.assertLessEqual(self.mice.deniedTimer.remainingTime(), 50)

    def testANewDenialDuringARetryGetsTheShortestWait(self):
        with mock.patch.object(receivers, "DENIED_RETRY_MAX_MS", 10000):
            self.nodes.deny(RECEIVER)
            self.mice.start()
            for i in range(3):
                self.mice.retryDenied()
            # the device is replaced while still denied, which the next retry meets as a new device node
            self.nodes.add(RECEIVER, silent)
            self.nodes.deny(RECEIVER)
            self.mice.retryDenied()
        self.assertEqual(self.mice.deniedTimer.interval(), 100)

    def testANewlyDeniedPathStartsTheBackoffOver(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.holdDeniedRetry()
        self.nodes.deny(CABLE)
        self.mice.scanNodes()
        self.assertEqual((self.mice.denied, self.mice.deniedTimer.interval()), ({RECEIVER, CABLE}, 100))

    def testAccessGrantedRightAfterAFailedOpenIsSeenAtTheNextScan(self):
        generation = [0]
        realOpen = self.nodes.open

        def openThenGrant(path, onEvent=None):
            try:
                return realOpen(path, onEvent)
            finally:
                # udev changes the node's group just after the open failed
                generation[0] += 1

        self.nodes.deny(RECEIVER)
        with mock.patch.object(receivers, "nodeSignature", lambda path: (path, generation[0])), mock.patch.object(hidpp.HidppNode, "open", openThenGrant):
            self.mice.start()
            self.holdDeniedRetry()
            self.nodes.allow(RECEIVER, mouseHandler())
            self.mice.onDevChanged("/dev")
            self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))

    def testAWriteTurnedAwayKeepsTheNodeAndItsMouse(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        realWrite = hidpp.HidppNode.writeReport
        failed = []

        def flakyWrite(node, *args):
            if (not failed):
                failed.append(1)
                raise OSError(errno.ETIMEDOUT, "Connection timed out")
            return realWrite(node, *args)
        with mock.patch.object(hidpp.HidppNode, "writeReport", flakyWrite):
            self.mice.readAll()
            self.assertTrue(waitUntil(lambda: failed and not self.mice.isBusy()))
        self.assertEqual((self.keys(), list(self.mice.nodes), self.mice.denied), (["02bc524c"], [RECEIVER], set()))
        self.assertEqual(self.errors.getvalue(), "Opened " + RECEIVER + "\nRequest failed on " + RECEIVER + ": ETIMEDOUT\n")

    def putToSleep(self, receiver):
        receiver.handler = silent
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.mouse().asleep and not self.mice.nodes[RECEIVER].pendingReads))

    def testAnAsleepMouseWhoseReadAnswersAnErrorIsReadAgain(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        self.putToSleep(receiver)
        # a busy receiver answers with an error while the mouse is there
        receiver.handler = mouseHandler(answers={(5, 0): [0x0F, 0x02]})
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].pendingReads))
        receiver.handler = mouseHandler()
        self.assertTrue(waitUntil(lambda: not self.mouse().asleep, timeout=2.0))

    def testAKnownMouseWhoseIdentifyCantFinishIsReadAgain(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        self.putToSleep(receiver)
        identified = []
        self.mice.message.connect(lambda message: identified.append(message[3][0]) if message[0] == "identified" else None)
        receiver.send(LINK_UP)
        self.assertTrue(waitUntil(lambda: identified == [hidpp.UNKNOWN]))
        receiver.handler = mouseHandler()
        self.assertTrue(waitUntil(lambda: not self.mouse().asleep, timeout=2.0))

    def testANoticeDuringASearchKeepsItsSlotFromBeingSkipped(self):
        keyboardFirst = [True]
        keyboard = mouseHandler(slot=2, kind=0)
        mouse = mouseHandler(slot=2, unitId=bytes.fromhex("bbbbbbbb"))

        def handler(request):
            if (request[1] != 2):
                return []
            if (keyboardFirst[0] and request[2] == 2 and (request[3] >> 4) == 2):
                # a new pairing's notice for slot 2 comes in ahead of the keyboard reply, so the worker reads it during the same search
                keyboardFirst[0] = False
                return [bytes([0x10, 2, 0x41, 0x10, 0x00, 0x00, 0x00])] + keyboard(request)
            return (keyboard if keyboardFirst[0] else mouse)(request)

        self.nodes.add(RECEIVER, handler)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["bbbbbbbb"], timeout=3.0))
        self.assertNotIn(2, self.mice.nodes[RECEIVER].skipped)

    def testANoticeDuringASearchKeepsAKnownMouseInItsSlot(self):
        keyboardFirst = [False]
        keyboard = mouseHandler(kind=0)
        mouse = mouseHandler()

        def handler(request):
            if (keyboardFirst[0] and request[1] == 1 and request[2] == 2 and (request[3] >> 4) == 2):
                # the search sees a keyboard in the mouse's slot, while a notice for the slot comes in ahead of that reply and its identify finds the mouse again
                keyboardFirst[0] = False
                return [LINK_UP] + keyboard(request)
            return mouse(request)

        self.nodes.add(RECEIVER, handler)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        known = self.mouse()
        searched = []
        self.mice.message.connect(lambda message: searched.append(message[2]) if message[0] == "searched" else None)
        keyboardFirst[0] = True
        self.mice.requestSearch(RECEIVER)
        self.assertTrue(waitUntil(lambda: searched and not self.mice.isBusy()))
        self.assertEqual(searched[0].skipped, frozenset({1}))
        self.assertIs(self.mouse(), known)
        self.assertEqual((self.keys(), self.mice.nodes[RECEIVER].skipped), (["02bc524c"], set()))

    def testASlotLeftUnknownGetsOneFollowUpSearch(self):
        receiver = self.nodes.add(RECEIVER, pingOnly)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: pingsTo(receiver, 1) == 2, timeout=2.0))
        QTest.qWait(800)
        self.assertEqual(pingsTo(receiver, 1), 2)

    def testDeniedRetriesBackOff(self):
        with mock.patch.object(receivers, "DENIED_RETRY_MAX_MS", 400):
            self.nodes.deny(RECEIVER)
            self.mice.start()
            intervals = [self.mice.deniedTimer.interval()]
            for i in range(3):
                self.mice.retryDenied()
                intervals.append(self.mice.deniedTimer.interval())
        self.assertEqual(intervals, [100, 200, 400, 400])

    def holdDeniedRetry(self):
        self.mice.deniedRetryMs = 60000
        self.mice.deniedTimer.start(self.mice.deniedRetryMs)

    def testADevChangeLeavesAnUnchangedDeniedNodeToItsBackoff(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.holdDeniedRetry()
        opened = []
        realOpen = self.nodes.open
        with mock.patch.object(hidpp.HidppNode, "open", lambda path, onEvent=None: opened.append(path) or realOpen(path, onEvent)):
            # any account can change a /dev entry's times, which the watch sees like any other change
            for i in range(3):
                self.mice.onDevChanged("/dev")
                self.assertTrue(waitUntil(lambda: not self.mice.settleTimer.isActive()))
        self.assertEqual((opened, self.mice.deniedTimer.interval()), ([], 60000))

    def testADeniedNodeWhoseDeviceChangedOpensAtTheNextScan(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.holdDeniedRetry()
        self.nodes.allow(RECEIVER, mouseHandler())
        self.mice.onDevChanged("/dev")
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.assertEqual(self.mice.denied, set())

    def testANodeThatKeepsDyingAfterItOpensBacksOff(self):
        with mock.patch.object(receivers, "DENIED_RETRY_MAX_MS", 10000):
            self.nodes.add(RECEIVER, mouseHandler())
            self.mice.start()
            intervals = []
            for i in range(3):
                self.mice.onLost(self.mice.nodes[RECEIVER], OSError(errno.EIO, "Input/output error"))
                intervals.append(self.mice.deniedTimer.interval())
                self.mice.retryDenied()
        self.assertEqual(intervals, [100, 200, 400])

    def testALossWhoseListingFailsStillClosesTheNodeAndRetriesIt(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        worker = self.mice.nodes[RECEIVER].worker
        with mock.patch.object(hidpp, "findHidppNodes", side_effect=OSError(errno.EMFILE, "Too many open files")):
            self.mice.onLost(self.mice.nodes[RECEIVER], OSError(errno.EIO, "Input/output error"))
            self.assertTrue(waitUntil(lambda: not worker.is_alive()))
            self.assertEqual((RECEIVER in self.mice.nodes, self.mice.denied, self.keys()), (False, {RECEIVER}, []))
            self.assertTrue(self.mice.deniedTimer.isActive())
            self.mice.deniedTimer.stop()
            self.mice.retryDenied()
        # the loss lists the nodes the way the scan and the denied retry do, so the run of failures prints once from whichever came first
        self.assertEqual(self.errors.getvalue(), "Opened " + RECEIVER + "\nCouldn't list HID++ nodes: EMFILE\nLost " + RECEIVER + ": EIO\n")

    def testADeniedRetryWhoseListingFailsOpensNothingAndComesBack(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.mice.deniedTimer.stop()
        opened = []
        with mock.patch.object(hidpp, "findHidppNodes", side_effect=OSError(errno.EMFILE, "Too many open files")), mock.patch.object(hidpp.HidppNode, "open", lambda path, onEvent=None: opened.append(path)):
            self.mice.retryDenied()
            self.mice.retryDenied()
        self.assertEqual((opened, self.mice.denied), ([], {RECEIVER}))
        self.assertTrue(self.mice.deniedTimer.isActive())
        self.assertEqual(self.mice.deniedTimer.interval(), 400)
        self.assertEqual(self.errors.getvalue(), "Couldn't open " + RECEIVER + ": EACCES\nCouldn't list HID++ nodes: EMFILE\n")

    def testAScanWhoseListingFailsChangesNothingAndComesBack(self):
        self.nodes.add(RECEIVER, mouseHandler())
        with mock.patch.object(hidpp, "findHidppNodes", side_effect=OSError(errno.EMFILE, "Too many open files")):
            # the start's scan and a scan the settle timer runs both list the nodes, and neither may end the service or strand a receiver plugged in then
            self.mice.start()
            self.assertEqual((self.mice.nodes, self.mice.denied), ({}, set()))
            self.assertTrue(self.mice.settleTimer.isActive())
            self.mice.settleTimer.stop()
            self.mice.scanNodes()
            self.assertTrue(self.mice.settleTimer.isActive())
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        # a run of failed listings prints once, and a listing that works lets the next failure print again
        with mock.patch.object(hidpp, "findHidppNodes", side_effect=OSError(errno.ENOENT, "No such file or directory")):
            self.mice.scanNodes()
        self.mice.settleTimer.stop()
        self.assertEqual(self.errors.getvalue(), "Couldn't list HID++ nodes: EMFILE\nOpened " + RECEIVER + "\nCouldn't list HID++ nodes: ENOENT\n")
        self.assertEqual(list(self.mice.nodes), [RECEIVER])

    def testAServiceThatHasntListedNodesYetSaysItsLooking(self):
        searching = []
        self.mice.changed.connect(lambda: searching.append(self.mice.isSearching()))
        with mock.patch.object(hidpp, "findHidppNodes", side_effect=OSError(errno.EMFILE, "Too many open files")):
            self.mice.start()
            self.mice.settleTimer.stop()
            self.mice.scanNodes()
            self.mice.settleTimer.stop()
        # with no listing yet the service can't say there's no receiver, so the menu says it's looking instead of finding no mouse
        self.assertEqual(searching, [True])
        self.mice.scanNodes()
        self.assertEqual(searching, [True, False])
        # a listing that worked already said what's plugged in, so a later failure leaves that standing
        with mock.patch.object(hidpp, "findHidppNodes", side_effect=OSError(errno.EMFILE, "Too many open files")):
            self.mice.scanNodes()
            self.mice.settleTimer.stop()
        self.assertEqual(searching, [True, False])
        self.assertFalse(self.mice.isSearching())

    def testANodeThatKeepsDyingAfterItOpensPrintsItsLossOnce(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        for i in range(3):
            self.mice.onLost(self.mice.nodes[RECEIVER], OSError(errno.EIO, "Input/output error"))
            self.mice.retryDenied()
        # the node opens again after each loss, and its second loss says it keeps failing, so the journal doesn't end on an open
        self.assertEqual(self.errors.getvalue(), "Opened " + RECEIVER + "\nLost " + RECEIVER + ": EIO\nOpened " + RECEIVER + "\n" + RECEIVER + " keeps failing (EIO), retrying quietly\n")

    def testANodeThatWorksAgainAfterFailingSaysSo(self):
        with mock.patch.object(receivers, "STABLE_MS", 300):
            self.nodes.add(RECEIVER, mouseHandler())
            self.mice.start()
            for i in range(2):
                # each loss comes straight after its open, however long a loaded machine took to get here
                self.mice.nodes[RECEIVER].openedAt = time.monotonic()
                self.mice.onLost(self.mice.nodes[RECEIVER], OSError(errno.EIO, "Input/output error"))
                self.mice.retryDenied()
            # the journal said the node keeps failing, so once it has served as long as a stable node does it says it works again
            self.assertTrue(waitUntil(lambda: "working again" in self.errors.getvalue()))
            self.mice.onLost(self.mice.nodes[RECEIVER], OSError(errno.EIO, "Input/output error"))
            self.mice.retryDenied()
        opened = "Opened " + RECEIVER + "\n"
        lost = "Lost " + RECEIVER + ": EIO\n"
        self.assertEqual(self.errors.getvalue(), opened + lost + opened + RECEIVER + " keeps failing (EIO), retrying quietly\n" + RECEIVER + " working again\n" + lost + opened)

    def testANodeThatServedAWhileStartsItsBackoffAndItsPrintingOver(self):
        with mock.patch.object(receivers, "DENIED_RETRY_MAX_MS", 10000):
            self.nodes.add(RECEIVER, mouseHandler())
            self.mice.start()
            for i in range(2):
                self.mice.onLost(self.mice.nodes[RECEIVER], OSError(errno.EIO, "Input/output error"))
                self.mice.retryDenied()
            state = self.mice.nodes[RECEIVER]
            state.openedAt -= receivers.STABLE_MS / 1000
            self.mice.onLost(state, OSError(errno.EIO, "Input/output error"))
            interval = self.mice.deniedTimer.interval()
            self.mice.retryDenied()
        opened = "Opened " + RECEIVER + "\n"
        lost = "Lost " + RECEIVER + ": EIO\n"
        self.assertEqual((interval, self.errors.getvalue()), (100, opened + lost + opened + RECEIVER + " keeps failing (EIO), retrying quietly\n" + lost + opened))

    def testAnErrorIsPrintedAgainForAPathThatWentAwayAndCameBack(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.nodes.denied.discard(RECEIVER)
        self.mice.scanNodes()
        self.nodes.deny(RECEIVER)
        self.mice.scanNodes()
        self.assertEqual(self.errors.getvalue(), ("Couldn't open " + RECEIVER + ": EACCES\n") * 2)

    def testADeniedPathThatsNoLongerListedIsntOpened(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.nodes.denied.discard(RECEIVER)
        opened = []
        with mock.patch.object(hidpp.HidppNode, "open", lambda path, onEvent=None: opened.append(path)):
            self.mice.retryDenied()
        self.assertEqual((opened, self.mice.denied), ([], set()))

    def testEveryBatteryReportEmitsAndOtherReportsDont(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        changes = []
        self.mice.changed.connect(lambda: changes.append(1))
        event = bytes([0x11, 1, 5, 0x00, 64, 0x04, 0, 0]).ljust(20, b"\x00")
        for i in range(3):
            receiver.send(event)
            receiver.send(OTHER_REPORT)
        self.assertTrue(waitUntil(lambda: len(changes) >= 3))
        QTest.qWait(200)
        self.assertEqual((self.mouse().reading, len(changes)), (hidpp.BatteryReading(64, 0, power=0), 3))

    def testItSaysItsSearchingWhileTheFirstSearchRuns(self):
        self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(self.mice.isSearching())
        self.assertTrue(waitUntil(lambda: not self.mice.isSearching()))

    def testAnOpenErrorIsPrintedOncePerPathWithItsErrnoName(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.mice.retryDenied()
        self.mice.retryDenied()
        self.assertEqual(self.errors.getvalue(), "Couldn't open " + RECEIVER + ": EACCES\n")

    def testANodeThatOpensIsPrinted(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.assertEqual(self.errors.getvalue(), "Opened " + RECEIVER + "\n")

    def testAnUnpluggedNodeIsPrintedAsRemoved(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.nodes.remove(RECEIVER)
        self.assertTrue(waitUntil(lambda: self.keys() == []))
        self.assertEqual(self.errors.getvalue(), "Opened " + RECEIVER + "\nRemoved " + RECEIVER + "\n")

    def testANodeDiscoveryDropsIsPrintedAsRemoved(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.nodes.hide(RECEIVER)
        self.mice.scanNodes()
        self.assertEqual(self.errors.getvalue(), "Opened " + RECEIVER + "\nRemoved " + RECEIVER + "\n")

    def testAnOpenErrorAfterALossWithTheSameErrnoIsStillPrinted(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.nodes.failOnce(RECEIVER)
        worker = self.mice.nodes[RECEIVER].worker
        with mock.patch.object(worker.node, "readReport", side_effect=OSError(errno.EIO, "Input/output error")):
            receiver.send(OTHER_REPORT)
            self.assertTrue(waitUntil(lambda: RECEIVER not in self.mice.nodes or self.mice.nodes[RECEIVER].worker is not worker))
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and self.mice.denied == set()))
        self.assertEqual(self.errors.getvalue(), "Opened " + RECEIVER + "\nLost " + RECEIVER + ": EIO\nCouldn't open " + RECEIVER + ": EIO\nOpened " + RECEIVER + "\n")

    def testAFailedFirstSearchEmitsAndStopsSearching(self):
        self.nodes.add(RECEIVER, silent)
        changes = []

        def failingSearch(node, skipped=frozenset(), pingTimeout=None):
            raise ValueError("boom")

        with mock.patch.object(hidpp, "searchNode", failingSearch), mock.patch.object(receivers.traceback, "print_exc"):
            self.mice.start()
            self.assertTrue(self.mice.isSearching())
            self.mice.changed.connect(lambda: changes.append(1))
            self.assertTrue(waitUntil(lambda: changes))
        self.assertEqual((self.mice.nodes[RECEIVER].searching, self.mice.isSearching()), (False, False))

    def testALostNodeStillClosesWhenItsPrintFails(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        state = self.mice.nodes[RECEIVER]
        hooked = []
        # the message goes through the signal, so a raise lands in sys.excepthook the way it does in the service
        with mock.patch.object(sys, "stderr", BrokenStream()), mock.patch.object(sys, "excepthook", lambda *info: hooked.append(info[1])):
            self.mice.message.emit(("lost", state.worker, OSError(errno.EIO, "Input/output error")))
        self.assertEqual((hooked, list(self.mice.nodes), self.mice.denied), ([], [], {RECEIVER}))

    def testANewNodeStillSearchesWhenItsPrintFails(self):
        self.nodes.add(RECEIVER, mouseHandler())
        hooked = []
        with mock.patch.object(sys, "stderr", BrokenStream()), mock.patch.object(sys, "excepthook", lambda *info: hooked.append(info[1])):
            self.mice.onDevChanged("/dev")
            found = waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isSearching())
        self.assertEqual((hooked, found), ([], True))

    def testAMessageFromAReplacedWorkerIsIgnored(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        oldWorker = self.mice.nodes[RECEIVER].worker
        lostState = self.mice.nodes[RECEIVER]
        self.nodes.replace(RECEIVER, mouseHandler())
        self.assertTrue(waitUntil(lambda: self.mice.nodes.get(RECEIVER) not in (None, lostState)))
        self.mice.onMessage(("lost", oldWorker, OSError("late")))
        self.assertIn(RECEIVER, self.mice.nodes)

    def testAFailureWhileApplyingASearchStillEndsIt(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        state = self.mice.nodes[RECEIVER]
        state.searching = True
        result = hidpp.SearchResult((hidpp.FoundMouse(1, "02bc524c", None, FEATURE, hidpp.BatteryReading(50, 0)),), frozenset(), frozenset())
        changes = []
        self.mice.changed.connect(lambda: changes.append(1))
        with mock.patch.object(self.mice, "registerFound", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.mice.onSearched(state, result, 0.0)
        # the end of a first search has to reach the tray even when applying it failed
        self.assertEqual((state.searching, changes), (False, [1]))

    def testAReplugWithAReadPendingReadsNormallyAfterwards(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        receiver.handler = silent
        self.mice.readAll()
        lostState = self.mice.nodes[RECEIVER]
        self.assertIn(1, lostState.pendingReads)
        answers = {(5, 0): [0x0F, 0x02], (5, 1): [33, 0x04, 0, 0]}
        self.nodes.replace(RECEIVER, mouseHandler(answers=answers))
        self.assertTrue(waitUntil(lambda: self.mice.nodes.get(RECEIVER) not in (None, lostState) and not self.mice.nodes[RECEIVER].searching and self.mouse() is not None and self.mouse().reading == hidpp.BatteryReading(33, 0, power=0)))
        answers[(5, 1)] = [34, 0x04, 0, 0]
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.mouse().reading == hidpp.BatteryReading(34, 0, power=0)))

    def testABatteryReportThatRepeatsTheReadingStillEmits(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        changes = []
        self.mice.changed.connect(lambda: changes.append(1))
        firstRead = self.mouse().lastRead
        receiver.send(bytes([0x11, 1, 5, 0x00, 81, 0x04, 0, 0]).ljust(20, b"\x00"))
        self.assertTrue(waitUntil(lambda: self.mouse().lastRead != firstRead))
        QTest.qWait(100)
        self.assertEqual((self.mouse().reading, len(changes)), (hidpp.BatteryReading(81, 0, power=0), 1))

    def testBusyWhileASearchOrAReadIsPending(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.assertFalse(self.mice.isBusy())
        self.mice.start()
        self.assertTrue(self.mice.isBusy())
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        self.mice.readAll()
        self.assertTrue(self.mice.isBusy())
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))

    def testSearchingFollowsOnlyTheFirstSearch(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(self.mice.isSearching())
        self.assertTrue(waitUntil(lambda: not self.mice.isSearching() and not self.mice.isBusy()))
        self.mice.requestSearch(RECEIVER)
        self.assertEqual((self.mice.nodes[RECEIVER].searching, self.mice.isSearching(), self.mice.isBusy()), (True, False, True))

    def testReadAllStartsAtMostOneBatchPerGap(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        before = batteryReads(receiver)
        self.mice.requestReadAll()
        self.assertTrue(self.mice.isBusy())
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))
        for i in range(5):
            self.mice.requestReadAll()
        # a batch waiting its turn isn't work in flight, since any local caller can keep one queued
        self.assertEqual((self.mice.readAllTimer.isActive(), self.mice.isBusy()), (True, False))
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))
        QTest.qWait(400)
        self.assertEqual(batteryReads(receiver) - before, 2)

    def testReadAllSearchesANodeWithNoKnownMouse(self):
        receiver = self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))
        self.assertEqual(self.keys(), [])
        receiver.handler = mouseHandler()
        self.mice.requestReadAll()
        self.assertTrue(self.mice.nodes[RECEIVER].searching)
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))

    def testReadAllSearchesANodeWithNoMouseAtMostOncePerGap(self):
        receiver = self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))
        searches = []
        for i in range(3):
            pings = pingsTo(receiver, 2)
            self.mice.requestReadAll()
            self.assertTrue(waitUntil(lambda: not self.mice.isBusy() and not self.mice.readAllTimer.isActive()))
            searches.append(pingsTo(receiver, 2) - pings)
            QTest.qWait(350)
        self.assertEqual(searches, [1, 0, 0])

    def testReadAllDuringTheFirstSearchQueuesNoSecondSearch(self):
        receiver = self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(self.mice.nodes[RECEIVER].searching)
        self.mice.requestReadAll()
        self.assertFalse(self.mice.nodes[RECEIVER].searchAgain)
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))
        QTest.qWait(200)
        self.assertEqual(pingsTo(receiver, 2), 1)

    def testReadAllLeavesANodeWithAMouseUnsearched(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.isBusy()))
        pings = pingsTo(receiver, 2)
        self.mice.requestReadAll()
        self.assertFalse(self.mice.nodes[RECEIVER].searching)
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy()))
        self.assertEqual(pingsTo(receiver, 2), pings)

    def testAMouseThatMovesToAnotherNodeGetsItsQuickRetriesBack(self):
        mouse = self.mice.mouseFor("02bc524c")
        mouse.nodePath = RECEIVER
        mouse.slot = 1
        mouse.errorRetries = receivers.ERROR_RETRY_LIMIT
        state = receivers.NodeState(CABLE, None, None)
        found = hidpp.FoundMouse(slot=0xFF, unitId="02bc524c", name=NAME.decode(), feature=FEATURE, reading=None, outcome=hidpp.ERROR)
        with mock.patch.object(self.mice, "retryLater") as retryLater:
            self.mice.registerFound(state, found)
        self.assertEqual((mouse.nodePath, mouse.errorRetries), (CABLE, 1))
        retryLater.assert_called_once_with(mouse)


if (__name__ == "__main__"):
    unittest.main()


WIRELESS_FEATURES = {0x0003: 3, 0x0005: 2, 0x1004: 5, 0x1D4B: 23}
WAKE_REPORT = bytes([0x11, 1, 23, 0x00, 1, 1, 1]).ljust(20, b"\x00")
LINK_DOWN = bytes([0x10, 1, 0x41, 0x10, 0x40, 0x00, 0x00])


class ReceiverHistoryTests(unittest.TestCase):
    def setUp(self):
        self.nodes = FakeNodes()
        self.errors = io.StringIO()
        self.folder = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(hidpp, "findHidppNodes", self.nodes.find),
            mock.patch.object(hidpp.HidppNode, "open", self.nodes.open),
            mock.patch.object(receivers, "nodeSignature", self.nodes.signature),
            mock.patch.object(hidpp, "PING_TIMEOUT", 0.2),
            mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.1),
            mock.patch.object(receivers, "WAKE_RETRY_MS", 300),
            mock.patch.object(receivers, "SETTLE_MS", 10),
            mock.patch.object(receivers, "READ_ALL_GAP_MS", 300),
            mock.patch.object(sys, "stderr", self.errors),
        ]
        for patch in self.patches:
            patch.start()
        self.recorder = history.Recorder(self.folder, timers=False)
        self.mice = receivers.ReceiverList(self.recorder)

    def tearDown(self):
        self.mice.stop()
        self.recorder.stop()
        for patch in self.patches:
            patch.stop()
        self.nodes.closeAll()
        sip.delete(self.mice)
        shutil.rmtree(self.folder)

    def mouse(self):
        return self.mice.mice[0] if self.mice.mice else None

    def origins(self):
        # stop writes every reading still waiting out the wake set and the hold, so the rows are all there
        self.recorder.stop()
        with sqlite3.connect(os.path.join(self.folder, history.FILE_NAME)) as db:
            return [origin for (origin,) in db.execute("select origin from readings order by at, seq")]

    def start(self, handler=None):
        receiver = self.nodes.add(RECEIVER, handler or mouseHandler(features=WIRELESS_FEATURES))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.mouse() is not None and not self.mice.isBusy()))
        return receiver

    def settle(self):
        # a reading within 2 s of a wake counts as part of its wake set, so the first reading is let through before a test sends one
        shift = history.WAKE_SET_SECONDS + 1 + (self.recorder.clock() - time.time())
        self.recorder.clock = lambda: time.time() + shift
        self.recorder.tick()

    def testTheFirstRegistrationWritesAGapBeforeItsReading(self):
        self.start()
        self.assertEqual(self.origins(), [history.ORIGIN_GAP, history.ORIGIN_READ])

    def testTheWirelessStatusIndexReachesTheMouseAndTheRecorder(self):
        self.start()
        self.assertEqual(self.mouse().wakeIndex, 23)
        self.assertTrue(self.recorder.entries[self.mouse().key].track.wakeSupport)

    def testAWirelessStatusEventIsAWakeWhileAwakeWithoutARead(self):
        receiver = self.start()
        self.settle()
        before = batteryReads(receiver)
        receiver.send(WAKE_REPORT)
        QTest.qWait(300)
        self.assertEqual(batteryReads(receiver), before)
        self.assertEqual(self.origins()[-1], history.ORIGIN_WAKE)

    def testAWirelessStatusEventReadsAMouseMarkedAsleep(self):
        receiver = self.start()
        self.mouse().asleep = True
        before = batteryReads(receiver)
        receiver.send(WAKE_REPORT)
        self.assertTrue(waitUntil(lambda: batteryReads(receiver) > before))

    def testAMouseWithoutTheFeatureKeepsTheOldWakeRule(self):
        receiver = self.start(mouseHandler())
        self.assertIsNone(self.mouse().wakeIndex)
        self.mouse().asleep = True
        before = batteryReads(receiver)
        receiver.send(OTHER_REPORT)
        self.assertTrue(waitUntil(lambda: batteryReads(receiver) > before))
        self.assertIn(history.ORIGIN_WAKE, self.origins())

    def testALinkUpWritesItsWakeThenTheIdentifysGap(self):
        receiver = self.start()
        self.settle()
        receiver.send(LINK_UP)
        self.assertTrue(waitUntil(lambda: not self.mice.isBusy() and self.mice.nodes[RECEIVER].identifiedAt))
        QTest.qWait(200)
        origins = self.origins()
        self.assertEqual(origins[2:4], [history.ORIGIN_WAKE, history.ORIGIN_GAP])

    def testALinkDownMarksTheMouseAsleepOnce(self):
        receiver = self.start()
        receiver.send(LINK_DOWN)
        receiver.send(LINK_DOWN)
        self.assertTrue(waitUntil(lambda: self.mouse().asleep))
        QTest.qWait(200)
        self.assertEqual(self.origins().count(history.ORIGIN_ASLEEP), 1)

    def testAReadWithNoAnswerMarksTheMouseAsleepOnce(self):
        receiver = self.start()
        receiver.handler = silent
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.mouse().asleep))
        self.mice.readAll()
        QTest.qWait(400)
        self.assertEqual(self.origins().count(history.ORIGIN_ASLEEP), 1)

    def testABatteryEventIsSavedAsAnEvent(self):
        receiver = self.start()
        receiver.send(bytes([0x11, 1, 5, 0x00, 64, 0x04, 0, 0]).ljust(20, b"\x00"))
        self.assertTrue(waitUntil(lambda: self.mouse().reading.percent == 64))
        self.assertEqual(self.origins()[-1], history.ORIGIN_EVENT)

    def testUnpluggingTheReceiverWritesAGap(self):
        self.start()
        self.nodes.remove(RECEIVER)
        self.assertTrue(waitUntil(lambda: not self.mice.mice))
        self.assertEqual(self.origins()[-1], history.ORIGIN_GAP)

    def testASavedRowMovesTheMousesRevision(self):
        self.start()
        changes = []
        self.mice.changed.connect(lambda: changes.append(1))
        self.recorder.tick()
        self.recorder.clock = lambda: time.time() + 60
        self.recorder.tick()
        self.assertTrue(waitUntil(lambda: self.mouse().historyRevision >= 2))
        self.assertTrue(changes)

    def testReadSilentSkipsAMouseThatReportsItself(self):
        receiver = self.start()
        before = batteryReads(receiver)
        with mock.patch.object(self.recorder, "reportsItself", lambda key: True):
            self.mice.requestReadSilent()
            QTest.qWait(300)
        self.assertEqual(batteryReads(receiver), before)
        self.mice.requestReadSilent()
        self.assertTrue(waitUntil(lambda: batteryReads(receiver) > before))

    def testAReadAllWhileAReadSilentWaitsStillRunsInFull(self):
        receiver = self.start()
        self.mice.requestReadSilent()
        self.mice.requestReadSilent()
        self.assertTrue(self.mice.isReadSilentQueued())
        with mock.patch.object(self.mice, "searchNodesWithoutMice") as search:
            self.mice.lastReadAll = None
            self.mice.requestReadAll()
            search.assert_called_once()
