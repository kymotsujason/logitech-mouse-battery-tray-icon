import contextlib
import errno
import io
import os
import sys
import time
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QTimer
from PyQt6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCall, QDBusServiceWatcher, QDBusVariant
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import dbusCalls
import hidpp
import upower
from tests.fakes import privateBus
from tests.fakes.recordingBus import RecordingBus

app = QApplication.instance() or QApplication([])
PATH = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"
OTHER_PATH = "/org/freedesktop/UPower/devices/mouse_hidpp_battery_1"


class FakeMessage:
    def __init__(self, path, arguments):
        self.pathValue = path
        self.argumentsValue = arguments

    def path(self):
        return self.pathValue

    def arguments(self):
        return self.argumentsValue


def mouseProperties(**changes):
    props = {"Type": 5, "NativePath": "hidpp_battery_0", "Vendor": "", "Model": "MX Master 3", "Serial": "12-ab-34-CD", "Percentage": 54.6, "State": 2, "BatteryLevel": 1}
    props.update(changes)
    return props


class ReadingTests(unittest.TestCase):
    def testPercentageMouse(self):
        mouse = upower.mouseFromProperties(PATH, mouseProperties())
        self.assertEqual(mouse, upower.UPowerMouse(PATH, "12ab34cd", "MX Master 3", hidpp.BatteryReading(55, 0)))

    def testAPathKeyIsMarked(self):
        self.assertTrue(upower.mouseFromProperties(PATH, mouseProperties(Serial="")).pathKey)
        self.assertFalse(upower.mouseFromProperties(PATH, mouseProperties()).pathKey)

    def testLevelOnlyMouseHasNoPercentage(self):
        reading = upower.mouseFromProperties(PATH, mouseProperties(BatteryLevel=3)).reading
        self.assertEqual(reading, hidpp.BatteryReading(None, 0, level="low"))
        self.assertEqual(hidpp.levelText(reading), "low")

    def testVendorMatchIgnoresCaseAndSuffix(self):
        self.assertIsNotNone(upower.mouseFromProperties(PATH, mouseProperties(NativePath="unrelated", Vendor="LOGITECH, Inc.")))

    def testDeviceWithNeitherNativePathNorVendorIsSkipped(self):
        self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties(NativePath="hid-5CK21J1000828-battery-19", Vendor="")))

    def testOtherDeviceTypesAreSkipped(self):
        self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties(Type=10)))

    def testStatesMapOntoChargingCodes(self):
        codes = {state: upower.mouseFromProperties(PATH, mouseProperties(State=state)).reading.charging for state in range(7)}
        self.assertEqual(codes, {0: 6, 1: 1, 2: 0, 3: 0, 4: 3, 5: 5, 6: 0})

    def testEmptySerialFallsBackToThePath(self):
        self.assertEqual(upower.mouseFromProperties(PATH, mouseProperties(Serial="")).key, PATH)

    def testPercentageRoundsHalfUpAndStaysInRange(self):
        self.assertEqual(upower.mouseFromProperties(PATH, mouseProperties(Percentage=54.5)).reading.percent, 55)
        self.assertEqual(upower.mouseFromProperties(PATH, mouseProperties(Percentage=100.7)).reading.percent, 100)

    def testWrappedValuesAreUnwrapped(self):
        props = {key: QDBusVariant(value) for key, value in mouseProperties().items()}
        self.assertEqual(upower.mouseFromProperties(PATH, props).key, "12ab34cd")

    def testUPowerCodesHaveNames(self):
        self.assertEqual(hidpp.chargingName(5), "pending charge")
        self.assertEqual(hidpp.chargingName(6), "unknown")

    def testHidppStatusAboveFourIsAChargingError(self):
        self.assertEqual(hidpp.parseUnifiedStatus(bytes([50, 0x04, 5]), True).charging, 4)

    def testANonFinitePercentageIsNoPercentage(self):
        for value in (float("nan"), float("inf")):
            self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties(Percentage=value)).reading.percent)

    def testUpdateTimeIsKeptAndZeroMeansUnknown(self):
        self.assertEqual(upower.mouseFromProperties(PATH, mouseProperties(UpdateTime=1800000000)).updatedAt, 1800000000)
        self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties(UpdateTime=0)).updatedAt)
        self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties()).updatedAt)

    def testAModelWithControlCharactersReadsClean(self):
        self.assertEqual(upower.mouseFromProperties(PATH, mouseProperties(Model="MX\x1b[2J Master")).name, "MX[2J Master")


class WatcherRoutingTests(unittest.TestCase):
    def setUp(self):
        self.bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "upowerTestBus")
        self.watcher = upower.UPowerWatcher(self.bus)
        self.refreshed = []
        self.watcher.refresh = self.refreshed.append

    def tearDown(self):
        QDBusConnection.disconnectFromBus("upowerTestBus")

    def testUnkeptPathWithoutTypeIsIgnored(self):
        message = FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []])
        self.watcher.onPropertiesChanged(message)
        self.assertEqual(self.refreshed, [])

    def testUnkeptPathWithTypeRefreshes(self):
        message = FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Type": 5}, []])
        self.watcher.onPropertiesChanged(message)
        self.assertEqual(self.refreshed, [PATH])

    def testKeptPathAlwaysRefreshes(self):
        self.watcher.paths.add(PATH)
        message = FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []])
        self.watcher.onPropertiesChanged(message)
        self.assertEqual(self.refreshed, [PATH])

    def testOtherInterfaceIsIgnored(self):
        message = FakeMessage(PATH, ["org.freedesktop.UPower", {"Type": 5}, []])
        self.watcher.onPropertiesChanged(message)
        self.assertEqual(self.refreshed, [])

    def testAPathBeingReadForTheFirstTimeRefreshesOnAnyChange(self):
        self.watcher.refreshing[PATH] = 1
        self.watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []]))
        self.assertEqual(self.refreshed, [PATH])

    def testAKeptPathWaitingOutItsBackoffIsLeftToItsRetry(self):
        self.watcher.paths.add(PATH)
        self.watcher.retryPaths.add(PATH)
        self.watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []]))
        self.watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Type": 5}, []]))
        self.assertEqual(self.refreshed, [])

    def testAPathWhoseRetryIsOutStillAsksForOneMore(self):
        self.watcher.retryPaths.add(PATH)
        self.watcher.refreshing[PATH] = 1
        self.watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []]))
        self.assertEqual(self.refreshed, [PATH])


class WatcherOwnerTests(unittest.TestCase):
    def setUp(self):
        self.bus = QDBusConnection.connectToBus("unix:path=/nonexistent/bus", "upowerOwnerBus")
        self.watcher = upower.UPowerWatcher(self.bus)
        self.removed = []
        self.refreshed = []
        self.watcher.mouseRemoved.connect(self.removed.append)
        self.watcher.refresh = self.refreshed.append
        self.watcher.paths.update({PATH, OTHER_PATH})

    def tearDown(self):
        QDBusConnection.disconnectFromBus("upowerOwnerBus")

    def testWatchesUPowerForANewOwner(self):
        self.watcher.watchOwner()
        self.assertEqual(self.watcher.ownerWatcher.watchedServices(), [upower.SERVICE])
        self.assertEqual(self.watcher.ownerWatcher.watchMode(), QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange)
        self.watcher.ownerWatcher.serviceOwnerChanged.emit(upower.SERVICE, ":1.5", "")
        self.assertEqual(sorted(self.removed), sorted([PATH, OTHER_PATH]))

    def testANewOwnerDropsTheOldMiceAndListsAgain(self):
        self.watcher.bus = ScriptedBus(lambda message: message.createReply([[PATH]]))
        self.watcher.onOwnerChanged(upower.SERVICE, ":1.5", ":1.9")
        self.assertEqual(sorted(self.removed), sorted([PATH, OTHER_PATH]))
        self.assertEqual(self.watcher.paths, set())
        self.assertTrue(waitFor(lambda: self.refreshed == [PATH]))

    def testUPowerGoingAwayOnlyDropsTheMice(self):
        self.watcher.bus = ScriptedBus(lambda message: message.createReply([[PATH]]))
        self.watcher.onOwnerChanged(upower.SERVICE, ":1.5", "")
        self.assertEqual(sorted(self.removed), sorted([PATH, OTHER_PATH]))
        self.assertEqual((self.watcher.paths, self.watcher.bus.calls), (set(), []))

    def testAListingStartedBeforeAnOwnerChangeIsSetAside(self):
        self.watcher.bus = ScriptedBus(lambda message: message.createReply([[OTHER_PATH]]))
        self.watcher.listDevices()
        self.watcher.onOwnerChanged(upower.SERVICE, ":1.5", "")
        QTest.qWait(50)
        self.assertEqual(self.refreshed, [])


class WatcherSubscriptionTests(unittest.TestCase):
    def testSignalsAreTakenOnlyFromUPower(self):
        bus = RecordingBus()
        upower.UPowerWatcher(bus).start()
        self.assertEqual(bus.subscriptions, [
            (upower.SERVICE, upower.ROOT_PATH, upower.SERVICE, "DeviceAdded"),
            (upower.SERVICE, upower.ROOT_PATH, upower.SERVICE, "DeviceRemoved"),
            (upower.SERVICE, "", upower.PROPERTIES_INTERFACE, "PropertiesChanged"),
        ])


class ScriptedBus(RecordingBus):
    # answers each call through a function, so a test can fail any call it likes
    def __init__(self, answer):
        super().__init__()
        self.answer = answer
        self.calls = []

    def call(self, message, mode=None, timeout=None):
        self.calls.append((message.member(), message.path(), timeout))
        return self.answer(message)

    def asyncCall(self, message, timeout=None):
        self.calls.append((message.member(), message.path(), timeout))
        return QDBusPendingCall.fromCompletedCall(self.answer(message))


class BrokenStream(io.StringIO):
    # stderr once the journal's end of it has gone away
    def write(self, text):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


def failWith(name):
    return lambda message: message.createErrorReply(name, "failed on purpose")


def waitFor(condition, timeout=3.0):
    for i in range(int(timeout * 50)):
        if (condition()):
            return True
        QTest.qWait(20)
    return condition()


def settle():
    # a scripted answer reaches the watcher from the event loop, a pass after the call
    QTest.qWait(20)


class RetryTests(unittest.TestCase):
    def setUp(self):
        # the watcher prints each failure it retries, which would fill the test output
        patch = mock.patch.object(sys, "stderr", io.StringIO())
        patch.start()
        self.addCleanup(patch.stop)

    def makeWatcher(self, answer):
        bus = ScriptedBus(answer)
        watcher = upower.UPowerWatcher(bus)
        self.addCleanup(watcher.stop)
        self.removed = []
        self.changed = []
        watcher.mouseRemoved.connect(self.removed.append)
        watcher.mouseChanged.connect(self.changed.append)
        return bus, watcher

    def testEveryCallHasTheDefaultTimeout(self):
        bus, watcher = self.makeWatcher(lambda message: message.createReply([[]]))
        watcher.start()
        settle()
        self.assertEqual(bus.calls, [("EnumerateDevices", upower.ROOT_PATH, 2000)])

    def testAFailedListingIsRetried(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.start()
        self.assertTrue(waitFor(lambda: watcher.enumerateDue and watcher.retryTimer.isActive()))

    def testAnErrorThatGetsRetriedIsPrintedOnce(self):
        errors = io.StringIO()
        with mock.patch.object(upower, "RETRY_MS", 10), mock.patch.object(upower, "RETRY_MAX_MS", 20), contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
            watcher.start()
            self.assertTrue(waitFor(lambda: len(bus.calls) >= 3))
        self.assertEqual(errors.getvalue(), "UPower answered org.freedesktop.DBus.Error.NoReply\n")

    def testAnErrorThatComesBackAfterUPowerAnsweredIsPrintedAgain(self):
        errors = io.StringIO()
        answers = [failWith("org.freedesktop.DBus.Error.NoReply"), lambda message: message.createReply([[]]), failWith("org.freedesktop.DBus.Error.NoReply")]
        with mock.patch.object(upower, "RETRY_MS", 10), mock.patch.object(upower, "RETRY_MAX_MS", 20), contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(lambda message: answers.pop(0)(message) if answers else message.createReply([[]]))
            watcher.start()
            self.assertTrue(waitFor(lambda: len(bus.calls) == 2 and not watcher.enumerateDue))
            watcher.listDevices()
            waitFor(lambda: len(bus.calls) >= 4 and not watcher.enumerateDue)
        self.assertEqual(errors.getvalue(), "UPower answered org.freedesktop.DBus.Error.NoReply\n" * 2)

    def testADeviceErrorThatComesBackAfterItsGetAllAnsweredIsPrintedAgain(self):
        errors = io.StringIO()
        answers = [failWith("org.freedesktop.DBus.Error.NoReply"), lambda message: message.createReply([mouseProperties()]), failWith("org.freedesktop.DBus.Error.NoReply")]
        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(lambda message: answers.pop(0)(message))
            for asked in (1, 2, 3):
                watcher.retryTimer.stop()
                watcher.refresh(PATH)
                self.assertTrue(waitFor(lambda: len(bus.calls) == asked and not watcher.refreshing))
        self.assertEqual(errors.getvalue(), ("UPower answered org.freedesktop.DBus.Error.NoReply for " + PATH + "\n") * 2)
        self.assertEqual(len(self.changed), 1)

    def testFailedCallsAreRetriedWhenTheirPrintFails(self):
        hooked = []
        with mock.patch.object(sys, "stderr", BrokenStream()), mock.patch.object(sys, "excepthook", lambda *info: hooked.append(info[1])):
            bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
            watcher.start()
            self.assertTrue(waitFor(lambda: watcher.enumerateDue and watcher.retryTimer.isActive()))
            watcher.retryTimer.stop()
            watcher.printedErrors.clear()
            watcher.refresh(PATH)
            self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths and watcher.retryTimer.isActive()))
        self.assertEqual(hooked, [])

    def testNoUPowerOnTheBusIsntRetried(self):
        for name in upower.ABSENT_ERRORS:
            with self.subTest(name=name):
                bus, watcher = self.makeWatcher(failWith(name))
                watcher.start()
                settle()
                self.assertEqual(len(bus.calls), 1)
                self.assertFalse(watcher.enumerateDue or watcher.retryTimer.isActive())

    def testARetriedListingThatFindsNoUPowerStopsListing(self):
        names = ["org.freedesktop.DBus.Error.NoReply", "org.freedesktop.DBus.Error.ServiceUnknown"]
        bus, watcher = self.makeWatcher(lambda message: message.createErrorReply(names.pop(0), "failed on purpose"))
        watcher.start()
        self.assertTrue(waitFor(lambda: watcher.enumerateDue))
        watcher.retryTimer.stop()
        watcher.onRetry()
        settle()
        self.assertEqual(len(bus.calls), 2)
        self.assertEqual((watcher.enumerateDue, watcher.retryTimer.isActive()), (False, False))

    def testAFailedGetAllKeepsTheMouseAndRetries(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.paths.add(PATH)
        watcher.refresh(PATH)
        self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths))
        self.assertEqual((self.removed, PATH in watcher.paths, PATH in watcher.retryPaths, watcher.retryTimer.isActive()), ([], True, True, True))

    def testAGoneDeviceIsRemoved(self):
        # the real UPower answers UnknownMethod for a device path that's gone
        self.assertIn("org.freedesktop.DBus.Error.UnknownMethod", upower.GONE_ERRORS)
        for name in upower.GONE_ERRORS:
            with self.subTest(name=name):
                bus, watcher = self.makeWatcher(failWith(name))
                watcher.paths.add(PATH)
                watcher.refresh(PATH)
                self.assertTrue(waitFor(lambda: self.removed))
                self.assertEqual((self.removed, watcher.retryPaths), ([PATH], set()))

    def testRetriesBackOffUpToACap(self):
        # long enough that no retry fires while the test waits for each answer
        with mock.patch.object(upower, "RETRY_MS", 1000), mock.patch.object(upower, "RETRY_MAX_MS", 4000):
            bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
            intervals = []
            for i in range(4):
                watcher.retryTimer.stop()
                watcher.refresh(PATH)
                self.assertTrue(waitFor(lambda: watcher.retryTimer.isActive()))
                intervals.append(watcher.retryTimer.interval())
        self.assertEqual(intervals, [1000, 2000, 4000, 4000])

    def backOffThenList(self, retryPaths):
        failing = [True]

        def answer(message):
            if (failing[0]):
                return message.createErrorReply("org.freedesktop.DBus.Error.NoReply", "failed on purpose")
            return message.createReply([[]])

        with mock.patch.object(upower, "RETRY_MS", 1000), mock.patch.object(upower, "RETRY_MAX_MS", 8000):
            bus, watcher = self.makeWatcher(answer)
            for i in range(2):
                watcher.retryTimer.stop()
                watcher.listDevices()
                self.assertTrue(waitFor(lambda: watcher.retryTimer.isActive()))
            watcher.retryTimer.stop()
            watcher.retryPaths |= retryPaths
            failing[0] = False
            watcher.listDevices()
            self.assertTrue(waitFor(lambda: not watcher.enumerateDue))
        return watcher.retryMs

    def testAListingThatGoesThroughStartsTheBackoffOver(self):
        self.assertEqual(self.backOffThenList(set()), 1000)

    def testAListingThatGoesThroughKeepsTheBackoffOfADeviceStillFailing(self):
        self.assertEqual(self.backOffThenList({PATH}), 4000)

    def testAnOwnerChangeCancelsEveryRetry(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.refresh(PATH)
        self.assertTrue(waitFor(lambda: watcher.retryTimer.isActive()))
        watcher.onOwnerChanged(upower.SERVICE, ":1.5", "")
        self.assertEqual((watcher.retryPaths, watcher.enumerateDue, watcher.retryTimer.isActive()), (set(), False, False))

    def testStopEndsTheRetries(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.refresh(PATH)
        self.assertTrue(waitFor(lambda: watcher.retryTimer.isActive()))
        watcher.stop()
        self.assertFalse(watcher.retryTimer.isActive())

    def testAReplyForADeviceRemovedMeanwhileIsSetAside(self):
        bus, watcher = self.makeWatcher(lambda message: message.createReply([mouseProperties()]))
        watcher.paths.add(PATH)
        watcher.refresh(PATH)
        watcher.onDeviceRemoved(FakeMessage("", [PATH]))
        settle()
        self.assertEqual((self.changed, self.removed, PATH in watcher.paths, watcher.refreshing), ([], [PATH], False, {}))

    def testADeviceAddedAgainWhileItsOldCallIsOutIsReadOnce(self):
        bus, watcher = self.makeWatcher(lambda message: message.createReply([mouseProperties()]))
        watcher.paths.add(PATH)
        watcher.refresh(PATH)
        watcher.onDeviceRemoved(FakeMessage("", [PATH]))
        watcher.onDeviceAdded(FakeMessage("", [PATH]))
        settle()
        self.assertEqual((len(bus.calls), len(self.changed), PATH in watcher.paths), (2, 1, True))

    def testChangesDuringAFailingCallStillBackOff(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.paths.add(PATH)
        watcher.refresh(PATH)
        watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {}, []]))
        self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths))
        settle()
        self.assertEqual((len(bus.calls), watcher.retryTimer.isActive(), watcher.refreshAgain), (1, True, set()))
        self.assertEqual(sys.stderr.getvalue(), "UPower answered org.freedesktop.DBus.Error.NoReply for " + PATH + "\n")

    def scriptedPercent(self, percent, props=None):
        # each GetAll answers with the percent at the time of the call, as UPower reads it when the call comes in
        props = mouseProperties() if props is None else props

        def answer(message):
            if (message.member() == "EnumerateDevices"):
                return message.createReply([[PATH]])
            return message.createReply([dict(props, Percentage=percent[0])])

        return answer

    def testAChangeDuringAMousesFirstReadIsReadToo(self):
        percent = [55.0]
        bus, watcher = self.makeWatcher(self.scriptedPercent(percent))
        watcher.refresh(PATH)
        percent[0] = 9.0
        watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []]))
        self.assertTrue(waitFor(lambda: not watcher.refreshing))
        settle()
        self.assertEqual(([mouse.reading.percent for mouse in self.changed], len(bus.calls), PATH in watcher.paths), ([9], 2, True))

    def testAChangeDuringTheFirstReadOfAnotherDeviceEndsWithNothingKept(self):
        percent = [55.0]
        bus, watcher = self.makeWatcher(self.scriptedPercent(percent, mouseProperties(Type=2)))
        watcher.refresh(PATH)
        watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []]))
        self.assertTrue(waitFor(lambda: not watcher.refreshing))
        settle()
        self.assertEqual((len(bus.calls), self.changed, self.removed, watcher.paths, watcher.refreshAgain), (2, [], [], set(), set()))

    def testADeviceRemovedDuringItsFirstReadStaysGoneThroughAChange(self):
        percent = [55.0]
        bus, watcher = self.makeWatcher(self.scriptedPercent(percent))
        watcher.refresh(PATH)
        watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []]))
        watcher.onDeviceRemoved(FakeMessage("", [PATH]))
        settle()
        settle()
        self.assertEqual((len(bus.calls), self.changed, self.removed, watcher.paths, watcher.refreshing, watcher.refreshAgain), (1, [], [], set(), {}, set()))

    def testChangesWhileAMouseBacksOffWaitForItsRetry(self):
        with mock.patch.object(upower, "RETRY_MS", 5000):
            bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
            watcher.paths.add(PATH)
            watcher.refresh(PATH)
            self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths and not watcher.refreshing))
            for i in range(3):
                watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []]))
                settle()
            self.assertEqual((len(bus.calls), watcher.retryTimer.isActive(), PATH in watcher.retryPaths), (1, True, True))

    def testAListingLeavesAMouseThatBacksOffToItsRetry(self):
        def answer(message):
            if (message.member() == "EnumerateDevices"):
                return message.createReply([[PATH]])
            return message.createErrorReply("org.freedesktop.DBus.Error.NoReply", "failed on purpose")

        with mock.patch.object(upower, "RETRY_MS", 5000):
            bus, watcher = self.makeWatcher(answer)
            watcher.refresh(PATH)
            self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths and not watcher.refreshing))
            watcher.listDevices()
            self.assertTrue(waitFor(lambda: len(bus.calls) >= 2))
            settle()
            settle()
        self.assertEqual([call[0] for call in bus.calls], ["GetAll", "EnumerateDevices"])

    def testARetryThatGoesThroughReadsTheChangesMadeWhileItWaited(self):
        answers = [failWith("org.freedesktop.DBus.Error.NoReply"), lambda message: message.createReply([mouseProperties(Percentage=9.0)])]
        with mock.patch.object(upower, "RETRY_MS", 50):
            bus, watcher = self.makeWatcher(lambda message: answers.pop(0)(message))
            watcher.paths.add(PATH)
            watcher.refresh(PATH)
            self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths and not watcher.refreshing))
            watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 9.0}, []]))
            self.assertTrue(waitFor(lambda: self.changed))
        self.assertEqual(([mouse.reading.percent for mouse in self.changed], len(bus.calls), watcher.retryPaths), ([9], 2, set()))

    def testAReplyAfterAStopIsSetAside(self):
        bus, watcher = self.makeWatcher(lambda message: message.createReply([{"Type": 5, "NativePath": "hidpp_battery_0", "Serial": "1"}]))
        watcher.refresh(PATH)
        watcher.stop()
        settle()
        self.assertEqual((self.changed, watcher.paths), ([], set()))


class BusTests(unittest.TestCase):
    def setUp(self):
        privateBus.requireTools(self)
        self.bus = privateBus.PrivateBus()
        self.addCleanup(self.bus.close)
        self.percentFile = os.path.join(self.bus.folder, "percent")
        with open(self.percentFile, "w") as f:
            f.write("55")
        self.log = self.bus.startFake("fakeUPower.py", self.percentFile)
        self.assertTrue(self.bus.waitForLine(self.log, "owns the upower name"))
        self.connection = QDBusConnection.connectToBus(self.bus.address, "upowerPrivateBus")
        self.addCleanup(QDBusConnection.disconnectFromBus, "upowerPrivateBus")
        # a second connection drives the fake, so the test process never imports PyGObject
        self.controlBus = QDBusConnection.connectToBus(self.bus.address, "upowerControlBus")
        self.addCleanup(QDBusConnection.disconnectFromBus, "upowerControlBus")
        self.changes = []
        self.removals = []
        # a failed GetAll the fake is told to give is printed by the watcher
        patch = mock.patch.object(sys, "stderr", io.StringIO())
        patch.start()
        self.addCleanup(patch.stop)

    def makeWatcher(self):
        watcher = upower.UPowerWatcher(self.connection)
        self.addCleanup(watcher.stop)
        watcher.mouseChanged.connect(self.changes.append)
        watcher.mouseRemoved.connect(self.removals.append)
        watcher.start()
        # the first listing answers later now, and a test that drives the fake has to wait for it first
        self.assertTrue(self.waitFor(lambda: self.changes))
        return watcher

    def control(self, method, value):
        message = QDBusMessage.createMethodCall(upower.SERVICE, upower.ROOT_PATH, "test.FakeUPower", method)
        message.setArguments([value])
        self.assertIsNone(dbusCalls.failure(dbusCalls.call(self.controlBus, message)))

    def waitFor(self, condition, timeout=3.0):
        for i in range(int(timeout * 50)):
            if (condition()):
                return True
            QTest.qWait(20)
        return condition()

    def setPercent(self, value):
        with open(self.percentFile, "w") as f:
            f.write(value)

    def testListsTheMouseWithAPlainPath(self):
        self.makeWatcher()
        self.assertTrue(self.waitFor(lambda: self.changes))
        self.assertEqual([(mouse.path, type(mouse.path), mouse.key) for mouse in self.changes], [("/org/freedesktop/UPower/devices/mouse_hidpp_battery_0", str, "12ab34cd")])

    def testListMiceReadsTheFake(self):
        found, errors = upower.listMice(self.connection)
        self.assertEqual(([mouse.reading.percent for mouse in found], errors), ([55], []))

    def testRemovedAndAddedFollowTheMouse(self):
        self.makeWatcher()
        self.control("SetPresent", False)
        self.assertTrue(self.waitFor(lambda: self.removals == ["/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"]))
        self.control("SetPresent", True)
        self.assertTrue(self.waitFor(lambda: len(self.changes) == 2))

    def testAFailedGetAllKeepsTheMouseAndRecovers(self):
        with mock.patch.object(upower, "RETRY_MS", 50):
            self.makeWatcher()
            self.control("FailNextGetAll", "org.freedesktop.DBus.Error.NoReply")
            self.setPercent("54")
            self.assertTrue(self.waitFor(lambda: self.changes[-1].reading.percent == 54))
        self.assertEqual(self.removals, [])

    def testAHungUPowerLeavesTheEventLoopRunning(self):
        watcher = self.makeWatcher()
        self.assertTrue(self.waitFor(lambda: self.changes))
        self.control("SetHanging", True)
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(10)
        started = time.monotonic()
        watcher.refresh(PATH)
        returned = time.monotonic() - started
        self.assertTrue(self.waitFor(lambda: PATH in watcher.retryPaths, timeout=5.0))
        timer.stop()
        # the audit measured 0 ticks over the 2.10 s a GetAll to a hung UPower held the tray
        self.assertLess(returned, 0.1)
        self.assertGreater(len(ticks), 100)
        self.assertEqual(self.removals, [])

    def testABurstOfChangesDuringAHungGetAllAsksOnce(self):
        watcher = self.makeWatcher()
        self.assertTrue(self.waitFor(lambda: self.changes))
        self.control("SetHanging", True)
        for i in range(5):
            watcher.refresh(PATH)
        QTest.qWait(300)
        self.assertEqual(self.bus.lines(self.log).count("held GetAll"), 1)

    def testAnUnknownObjectErrorRemovesTheMouse(self):
        self.makeWatcher()
        self.control("FailNextGetAll", "org.freedesktop.DBus.Error.UnknownObject")
        self.setPercent("54")
        self.assertTrue(self.waitFor(lambda: self.removals == ["/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"]))


if (__name__ == "__main__"):
    unittest.main()
