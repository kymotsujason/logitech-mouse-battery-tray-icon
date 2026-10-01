import math
from dataclasses import dataclass

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage, QDBusServiceWatcher

import dbusCalls
import hidpp

SERVICE = "org.freedesktop.UPower"
ROOT_PATH = "/org/freedesktop/UPower"
DEVICE_INTERFACE = "org.freedesktop.UPower.Device"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
TYPE_MOUSE = 5
# hid-logitech-hidpp names its power supplies hidpp_battery_<N>, and UPower's NativePath carries that name
NATIVE_PATH_PREFIX = "hidpp_battery"
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


@dataclass(frozen=True)
class UPowerMouse:
    path: str
    key: str
    name: str | None
    reading: hidpp.BatteryReading
    pathKey: bool = False


def mouseFromProperties(path, props):
    values = {key: dbusCalls.plain(value) for key, value in props.items()}
    if (values.get("Type") != TYPE_MOUSE):
        return None
    nativePath = str(values.get("NativePath", ""))
    vendor = str(values.get("Vendor", ""))
    if (not nativePath.startswith(NATIVE_PATH_PREFIX) and "logitech" not in vendor.lower()):
        return None
    charging = STATE_CODES.get(values.get("State", 0), UNKNOWN_CODE)
    level = values.get("BatteryLevel", LEVEL_NONE)
    if (level == LEVEL_NONE):
        percentage = float(values.get("Percentage", 0.0))
        percent = min(100, max(0, int(percentage + 0.5))) if math.isfinite(percentage) else None
        reading = hidpp.BatteryReading(percent, charging)
    else:
        # UPower's docs call the percentage an approximation whenever the device reports coarse levels
        reading = hidpp.BatteryReading(None, charging, level=LEVEL_NAMES.get(level, "unknown level"))
    serial = str(values.get("Serial", "")).replace("-", "").lower()
    model = hidpp.cleanText(values.get("Model")) or None
    return UPowerMouse(path, serial or path, model, reading, pathKey=not serial)


def callMethod(bus, path, interface, method, arguments=None):
    message = QDBusMessage.createMethodCall(SERVICE, path, interface, method)
    if (arguments is not None):
        message.setArguments(arguments)
    reply = dbusCalls.call(bus, message)
    error = dbusCalls.failure(reply)
    if (error is not None):
        return (None, error)
    return (reply.arguments(), None)


def enumeratePaths(bus):
    arguments, error = callMethod(bus, ROOT_PATH, SERVICE, "EnumerateDevices")
    if (error is not None):
        return ([], error)
    if (not arguments):
        return ([], None)
    return ([dbusCalls.pathText(path) for path in arguments[0]], None)


def getProperties(bus, path):
    arguments, error = callMethod(bus, path, PROPERTIES_INTERFACE, "GetAll", [DEVICE_INTERFACE])
    if (error is not None):
        return (None, error)
    if (not arguments):
        return ({}, None)
    return (arguments[0], None)


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
        self.enumerateDue = False
        self.retryMs = RETRY_MS
        self.retryTimer = QTimer(self)
        self.retryTimer.setSingleShot(True)
        self.retryTimer.timeout.connect(self.onRetry)
        self.ownerWatcher = None

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

    def watchOwner(self):
        # after a UPower restart the old paths and readings can be stale, so a new owner means listing again
        self.ownerWatcher = QDBusServiceWatcher(SERVICE, self.bus, QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange, self)
        self.ownerWatcher.serviceOwnerChanged.connect(self.onOwnerChanged)

    def onOwnerChanged(self, name, oldOwner, newOwner):
        self.stop()
        self.retryMs = RETRY_MS
        for path in sorted(self.paths):
            self.mouseRemoved.emit(path)
        self.paths.clear()
        if (not newOwner):
            return
        self.listDevices()

    def listDevices(self):
        paths, error = enumeratePaths(self.bus)
        if (error is not None):
            if (error not in ABSENT_ERRORS):
                self.enumerateDue = True
                self.scheduleRetry()
            return
        self.enumerateDue = False
        for path in paths:
            self.refresh(path)

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
        props, error = getProperties(self.bus, path)
        if (error is not None):
            self.retryPaths.discard(path)
            if (error in GONE_ERRORS):
                self.dropPath(path)
            elif (error not in ABSENT_ERRORS):
                # a call that failed for another reason says nothing about the mouse, so it keeps its last reading until the retry
                self.retryPaths.add(path)
                self.scheduleRetry()
            return
        self.retryPaths.discard(path)
        if (not self.retryPaths and not self.enumerateDue):
            self.retryMs = RETRY_MS
        mouse = mouseFromProperties(path, props)
        if (mouse is None):
            self.dropPath(path)
            return
        self.paths.add(path)
        self.mouseChanged.emit(mouse)

    def dropPath(self, path):
        if (path in self.paths):
            self.paths.discard(path)
            self.mouseRemoved.emit(path)

    @pyqtSlot(QDBusMessage)
    def onDeviceAdded(self, message):
        self.refresh(pathArgument(message))

    @pyqtSlot(QDBusMessage)
    def onDeviceRemoved(self, message):
        path = pathArgument(message)
        self.retryPaths.discard(path)
        self.dropPath(path)

    @pyqtSlot(QDBusMessage)
    def onPropertiesChanged(self, message):
        arguments = message.arguments()
        if (not arguments or arguments[0] != DEVICE_INTERFACE):
            return
        path = message.path()
        if (path in self.paths):
            self.refresh(path)
            return
        # UPower can set Type after DeviceAdded, once the mouse's input device shows up
        changed = dbusCalls.plain(arguments[1]) if len(arguments) > 1 else {}
        if ("Type" in changed):
            self.refresh(path)


def pathArgument(message):
    arguments = message.arguments()
    if (not arguments):
        return ""
    return dbusCalls.pathText(arguments[0])
