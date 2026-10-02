import io
import os
import signal
import sys
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6 import sip
from PyQt6.QtCore import QObject, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusError, QDBusMessage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import hidpp
import receivers
import service
import serviceState
import version
from tests.fakes.fakeDevice import FakeNodes, mouseHandler
from tests.fakes.privateBus import PrivateBus, requireTools

app = QApplication.instance() or QApplication([])
RECEIVER = "/dev/hidraw18"
OTHER_REPORT = bytes([0x11, 1, 9, 0x00, 1, 2, 3]).ljust(20, b"\x00")
REPEATED_BATTERY = bytes([0x11, 1, 5, 0x00, 81, 0x04, 0, 0]).ljust(20, b"\x00")


def silent(request):
    return []


def waitUntil(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while (time.monotonic() < deadline):
        if (condition()):
            return True
        QTest.qWait(10)
    return condition()


def batteryReads(device, slot=1, featureIndex=5):
    return sum(1 for sent in list(device.requests) if sent[1] == slot and sent[2] == featureIndex and (sent[3] >> 4) == 1)


class Caller(QObject):
    def __init__(self, bus):
        super().__init__()
        self.bus = bus
        self.answers = []
        self.signals = []

    # Qt frees the message once the slot returns, so a slot copies out what it needs and never keeps the message
    @pyqtSlot(QDBusMessage)
    def onReply(self, message):
        self.answers.append(message.arguments())

    @pyqtSlot(QDBusError, QDBusMessage)
    def onError(self, error, message):
        self.answers.append(error.name())

    @pyqtSlot(QDBusMessage)
    def onState(self, message):
        self.signals.append(message.arguments()[0])

    def call(self, member, interface=serviceState.INTERFACE, timeout=3.0):
        self.answers = []
        message = QDBusMessage.createMethodCall(serviceState.SERVICE_NAME, serviceState.OBJECT_PATH, interface, member)
        self.bus.callWithCallback(message, self.onReply, self.onError, 3000)
        waitUntil(lambda: self.answers, timeout)
        return self.answers[0] if self.answers else None

    def state(self):
        answer = self.call("GetState")
        if (not isinstance(answer, list)):
            return None
        return serviceState.decodeState(answer[0])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        requireTools(self)
        self.privateBus = PrivateBus()
        self.nodes = FakeNodes()
        self.patches = [
            mock.patch.object(hidpp, "findHidppNodes", self.nodes.find),
            mock.patch.object(hidpp.HidppNode, "open", self.nodes.open),
            mock.patch.object(hidpp, "PING_TIMEOUT", 0.2),
            mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.5),
            mock.patch.object(receivers, "SETTLE_MS", 10),
            mock.patch.object(receivers, "WAKE_RETRY_MS", 300),
        ]
        for patch in self.patches:
            patch.start()
        self.names = ["service" + str(id(self)), "client" + str(id(self))]
        self.serviceBus = QDBusConnection.connectToBus(self.privateBus.address, self.names[0])
        self.clientBus = QDBusConnection.connectToBus(self.privateBus.address, self.names[1])
        self.receiverList = receivers.ReceiverList()
        self.served = service.serve(self.serviceBus, self.receiverList)
        self.caller = Caller(self.clientBus)

    def tearDown(self):
        self.receiverList.stop()
        for name in self.names:
            QDBusConnection.disconnectFromBus(name)
        for patch in self.patches:
            patch.stop()
        self.nodes.closeAll()
        self.privateBus.close()
        sip.delete(self.receiverList)

    def connectTo(self, name):
        self.names.append(name + str(id(self)))
        return QDBusConnection.connectToBus(self.privateBus.address, self.names[-1])

    def startWithMouse(self):
        device = self.nodes.add(RECEIVER, mouseHandler())
        self.receiverList.start()
        self.assertTrue(waitUntil(lambda: self.receiverList.mice and not self.receiverList.isBusy()))
        return device

    def testGetStateMatchesEncodeStateThroughTheBus(self):
        self.assertIsNotNone(self.served)
        self.startWithMouse()
        answer = self.caller.call("GetState")
        self.assertEqual(answer, [serviceState.encodeState(self.receiverList)])
        state = serviceState.decodeState(answer[0])
        self.assertEqual((state.nodes, [(entry.key, entry.reading.percent) for entry in state.mice]), ((RECEIVER,), [("02bc524c", 81)]))

    def testBusyWhileAReadIsPendingButNotWhileABatchWaits(self):
        device = self.startWithMouse()
        device.handler = silent
        self.caller.call("ReadAll")
        self.assertTrue(self.caller.state().busy)
        self.assertTrue(waitUntil(lambda: not self.caller.state().busy))
        self.caller.call("ReadAll")
        # a queued batch would otherwise let any local caller hold busy true, which makes every check wait its full ten seconds
        self.assertEqual((self.receiverList.readAllTimer.isActive(), self.caller.state().busy), (True, False))
        self.assertTrue(waitUntil(lambda: not self.caller.state().busy, timeout=4.0))

    def subscribe(self):
        self.assertTrue(self.clientBus.connect(serviceState.SERVICE_NAME, serviceState.OBJECT_PATH, serviceState.INTERFACE, "StateChanged", self.caller.onState))

    def ageReading(self):
        # stamps go out in whole seconds, so the reading is moved back a few for a repeat of it to be news
        self.receiverList.mice[0].lastRead -= 5
        self.receiverList.changed.emit()
        self.assertTrue(waitUntil(lambda: self.caller.signals and self.caller.signals[-1] == serviceState.encodeState(self.receiverList)))
        return serviceState.decodeState(self.caller.signals[-1]).mice[0].lastRead

    def testStateChangedFollowsAReadAndARepeatedReportButNotANoChangeReport(self):
        device = self.startWithMouse()
        self.subscribe()
        agedRead = self.ageReading()
        count = len(self.caller.signals)
        device.send(REPEATED_BATTERY)
        self.assertTrue(waitUntil(lambda: len(self.caller.signals) > count))
        self.assertGreater(serviceState.decodeState(self.caller.signals[-1]).mice[0].lastRead, agedRead)
        count = len(self.caller.signals)
        device.send(OTHER_REPORT)
        QTest.qWait(400)
        self.assertEqual(len(self.caller.signals), count)
        self.ageReading()
        count = len(self.caller.signals)
        self.caller.call("ReadAll")
        self.assertTrue(waitUntil(lambda: len(self.caller.signals) > count))

    def testAChangedSignalWithAnUnchangedStateSendsNoStateChanged(self):
        device = self.startWithMouse()
        self.subscribe()
        self.ageReading()
        count = len(self.caller.signals)
        self.receiverList.changed.emit()
        QTest.qWait(400)
        self.assertEqual(len(self.caller.signals), count)
        device.send(REPEATED_BATTERY)
        self.assertTrue(waitUntil(lambda: len(self.caller.signals) > count))

    def testAFloodOfReportsGoesOutAFewTimesInsteadOfOncePerReport(self):
        device = self.startWithMouse()
        self.subscribe()
        self.ageReading()
        count = len(self.caller.signals)
        # each report changes the level, so every one of them is a real change of state
        for i in range(100):
            device.send(bytes([0x11, 1, 5, 0x00, 80 - (i % 2), 0x04, 0, 0]).ljust(20, b"\x00"))
        QTest.qWait(service.BROADCAST_MS * 3)
        self.assertEqual(self.receiverList.mice[0].reading.percent, 79)
        sent = self.caller.signals[count:]
        self.assertLessEqual(len(sent), 5)
        self.assertEqual(serviceState.decodeState(sent[-1]).mice[0].reading.percent, 79)

    def testSearchingGoesFalseAfterTheFirstSearchEvenWhileAnotherRuns(self):
        self.startWithMouse()
        self.receiverList.requestSearch(RECEIVER)
        state = self.caller.state()
        self.assertEqual((state.searching, state.busy), (False, True))

    def testALoopOfReadAllCallsStartsOneBatchPerSecond(self):
        device = self.startWithMouse()
        before = batteryReads(device)
        caller = Caller(self.connectTo("other"))
        started = time.monotonic()
        while (time.monotonic() - started < 0.8):
            self.assertIn(caller.call("ReadAll"), ([], service.READ_ALL_LIMITED))
            self.assertIsNotNone(self.caller.state())
        QTest.qWait(1500)
        self.assertEqual(batteryReads(device) - before, 2)

    def readAllLoop(self, caller, seconds):
        started = time.monotonic()
        while (time.monotonic() - started < seconds):
            # every call is answered, with a read or with the refusal once the account's budget runs out
            self.assertIn(caller.call("ReadAll"), ([], service.READ_ALL_LIMITED))

    def testOneAccountsLoopOfReadAllRunsOutOfItsBurst(self):
        device = self.startWithMouse()
        before = batteryReads(device)
        with mock.patch.object(receivers, "READ_ALL_GAP_MS", 50), mock.patch.object(service, "READ_ALL_BURST", 2):
            self.readAllLoop(Caller(self.connectTo("other")), 1.0)
            QTest.qWait(200)
        self.assertEqual(batteryReads(device) - before, 2)

    def testAnotherAccountStillGetsItsReadsWhileOneLoops(self):
        device = self.startWithMouse()
        adaptor = self.served[1]
        with mock.patch.object(receivers, "READ_ALL_GAP_MS", 50), mock.patch.object(service, "READ_ALL_BURST", 2), mock.patch.object(adaptor, "callerOf", lambda message: message.service()):
            self.readAllLoop(Caller(self.connectTo("other")), 0.5)
            QTest.qWait(200)
            before = batteryReads(device)
            self.caller.call("ReadAll")
            self.assertTrue(waitUntil(lambda: batteryReads(device) > before))

    def testAnAccountsBudgetRefillsOverTime(self):
        device = self.startWithMouse()
        caller = Caller(self.connectTo("other"))
        with mock.patch.object(receivers, "READ_ALL_GAP_MS", 50), mock.patch.object(service, "READ_ALL_BURST", 1), mock.patch.object(service, "READ_ALL_REFILL_SECONDS", 0.3):
            self.readAllLoop(caller, 0.2)
            QTest.qWait(400)
            before = batteryReads(device)
            caller.call("ReadAll")
            self.assertTrue(waitUntil(lambda: batteryReads(device) > before))

    def testARefusedReadAllAnswersWithAnError(self):
        self.startWithMouse()
        caller = Caller(self.connectTo("other"))
        with mock.patch.object(service, "READ_ALL_BURST", 1):
            answers = [caller.call("ReadAll"), caller.call("ReadAll")]
        self.assertEqual(answers, [[], "org.freedesktop.DBus.Error.LimitsExceeded"])

    def testAReadAllFromACallerItCantPlaceAnswersWithAnError(self):
        self.startWithMouse()
        adaptor = self.served[1]
        with mock.patch.object(adaptor, "callerOf", lambda message: None):
            self.assertEqual(self.caller.call("ReadAll"), "org.freedesktop.DBus.Error.Failed")

    def testAGetStateDuringTheBroadcastWaitStillLetsTheBroadcastOut(self):
        adaptor = self.served[1]
        emitted = []
        adaptor.StateChanged.connect(emitted.append)
        current = ["A"]
        with mock.patch.object(serviceState, "encodeState", lambda receiverList: current[0]):
            adaptor.sentState = "A"
            current[0] = "B"
            adaptor.onChanged()
            self.assertEqual(adaptor.GetState(), "B")
            # the state goes back to the one last sent before the wait ends, and the caller still holds B
            current[0] = "A"
            self.assertTrue(waitUntil(lambda: not adaptor.broadcastTimer.isActive()))
        self.assertEqual(emitted, ["A"])

    def testTheCallerIsTheAccountBehindTheConnection(self):
        self.startWithMouse()
        adaptor = self.served[1]
        callers = []
        realCallerOf = adaptor.callerOf
        with mock.patch.object(adaptor, "callerOf", lambda message: callers.append(realCallerOf(message)) or callers[-1]):
            self.caller.call("ReadAll")
        self.assertEqual(callers, [os.getuid()])

    def testReadAllTakesNoArgumentsOnTheBus(self):
        xml = self.caller.call("Introspect", interface="org.freedesktop.DBus.Introspectable")[0]
        start = xml.index('<method name="ReadAll">')
        self.assertEqual(xml[start:xml.index("</method>", start)].count("<arg"), 0)

    def testServingFailsWhenAnotherConnectionOwnsTheName(self):
        errors = io.StringIO()
        with mock.patch.object(sys, "stderr", errors):
            self.assertIsNone(service.serve(self.connectTo("taken"), receivers.ReceiverList()))
        self.assertIn("Couldn't take " + serviceState.SERVICE_NAME, errors.getvalue())

    def testMainExitsOneWhenTheNameIsTaken(self):
        with mock.patch.object(service, "installExceptionHooks"), mock.patch.object(service.signal, "signal"), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(service.main(self.connectTo("second")), 1)


class ServiceProcessTests(unittest.TestCase):
    def setUp(self):
        requireTools(self)
        self.privateBus = PrivateBus()
        self.name = "processClient" + str(id(self))
        self.caller = Caller(QDBusConnection.connectToBus(self.privateBus.address, self.name))

    def tearDown(self):
        QDBusConnection.disconnectFromBus(self.name)
        self.privateBus.close()

    def testAnExceptionInASlotLeavesTheServiceRunning(self):
        log = self.privateBus.startFake("runService.py", "failOnce")
        self.assertTrue(waitUntil(lambda: self.caller.state() is not None, timeout=15.0))
        self.assertIsNone(self.privateBus.fakes[-1][0].poll())
        self.assertTrue(self.privateBus.waitForLine(log, "RuntimeError: the first state fails"))

    def testTheServiceSaysItStartedWithItsVersion(self):
        log = self.privateBus.startFake("runService.py")
        self.assertTrue(self.privateBus.waitForLine(log, "Mouse Battery service " + version.VERSION + " started"))

    def testSigtermStopsTheServiceThroughItsShutdownPath(self):
        self.privateBus.startFake("runService.py")
        self.assertTrue(waitUntil(lambda: self.caller.state() is not None, timeout=15.0))
        process = self.privateBus.fakes[-1][0]
        process.send_signal(signal.SIGTERM)
        # main returns app.exec()'s code only after ReceiverList.stop() has closed every node, while an unhandled SIGTERM ends the process with -15
        self.assertEqual(process.wait(10), 0)


if (__name__ == "__main__"):
    unittest.main()
