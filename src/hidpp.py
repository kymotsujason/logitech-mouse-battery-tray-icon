import os
import random
import select
import time
import unicodedata
from dataclasses import dataclass

from hidDescriptor import isHidppDescriptor

LOGITECH_VENDOR = "0000046D"
USB_BUS = "0003"
HID_GENERIC = "hid-generic"
LONG_REPORT = 0x11
REPORT_LENGTH = 20
SW_IDS = range(0x08, 0x10)
RECEIVER_ERROR = 0x8F
DEVICE_ERROR = 0xFF
REQUEST_TIMEOUT = 1.0
SHORT_REPORT = 0x10
REPORT_LENGTHS = {SHORT_REPORT: 7, LONG_REPORT: 20}
# unified battery level flags, highest first, since the highest set flag names the level
LEVEL_FLAGS = [(0x08, "full"), (0x04, "good"), (0x02, "low"), (0x01, "critical")]
FEATURE_ROOT = 0x0000
FEATURE_NAME = 0x0005
FEATURE_BATTERY_STATUS = 0x1000
FEATURE_BATTERY_VOLTAGE = 0x1001
FEATURE_UNIFIED_BATTERY = 0x1004
BATTERY_FEATURES = [FEATURE_UNIFIED_BATTERY, FEATURE_BATTERY_STATUS, FEATURE_BATTERY_VOLTAGE]
FEATURE_DEVICE_INFO = 0x0003
MOUSE_KINDS = (3, 4, 5)

ANSWER = 0
ERROR = 1
TIMEOUT = 2
UNREACHABLE = 3
# HID++ 1.0's busy code, which a receiver sends while the device is there but occupied
BUSY_ERROR = 0x07

FOUND = "mouse"
SKIP = "skip"
UNKNOWN = "unknown"

CONNECTION_NOTIFICATION = 0x41
DEVICE_SLOTS = [1, 2, 3, 4, 5, 6, 0xFF]
PING_TIMEOUT = 0.25

CHARGING_NAMES = ["discharging", "charging", "charging slowly", "full", "charging error", "pending charge", "unknown"]
# HID++ defines the first five codes, and the last two only come from UPower's states
HIDPP_STATUS_COUNT = 5
CHARGING_DISCHARGING = 0
CHARGING_CHARGING = 1
CHARGING_SLOW = 2
CHARGING_FULL = 3
CHARGING_ERROR = 4
CHARGING_PENDING = 5
CHARGING_UNKNOWN = 6
EXTERNAL_POWER = (CHARGING_CHARGING, CHARGING_SLOW, CHARGING_FULL)

EVENT_NONE = 0
EVENT_OTHER = 1
EVENT_BATTERY = 2
EVENT_LINK_DOWN = 3
EVENT_LINK_UP = 4


@dataclass(frozen=True)
class BatteryFeature:
    deviceIndex: int
    featureId: int
    featureIndex: int
    hasPercent: bool


@dataclass(frozen=True)
class BatteryReading:
    percent: int | None
    charging: int
    millivolts: int | None = None
    level: str | None = None


@dataclass(frozen=True)
class FoundMouse:
    slot: int
    unitId: str | None
    name: str | None
    feature: BatteryFeature
    reading: BatteryReading | None
    outcome: int = ANSWER


@dataclass(frozen=True)
class SearchResult:
    mice: tuple
    skipped: frozenset
    unknown: frozenset


def readUevent(text):
    fields = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if (separator):
            fields[key] = value
    return fields


def isHidppNode(uevent, descriptor):
    fields = readUevent(uevent)
    parts = fields.get("HID_ID", "").upper().split(":")
    if (len(parts) != 3 or parts[0] != USB_BUS or parts[1] != LOGITECH_VENDOR):
        return False
    if (fields.get("DRIVER") != HID_GENERIC):
        return False
    return isHidppDescriptor(descriptor)


def findHidppNodes(sysRoot="/sys/class/hidraw", devRoot="/dev"):
    nodes = []
    if (not os.path.isdir(sysRoot)):
        return nodes
    for name in sorted(os.listdir(sysRoot)):
        base = os.path.join(sysRoot, name, "device")
        try:
            with open(os.path.join(base, "uevent"), encoding="utf-8", errors="replace") as f:
                uevent = f.read()
            with open(os.path.join(base, "report_descriptor"), "rb") as f:
                descriptor = f.read()
        except OSError:
            continue
        if (isHidppNode(uevent, descriptor)):
            nodes.append(os.path.join(devRoot, name))
    return nodes


class HidppNode:
    def __init__(self, fd, onEvent=None):
        self.fd = fd
        self.onEvent = onEvent
        # a random start keeps two programs on one node from moving through the same ids in step
        self.requestCount = random.randrange(len(SW_IDS))

    def nextSwId(self):
        # a new software id per request keeps a late reply from answering the next request
        swId = SW_IDS[self.requestCount % len(SW_IDS)]
        self.requestCount += 1
        return swId

    @classmethod
    def open(cls, path, onEvent=None):
        return cls(os.open(path, os.O_RDWR | os.O_NONBLOCK), onEvent)

    def close(self):
        if (self.fd is None):
            return
        os.close(self.fd)
        self.fd = None

    def readReport(self):
        try:
            report = os.read(self.fd, 64)
        except BlockingIOError:
            return None
        if (not report):
            raise OSError("HID++ node closed")
        # a report shorter than its id's full length, or with an id HID++ doesn't use, can't be parsed safely, so it counts as no data
        length = REPORT_LENGTHS.get(report[0])
        if (length is None or len(report) < length):
            return None
        return report

    def writeReport(self, deviceIndex, featureIndex, funcSw, params=b""):
        report = bytes([LONG_REPORT, deviceIndex, featureIndex, funcSw]) + bytes(params)
        os.write(self.fd, report.ljust(REPORT_LENGTH, b"\x00"))

    def requestOutcome(self, deviceIndex, featureIndex, function, params=b"", timeout=None):
        if (timeout is None):
            timeout = REQUEST_TIMEOUT
        funcSw = (function << 4) | self.nextSwId()
        self.writeReport(deviceIndex, featureIndex, funcSw, params)
        deadline = time.monotonic() + timeout
        while (True):
            remaining = deadline - time.monotonic()
            if (remaining <= 0):
                return (TIMEOUT, None)
            ready, _, _ = select.select([self.fd], [], [], remaining)
            if (not ready):
                return (TIMEOUT, None)
            reply = self.readReport()
            if (reply is None):
                continue
            if (len(reply) >= 5 and reply[1] == deviceIndex):
                if (reply[2] in (RECEIVER_ERROR, DEVICE_ERROR) and reply[3] == featureIndex and reply[4] == funcSw):
                    # a receiver error means it couldn't reach the device, except for busy, where the device is there
                    if (reply[2] == RECEIVER_ERROR and reply[5] != BUSY_ERROR):
                        return (UNREACHABLE, None)
                    return (ERROR, None)
                if (reply[2] == featureIndex and reply[3] == funcSw):
                    return (ANSWER, reply[4:])
            if (self.onEvent is not None):
                self.onEvent(reply)

    def request(self, deviceIndex, featureIndex, function, params=b"", timeout=None):
        kind, reply = self.requestOutcome(deviceIndex, featureIndex, function, params, timeout)
        return reply


def getFeatureIndex(node, deviceIndex, featureId):
    reply = node.request(deviceIndex, FEATURE_ROOT, 0, bytes([featureId >> 8, featureId & 0xFF]))
    if (reply is None or reply[0] == 0):
        return None
    return reply[0]


def readName(node, deviceIndex):
    featureIndex = getFeatureIndex(node, deviceIndex, FEATURE_NAME)
    if (featureIndex is None):
        return None
    reply = node.request(deviceIndex, featureIndex, 0)
    if (reply is None):
        return None
    length = reply[0]
    name = b""
    while (len(name) < length):
        reply = node.request(deviceIndex, featureIndex, 1, bytes([len(name)]))
        if (reply is None):
            return None
        chunk = reply[:length - len(name)]
        if (not chunk):
            return None
        name += chunk
    return cleanText(name.decode("utf-8", "replace"))


def lookupFeature(node, deviceIndex, featureId):
    kind, reply = node.requestOutcome(deviceIndex, FEATURE_ROOT, 0, bytes([featureId >> 8, featureId & 0xFF]))
    if (kind != ANSWER):
        return (kind, None)
    return (ANSWER, reply[0])


def readDeviceType(node, deviceIndex):
    kind, nameIndex = lookupFeature(node, deviceIndex, FEATURE_NAME)
    if (kind != ANSWER or nameIndex == 0):
        return (kind, None)
    kind, reply = node.requestOutcome(deviceIndex, nameIndex, 2)
    if (kind != ANSWER):
        return (kind, None)
    return (ANSWER, reply[0])


def findBattery(node, deviceIndex):
    for featureId in BATTERY_FEATURES:
        kind, featureIndex = lookupFeature(node, deviceIndex, featureId)
        if (kind != ANSWER):
            return (kind, None)
        if (featureIndex == 0):
            continue
        hasPercent = (featureId == FEATURE_BATTERY_STATUS)
        if (featureId == FEATURE_UNIFIED_BATTERY):
            kind, capabilities = node.requestOutcome(deviceIndex, featureIndex, 0)
            if (kind != ANSWER):
                return (kind, None)
            # bit 1 of the capability flags says whether the device reports a percentage
            hasPercent = (capabilities[1] & 0x02) != 0
        return (ANSWER, BatteryFeature(deviceIndex, featureId, featureIndex, hasPercent))
    return (ANSWER, None)


def readUnitId(node, deviceIndex):
    kind, infoIndex = lookupFeature(node, deviceIndex, FEATURE_DEVICE_INFO)
    if (kind != ANSWER or infoIndex == 0):
        return (kind, None)
    kind, reply = node.requestOutcome(deviceIndex, infoIndex, 0)
    if (kind != ANSWER):
        return (kind, None)
    unitId = reply[1:5].hex()
    return (ANSWER, None if unitId == "00000000" else unitId)


def pingSlots(node, slots=None, timeout=None):
    # every ping goes out before any answer is read, thus silent slots cost one wait in total
    if (slots is None):
        slots = DEVICE_SLOTS
    if (timeout is None):
        timeout = PING_TIMEOUT
    pending = {}
    for slot in slots:
        funcSw = (1 << 4) | node.nextSwId()
        node.writeReport(slot, FEATURE_ROOT, funcSw, bytes([0, 0, 0xAA]))
        pending[(slot, funcSw)] = slot
    answered = []
    deadline = time.monotonic() + timeout
    while (pending):
        remaining = deadline - time.monotonic()
        if (remaining <= 0):
            break
        ready, _, _ = select.select([node.fd], [], [], remaining)
        if (not ready):
            break
        report = node.readReport()
        if (report is None):
            continue
        if (len(report) >= 5 and report[2] in (RECEIVER_ERROR, DEVICE_ERROR) and report[3] == FEATURE_ROOT and (report[1], report[4]) in pending):
            del pending[(report[1], report[4])]
            continue
        if (len(report) >= 7 and report[2] == FEATURE_ROOT and (report[1], report[3]) in pending and report[6] == 0xAA):
            answered.append(pending.pop((report[1], report[3])))
            continue
        if (node.onEvent is not None):
            node.onEvent(report)
    return sorted(answered)


def identifySlot(node, slot):
    kind, deviceType = readDeviceType(node, slot)
    if (kind != ANSWER):
        return (UNKNOWN, None)
    if (deviceType not in MOUSE_KINDS):
        return (SKIP, None)
    kind, feature = findBattery(node, slot)
    if (kind != ANSWER):
        return (UNKNOWN, None)
    if (feature is None):
        return (SKIP, None)
    kind, unitId = readUnitId(node, slot)
    if (kind != ANSWER):
        return (UNKNOWN, None)
    name = readName(node, slot)
    outcome, reading = readBattery(node, feature)
    return (FOUND, FoundMouse(slot, unitId, name, feature, reading, outcome))


def searchNode(node, skipped=frozenset(), pingTimeout=None):
    mice = []
    newSkipped = set()
    unknown = set()
    for slot in pingSlots(node, [slot for slot in DEVICE_SLOTS if slot not in skipped], pingTimeout):
        outcome, found = identifySlot(node, slot)
        if (outcome == FOUND):
            mice.append(found)
        elif (outcome == SKIP):
            newSkipped.add(slot)
        else:
            unknown.add(slot)
    return SearchResult(tuple(mice), frozenset(newSkipped), frozenset(unknown))


def statusCharging(status):
    # battery status (0x1000) codes mapped onto the unified battery ones
    if (status == 0):
        return CHARGING_DISCHARGING
    if (status in (1, 2)):
        return CHARGING_CHARGING
    if (status == 3):
        return CHARGING_FULL
    if (status == 4):
        return CHARGING_SLOW
    return CHARGING_ERROR


def voltageCharging(flags):
    # bit 7 means external power, and the low 3 bits then give the charge state
    if (not (flags & 0x80)):
        return CHARGING_DISCHARGING
    state = flags & 0x07
    if (state == 0):
        return CHARGING_CHARGING
    if (state == 1):
        return CHARGING_FULL
    return CHARGING_ERROR


def parseBatteryStatus(level, status):
    # as in the kernel's hidpp20_batterylevel_map_status_capacity, a level of 0 means unknown outside discharging, and a full charge means 100
    if (status == 3):
        return BatteryReading(100, statusCharging(status))
    if (level == 0 and status != 0):
        return BatteryReading(None, statusCharging(status))
    return BatteryReading(min(level, 100), statusCharging(status))


def unifiedLevelName(flags):
    for flag, name in LEVEL_FLAGS:
        if (flags & flag):
            return name
    return None


def parseUnifiedStatus(params, hasPercent):
    charging = params[2] if params[2] < HIDPP_STATUS_COUNT else CHARGING_ERROR
    if (hasPercent):
        return BatteryReading(min(params[0], 100), charging)
    return BatteryReading(None, charging, level=unifiedLevelName(params[1]))


def readBattery(node, feature):
    function = 1 if feature.featureId == FEATURE_UNIFIED_BATTERY else 0
    kind, reply = node.requestOutcome(feature.deviceIndex, feature.featureIndex, function)
    if (kind != ANSWER):
        return (kind, None)
    if (feature.featureId == FEATURE_UNIFIED_BATTERY):
        return (ANSWER, parseUnifiedStatus(reply, feature.hasPercent))
    if (feature.featureId == FEATURE_BATTERY_STATUS):
        return (ANSWER, parseBatteryStatus(reply[0], reply[2]))
    return (ANSWER, BatteryReading(None, voltageCharging(reply[2]), (reply[0] << 8) | reply[1]))


def isReply(report):
    # the device sends its own events with software id 0, thus anything else answers some program's request
    if (len(report) < 5):
        return False
    if (report[2] in (RECEIVER_ERROR, DEVICE_ERROR)):
        return (report[4] & 0x0F) != 0
    if (report[2] == CONNECTION_NOTIFICATION):
        return False
    return (report[3] & 0x0F) != 0


def parseEvent(report, feature):
    if (len(report) < 5 or report[1] != feature.deviceIndex):
        return (EVENT_NONE, None)
    if (isReply(report)):
        return (EVENT_NONE, None)
    if (report[2] == CONNECTION_NOTIFICATION):
        # HID++ 1.0 connection notice, bit 6 of the device info byte means the link is down
        if (report[4] & 0x40):
            return (EVENT_LINK_DOWN, None)
        return (EVENT_LINK_UP, None)
    # a feature event has function 0 and software id 0, since the device sent it on its own
    if (report[2] == feature.featureIndex and report[3] == 0x00 and len(report) >= 7):
        if (feature.featureId == FEATURE_UNIFIED_BATTERY):
            return (EVENT_BATTERY, parseUnifiedStatus(report[4:], feature.hasPercent))
        if (feature.featureId == FEATURE_BATTERY_STATUS):
            return (EVENT_BATTERY, parseBatteryStatus(report[4], report[6]))
        if (feature.featureId == FEATURE_BATTERY_VOLTAGE):
            return (EVENT_BATTERY, BatteryReading(None, voltageCharging(report[6]), (report[4] << 8) | report[5]))
    return (EVENT_OTHER, None)


def chargingName(code):
    if (0 <= code < len(CHARGING_NAMES)):
        return CHARGING_NAMES[code]
    return "status " + str(code)


def levelText(reading):
    if (reading.percent is not None):
        return str(reading.percent) + "%"
    if (reading.millivolts is not None):
        return str(reading.millivolts) + " mV"
    if (reading.level is not None):
        return reading.level
    return "unknown level"


def describeReading(reading):
    return levelText(reading) + ", " + chargingName(reading.charging)


def cleanText(text):
    # device strings reach the menu, notices, and the terminal, so control characters such as an escape are dropped
    if (text is None):
        return None
    return "".join(character for character in text if not unicodedata.category(character).startswith("C")).strip()
