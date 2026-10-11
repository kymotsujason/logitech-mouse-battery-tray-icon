import glob
import math
import os
import re
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage, QDBusServiceWatcher

import dbusCalls
import hidpp
from exceptionHooks import printLine

SERVICE = "org.freedesktop.UPower"
ROOT_PATH = "/org/freedesktop/UPower"
DEVICE_INTERFACE = "org.freedesktop.UPower.Device"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
TYPE_MOUSE = 5
TYPE_KEYBOARD = 6
# hid-logitech-hidpp names its power supplies hidpp_battery_<N>, and UPower's NativePath carries that name
NATIVE_PATH_PREFIX = "hidpp_battery"
NATIVE_PATH_PATTERN = re.compile(r"hidpp_battery_[0-9]+")
SYSFS_POWER_SUPPLY = "/sys/class/power_supply"
UDEV_DATA = "/run/udev/data"
UDEV_MOUSE_LINE = "E:ID_INPUT_MOUSE=1"
# hid-logitech-hidpp sets its battery's status to unknown when the mouse drops its link, which the G703 does when it sleeps
STATE_UNKNOWN = 0
LEVEL_NONE = 1
LEVEL_NAMES = {3: "low", 4: "critical", 6: "normal", 7: "high", 8: "full"}
# UPower's State values mapped onto the charging codes, where pending charge keeps a code of its own and any value missing here, such as 0 (unknown), becomes CHARGING_UNKNOWN
STATE_CODES = {1: hidpp.CHARGING_CHARGING, 2: hidpp.CHARGING_DISCHARGING, 3: hidpp.CHARGING_DISCHARGING, 4: hidpp.CHARGING_FULL, 5: hidpp.CHARGING_PENDING, 6: hidpp.CHARGING_DISCHARGING}
UNKNOWN_CODE = hidpp.CHARGING_UNKNOWN
# a device path that no longer answers belongs to a device that's gone
GONE_ERRORS = ("org.freedesktop.DBus.Error.UnknownObject", "org.freedesktop.DBus.Error.UnknownMethod")
# UPower isn't on the bus at all, and the owner watch lists the devices once it shows up
ABSENT_ERRORS = ("org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner", "org.freedesktop.DBus.Error.Disconnected")
RETRY_MS = 5 * 1000
RETRY_MAX_MS = 60 * 1000
# a reply that isn't the type UPower documents counts as a failed call, so a mouse keeps its last reading through it
UNREADABLE = "a reply this version can't read"
# each property's type as PyQt6 hands it over, where an int stands for every D-Bus integer width
PROPERTY_TYPES = {"Type": int, "NativePath": str, "Vendor": str, "Model": str, "Serial": str, "Percentage": (int, float), "State": int, "BatteryLevel": int, "UpdateTime": (int, float)}


@dataclass(frozen=True)
class UPowerMouse:
    path: str
    key: str
    name: str | None
    reading: hidpp.BatteryReading
    pathKey: bool = False
    # when UPower last read the device, in seconds since the epoch, or None when it didn't say
    updatedAt: float | None = None
    asleep: bool = False


def wrongProperties(values):
    # another UPower version, another daemon on the name, or a broken reply can send a property in another type, and a D-Bus boolean arrives as a bool, which Python counts as an int
    return sorted(key for key, kinds in PROPERTY_TYPES.items() if key in values and (not isinstance(values[key], kinds) or isinstance(values[key], bool)))


def plainValues(props):
    return {key: dbusCalls.plain(value) for key, value in props.items()}


def inputIsMouse(name):
    try:
        with open(os.path.join(UDEV_DATA, "+input:" + name)) as f:
            return any(line.rstrip("\n") == UDEV_MOUSE_LINE for line in f)
    except OSError:
        return False


def cableHasMouse(nativePath):
    # UPower looks at every interface of a USB device and ranks a keyboard above a mouse, so a mouse on its cable can come out a keyboard
    if (not NATIVE_PATH_PATTERN.fullmatch(nativePath)):
        return False
    hidDevice = os.path.realpath(os.path.join(SYSFS_POWER_SUPPLY, nativePath, "device"))
    interface = os.path.dirname(hidDevice)
    # a battery on a receiver hangs off the receiver's HID device, whose USB device also holds every other paired device's inputs
    if (not os.path.isfile(os.path.join(interface, "bInterfaceNumber"))):
        return False
    usbDevice = os.path.dirname(interface)
    for inputPath in glob.glob(os.path.join(glob.escape(usbDevice), "*", "*", "input", "input*")):
        if (inputIsMouse(os.path.basename(inputPath))):
            return True
    return False


def mouseFromProperties(path, props):
    if (not isinstance(props, dict)):
        return None
    values = plainValues(props)
    # a property of the wrong type is read as missing, so the rest of the device still reads
    for key in wrongProperties(values):
        del values[key]
    kind = values.get("Type")
    nativePath = str(values.get("NativePath", ""))
    if (kind != TYPE_MOUSE and not (kind == TYPE_KEYBOARD and cableHasMouse(nativePath))):
        return None
    vendor = str(values.get("Vendor", ""))
    if (not nativePath.startswith(NATIVE_PATH_PREFIX) and "logitech" not in vendor.lower()):
        return None
    charging = STATE_CODES.get(values.get("State", 0), UNKNOWN_CODE)
    level = values.get("BatteryLevel", LEVEL_NONE)
    if (level == LEVEL_NONE):
        # a missing percentage is no percentage, never an empty battery that would warn
        percentage = float(values.get("Percentage", math.nan))
        percent = min(100, max(0, int(percentage + 0.5))) if math.isfinite(percentage) else None
        reading = hidpp.BatteryReading(percent, charging)
    else:
        # UPower's docs call the percentage an approximation whenever the device reports coarse levels
        reading = hidpp.BatteryReading(None, charging, level=LEVEL_NAMES.get(level, "unknown level"))
    serial = str(values.get("Serial", "")).replace("-", "").lower()
    model = hidpp.cleanText(values.get("Model")) or None
    updatedAt = values.get("UpdateTime", 0)
    updatedAt = float(updatedAt) if isinstance(updatedAt, (int, float)) and updatedAt > 0 else None
    asleep = values.get("State") == STATE_UNKNOWN and nativePath.startswith(NATIVE_PATH_PREFIX)
    return UPowerMouse(path, serial or path, model, reading, pathKey=not serial, updatedAt=updatedAt, asleep=asleep)


def methodCall(path, interface, method, arguments=None):
    message = QDBusMessage.createMethodCall(SERVICE, path, interface, method)
    if (arguments is not None):
        message.setArguments(arguments)
    return message


def enumerateCall():
    return methodCall(ROOT_PATH, SERVICE, "EnumerateDevices")


def getAllCall(path):
    return methodCall(path, PROPERTIES_INTERFACE, "GetAll", [DEVICE_INTERFACE])


def pathsFrom(reply):
    error = dbusCalls.failure(reply)
    if (error is not None):
        return ([], error)
    arguments = reply.arguments()
    if (not arguments or not isinstance(arguments[0], list)):
        return ([], UNREADABLE)
    paths = [devicePath(path) for path in arguments[0]]
    return ([path for path in paths if path is not None], None)


def propertiesFrom(reply):
    error = dbusCalls.failure(reply)
    if (error is not None):
        return (None, error)
    arguments = reply.arguments()
    if (not arguments or not isinstance(arguments[0], dict)):
        return (None, UNREADABLE)
    return (arguments[0], None)


def devicePath(value):
    # anything but an object path goes out as a call Qt can't send, which fails with InvalidObjectPath and would be retried for as long as the app runs
    path = dbusCalls.pathText(value)
    return path if path.startswith("/") else None


def enumeratePaths(bus):
    return pathsFrom(dbusCalls.call(bus, enumerateCall()))


def getProperties(bus, path):
    return propertiesFrom(dbusCalls.call(bus, getAllCall(path)))


def listMice(bus=None):
    if (bus is None):
        bus = QDBusConnection.systemBus()
    paths, error = enumeratePaths(bus)
    if (error is not None):
        return ([], [error])
    found = []
    errors = []
    for path in paths:
        props, error = getProperties(bus, path)
        if (error is not None):
            errors.append(error)
            continue
        mouse = mouseFromProperties(path, props)
        if (mouse is not None):
            found.append(mouse)
    return (found, errors)


class UPowerWatcher(QObject):
    mouseChanged = pyqtSignal(object)
    mouseRemoved = pyqtSignal(str)

    def __init__(self, bus=None):
        super().__init__()
        self.bus = bus if bus is not None else QDBusConnection.systemBus()
        self.paths = set()
        self.retryPaths = set()
        # the properties each device last sent in a type this version can't read, so each set is printed once
        self.wrongTypes = {}
        self.enumerateDue = False
        self.retryMs = RETRY_MS
        self.retryTimer = QTimer(self)
        self.retryTimer.setSingleShot(True)
        self.retryTimer.timeout.connect(self.onRetry)
        self.ownerWatcher = None
        # a reply to a call made before an owner change or a stop belongs to a UPower that's gone
        self.generation = 0
        # each device's call still out, by a number of its own, so an answer that comes after the device was removed can't bring it back
        self.refreshing = {}
        self.callCount = 0
        self.refreshAgain = set()
        # the failures printed for each device's call, and under None for the listing's
        self.printedErrors = {}

    def start(self):
        # subscribing before listing keeps a device added in between from going missing
        self.bus.connect(SERVICE, ROOT_PATH, SERVICE, "DeviceAdded", self.onDeviceAdded)
        self.bus.connect(SERVICE, ROOT_PATH, SERVICE, "DeviceRemoved", self.onDeviceRemoved)
        # each device sends its own PropertiesChanged, so only the path is left open
        self.bus.connect(SERVICE, "", PROPERTIES_INTERFACE, "PropertiesChanged", self.onPropertiesChanged)
        self.listDevices()

    def stop(self):
        self.retryTimer.stop()
        self.retryPaths.clear()
        self.enumerateDue = False
        self.generation += 1
        self.refreshing.clear()
        self.refreshAgain.clear()

    def watchOwner(self):
        # after a UPower restart the old paths and readings can be stale, so a new owner means listing again
        self.ownerWatcher = QDBusServiceWatcher(SERVICE, self.bus, QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange, self)
        self.ownerWatcher.serviceOwnerChanged.connect(self.onOwnerChanged)

    def onOwnerChanged(self, name, oldOwner, newOwner):
        self.stop()
        self.retryMs = RETRY_MS
        # every device goes with the old UPower, while the listing's failure is over only once a listing goes through
        self.printedErrors = {path: errors for path, errors in self.printedErrors.items() if path is None}
        self.wrongTypes.clear()
        for path in sorted(self.paths):
            self.mouseRemoved.emit(path)
        self.paths.clear()
        if (not newOwner):
            return
        self.listDevices()

    def listDevices(self):
        generation = self.generation
        dbusCalls.callLater(self.bus, enumerateCall(), lambda reply: self.onListed(generation, reply), self)

    def onListed(self, generation, reply):
        if (generation != self.generation):
            return
        paths, error = pathsFrom(reply)
        if (error is not None):
            if (error not in ABSENT_ERRORS):
                self.reportError(error)
                self.enumerateDue = True
                self.scheduleRetry()
            else:
                self.enumerateDue = False
            return
        self.enumerateDue = False
        self.answered()
        # a device still failing keeps its backoff, which its own answer starts over once it goes through
        if (not self.retryPaths):
            self.retryMs = RETRY_MS
        for path in paths:
            # a device still failing is read by its own retry, so a listing can't cut its backoff short
            if (path not in self.retryPaths):
                self.refresh(path)

    def reportError(self, error, path=None):
        # the retries keep going quietly, so each kind of failure gets one line until the call that failed goes through, naming the device when it's one device's call
        printed = self.printedErrors.setdefault(path, set())
        if (error in printed):
            return
        printed.add(error)
        printLine("UPower answered " + error + ("" if path is None else " for " + path))

    def answered(self, path=None):
        # a printed failure gets a line when it's over too, so the journal shows how long readings were out
        if (self.printedErrors.pop(path, None)):
            printLine("UPower answers again" + ("" if path is None else " for " + path))

    def scheduleRetry(self):
        if (not self.retryTimer.isActive()):
            self.retryTimer.start(self.retryMs)
            self.retryMs = min(self.retryMs * 2, RETRY_MAX_MS)

    def onRetry(self):
        if (self.enumerateDue):
            self.listDevices()
        for path in sorted(self.retryPaths):
            self.refresh(path)

    def refresh(self, path):
        # a device gets one GetAll at a time, and changes during it ask for one more, so a burst of signals can't stack up calls
        if (path in self.refreshing):
            self.refreshAgain.add(path)
            return
        self.callCount += 1
        call = self.callCount
        self.refreshing[path] = call
        generation = self.generation
        dbusCalls.callLater(self.bus, getAllCall(path), lambda reply: self.onRefreshed(generation, path, reply, call), self)

    def onRefreshed(self, generation, path, reply, call):
        if (generation != self.generation or self.refreshing.get(path) != call):
            return
        self.refreshing.pop(path, None)
        props, error = propertiesFrom(reply)
        if (error is not None):
            # a failed call goes through its backoff even when the device changed meanwhile, and the retry reads those changes too
            self.refreshAgain.discard(path)
            self.retryPaths.discard(path)
            if (error in GONE_ERRORS):
                # a device that's gone has nothing left to recover from
                self.printedErrors.pop(path, None)
                self.wrongTypes.pop(path, None)
                self.dropPath(path)
            elif (error not in ABSENT_ERRORS):
                # a call that failed for another reason says nothing about the mouse, so it keeps its last reading until the retry
                self.reportError(error, path)
                self.retryPaths.add(path)
                self.scheduleRetry()
            return
        self.answered(path)
        if (path in self.refreshAgain):
            # the device changed after this call went out, so its answer may already be old
            self.refreshAgain.discard(path)
            self.refresh(path)
            return
        self.retryPaths.discard(path)
        if (not self.retryPaths and not self.enumerateDue):
            self.retryMs = RETRY_MS
        self.reportWrongTypes(path, wrongProperties(plainValues(props)))
        mouse = mouseFromProperties(path, props)
        if (mouse is None):
            self.dropPath(path)
            return
        self.paths.add(path)
        self.mouseChanged.emit(mouse)

    def reportWrongTypes(self, path, wrong):
        if (not wrong):
            self.wrongTypes.pop(path, None)
            return
        if (self.wrongTypes.get(path) == wrong):
            return
        self.wrongTypes[path] = wrong
        printLine("UPower sent " + ", ".join(wrong) + " for " + path + " as a type this version can't read, so it's left out")

    def dropPath(self, path):
        if (path in self.paths):
            self.paths.discard(path)
            self.mouseRemoved.emit(path)

    @pyqtSlot(QDBusMessage)
    def onDeviceAdded(self, message):
        path = pathArgument(message)
        if (path is None):
            return
        self.refresh(path)

    @pyqtSlot(QDBusMessage)
    def onDeviceRemoved(self, message):
        path = pathArgument(message)
        self.retryPaths.discard(path)
        self.printedErrors.pop(path, None)
        self.wrongTypes.pop(path, None)
        self.refreshing.pop(path, None)
        self.refreshAgain.discard(path)
        self.dropPath(path)

    @pyqtSlot(QDBusMessage)
    def onPropertiesChanged(self, message):
        arguments = message.arguments()
        if (not arguments or arguments[0] != DEVICE_INTERFACE):
            return
        path = message.path()
        # a change during a device's call makes its answer old, which holds for its first call too, before anything says it's a mouse
        if (path in self.refreshing):
            self.refresh(path)
            return
        # a device that's failing waits out its backoff, and the retry reads whatever changed meanwhile
        if (path in self.retryPaths):
            return
        if (path in self.paths):
            self.refresh(path)
            return
        # UPower can set Type after DeviceAdded, once the mouse's input device shows up
        changed = dbusCalls.plain(arguments[1]) if len(arguments) > 1 else {}
        if (isinstance(changed, dict) and "Type" in changed):
            self.refresh(path)


def pathArgument(message):
    arguments = message.arguments()
    if (not arguments):
        return None
    return devicePath(arguments[0])
