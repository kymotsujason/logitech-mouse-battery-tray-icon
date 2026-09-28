from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage, QDBusServiceWatcher, QDBusVariant

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
# UPower's states on the HID++ charging codes, with 5 and 6 for the two states HID++ doesn't have
STATE_CODES = {1: 1, 2: 0, 3: 0, 4: 3, 5: 5, 6: 0}
UNKNOWN_CODE = 6


@dataclass(frozen=True)
class UPowerMouse:
    path: str
    key: str
    name: str | None
    reading: hidpp.BatteryReading


def plain(value):
    while (isinstance(value, QDBusVariant)):
        value = value.variant()
    return value


def mouseFromProperties(path, props):
    values = {key: plain(value) for key, value in props.items()}
    if (values.get("Type") != TYPE_MOUSE):
        return None
    nativePath = str(values.get("NativePath", ""))
    vendor = str(values.get("Vendor", ""))
    if (not nativePath.startswith(NATIVE_PATH_PREFIX) and "logitech" not in vendor.lower()):
        return None
    charging = STATE_CODES.get(values.get("State", 0), UNKNOWN_CODE)
    level = values.get("BatteryLevel", LEVEL_NONE)
    if (level == LEVEL_NONE):
        percent = min(100, max(0, int(float(values.get("Percentage", 0.0)) + 0.5)))
        reading = hidpp.BatteryReading(percent, charging)
    else:
        # UPower's docs call the percentage an approximation whenever the device reports coarse levels
        reading = hidpp.BatteryReading(None, charging, level=LEVEL_NAMES.get(level, "unknown level"))
    key = str(values.get("Serial", "")).replace("-", "").lower() or path
    return UPowerMouse(path, key, values.get("Model") or None, reading)


def callMethod(bus, path, interface, method, arguments=None):
    message = QDBusMessage.createMethodCall(SERVICE, path, interface, method)
    if (arguments is not None):
        message.setArguments(arguments)
    reply = bus.call(message)
    if (reply.type() != QDBusMessage.MessageType.ReplyMessage):
        return None
    return reply.arguments()


def enumeratePaths(bus):
    arguments = callMethod(bus, ROOT_PATH, SERVICE, "EnumerateDevices")
    if (not arguments):
        return []
    return [str(plain(path)) for path in arguments[0]]


def getProperties(bus, path):
    arguments = callMethod(bus, path, PROPERTIES_INTERFACE, "GetAll", [DEVICE_INTERFACE])
    if (not arguments):
        return None
    return arguments[0]


def listMice(bus=None):
    if (bus is None):
        bus = QDBusConnection.systemBus()
    found = []
    for path in enumeratePaths(bus):
        props = getProperties(bus, path)
        if (props is None):
            continue
        mouse = mouseFromProperties(path, props)
        if (mouse is not None):
            found.append(mouse)
    return found


class UPowerWatcher(QObject):
    mouseChanged = pyqtSignal(object)
    mouseRemoved = pyqtSignal(str)

    def __init__(self, bus=None):
        super().__init__()
        self.bus = bus if bus is not None else QDBusConnection.systemBus()
        self.paths = set()

    def start(self):
        # subscribing before listing keeps a device added in between from going missing
        self.bus.connect(SERVICE, ROOT_PATH, SERVICE, "DeviceAdded", self.onDeviceAdded)
        self.bus.connect(SERVICE, ROOT_PATH, SERVICE, "DeviceRemoved", self.onDeviceRemoved)
        # each device sends its own PropertiesChanged, thus only the path is left open
        self.bus.connect(SERVICE, "", PROPERTIES_INTERFACE, "PropertiesChanged", self.onPropertiesChanged)
        for path in enumeratePaths(self.bus):
            self.refresh(path)

    def watchOwner(self):
        # after a UPower restart the old paths and readings can be stale, thus a new owner means listing again
        self.ownerWatcher = QDBusServiceWatcher(SERVICE, self.bus, QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange, self)
        self.ownerWatcher.serviceOwnerChanged.connect(self.onOwnerChanged)

    def onOwnerChanged(self, name, oldOwner, newOwner):
        for path in sorted(self.paths):
            self.mouseRemoved.emit(path)
        self.paths.clear()
        if (not newOwner):
            return
        for path in enumeratePaths(self.bus):
            self.refresh(path)

    def refresh(self, path):
        props = getProperties(self.bus, path)
        mouse = None if props is None else mouseFromProperties(path, props)
        if (mouse is None):
            if (path in self.paths):
                self.paths.discard(path)
                self.mouseRemoved.emit(path)
            return
        self.paths.add(path)
        self.mouseChanged.emit(mouse)

    @pyqtSlot(QDBusMessage)
    def onDeviceAdded(self, message):
        self.refresh(pathArgument(message))

    @pyqtSlot(QDBusMessage)
    def onDeviceRemoved(self, message):
        path = pathArgument(message)
        if (path in self.paths):
            self.paths.discard(path)
            self.mouseRemoved.emit(path)

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
        changed = plain(arguments[1]) if len(arguments) > 1 else {}
        if ("Type" in changed):
            self.refresh(path)


def pathArgument(message):
    arguments = message.arguments()
    if (not arguments):
        return ""
    value = plain(arguments[0])
    return value.path() if hasattr(value, "path") else str(value)
