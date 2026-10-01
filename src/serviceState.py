import json
import math
from dataclasses import dataclass

import hidpp

SERVICE_NAME = "io.github.kymotsujason.LogitechMouseBattery"
OBJECT_PATH = "/io/github/kymotsujason/LogitechMouseBattery"
INTERFACE = "io.github.kymotsujason.LogitechMouseBattery1"
STATE_VERSION = 1
STATE_KEYS = ("version", "busy", "searching", "nodes", "deniedPaths", "mice")
ENTRY_KEYS = ("key", "pathKey", "name", "source", "percent", "charging", "millivolts", "level", "asleep", "lastRead", "wokeAt")


@dataclass(frozen=True)
class ServiceEntry:
    key: str
    pathKey: bool
    name: str | None
    source: str
    reading: hidpp.BatteryReading | None
    asleep: bool
    lastRead: float | None
    wokeAt: float | None


@dataclass(frozen=True)
class ServiceState:
    busy: bool
    searching: bool
    nodes: tuple
    deniedPaths: tuple
    mice: tuple


def entryFields(mouse):
    reading = mouse.reading
    return {
        "key": mouse.key,
        "pathKey": mouse.pathKey,
        "name": mouse.name,
        "source": mouse.nodePath,
        "percent": None if reading is None else reading.percent,
        "charging": None if reading is None else reading.charging,
        "millivolts": None if reading is None else reading.millivolts,
        "level": None if reading is None else reading.level,
        "asleep": mouse.asleep,
        "lastRead": mouse.lastRead,
        "wokeAt": mouse.wokeAt,
    }


def encodeState(receiverList):
    # sorted keys and lists make two equal states the same string, which is how the service tells whether anything changed
    fields = {
        "version": STATE_VERSION,
        "busy": receiverList.isBusy(),
        "searching": receiverList.isSearching(),
        "nodes": sorted(receiverList.nodes),
        "deniedPaths": sorted(receiverList.denied),
        "mice": sorted((entryFields(mouse) for mouse in receiverList.mice), key=lambda entry: entry["key"]),
    }
    return json.dumps(fields, sort_keys=True)


def isStamp(value):
    if (value is None):
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def decodeEntry(fields):
    if (not isinstance(fields, dict) or any(key not in fields for key in ENTRY_KEYS)):
        return None
    if (not isinstance(fields["key"], str) or not isinstance(fields["source"], str)):
        return None
    if (not isStamp(fields["lastRead"]) or not isStamp(fields["wokeAt"])):
        return None
    reading = None
    if (fields["charging"] is not None):
        reading = hidpp.BatteryReading(fields["percent"], fields["charging"], fields["millivolts"], fields["level"])
    name = fields["name"] if isinstance(fields["name"], str) else None
    return ServiceEntry(fields["key"], bool(fields["pathKey"]), name, fields["source"], reading, bool(fields["asleep"]), fields["lastRead"], fields["wokeAt"])


def decodeState(text):
    try:
        fields = json.loads(text)
    except (TypeError, ValueError):
        return None
    if (not isinstance(fields, dict) or any(key not in fields for key in STATE_KEYS)):
        return None
    version = fields["version"]
    if (isinstance(version, bool) or version != STATE_VERSION):
        return None
    if (not all(isinstance(fields[key], list) for key in ("nodes", "deniedPaths", "mice"))):
        return None
    mice = tuple(decodeEntry(entry) for entry in fields["mice"])
    if (None in mice):
        return None
    return ServiceState(bool(fields["busy"]), bool(fields["searching"]), tuple(str(path) for path in fields["nodes"]), tuple(str(path) for path in fields["deniedPaths"]), mice)
