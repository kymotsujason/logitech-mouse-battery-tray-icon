import os
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection, QDBusMessage, QDBusServiceWatcher, QDBusVariant
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
        with mock.patch.object(upower, "enumeratePaths", return_value=([], None)):
            self.watcher.ownerWatcher.serviceOwnerChanged.emit(upower.SERVICE, ":1.5", "")
        self.assertEqual(sorted(self.removed), sorted([PATH, OTHER_PATH]))

    def testANewOwnerDropsTheOldMiceAndListsAgain(self):
        with mock.patch.object(upower, "enumeratePaths", return_value=([PATH], None)):
            self.watcher.onOwnerChanged(upower.SERVICE, ":1.5", ":1.9")
        self.assertEqual(sorted(self.removed), sorted([PATH, OTHER_PATH]))
        self.assertEqual(self.watcher.paths, set())
        self.assertEqual(self.refreshed, [PATH])

    def testUPowerGoingAwayOnlyDropsTheMice(self):
        with mock.patch.object(upower, "enumeratePaths") as enumerate:
            self.watcher.onOwnerChanged(upower.SERVICE, ":1.5", "")
        self.assertEqual(sorted(self.removed), sorted([PATH, OTHER_PATH]))
        self.assertEqual(self.watcher.paths, set())
        self.assertFalse(enumerate.called)


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


def failWith(name):
    return lambda message: message.createErrorReply(name, "failed on purpose")


class RetryTests(unittest.TestCase):
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
        self.assertEqual(bus.calls, [("EnumerateDevices", upower.ROOT_PATH, 2000)])

    def testAFailedListingIsRetried(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.start()
        self.assertTrue(watcher.enumerateDue and watcher.retryTimer.isActive())

    def testNoUPowerOnTheBusIsntRetried(self):
        for name in upower.ABSENT_ERRORS:
            with self.subTest(name=name):
                bus, watcher = self.makeWatcher(failWith(name))
                watcher.start()
                self.assertFalse(watcher.enumerateDue or watcher.retryTimer.isActive())

    def testARetriedListingThatFindsNoUPowerStopsListing(self):
        names = ["org.freedesktop.DBus.Error.NoReply", "org.freedesktop.DBus.Error.ServiceUnknown"]
        bus, watcher = self.makeWatcher(lambda message: message.createErrorReply(names.pop(0), "failed on purpose"))
        watcher.start()
        self.assertTrue(watcher.enumerateDue)
        watcher.retryTimer.stop()
        watcher.onRetry()
        self.assertEqual((watcher.enumerateDue, watcher.retryTimer.isActive()), (False, False))

    def testAFailedGetAllKeepsTheMouseAndRetries(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.paths.add(PATH)
        watcher.refresh(PATH)
        self.assertEqual((self.removed, PATH in watcher.paths, PATH in watcher.retryPaths, watcher.retryTimer.isActive()), ([], True, True, True))

    def testAGoneDeviceIsRemoved(self):
        # the real UPower answers UnknownMethod for a device path that's gone
        self.assertIn("org.freedesktop.DBus.Error.UnknownMethod", upower.GONE_ERRORS)
        for name in upower.GONE_ERRORS:
            with self.subTest(name=name):
                bus, watcher = self.makeWatcher(failWith(name))
                watcher.paths.add(PATH)
                watcher.refresh(PATH)
                self.assertEqual((self.removed, watcher.retryPaths), ([PATH], set()))

    def testRetriesBackOffUpToACap(self):
        with mock.patch.object(upower, "RETRY_MS", 10), mock.patch.object(upower, "RETRY_MAX_MS", 40):
            bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
            intervals = []
            for i in range(4):
                watcher.retryTimer.stop()
                watcher.refresh(PATH)
                intervals.append(watcher.retryTimer.interval())
        self.assertEqual(intervals, [10, 20, 40, 40])

    def testAnOwnerChangeCancelsEveryRetry(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.refresh(PATH)
        watcher.onOwnerChanged(upower.SERVICE, ":1.5", "")
        self.assertEqual((watcher.retryPaths, watcher.enumerateDue, watcher.retryTimer.isActive()), (set(), False, False))

    def testStopEndsTheRetries(self):
        bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
        watcher.refresh(PATH)
        watcher.stop()
        self.assertFalse(watcher.retryTimer.isActive())


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

    def makeWatcher(self):
        watcher = upower.UPowerWatcher(self.connection)
        self.addCleanup(watcher.stop)
        watcher.mouseChanged.connect(self.changes.append)
        watcher.mouseRemoved.connect(self.removals.append)
        watcher.start()
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

    def testAnUnknownObjectErrorRemovesTheMouse(self):
        self.makeWatcher()
        self.control("FailNextGetAll", "org.freedesktop.DBus.Error.UnknownObject")
        self.setPercent("54")
        self.assertTrue(self.waitFor(lambda: self.removals == ["/org/freedesktop/UPower/devices/mouse_hidpp_battery_0"]))


if (__name__ == "__main__"):
    unittest.main()
