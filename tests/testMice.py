import io
import os
import queue
import sys
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import hidpp
import mice
import upower
from tests.fakes.fakeDevice import NAME, FakeDevice, FakeNodes, mouseHandler, reply

app = QApplication.instance() or QApplication([])
FEATURE = hidpp.BatteryFeature(1, hidpp.FEATURE_UNIFIED_BATTERY, 5, True)
PATH = "/dev/hidraw18"


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
        self.worker = mice.NodeWorker(PATH, self.device.node(), self.messages.put)
        self.worker.start()

    def testQueuedWorkRunsWhileTheDeviceIsSilent(self):
        self.startWorker(mouseHandler())
        time.sleep(0.2)
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        message = nextMessage(self.messages, "read")
        self.assertIs(message[1], self.worker)
        self.assertEqual(message[2:], (1, (hidpp.ANSWER, hidpp.BatteryReading(81, 0)), 0.0))

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
        self.assertEqual(nextMessage(self.messages, "report"), ("report", self.worker, event))

    def testExceptionInAWorkItemKeepsTheThreadRunning(self):
        self.startWorker(mouseHandler())
        with mock.patch.object(mice.traceback, "print_exc") as printed:
            self.worker.queueWork(("bogus",))
            self.assertEqual(nextMessage(self.messages, "failed"), ("failed", self.worker, ("bogus",)))
        self.assertTrue(printed.called)
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.assertIsNotNone(nextMessage(self.messages, "read"))

    def testUnplugPostsLost(self):
        self.startWorker(mouseHandler())
        self.device.unplug()
        self.assertEqual(nextMessage(self.messages, "lost")[:2], ("lost", self.worker))

    def testStopEndsTheThreadAndClosesThePipe(self):
        self.startWorker(mouseHandler())
        self.worker.stop()
        self.assertFalse(self.worker.is_alive())
        self.assertIsNone(self.worker.wakeRead)
        self.worker = None

    def testAnUnexpectedErrorInTheLoopPostsLost(self):
        self.startWorker(mouseHandler())
        with mock.patch.object(mice.traceback, "print_exc") as printed, mock.patch.object(self.worker.node, "readReport", side_effect=ValueError("bad")):
            self.device.send(bytes([0x11, 1, 9, 0x00, 1, 2, 3]).ljust(20, b"\x00"))
            message = nextMessage(self.messages, "lost")
        self.assertEqual((message[1], type(message[2])), (self.worker, ValueError))
        self.assertTrue(printed.called)

    def testAStopThatGivesUpWaitingStillClosesThePipeLater(self):
        with mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.5):
            self.startWorker(silent)
            self.worker.queueWork(("read", 1, FEATURE, 0.0))
            time.sleep(0.05)
            self.worker.stop(timeout=0.01)
            self.assertTrue(self.worker.is_alive())
            self.worker.wake()
            self.worker.join(2)
        self.assertFalse(self.worker.is_alive())
        self.assertIsNone(self.worker.wakeRead)
        # a wake after the thread closed its pipe does nothing
        self.worker.wake()
        self.worker = None

    def testAReportTakenByARequestDoesntStallTheNextWork(self):
        self.device = FakeDevice(mouseHandler())
        self.worker = mice.NodeWorker(PATH, self.device.node(), self.messages.put)
        # the report and the wake both wait at the first select, and the queued read takes the report off the node before the loop reads it
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.device.send(OTHER_REPORT)
        self.worker.start()
        self.assertIsNotNone(nextMessage(self.messages, "read"))
        self.worker.queueWork(("read", 1, FEATURE, 0.0))
        self.assertIsNotNone(nextMessage(self.messages, "read"))


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


class MouseListTests(unittest.TestCase):
    def setUp(self):
        self.nodes = FakeNodes()
        self.errors = io.StringIO()
        self.patches = [
            mock.patch.object(hidpp, "findHidppNodes", self.nodes.find),
            mock.patch.object(hidpp.HidppNode, "open", self.nodes.open),
            mock.patch.object(hidpp, "PING_TIMEOUT", 0.02),
            mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.1),
            mock.patch.object(mice, "WAKE_RETRY_MS", 300),
            mock.patch.object(mice, "SETTLE_MS", 10),
            mock.patch.object(mice, "DENIED_RETRY_MS", 100),
            mock.patch.object(sys, "stderr", self.errors),
        ]
        for patch in self.patches:
            patch.start()
        self.mice = mice.MouseList()

    def tearDown(self):
        self.mice.stop()
        for patch in self.patches:
            patch.stop()
        self.nodes.closeAll()
        # a list left to the cyclic GC is freed with every other one in a single pass, and that pause can outlast a later test's 20 ms ping window
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
        self.assertEqual((mouse.name, mouse.nodePath, mouse.slot, mouse.reading), (NAME.decode(), RECEIVER, 1, hidpp.BatteryReading(81, 0)))
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

    def testASlotTheSearchSkippedIsntRetried(self):
        self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        state = self.mice.nodes[RECEIVER]
        report = bytearray(OTHER_REPORT)
        report[1] = 2
        self.mice.onReport(state, bytes(report))
        self.mice.onSearched(state, hidpp.SearchResult((), frozenset(), frozenset({2})), 0.0)
        self.assertTrue(state.retryTimer.isActive())
        state.retryTimer.stop()
        self.mice.onReport(state, OTHER_REPORT)
        self.mice.onSearched(state, hidpp.SearchResult((), frozenset({1}), frozenset()), 0.0)
        self.assertFalse(state.retryTimer.isActive())

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
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        receiver.handler = silent
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.mouse().asleep))
        receiver.send(OTHER_REPORT)
        QTest.qWait(150)
        self.assertTrue(self.mouse().asleep)
        receiver.handler = mouseHandler()
        self.assertTrue(waitUntil(lambda: not self.mouse().asleep, timeout=1.0))

    def testABatteryEventUpdatesTheReading(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        receiver.send(bytes([0x11, 1, 5, 0x00, 64, 0x04, 1, 1]).ljust(20, b"\x00"))
        self.assertTrue(waitUntil(lambda: self.mouse().reading == hidpp.BatteryReading(64, 1)))

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
        # the node's retry timer and the mouse's wake timer
        self.assertEqual(len(self.mice.findChildren(QTimer)), before - 2)

    def testAMergedMouseFoundAgainAfterAReplugKeepsItsWakeRetry(self):
        hooked = []
        with mock.patch.object(sys, "excepthook", lambda *info: hooked.append(info[1])):
            self.nodes.add(RECEIVER, mouseHandler())
            self.mice.start()
            self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
            self.mice.applyUPower(upower.UPowerMouse("/org/freedesktop/UPower/devices/mouse_hidpp_battery_0", "02bc524c", "PRO X3", hidpp.BatteryReading(55, 0)))
            merged = self.mouse()
            self.mice.onWake(merged)
            lostState = self.mice.nodes[RECEIVER]
            receiver = self.nodes.replace(RECEIVER, mouseHandler())
            self.assertTrue(waitUntil(lambda: self.mice.nodes.get(RECEIVER) not in (None, lostState) and merged.nodePath == RECEIVER, timeout=2.0))
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            self.assertIs(self.mouse(), merged)
            receiver.handler = silent
            self.mice.readAll()
            self.assertTrue(waitUntil(lambda: merged.asleep))
            receiver.send(OTHER_REPORT)
            QTest.qWait(150)
            self.assertTrue(merged.asleep)
            receiver.handler = mouseHandler()
            woke = waitUntil(lambda: not merged.asleep, timeout=1.0)
        self.assertEqual(hooked, [])
        self.assertTrue(woke)

    def testANodeThatFailsToOpenIsTriedAgain(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.nodes.failOnce(RECEIVER)
        self.mice.start()
        self.assertEqual((self.mice.nodes, self.mice.denied, self.mice.status()), ({}, {RECEIVER}, mice.STATUS_DENIED))
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"], timeout=2.0))
        self.assertEqual(self.mice.denied, set())

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

        with mock.patch.object(hidpp, "searchNode", flakySearch), mock.patch.object(mice.traceback, "print_exc"):
            self.mice.start()
            self.assertTrue(waitUntil(lambda: calls and not self.mice.nodes[RECEIVER].searching))
            self.mice.requestSearch(RECEIVER)
            self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))

    def testTheShownMouseIsTheOneThatWokeLast(self):
        first = self.nodes.add("/dev/hidraw10", mouseHandler(unitId=bytes.fromhex("aaaaaaaa")))
        self.nodes.add(RECEIVER, mouseHandler(unitId=bytes.fromhex("bbbbbbbb")))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: len(self.mice.mice) == 2))
        first.handler = silent
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.byKey("aaaaaaaa").asleep))
        self.assertEqual(self.mice.shownMouse().key, "bbbbbbbb")
        first.handler = mouseHandler(unitId=bytes.fromhex("aaaaaaaa"))
        first.send(OTHER_REPORT)
        self.assertTrue(waitUntil(lambda: not self.byKey("aaaaaaaa").asleep))
        self.assertEqual(self.mice.shownMouse().key, "aaaaaaaa")

    def testASearchThatRefindsAnAwakeMouseLeavesTheShownMouseAlone(self):
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
        self.assertEqual(self.mice.shownMouse().key, "aaaaaaaa")
        self.mice.requestSearch(RECEIVER)
        self.assertTrue(waitUntil(lambda: not self.mice.nodes[RECEIVER].searching))
        self.assertEqual(self.mice.shownMouse().key, "aaaaaaaa")

    def testMiceWithTheSameNameGetNumbers(self):
        self.nodes.add("/dev/hidraw10", mouseHandler(unitId=bytes.fromhex("aaaaaaaa")))
        self.nodes.add(RECEIVER, mouseHandler(unitId=bytes.fromhex("bbbbbbbb")))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: len(self.mice.mice) == 2))
        self.assertEqual(sorted(self.mice.displayNames().values()), [NAME.decode(), NAME.decode() + " 2"])

    def testLowBatteryIsSignalled(self):
        warnings = []

        def onLowBattery(key, name, percent):
            warnings.append((key, name, percent))
            # the tray marks a warning sent once its notification goes out
            self.mice.markWarningSent(key, percent)

        self.mice.lowBattery.connect(onLowBattery)
        self.nodes.add(RECEIVER, mouseHandler(answers={(5, 0): [0x0F, 0x02], (5, 1): [9, 0x02, 0, 0]}))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: warnings == [("02bc524c", NAME.decode(), 9)]))
        # a second reading at 9% has to reach the warner before the count means anything
        firstRead = self.mouse().lastRead
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.mouse().lastRead != firstRead))
        QTest.qWait(300)
        self.assertEqual(warnings, [("02bc524c", NAME.decode(), 9)])

    def sentWarnings(self):
        warnings = []

        def onLowBattery(key, name, percent):
            warnings.append(percent)
            # the tray marks a warning sent once its notification goes out
            self.mice.markWarningSent(key, percent)

        self.mice.lowBattery.connect(onLowBattery)
        return warnings

    def upowerWarningsAcrossARemoval(self, path, key):
        warnings = self.sentWarnings()
        lowMouse = upower.UPowerMouse(path, key, "MX Master 3", hidpp.BatteryReading(9, 0), pathKey=(key == path))
        self.mice.applyUPower(lowMouse)
        self.mice.removeUPower(path)
        self.mice.applyUPower(lowMouse)
        return warnings

    def warningsAfterTheNodeGoes(self, unitId, key):
        self.sentWarnings()
        self.nodes.add(RECEIVER, mouseHandler(answers={(5, 0): [0x0F, 0x02], (5, 1): [9, 0x02, 0, 0]}, unitId=unitId))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: key in self.mice.warner.sent))
        self.nodes.remove(RECEIVER)
        self.assertTrue(waitUntil(lambda: self.keys() == []))
        return self.mice.warner.sent

    def testAUPowerMouseKeyedByItsPathIsWarnedAgainAfterARemoval(self):
        path = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_1"
        self.assertEqual(self.upowerWarningsAcrossARemoval(path, path), [9, 9])

    def testAUPowerMouseWithASerialIsntWarnedAgainAfterARemoval(self):
        self.assertEqual(self.upowerWarningsAcrossARemoval("/org/freedesktop/UPower/devices/mouse_hidpp_battery_1", "12ab34cd"), [9])

    def testAMouseKeyedByNodeAndSlotForgetsItsWarningsWithTheNode(self):
        self.assertEqual(self.warningsAfterTheNodeGoes(bytes(4), RECEIVER + "#1"), {})

    def testAMouseWithAUnitIdKeepsItsWarningsWithoutItsNode(self):
        self.assertEqual(self.warningsAfterTheNodeGoes(bytes.fromhex("02bc524c"), "02bc524c"), {"02bc524c": {10}})

    def testAUPowerMouseMergesWithTheSameHidppMouse(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        path = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"
        self.mice.applyUPower(upower.UPowerMouse(path, "02bc524c", "PRO X3", hidpp.BatteryReading(55, 0)))
        self.assertEqual((self.keys(), self.mouse().upowerPath, self.mouse().reading.percent), (["02bc524c"], path, 55))
        self.mice.markWarningSent("02bc524c", 9)
        self.mice.removeUPower(path)
        self.assertEqual((self.keys(), self.mouse().upowerPath, self.mice.warner.sent), (["02bc524c"], None, {"02bc524c": {10}}))

    def testAUPowerOnlyMouseComesAndGoes(self):
        path = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_1"
        self.mice.applyUPower(upower.UPowerMouse(path, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        self.assertEqual(self.keys(), ["12ab34cd"])
        self.assertFalse(self.mouse().asleep)
        self.mice.removeUPower(path)
        self.assertEqual(self.keys(), [])

    def testAChangedUPowerReadingCountsAsWaking(self):
        path = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_1"
        self.mice.applyUPower(upower.UPowerMouse(path, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        firstWake = self.mouse().wokeAt
        time.sleep(0.02)
        self.mice.applyUPower(upower.UPowerMouse(path, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))
        self.assertEqual(self.mouse().wokeAt, firstWake)
        self.mice.applyUPower(upower.UPowerMouse(path, "12ab34cd", "MX Master 3", hidpp.BatteryReading(54, 0)))
        self.assertGreater(self.mouse().wokeAt, firstWake)

    def testStatus(self):
        self.assertEqual(self.mice.status(), mice.STATUS_NONE)
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.assertEqual(self.mice.status(), mice.STATUS_DENIED)
        self.nodes.allow(RECEIVER, silent)
        self.assertTrue(waitUntil(lambda: self.mice.status() == mice.STATUS_WAITING))
        self.mice.applyUPower(upower.UPowerMouse("/p", "12ab34cd", None, hidpp.BatteryReading(55, 0)))
        self.assertEqual(self.mice.status(), mice.STATUS_MICE)

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

        with mock.patch.object(hidpp, "readBattery", flakyRead), mock.patch.object(mice.traceback, "print_exc"):
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
        self.assertEqual((self.mouse().reading, self.mouse().asleep), (hidpp.BatteryReading(42, 0), False))

    def testAnErrorOnANewMousesFirstReadReadsItAgainSoon(self):
        answers = {(5, 0): [0x0F, 0x02]}
        self.nodes.add(RECEIVER, mouseHandler(answers=answers))
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        mouse = self.mouse()
        self.assertEqual((mouse.reading, mouse.asleep), (None, False))
        self.assertTrue(mouse.wakeTimer is not None and mouse.wakeTimer.isActive())
        answers[(5, 1)] = [77, 0x04, 0, 0]
        self.assertTrue(waitUntil(lambda: self.mouse().reading == hidpp.BatteryReading(77, 0), timeout=2.0))

    def testANoticeDuringASearchKeepsItsSlotFromBeingSkipped(self):
        keyboardFirst = [True]
        keyboard = mouseHandler(slot=2, kind=0)
        mouse = mouseHandler(slot=2, unitId=bytes.fromhex("bbbbbbbb"))

        def handler(request):
            if (request[1] != 2):
                return []
            if (keyboardFirst[0] and request[2] == 2 and (request[3] >> 4) == 2):
                # slot 2 answers keyboard, and a new pairing's notice for it follows during the same search
                keyboardFirst[0] = False
                return keyboard(request) + [bytes([0x10, 2, 0x41, 0x10, 0x00, 0x00, 0x00])]
            return (keyboard if keyboardFirst[0] else mouse)(request)

        self.nodes.add(RECEIVER, handler)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["bbbbbbbb"], timeout=3.0))
        self.assertNotIn(2, self.mice.nodes[RECEIVER].skipped)

    def testASlotLeftUnknownGetsOneFollowUpSearch(self):
        receiver = self.nodes.add(RECEIVER, pingOnly)
        self.mice.start()
        self.assertTrue(waitUntil(lambda: pingsTo(receiver, 1) == 2, timeout=2.0))
        QTest.qWait(800)
        self.assertEqual(pingsTo(receiver, 1), 2)

    def testDeniedRetriesBackOffAndADevChangeStartsThemOver(self):
        with mock.patch.object(mice, "DENIED_RETRY_MAX_MS", 400):
            self.nodes.deny(RECEIVER)
            self.mice.start()
            intervals = [self.mice.deniedTimer.interval()]
            for i in range(3):
                self.mice.retryDenied()
                intervals.append(self.mice.deniedTimer.interval())
            self.assertEqual(intervals, [100, 200, 400, 400])
            self.mice.onDevChanged("/dev")
            self.assertEqual(self.mice.deniedTimer.interval(), 100)

    def testADeniedPathThatsNoLongerListedIsntOpened(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.nodes.denied.discard(RECEIVER)
        opened = []
        with mock.patch.object(hidpp.HidppNode, "open", lambda path, onEvent=None: opened.append(path)):
            self.mice.retryDenied()
        self.assertEqual((opened, self.mice.denied), ([], set()))

    def testOnlyARealChangeRedraws(self):
        receiver = self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"] and not self.mice.nodes[RECEIVER].searching))
        changes = []
        self.mice.changed.connect(lambda: changes.append(1))
        event = bytes([0x11, 1, 5, 0x00, 64, 0x04, 0, 0]).ljust(20, b"\x00")
        for i in range(3):
            receiver.send(event)
            receiver.send(OTHER_REPORT)
        self.assertTrue(waitUntil(lambda: self.mouse().reading == hidpp.BatteryReading(64, 0)))
        QTest.qWait(200)
        self.assertEqual(len(changes), 1)

    def testItSaysItsLookingWhileTheFirstSearchRuns(self):
        self.nodes.add(RECEIVER, silent)
        self.mice.start()
        self.assertEqual(self.mice.status(), mice.STATUS_SEARCHING)
        self.assertTrue(waitUntil(lambda: self.mice.status() == mice.STATUS_WAITING))

    def testAnOpenErrorIsPrintedOncePerPathWithItsErrnoName(self):
        self.nodes.deny(RECEIVER)
        self.mice.start()
        self.mice.retryDenied()
        self.mice.retryDenied()
        self.assertEqual(self.errors.getvalue(), "Couldn't open " + RECEIVER + ": EACCES\n")

    def testALostNodeIsPrintedWithItsError(self):
        self.nodes.add(RECEIVER, mouseHandler())
        self.mice.start()
        self.assertTrue(waitUntil(lambda: self.keys() == ["02bc524c"]))
        self.nodes.remove(RECEIVER)
        self.assertTrue(waitUntil(lambda: self.keys() == []))
        self.assertIn("Lost " + RECEIVER + ": HID++ node closed\n", self.errors.getvalue())

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
        with mock.patch.object(self.mice, "registerFound", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.mice.onSearched(state, result, 0.0)
        self.assertFalse(state.searching)

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
        self.assertTrue(waitUntil(lambda: self.mice.nodes.get(RECEIVER) not in (None, lostState) and not self.mice.nodes[RECEIVER].searching and self.mouse() is not None and self.mouse().reading == hidpp.BatteryReading(33, 0)))
        answers[(5, 1)] = [34, 0x04, 0, 0]
        self.mice.readAll()
        self.assertTrue(waitUntil(lambda: self.mouse().reading == hidpp.BatteryReading(34, 0)))


if (__name__ == "__main__"):
    unittest.main()
