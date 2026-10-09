import errno
import io
import json
import os
import shutil
import signal
import sqlite3
import sys
import tempfile
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
import history
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


class BrokenStream(io.StringIO):
    # stderr once the journal's end of it has gone away
    def write(self, text):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


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

    def call(self, member, interface=serviceState.INTERFACE, timeout=3.0, arguments=None):
        self.answers = []
        message = QDBusMessage.createMethodCall(serviceState.SERVICE_NAME, serviceState.OBJECT_PATH, interface, member)
        if (arguments is not None):
            message.setArguments(arguments)
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
        changes = []
        self.receiverList.changed.connect(lambda: changes.append(1))
        # each report changes the level, so every one of them is a real change of state
        for i in range(100):
            device.send(bytes([0x11, 1, 5, 0x00, 80 - (i % 2), 0x04, 0, 0]).ljust(20, b"\x00"))
        # the waits end on what arrived instead of on a fixed time, so a slow runner gets longer and still sees every broadcast the flood caused
        self.assertTrue(waitUntil(lambda: len(changes) >= 100, timeout=10.0))
        self.assertTrue(waitUntil(lambda: not self.served[1].broadcastTimer.isActive() and self.caller.signals[-1] == serviceState.encodeState(self.receiverList), timeout=10.0))
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

    def testAnAccountWhoseBudgetRefilledIsDropped(self):
        adaptor = self.served[1]
        clock = [1000.0]
        with mock.patch.object(service, "READ_ALL_BURST", 2), mock.patch.object(service, "READ_ALL_REFILL_SECONDS", 0.1), mock.patch.object(service.time, "monotonic", lambda: clock[0]):
            for caller in range(50):
                self.assertTrue(adaptor.takeRead(caller))
                clock[0] += 0.001
            # a budget that's still refilling is kept, since dropping it would hand its account a full one early
            self.assertEqual(len(adaptor.callerBudgets), 50)
            clock[0] += 0.3
            self.assertTrue(adaptor.takeRead(1000))
        self.assertEqual(list(adaptor.callerBudgets), [1000])

    def testAReadAllLooksAtAFewBudgetsInsteadOfEveryAccountsBudget(self):
        adaptor = self.served[1]
        looked = []

        class CountedBudget(tuple):
            def __iter__(self):
                looked.append(1)
                return super().__iter__()

            def __getitem__(self, index):
                looked.append(1)
                return super().__getitem__(index)

        clock = [1000.0]
        with mock.patch.object(service.time, "monotonic", lambda: clock[0]):
            for caller in range(1000):
                adaptor.callerBudgets[caller] = CountedBudget((0.0, clock[0]))
                clock[0] += 0.001
            self.assertTrue(adaptor.takeRead(5000))
        self.assertLess(len(looked), 5)
        self.assertEqual(len(adaptor.callerBudgets), 1001)

    def testABudgetStillRefillingBehindAFullOneIsKept(self):
        adaptor = self.served[1]
        clock = [1000.0]
        with mock.patch.object(service, "READ_ALL_BURST", 2), mock.patch.object(service, "READ_ALL_REFILL_SECONDS", 1.0), mock.patch.object(service.time, "monotonic", lambda: clock[0]):
            self.assertTrue(adaptor.takeRead("full"))
            clock[0] += 0.5
            self.assertTrue(adaptor.takeRead("spent"))
            self.assertTrue(adaptor.takeRead("spent"))
            clock[0] += 0.6
            self.assertTrue(adaptor.takeRead("new"))
            # "spent" has 0.6 of a read back, so it's refused instead of handed a full budget
            self.assertFalse(adaptor.takeRead("spent"))
        self.assertEqual(sorted(adaptor.callerBudgets), ["new", "spent"])

    def testARefusedReadAllAnswersWithAnError(self):
        self.startWithMouse()
        caller = Caller(self.connectTo("other"))
        with mock.patch.object(service, "READ_ALL_BURST", 1):
            answers = [caller.call("ReadAll"), caller.call("ReadAll")]
        self.assertEqual(answers, [[], "org.freedesktop.DBus.Error.LimitsExceeded"])

    def testAReadAllThatJoinsAQueuedBatchCostsNoRead(self):
        device = self.startWithMouse()
        before = batteryReads(device)
        caller = Caller(self.connectTo("other"))
        with mock.patch.object(service, "READ_ALL_BURST", 2):
            # the first runs a batch and the second queues one behind the gap, which spends the burst
            self.assertEqual([caller.call("ReadAll"), caller.call("ReadAll")], [[], []])
            self.assertTrue(self.receiverList.readAllTimer.isActive())
            answers = [caller.call("ReadAll") for i in range(5)]
            self.assertTrue(self.receiverList.readAllTimer.isActive())
            self.assertEqual(answers, [[]] * 5)
            self.assertTrue(waitUntil(lambda: not self.receiverList.readAllTimer.isActive() and not self.receiverList.isBusy()))
            # once the queued batch has run, a call that would start device work is still held to the spent burst
            self.assertEqual(caller.call("ReadAll"), service.READ_ALL_LIMITED)
        QTest.qWait(200)
        self.assertEqual(batteryReads(device) - before, 2)

    def testAReadAllFromACallerItCantPlaceAnswersWithAnError(self):
        self.startWithMouse()
        adaptor = self.served[1]
        with mock.patch.object(adaptor, "callerOf", lambda message: None):
            self.assertEqual(self.caller.call("ReadAll"), "io.github.kymotsujason.LogitechMouseBattery1.Error.UnknownCaller")

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

    def testAGetStateThatSawABusyFlagNoBroadcastSentIsFollowedByOne(self):
        device = self.startWithMouse()
        adaptor = self.served[1]
        self.subscribe()
        device.handler = silent
        mouse = self.receiverList.mice[0]
        mouse.asleep = True
        self.receiverList.changed.emit()
        self.assertTrue(waitUntil(lambda: self.caller.signals and serviceState.decodeState(self.caller.signals[-1]).mice[0].asleep))
        self.assertTrue(waitUntil(lambda: not adaptor.broadcastTimer.isActive()))
        # a read of a mouse already asleep marks it busy without a change signal, and its timeout leaves the state where it was
        self.receiverList.readMouse(mouse)
        self.assertFalse(adaptor.broadcastTimer.isActive())
        count = len(self.caller.signals)
        self.assertTrue(self.caller.state().busy)
        self.assertTrue(waitUntil(lambda: not self.receiverList.isBusy()))
        # the broadcast this GetState sets off can still show the pending read, so the wait is for the one after it
        self.assertTrue(waitUntil(lambda: len(self.caller.signals) > count and not serviceState.decodeState(self.caller.signals[-1]).busy))
        self.assertTrue(waitUntil(lambda: not adaptor.broadcastTimer.isActive()))
        QTest.qWait(100)
        # a state the caller already holds from the last broadcast sets off no more of them
        count = len(self.caller.signals)
        for i in range(5):
            self.assertFalse(self.caller.state().busy)
        QTest.qWait(400)
        self.assertEqual(len(self.caller.signals), count)

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

    def testMainStartsTheReceiversWhenItsStartLineCantBePrinted(self):
        with mock.patch.object(service, "installExceptionHooks"), mock.patch.object(service.signal, "signal"), mock.patch.object(service, "serve", return_value=(object(), object())), mock.patch.object(service, "ReceiverList") as receiverList, mock.patch.object(service, "UPowerHistory"), mock.patch.object(service, "quitOnSigterm"), mock.patch.object(service, "QCoreApplication") as application, mock.patch.object(sys, "stderr", BrokenStream()):
            application.instance.return_value.exec.return_value = 0
            self.assertEqual(service.main(object()), 0)
        self.assertTrue(receiverList.return_value.start.called)

    def testMainBuildsTheRecorderFromTheStateFolderAndStopsItLast(self):
        manager = mock.Mock()
        with mock.patch.dict(os.environ, {"STATE_DIRECTORY": "/var/lib/example:/other"}), mock.patch.object(service, "installExceptionHooks"), mock.patch.object(service.signal, "signal"), mock.patch.object(service, "serve", return_value=(object(), object())), mock.patch.object(service.history, "Recorder") as recorder, mock.patch.object(service, "ReceiverList") as receiverList, mock.patch.object(service, "UPowerHistory") as upowerHistory, mock.patch.object(service, "quitOnSigterm"), mock.patch.object(service, "QCoreApplication") as application, mock.patch.object(sys, "stdout", io.StringIO()):
            application.instance.return_value.exec.return_value = 0
            manager.attach_mock(upowerHistory.return_value.stop, "upowerStop")
            manager.attach_mock(receiverList.return_value.stop, "receiversStop")
            manager.attach_mock(recorder.return_value.stop, "recorderStop")
            self.assertEqual(service.main(object()), 0)
        recorder.assert_called_once_with("/var/lib/example")
        receiverList.assert_called_once_with(recorder.return_value)
        self.assertEqual([call[0] for call in manager.mock_calls], ["upowerStop", "receiversStop", "recorderStop"])


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


class HistoryServiceTests(unittest.TestCase):
    def setUp(self):
        requireTools(self)
        self.privateBus = PrivateBus()
        self.nodes = FakeNodes()
        self.folder = tempfile.mkdtemp()
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
        self.names = ["historyService" + str(id(self)), "historyClient" + str(id(self))]
        self.serviceBus = QDBusConnection.connectToBus(self.privateBus.address, self.names[0])
        self.clientBus = QDBusConnection.connectToBus(self.privateBus.address, self.names[1])
        self.recorder = history.Recorder(self.folder)
        self.receiverList = receivers.ReceiverList(self.recorder)
        self.served = service.serve(self.serviceBus, self.receiverList)
        self.caller = Caller(self.clientBus)

    def tearDown(self):
        self.receiverList.stop()
        self.recorder.stop()
        for name in self.names:
            QDBusConnection.disconnectFromBus(name)
        for patch in self.patches:
            patch.stop()
        self.nodes.closeAll()
        self.privateBus.close()
        sip.delete(self.receiverList)
        shutil.rmtree(self.folder)

    def startWithMouse(self):
        device = self.nodes.add(RECEIVER, mouseHandler())
        self.receiverList.start()
        self.assertTrue(waitUntil(lambda: self.receiverList.mice and not self.receiverList.isBusy()))
        return device

    def history(self, key="02bc524c"):
        answer = self.caller.call("GetHistory", arguments=[key])
        return json.loads(answer[0]) if isinstance(answer, list) else answer

    def testGetHistoryAnswersTheMousesSummary(self):
        self.startWithMouse()
        # the stop writes the rows still waiting out the wake set
        self.recorder.stop()
        self.recorder.store.lastAttempt = None
        summary = self.history()
        self.assertEqual((summary["key"], summary["name"], summary["source"]), ("02bc524c", "PRO X3 SUPERSTRIKE", "hidpp"))

    def testAnUnknownKeyAnswersAnEmptyHistory(self):
        summary = self.history("ffffffff")
        self.assertEqual((summary["key"], summary["sessions"]), ("ffffffff", []))

    def testWithoutAFolderGetHistoryAnswersHistoryUnavailable(self):
        self.receiverList.recorder = history.Recorder(None)
        self.assertEqual(self.history(), serviceState.HISTORY_UNAVAILABLE)

    def failQueries(self):
        self.assertTrue(self.recorder.available())
        error = sqlite3.DatabaseError("database disk image is malformed")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
        self.recorder.store.db = mock.Mock(execute=mock.Mock(side_effect=error), close=self.recorder.store.db.close)

    def testACorruptQueryAnswersHistoryUnavailable(self):
        self.failQueries()
        self.assertEqual(self.history(), serviceState.HISTORY_UNAVAILABLE)
        self.assertTrue(os.path.exists(os.path.join(self.folder, history.INTEGRITY_FLAG)))
        self.assertEqual(self.history(), serviceState.HISTORY_UNAVAILABLE)

    def testACorruptQueryAnswersLevelsUnavailable(self):
        self.failQueries()
        self.assertEqual(self.levels(), serviceState.HISTORY_UNAVAILABLE)
        self.assertTrue(os.path.exists(os.path.join(self.folder, history.INTEGRITY_FLAG)))
        self.assertEqual(self.levels(), serviceState.HISTORY_UNAVAILABLE)

    def testGetHistoryHasABudgetOfItsOwn(self):
        answers = [self.history("ffffffff") for i in range(service.READ_ALL_BURST + 1)]
        self.assertEqual(answers[-1], serviceState.READ_ALL_LIMITED)
        self.assertTrue(all(isinstance(answer, dict) for answer in answers[:-1]))
        self.assertEqual(self.caller.call("ReadAll"), [])

    def levels(self, key="02bc524c", rangeName="year"):
        answer = self.caller.call("GetLevels", arguments=[key, rangeName])
        return json.loads(answer[0]) if isinstance(answer, list) else answer

    def testGetLevelsAnswersTheRangesSlots(self):
        levels = self.levels("ffffffff", "halfYear")
        self.assertEqual((levels["key"], levels["range"], levels["slots"]), ("ffffffff", "halfYear", []))

    def testAnUnknownRangeAnswersInvalidArgs(self):
        self.assertEqual(self.levels(rangeName="decade"), "org.freedesktop.DBus.Error.InvalidArgs")

    def testWithoutAFolderGetLevelsAnswersHistoryUnavailable(self):
        self.receiverList.recorder = history.Recorder(None)
        self.assertEqual(self.levels(), serviceState.HISTORY_UNAVAILABLE)

    def testAnOverlongKeyIsRefusedBeforeAnyWork(self):
        # a reply echoes the key, so a huge one would cost the service a reply three times its size
        key = "k" * (serviceState.MAX_KEY_LENGTH + 1)
        self.assertEqual((self.levels(key), self.history(key)), (serviceState.INVALID_ARGS, serviceState.INVALID_ARGS))
        self.assertIsInstance(self.levels("k" * serviceState.MAX_KEY_LENGTH), dict)

    def testGetLevelsHasABudgetOfItsOwn(self):
        # a person tapping through the six ranges spends six calls, so the burst is larger than the history's
        answers = [self.levels("ffffffff") for i in range(serviceState.LEVELS_BURST + 1)]
        self.assertEqual(answers[-1], serviceState.READ_ALL_LIMITED)
        self.assertTrue(all(isinstance(answer, dict) for answer in answers[:-1]))
        self.assertIsInstance(self.history("ffffffff"), dict)

    def testReadSilentReadsOnlyMiceThatDontReportOnTheirOwn(self):
        device = self.startWithMouse()
        before = batteryReads(device)
        with mock.patch.object(self.recorder, "reportsItself", lambda key: True):
            self.assertEqual(self.caller.call("ReadSilent"), [])
            QTest.qWait(300)
            self.assertEqual(batteryReads(device), before)
        self.assertEqual(self.caller.call("ReadSilent"), [])
        self.assertTrue(waitUntil(lambda: batteryReads(device) > before))

    def testReadSilentSpendsTheReadAllBudget(self):
        self.startWithMouse()
        for i in range(service.READ_ALL_BURST):
            self.caller.call("ReadSilent")
            QTest.qWait(1100)
        self.assertEqual(self.caller.call("ReadAll"), serviceState.READ_ALL_LIMITED)

    def testAReadSilentWhileABatchWaitsCostsNoRead(self):
        self.startWithMouse()
        self.caller.call("ReadAll")
        self.caller.call("ReadAll")
        self.assertTrue(self.receiverList.isReadAllQueued())
        with mock.patch.object(self.served[1], "takeRead", wraps=self.served[1].takeRead) as taken:
            self.assertEqual(self.caller.call("ReadSilent"), [])
            taken.assert_not_called()


class FakeUPowerMouse:
    def __init__(self, path, key, percent, pathKey=False):
        self.path = path
        self.key = key
        self.name = "MX Master 3S"
        self.reading = hidpp.BatteryReading(percent, 0)
        self.pathKey = pathKey


class UPowerHistoryTests(unittest.TestCase):
    def testUPowerReadingsGoToTheRecorderAndARemovalIsAGap(self):
        recorder = mock.Mock()
        glue = service.UPowerHistory(recorder, QDBusConnection("unconnected"))
        glue.onMouse(FakeUPowerMouse("/org/freedesktop/UPower/devices/mouse_0", "9a1b2c3d", 70))
        glue.onRemoved("/org/freedesktop/UPower/devices/mouse_0")
        glue.onRemoved("/org/freedesktop/UPower/devices/other")
        recorder.addReading.assert_called_once_with("9a1b2c3d", hidpp.BatteryReading(70, 0), history.ORIGIN_UPOWER, name="MX Master 3S", source=history.SOURCE_UPOWER, pathKey=False)
        recorder.addMarker.assert_called_once_with("9a1b2c3d", history.ORIGIN_GAP, source=history.SOURCE_UPOWER, pathKey=False)


class StateFolderTests(unittest.TestCase):
    def testTheFirstAbsoluteFolderIsTheHistorys(self):
        self.assertEqual(service.stateFolder({"STATE_DIRECTORY": "/var/lib/logitech-mouse-battery:/var/lib/other"}), "/var/lib/logitech-mouse-battery")

    def testNoFolderOrARelativeOneMeansNoHistory(self):
        self.assertEqual((service.stateFolder({}), service.stateFolder({"STATE_DIRECTORY": "relative"})), (None, None))


if (__name__ == "__main__"):
    unittest.main()
