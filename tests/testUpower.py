import os
import unittest
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtDBus import QDBusConnection, QDBusMessage, QDBusServiceWatcher, QDBusVariant

import hidpp
import upower

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


class RecordingBus:
    def __init__(self):
        self.subscriptions = []

    def connect(self, service, path, interface, name, slot):
        self.subscriptions.append((service, path, interface, name))
        return True

    def call(self, message):
        return QDBusMessage()


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
        with mock.patch.object(upower, "enumeratePaths", return_value=[]):
            self.watcher.ownerWatcher.serviceOwnerChanged.emit(upower.SERVICE, ":1.5", "")
        self.assertEqual(sorted(self.removed), sorted([PATH, OTHER_PATH]))

    def testANewOwnerDropsTheOldMiceAndListsAgain(self):
        with mock.patch.object(upower, "enumeratePaths", return_value=[PATH]):
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


if (__name__ == "__main__"):
    unittest.main()
