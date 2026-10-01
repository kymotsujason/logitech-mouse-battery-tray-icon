import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

import hidpp
from lowBattery import LowBatteryWarner

READ_INTERVAL_MS = 300 * 1000
DEFAULT_NAME = "Logitech mouse"
STATUS_MICE = "mice"
STATUS_SEARCHING = "searching"
STATUS_WAITING = "waiting"
STATUS_DENIED = "denied"
STATUS_NONE = "none"
STATUS_NO_SERVICE = "noService"
STATUS_TEXT = {
    STATUS_SEARCHING: "Looking for mice...",
    STATUS_WAITING: "Move your mouse to wake it.",
    STATUS_DENIED: "Can't read the receiver or cable. Unplug it and plug it back in.",
    STATUS_NONE: "No Logitech mouse found.",
    STATUS_NO_SERVICE: "Can't reach the battery service.",
}
# the menu is in English, so a date in it is too, whatever the locale
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def readingTime(stamp, now):
    local = time.localtime(stamp)
    clock = time.strftime("%H:%M", local)
    if (local[:3] == time.localtime(now)[:3]):
        return "at " + clock
    return "on " + MONTH_NAMES[local.tm_mon - 1] + " " + str(local.tm_mday) + " at " + clock


def singleShotTimer(parent, interval, slot):
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(interval)
    timer.timeout.connect(slot)
    return timer


def isLater(stamp, other):
    # a null stamp is never later, and any stamp is later than none
    if (stamp is None):
        return False
    return other is None or stamp > other


class Mouse:
    def __init__(self, key, pathKey=False):
        self.key = key
        # a key built from a path can pass to a different mouse later, while a unit id or serial stays with its mouse
        self.pathKey = pathKey
        self.name = None
        self.nodePath = None
        self.upowerPath = None
        self.reading = None
        self.asleep = False
        self.lastRead = None
        self.wokeAt = None
        self.readFromUPower = False


class MouseList(QObject):
    changed = pyqtSignal()
    lowBattery = pyqtSignal(str, str, int)
    readRequested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.mice = []
        self.stopped = False
        self.warner = LowBatteryWarner()
        self.answered = False
        self.serviceState = None
        self.appliedEntries = {}
        self.readTimer = QTimer(self)
        self.readTimer.setInterval(READ_INTERVAL_MS)
        self.readTimer.timeout.connect(self.readAll)

    def start(self):
        self.readTimer.start()

    def stop(self):
        self.stopped = True
        self.readTimer.stop()

    def readAll(self):
        self.readRequested.emit()

    def applyService(self, state):
        self.answered = True
        self.serviceState = state
        if (state is None):
            # the service can come back with the same entries, so they all get copied again then
            self.appliedEntries.clear()
            for mouse in self.mice:
                if (mouse.nodePath is not None and mouse.upowerPath is None):
                    mouse.asleep = True
            self.changed.emit()
            return
        listed = set()
        for entry in state.mice:
            listed.add(entry.key)
            if (self.appliedEntries.get(entry.key) == entry):
                continue
            self.appliedEntries[entry.key] = entry
            self.copyEntry(entry)
        for mouse in list(self.mice):
            if (mouse.nodePath is None or mouse.key in listed):
                continue
            # a service that just started lists no mouse until its first search ends
            if (state.searching and mouse.nodePath in state.nodes):
                continue
            self.appliedEntries.pop(mouse.key, None)
            self.detachHidpp(mouse)
        self.changed.emit()

    def copyEntry(self, entry):
        mouse = self.mouseFor(entry.key, pathKey=entry.pathKey)
        if (entry.name):
            mouse.name = entry.name
        mouse.nodePath = entry.source
        # a reading UPower gave stays until the service has a later one, so an older entry never brings back a stale level or re-arms the warner
        if ((mouse.upowerPath is not None or mouse.readFromUPower) and not isLater(entry.lastRead, mouse.lastRead)):
            if (mouse.upowerPath is None):
                # with UPower gone, only the service can say the mouse went to sleep
                mouse.asleep = entry.asleep
            return
        mouse.readFromUPower = False
        mouse.reading = entry.reading
        mouse.asleep = entry.asleep
        mouse.lastRead = entry.lastRead
        mouse.wokeAt = entry.wokeAt
        self.warn(mouse)

    def warn(self, mouse):
        percent = self.warner.update(mouse.key, mouse.reading, mouse.asleep)
        if (percent is not None):
            self.lowBattery.emit(mouse.key, self.displayNames()[mouse.key], percent)

    def detachHidpp(self, mouse):
        mouse.nodePath = None
        if (mouse.upowerPath is None and mouse in self.mice):
            self.removeMouse(mouse)

    def applyReading(self, mouse, reading, woke=False):
        now = time.time()
        if (reading is None):
            mouse.asleep = True
        else:
            if (woke or mouse.asleep or mouse.reading is None):
                mouse.wokeAt = now
            mouse.reading = reading
            mouse.asleep = False
            mouse.lastRead = now
        self.warn(mouse)

    def mouseFor(self, key, pathKey=False):
        for mouse in self.mice:
            if (mouse.key == key):
                return mouse
        mouse = Mouse(key, pathKey)
        self.mice.append(mouse)
        return mouse

    def removeMouse(self, mouse):
        self.mice.remove(mouse)
        # a unit id or serial stays with its mouse and clears itself above REARM_PERCENT, while a path key can pass to a different mouse
        if (mouse.pathKey):
            self.warner.forget(mouse.key)

    def markWarningSent(self, key, percent):
        self.warner.markSent(key, percent)

    def applyUPower(self, upowerMouse):
        mouse = self.mouseFor(upowerMouse.key, pathKey=upowerMouse.pathKey)
        mouse.upowerPath = upowerMouse.path
        if (mouse.name is None):
            mouse.name = upowerMouse.name
        mouse.readFromUPower = True
        # UPower sends no sleep signal, so a changed reading is the only sign the mouse is in use
        self.applyReading(mouse, upowerMouse.reading, woke=(mouse.reading != upowerMouse.reading))
        self.changed.emit()

    def removeUPower(self, path):
        for mouse in list(self.mice):
            if (mouse.upowerPath == path):
                mouse.upowerPath = None
                if (mouse.nodePath is None):
                    self.removeMouse(mouse)
                else:
                    self.takeServiceHalf(mouse)
        self.changed.emit()

    def takeServiceHalf(self, mouse):
        # the reading is at least as new as the last entry, and a service whose mouse is already asleep sends no new entry
        state = self.serviceState
        entry = None
        if (state is not None):
            entry = next((each for each in state.mice if each.key == mouse.key), None)
        mouse.asleep = True if entry is None else entry.asleep

    def shownMouse(self):
        awake = [mouse for mouse in self.mice if not mouse.asleep and mouse.reading is not None]
        if (awake):
            return max(awake, key=lambda mouse: mouse.wokeAt or 0)
        read = [mouse for mouse in self.mice if mouse.lastRead is not None]
        if (read):
            return max(read, key=lambda mouse: mouse.lastRead)
        return None

    def displayNames(self):
        counts = {}
        names = {}
        for mouse in self.mice:
            base = mouse.name or DEFAULT_NAME
            counts[base] = counts.get(base, 0) + 1
            names[mouse.key] = base if counts[base] == 1 else base + " " + str(counts[base])
        return names

    def status(self):
        if (self.mice):
            return STATUS_MICE
        if (not self.answered):
            return STATUS_SEARCHING
        state = self.serviceState
        if (state is None):
            return STATUS_NO_SERVICE
        if (state.searching):
            return STATUS_SEARCHING
        if (state.nodes):
            return STATUS_WAITING
        if (state.deniedPaths):
            return STATUS_DENIED
        return STATUS_NONE
