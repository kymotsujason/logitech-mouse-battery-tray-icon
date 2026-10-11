import contextlib
import errno
import io
import os
import shutil
import sys
import tempfile
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

    def testAPropertyOfTheWrongTypeIsReadAsMissing(self):
        cases = {
            "State": lambda mouse: mouse.reading.charging == upower.UNKNOWN_CODE,
            "BatteryLevel": lambda mouse: mouse.reading == hidpp.BatteryReading(55, 0),
            "Percentage": lambda mouse: mouse.reading.percent is None,
            "Model": lambda mouse: mouse.name is None,
            "Serial": lambda mouse: (mouse.key, mouse.pathKey) == (PATH, True),
            "UpdateTime": lambda mouse: mouse.updatedAt is None,
            "NativePath": lambda mouse: mouse is None,
        }
        for name, holds in cases.items():
            wrong = [5, 5.5] if name in ("Model", "Serial", "NativePath") else ["5"] if name in ("Percentage", "UpdateTime") else ["5", 5.5]
            for value in [[1, 2], {"a": 1}, True] + wrong:
                with self.subTest(name=name, value=value):
                    self.assertTrue(holds(upower.mouseFromProperties(PATH, mouseProperties(**{name: value}))))

    def testATypeOfTheWrongTypeIsNoMouse(self):
        for value in ("5", [5], 5.0, True):
            with self.subTest(value=value):
                self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties(Type=value)))

    def testAVendorOfTheWrongTypeLeavesTheNativePathToDecide(self):
        self.assertIsNotNone(upower.mouseFromProperties(PATH, mouseProperties(Vendor=["Logitech"])))
        self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties(Vendor=["Logitech"], NativePath="unrelated")))

    def testAMissingPercentageIsNoPercentage(self):
        props = mouseProperties()
        del props["Percentage"]
        self.assertIsNone(upower.mouseFromProperties(PATH, props).reading.percent)

    def testIntegerTypesOfEveryWidthStillRead(self):
        mouse = upower.mouseFromProperties(PATH, mouseProperties(Percentage=54, UpdateTime=2 ** 40, BatteryLevel=8))
        self.assertEqual((mouse.reading, mouse.updatedAt), (hidpp.BatteryReading(None, 0, level="full"), float(2 ** 40)))
        self.assertEqual(upower.mouseFromProperties(PATH, mouseProperties(Percentage=54)).reading.percent, 54)

    def testPropertiesThatArentADictAreNoMouse(self):
        for props in ("text", [mouseProperties()], 5, None):
            with self.subTest(props=props):
                self.assertIsNone(upower.mouseFromProperties(PATH, props))

    def testWrongPropertiesNamesEachOneOfTheWrongType(self):
        self.assertEqual(upower.wrongProperties(mouseProperties(State="2", Model=5, Unknown=[1])), ["Model", "State"])
        self.assertEqual(upower.wrongProperties(mouseProperties(UpdateTime=1800000000)), [])

    def testUnknownStateOnAKernelBatteryIsAsleep(self):
        mouse = upower.mouseFromProperties(PATH, mouseProperties(State=0))
        self.assertTrue(mouse.asleep)
        # the reading is kept as UPower gave it
        self.assertEqual(mouse.reading, hidpp.BatteryReading(55, hidpp.CHARGING_UNKNOWN))

    def testUnknownStateFromAnotherDriverIsntAsleep(self):
        self.assertFalse(upower.mouseFromProperties(PATH, mouseProperties(State=0, NativePath="unrelated", Vendor="Logitech")).asleep)

    def testAwakeStatesArentAsleep(self):
        for state in range(1, 7):
            self.assertFalse(upower.mouseFromProperties(PATH, mouseProperties(State=state)).asleep, state)

    def testAStateOfTheWrongTypeIsntAsleep(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(upower.mouseFromProperties(PATH, mouseProperties(State="0")).asleep)
        props = mouseProperties()
        del props["State"]
        self.assertFalse(upower.mouseFromProperties(PATH, props).asleep)


class CableTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.supplies = os.path.join(self.root, "class", "power_supply")
        self.udev = os.path.join(self.root, "udev")
        os.makedirs(self.supplies)
        os.makedirs(self.udev)
        for patch in (mock.patch.object(upower, "SYSFS_POWER_SUPPLY", self.supplies), mock.patch.object(upower, "UDEV_DATA", self.udev)):
            patch.start()
            self.addCleanup(patch.stop)

    def addInput(self, hidDevice, name, flags):
        os.makedirs(os.path.join(hidDevice, "input", name))
        with open(os.path.join(self.udev, "+input:" + name), "w") as f:
            f.write("I:4454399\n" + "".join("E:" + flag + "=1\n" for flag in flags))

    def linkBattery(self, name, hidDevice):
        supply = os.path.join(self.supplies, name)
        os.makedirs(supply)
        os.symlink(hidDevice, os.path.join(supply, "device"))

    def addCable(self, mouseFlags=("ID_INPUT_MOUSE",)):
        # the G703 on its cable as the tester's report showed it, with the battery on interface 2 and no input of its own
        usb = os.path.join(self.root, "devices", "usb1", "1-9")
        interfaces = []
        for number in range(3):
            interface = os.path.join(usb, "1-9:1." + str(number))
            os.makedirs(interface)
            with open(os.path.join(interface, "bInterfaceNumber"), "w") as f:
                f.write("0" + str(number) + "\n")
            interfaces.append(interface)
        batteryHid = os.path.join(interfaces[2], "0003:046D:C090.0020")
        os.makedirs(batteryHid)
        self.addInput(os.path.join(interfaces[0], "0003:046D:C090.001E"), "input51", mouseFlags)
        self.addInput(os.path.join(interfaces[1], "0003:046D:C090.001F"), "input52", ("ID_INPUT_KEYBOARD",))
        self.linkBattery("hidpp_battery_4", batteryHid)

    def addReceiver(self):
        # a paired keyboard's battery next to a paired mouse, both under the receiver's own HID device
        interface = os.path.join(self.root, "devices", "usb1", "1-8", "1-8:1.2")
        os.makedirs(interface)
        with open(os.path.join(interface, "bInterfaceNumber"), "w") as f:
            f.write("02\n")
        receiverHid = os.path.join(interface, "0003:046D:C539.001C")
        keyboardHid = os.path.join(receiverHid, "0003:046D:4075.0021")
        os.makedirs(keyboardHid)
        self.addInput(os.path.join(receiverHid, "0003:046D:4086.001D"), "input50", ("ID_INPUT_MOUSE",))
        self.addInput(keyboardHid, "input53", ("ID_INPUT_KEYBOARD",))
        self.linkBattery("hidpp_battery_5", keyboardHid)

    def keyboardBattery(self, nativePath):
        return upower.mouseFromProperties(PATH, mouseProperties(Type=6, NativePath=nativePath, Model="G703 LIGHTSPEED Wireless Gaming Mouse w/ HERO"))

    def testACableBatteryNextToAMouseInputIsKept(self):
        self.addCable()
        mouse = self.keyboardBattery("hidpp_battery_4")
        self.assertEqual(mouse, upower.UPowerMouse(PATH, "12ab34cd", "G703 LIGHTSPEED Wireless Gaming Mouse w/ HERO", hidpp.BatteryReading(55, 0)))

    def testAUdevFileWithAByteItCantDecodeStillKeepsTheCable(self):
        self.addCable()
        with open(os.path.join(self.udev, "+input:input51"), "wb") as f:
            f.write(b"I:4454399\nE:NAME=G703 \xff\nE:ID_INPUT_MOUSE=1\n")
        mouse = self.keyboardBattery("hidpp_battery_4")
        self.assertEqual(mouse, upower.UPowerMouse(PATH, "12ab34cd", "G703 LIGHTSPEED Wireless Gaming Mouse w/ HERO", hidpp.BatteryReading(55, 0)))

    def testAReceiverKeyboardNextToAPairedMouseIsLeftOut(self):
        self.addReceiver()
        self.assertIsNone(self.keyboardBattery("hidpp_battery_5"))

    def testACableWithoutTheMousesUdevFileIsLeftOut(self):
        self.addCable()
        os.remove(os.path.join(self.udev, "+input:input51"))
        self.assertIsNone(self.keyboardBattery("hidpp_battery_4"))

    def testACableWithOnlyKeyboardInputsIsLeftOut(self):
        self.addCable(mouseFlags=("ID_INPUT_KEYBOARD",))
        self.assertIsNone(self.keyboardBattery("hidpp_battery_4"))

    def testANativePathThatLeavesTheFolderIsLeftOut(self):
        self.addCable()
        for nativePath in ("hidpp_battery_4/../hidpp_battery_4", "../hidpp_battery_4", "hidpp_battery_", "hidpp_battery_4x"):
            self.assertIsNone(self.keyboardBattery(nativePath), nativePath)

    def testABatteryThatsGoneIsLeftOut(self):
        self.addCable()
        self.assertIsNone(self.keyboardBattery("hidpp_battery_7"))

    def testAMouseTypedDeviceNeedsNoTree(self):
        self.assertIsNotNone(upower.mouseFromProperties(PATH, mouseProperties(NativePath="hidpp_battery_4")))

    def testOtherTypesStayLeftOutEvenNextToAMouse(self):
        self.addCable()
        self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties(Type=10, NativePath="hidpp_battery_4")))

    def testAMissingUdevFileLeavesTheCableUndecided(self):
        self.addCable()
        os.remove(os.path.join(self.udev, "+input:input51"))
        self.assertIsNone(upower.cableHasMouse("hidpp_battery_4"))
        self.assertIsNotNone(upower.mouseFromProperties(PATH, mouseProperties(Type=6, NativePath="hidpp_battery_4"), undecided=True))

    def testACableWithOnlyKeyboardInputsIsDecided(self):
        self.addCable(mouseFlags=("ID_INPUT_KEYBOARD",))
        self.assertIs(upower.cableHasMouse("hidpp_battery_4"), False)
        self.assertIsNone(upower.mouseFromProperties(PATH, mouseProperties(Type=6, NativePath="hidpp_battery_4"), undecided=True))

    def cableWatcher(self, nativePath="hidpp_battery_4"):
        bus = ScriptedBus(lambda message: message.createReply([mouseProperties(Type=6, NativePath=nativePath)]))
        watcher = upower.UPowerWatcher(bus)
        self.addCleanup(watcher.stop)
        self.changed = []
        self.removed = []
        watcher.mouseChanged.connect(self.changed.append)
        watcher.mouseRemoved.connect(self.removed.append)
        return bus, watcher

    def readAndSettle(self, watcher):
        self.assertTrue(waitFor(lambda: not watcher.refreshing))
        settle()

    def batteryChange(self, watcher):
        watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, {"Percentage": 50.0, "State": 1}, []]))

    def testACableLeftOutWhileItsUdevFileWasMissingComesBackOnTheNextChange(self):
        # udevd can write the mouse input's file after UPower lists the battery
        self.addCable()
        udevFile = os.path.join(self.udev, "+input:input51")
        saved = udevFile + ".saved"
        os.rename(udevFile, saved)
        bus, watcher = self.cableWatcher()
        watcher.refresh(PATH)
        self.readAndSettle(watcher)
        self.assertEqual((self.changed, watcher.paths), ([], set()))
        os.rename(saved, udevFile)
        self.batteryChange(watcher)
        self.readAndSettle(watcher)
        self.assertEqual(([mouse.path for mouse in self.changed], watcher.paths, self.removed), ([PATH], {PATH}, []))

    def testAKeptCableKeepsItsMouseThroughAMissingUdevFile(self):
        self.addCable()
        bus, watcher = self.cableWatcher()
        watcher.refresh(PATH)
        self.readAndSettle(watcher)
        os.remove(os.path.join(self.udev, "+input:input51"))
        self.batteryChange(watcher)
        self.readAndSettle(watcher)
        self.assertEqual((len(self.changed), self.removed, watcher.paths), (2, [], {PATH}))

    def testAKeptCableWhoseMouseInputIsGoneIsDropped(self):
        self.addCable()
        bus, watcher = self.cableWatcher()
        watcher.refresh(PATH)
        self.readAndSettle(watcher)
        os.remove(os.path.join(self.udev, "+input:input51"))
        shutil.rmtree(os.path.join(self.root, "devices", "usb1", "1-9", "1-9:1.0", "0003:046D:C090.001E", "input", "input51"))
        self.batteryChange(watcher)
        self.readAndSettle(watcher)
        self.assertEqual((len(self.changed), self.removed, watcher.paths), (1, [PATH], set()))

    def testAReceiverKeyboardIsReadOnceForEachChangeUntilItsRemoved(self):
        self.addReceiver()
        bus, watcher = self.cableWatcher("hidpp_battery_5")
        watcher.refresh(PATH)
        self.readAndSettle(watcher)
        for i in range(2):
            self.batteryChange(watcher)
            self.readAndSettle(watcher)
        watcher.onDeviceRemoved(FakeMessage("", [PATH]))
        self.batteryChange(watcher)
        settle()
        self.assertEqual((len(bus.calls), self.changed, watcher.paths), (3, [], set()))


class ReplyTests(unittest.TestCase):
    def reply(self, *arguments):
        return upower.enumerateCall().createReply(list(arguments))

    def testAListingOfAnotherTypeIsUnreadable(self):
        for arguments in ([], ["/org/freedesktop/UPower/devices/mouse_0"], [5], [{"a": 1}]):
            with self.subTest(arguments=arguments):
                self.assertEqual(upower.pathsFrom(self.reply(*arguments)), ([], upower.UNREADABLE))

    def testAListingKeepsOnlyObjectPaths(self):
        self.assertEqual(upower.pathsFrom(self.reply([PATH, 5, "", "relative", OTHER_PATH])), ([PATH, OTHER_PATH], None))

    def testPropertiesOfAnotherTypeAreUnreadable(self):
        for arguments in ([], ["text"], [[1, 2]], [5]):
            with self.subTest(arguments=arguments):
                self.assertEqual(upower.propertiesFrom(self.reply(*arguments)), (None, upower.UNREADABLE))

    def testPropertiesThatAreADictRead(self):
        self.assertEqual(upower.propertiesFrom(self.reply({"Type": 5})), ({"Type": 5}, None))


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

    def testChangesThatArentADictAreIgnoredForAnUnkeptPath(self):
        for changed in (5, None, ["Type"], "Type"):
            with self.subTest(changed=changed):
                self.watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, changed, []]))
                self.watcher.onPropertiesChanged(FakeMessage(PATH, [upower.DEVICE_INTERFACE, QDBusVariant(changed), []]))
        self.assertEqual(self.refreshed, [])

    def testADeviceAddedWithoutAnObjectPathIsIgnored(self):
        for arguments in ([], [5], [""], ["relative"]):
            with self.subTest(arguments=arguments):
                self.watcher.onDeviceAdded(FakeMessage("", arguments))
        self.assertEqual(self.refreshed, [])
        self.watcher.onDeviceAdded(FakeMessage("", [PATH]))
        self.assertEqual(self.refreshed, [PATH])

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

    def testANewOwnerForgetsTheTurnedAwayCables(self):
        self.watcher.bus = ScriptedBus(lambda message: message.createReply([[]]))
        self.watcher.recheckPaths.add("/org/freedesktop/UPower/devices/keyboard_hidpp_battery_5")
        self.watcher.onOwnerChanged(upower.SERVICE, ":1.5", "")
        self.assertEqual(self.watcher.recheckPaths, set())

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
        self.assertEqual(errors.getvalue(), "UPower answered org.freedesktop.DBus.Error.NoReply\nUPower answers again\n" * 2)

    def testADeviceErrorThatComesBackAfterItsGetAllAnsweredIsPrintedAgain(self):
        errors = io.StringIO()
        answers = [failWith("org.freedesktop.DBus.Error.NoReply"), lambda message: message.createReply([mouseProperties()]), failWith("org.freedesktop.DBus.Error.NoReply")]
        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(lambda message: answers.pop(0)(message))
            for asked in (1, 2, 3):
                watcher.retryTimer.stop()
                watcher.refresh(PATH)
                self.assertTrue(waitFor(lambda: len(bus.calls) == asked and not watcher.refreshing))
        self.assertEqual(errors.getvalue(), "UPower answered org.freedesktop.DBus.Error.NoReply for " + PATH + "\nUPower answers again for " + PATH + "\nUPower answered org.freedesktop.DBus.Error.NoReply for " + PATH + "\n")
        self.assertEqual(len(self.changed), 1)

    def testOnlyTheDeviceWhoseCallWentThroughSaysItAnswersAgain(self):
        errors = io.StringIO()
        failing = {PATH, OTHER_PATH}

        def answer(message):
            if (message.path() in failing):
                return message.createErrorReply("org.freedesktop.DBus.Error.NoReply", "failed on purpose")
            return message.createReply([mouseProperties()])

        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(answer)
            watcher.refresh(PATH)
            watcher.refresh(OTHER_PATH)
            self.assertTrue(waitFor(lambda: watcher.retryPaths == {PATH, OTHER_PATH}))
            failing.discard(PATH)
            watcher.retryTimer.stop()
            watcher.onRetry()
            self.assertTrue(waitFor(lambda: watcher.retryPaths == {OTHER_PATH} and not watcher.refreshing))
            settle()
        self.assertEqual(errors.getvalue().splitlines(), ["UPower answered org.freedesktop.DBus.Error.NoReply for " + PATH, "UPower answered org.freedesktop.DBus.Error.NoReply for " + OTHER_PATH, "UPower answers again for " + PATH])

    def testADeviceRemovedWhileFailingEndsWithoutALineAndIsPrintedAgainWhenItFailsLater(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
            watcher.refresh(PATH)
            self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths))
            watcher.retryTimer.stop()
            watcher.onDeviceRemoved(FakeMessage("", [PATH]))
            watcher.onDeviceAdded(FakeMessage("", [PATH]))
            self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths and not watcher.refreshing))
        self.assertEqual(errors.getvalue(), ("UPower answered org.freedesktop.DBus.Error.NoReply for " + PATH + "\n") * 2)

    def testAListingThatGoesThroughSaysNothingWhenOnlyADeviceFailed(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(lambda message: message.createReply([[]]) if message.member() == "EnumerateDevices" else message.createErrorReply("org.freedesktop.DBus.Error.NoReply", "failed on purpose"))
            watcher.refresh(PATH)
            self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths))
            watcher.listDevices()
            self.assertTrue(waitFor(lambda: len(bus.calls) == 2))
            settle()
        self.assertEqual(errors.getvalue(), "UPower answered org.freedesktop.DBus.Error.NoReply for " + PATH + "\n")

    def testANewUPowerDropsTheDevicesFailuresButNotTheListings(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(failWith("org.freedesktop.DBus.Error.NoReply"))
            watcher.reportError("org.freedesktop.DBus.Error.NoReply")
            watcher.reportError("org.freedesktop.DBus.Error.NoReply", PATH)
            watcher.bus = ScriptedBus(lambda message: message.createReply([[]]))
            watcher.onOwnerChanged(upower.SERVICE, ":1.5", ":1.9")
            self.assertTrue(waitFor(lambda: watcher.bus.calls))
            settle()
        self.assertEqual(errors.getvalue().splitlines(), ["UPower answered org.freedesktop.DBus.Error.NoReply", "UPower answered org.freedesktop.DBus.Error.NoReply for " + PATH, "UPower answers again"])
        self.assertEqual(watcher.printedErrors, {})

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

    def testAnUnreadableGetAllKeepsTheMouseAndRetries(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(lambda message: message.createReply(["not properties"]))
            watcher.paths.add(PATH)
            watcher.refresh(PATH)
            self.assertTrue(waitFor(lambda: PATH in watcher.retryPaths))
        self.assertEqual((self.removed, PATH in watcher.paths, watcher.retryTimer.isActive()), ([], True, True))
        self.assertEqual(errors.getvalue(), "UPower answered " + upower.UNREADABLE + " for " + PATH + "\n")

    def testAnUnreadableListingIsRetried(self):
        bus, watcher = self.makeWatcher(lambda message: message.createReply(["not paths"]))
        watcher.start()
        self.assertTrue(waitFor(lambda: watcher.enumerateDue and watcher.retryTimer.isActive()))
        self.assertEqual(len(bus.calls), 1)

    def testAPropertyOfTheWrongTypeIsPrintedOnceAndTheRestStillReads(self):
        errors = io.StringIO()
        answers = [mouseProperties(State=[2]), mouseProperties(State=[2], Percentage=40.0), mouseProperties(State=[2], Model=5), mouseProperties(), mouseProperties(State=[2])]
        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(lambda message: message.createReply([answers.pop(0)]))
            for asked in range(1, 6):
                watcher.refresh(PATH)
                self.assertTrue(waitFor(lambda: len(self.changed) == asked))
        self.assertEqual([(mouse.reading.percent, mouse.reading.charging) for mouse in self.changed], [(55, 6), (40, 6), (55, 6), (55, 0), (55, 6)])
        line = "UPower sent {} for " + PATH + " as a type this version can't read, so it's left out"
        self.assertEqual(errors.getvalue().splitlines(), [line.format("State"), line.format("Model, State"), line.format("State")])

    def testAnotherDeviceWithATypeOfTheWrongTypeIsPrintedToo(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            bus, watcher = self.makeWatcher(lambda message: message.createReply([{"Type": "2", "Percentage": 80.0}]))
            watcher.refresh(PATH)
            self.assertTrue(waitFor(lambda: not watcher.refreshing))
            settle()
        self.assertEqual((self.changed, errors.getvalue()), ([], "UPower sent Type for " + PATH + " as a type this version can't read, so it's left out\n"))

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
