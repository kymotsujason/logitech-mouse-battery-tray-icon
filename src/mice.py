import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from lowBattery import LowBatteryWarner

READ_INTERVAL_MS = 300 * 1000
READ_ALL = "ReadAll"
READ_SILENT = "ReadSilent"
# a timed read may start the service this often, long past the unit's 60 s start limit, so a service that hit the limit for a cause that passed comes back without anyone opening the menu
TIMED_START_INTERVAL_S = 60 * 60
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
        # the reading is the last one the service gave before it went away, so it's shown with its time and no claim the mouse sleeps
        self.stale = False
        self.lastRead = None
        self.wokeAt = None
        self.readFromUPower = False
        self.reportsItself = False
        self.historyRevision = 0


class MouseList(QObject):
    changed = pyqtSignal()
    lowBattery = pyqtSignal(str, str, int)
    # the flag says the read may start the service, which a person's read always may and a timed read only once an hour
    readRequested = pyqtSignal(bool)
    readSilentRequested = pyqtSignal(bool)

    def __init__(self, sleepCheckSeconds=0):
        super().__init__()
        self.sleepCheckSeconds = sleepCheckSeconds
        self.started = False
        self.timedMethod = None
        self.mice = []
        self.warner = LowBatteryWarner()
        self.answered = False
        self.serviceState = None
        self.appliedEntries = {}
        self.readTimer = QTimer(self)
        self.readTimer.setInterval(READ_INTERVAL_MS)
        self.readTimer.timeout.connect(self.readOnTimer)
        # the tray's first GetState may start the service as it starts, so the hour counts from here
        self.lastStartingRead = time.monotonic()

    def start(self):
        self.started = True
        self.updateTimer()

    def stop(self):
        self.started = False
        self.readTimer.stop()

    def setSleepCheck(self, seconds):
        self.sleepCheckSeconds = seconds
        self.updateTimer()

    def timedRead(self):
        if (self.sleepCheckSeconds > 0):
            return (self.sleepCheckSeconds * 1000, READ_ALL)
        hidppMice = [mouse for mouse in self.mice if mouse.nodePath is not None]
        # with the service gone or no mouse listed, the timed read keeps bringing a stopped service back within the hour and searching a new receiver
        if (self.serviceState is None or not hidppMice):
            return (READ_INTERVAL_MS, READ_ALL)
        if (any(not mouse.reportsItself for mouse in hidppMice)):
            return (READ_INTERVAL_MS, READ_SILENT)
        return (None, None)

    def updateTimer(self):
        if (not self.started):
            return
        interval, method = self.timedRead()
        self.timedMethod = method
        if (interval is None):
            self.readTimer.stop()
            return
        # a timer already running at this interval keeps its place, so a stream of states doesn't push the read out forever
        if (self.readTimer.isActive() and self.readTimer.interval() == interval):
            return
        self.readTimer.setInterval(interval)
        self.readTimer.start()

    def readAll(self):
        self.lastStartingRead = time.monotonic()
        self.readRequested.emit(True)

    def readOnTimer(self):
        now = time.monotonic()
        activate = now - self.lastStartingRead >= TIMED_START_INTERVAL_S
        if (activate):
            self.lastStartingRead = now
        if (self.timedMethod == READ_SILENT):
            self.readSilentRequested.emit(activate)
        else:
            self.readRequested.emit(activate)

    def applyService(self, state):
        self.answered = True
        self.serviceState = state
        if (state is None):
            # the service can come back with the same entries, so they all get copied again then
            self.appliedEntries.clear()
            for mouse in self.mice:
                if (mouse.nodePath is not None and mouse.upowerPath is None):
                    mouse.asleep = True
                    mouse.stale = True
            self.updateTimer()
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
        self.updateTimer()
        self.changed.emit()

    def copyEntry(self, entry):
        mouse = self.mouseFor(entry.key, pathKey=entry.pathKey)
        mouse.stale = False
        mouse.reportsItself = entry.reportsItself
        mouse.historyRevision = entry.historyRevision
        if (entry.name):
            mouse.name = entry.name
        mouse.nodePath = entry.source
        # a reading UPower gave stays until the service has a later one, so an older entry never brings back a stale level or makes the warner warn again
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
        mouse.stale = False
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
        # a device can name a fresh unit id at every identify, so nothing is kept armed for a key that's gone
        self.warner.forget(mouse.key, mouse.pathKey)

    def markWarningSent(self, key, percent):
        self.warner.confirm(key, percent)

    def markWarningFailed(self, key, percent):
        self.warner.giveBack(key, percent)

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
        mouse.stale = self.serviceLost()

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
        # key order, since a mouse that comes back goes to the end of the list and would take another one's number
        for mouse in sorted(self.mice, key=lambda mouse: mouse.key):
            base = mouse.name or DEFAULT_NAME
            counts[base] = counts.get(base, 0) + 1
            names[mouse.key] = base if counts[base] == 1 else base + " " + str(counts[base])
        return names

    def serviceLost(self):
        return self.answered and self.serviceState is None

    def anyDenied(self):
        return self.serviceState is not None and bool(self.serviceState.deniedPaths)

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
