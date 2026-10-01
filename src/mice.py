import errno
import os
import queue
import select
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, pyqtSignal

import hidpp
from lowBattery import LowBatteryWarner

READ_INTERVAL_MS = 300 * 1000
WAKE_RETRY_MS = 6 * 1000
SETTLE_MS = 1500
DENIED_RETRY_MS = 5 * 1000
DENIED_RETRY_MAX_MS = 300 * 1000
DEFAULT_NAME = "Logitech mouse"
STATUS_MICE = "mice"
STATUS_SEARCHING = "searching"
STATUS_WAITING = "waiting"
STATUS_DENIED = "denied"
STATUS_NONE = "none"
STATUS_TEXT = {
    STATUS_SEARCHING: "Looking for mice...",
    STATUS_WAITING: "Move your mouse to wake it.",
    STATUS_DENIED: "Can't read the receiver. Unplug it and plug it back in.",
    STATUS_NONE: "No Logitech mouse found",
}


class NodeWorker(threading.Thread):
    def __init__(self, path, node, post):
        super().__init__(name="hidpp " + path, daemon=True)
        self.path = path
        self.node = node
        self.post = post
        self.work = queue.Queue()
        self.stopping = threading.Event()
        # wake() runs on the Qt thread while this thread closes the pipe, and a write after the close could land in a reused fd
        self.pipeLock = threading.Lock()
        self.wakeRead, self.wakeWrite = os.pipe()
        os.set_blocking(self.wakeWrite, False)
        node.onEvent = self.onEvent

    def onEvent(self, report):
        self.post(("report", self, report))

    def queueWork(self, item):
        self.work.put(item)
        self.wake()

    def wake(self):
        with self.pipeLock:
            if (self.wakeWrite is None):
                return
            try:
                os.write(self.wakeWrite, b"w")
            except BlockingIOError:
                # a full pipe already holds a wake the thread hasn't read
                pass

    def stop(self, timeout=1.0):
        self.stopping.set()
        self.wake()
        if (self.is_alive() and threading.current_thread() is not self):
            self.join(timeout)

    def closePipe(self):
        with self.pipeLock:
            for fd in (self.wakeRead, self.wakeWrite):
                if (fd is not None):
                    os.close(fd)
            self.wakeRead = None
            self.wakeWrite = None

    def run(self):
        try:
            while (not self.stopping.is_set()):
                ready, _, _ = select.select([self.node.fd, self.wakeRead], [], [])
                if (self.wakeRead in ready):
                    os.read(self.wakeRead, 256)
                    self.runQueued()
                if (self.node.fd in ready and not self.stopping.is_set()):
                    report = self.node.readReport()
                    if (report is not None):
                        self.onEvent(report)
        except Exception as error:
            if (not isinstance(error, OSError)):
                # sys.excepthook never sees another thread's exceptions, so print it here before giving the node up
                traceback.print_exc()
            if (not self.stopping.is_set()):
                self.post(("lost", self, error))
        finally:
            self.node.close()
            # the thread owns its pipe, so it closes it even when stop() gave up waiting for it
            self.closePipe()

    def runQueued(self):
        while (not self.stopping.is_set()):
            try:
                item = self.work.get_nowait()
            except queue.Empty:
                return
            try:
                self.post(self.runItem(item))
            except OSError:
                raise
            except Exception:
                # sys.excepthook never sees another thread's exceptions, so print it here and keep serving the node
                traceback.print_exc()
                self.post(("failed", self, item))

    def runItem(self, item):
        kind = item[0]
        if (kind == "search"):
            return ("searched", self, hidpp.searchNode(self.node, item[1]), item[2])
        if (kind == "read"):
            return ("read", self, item[1], hidpp.readBattery(self.node, item[2]), item[3])
        if (kind == "identify"):
            return ("identified", self, item[1], hidpp.identifySlot(self.node, item[1]), item[2])
        raise ValueError("unknown work item " + repr(kind))


def singleShotTimer(parent, interval, slot):
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(interval)
    timer.timeout.connect(slot)
    return timer


class Mouse:
    def __init__(self, key, pathKey=False):
        self.key = key
        # a key built from a path can pass to a different mouse later, while a unit id or serial stays with its mouse
        self.pathKey = pathKey
        self.name = None
        self.nodePath = None
        self.slot = None
        self.feature = None
        self.upowerPath = None
        self.reading = None
        self.asleep = False
        self.lastRead = None
        self.wokeAt = None
        self.batteryEventAt = None
        self.wakeTimer = None


class NodeState:
    def __init__(self, path, worker, retryTimer):
        self.path = path
        self.worker = worker
        self.retryTimer = retryTimer
        self.skipped = set()
        self.searching = False
        self.searchAgain = False
        self.searched = False
        self.followUp = False
        self.retrySlots = set()
        self.noticedSlots = set()
        self.pendingReads = set()
        self.readAgain = set()
        self.pendingIdentifies = set()
        self.identifyAgain = set()


class MouseList(QObject):
    changed = pyqtSignal()
    message = pyqtSignal(object)
    lowBattery = pyqtSignal(str, str, int)

    def __init__(self):
        super().__init__()
        self.nodes = {}
        self.denied = set()
        self.mice = []
        self.stopped = False
        self.reportedErrors = set()
        self.warner = LowBatteryWarner()
        # workers emit this from their own threads, so Qt queues it onto this object's thread
        self.message.connect(self.onMessage)
        self.readTimer = QTimer(self)
        self.readTimer.setInterval(READ_INTERVAL_MS)
        self.readTimer.timeout.connect(self.readAll)
        self.deniedRetryMs = DENIED_RETRY_MS
        self.deniedTimer = singleShotTimer(self, DENIED_RETRY_MS, self.retryDenied)
        # udev sets the uaccess ACL a moment after a node shows up, so wait before looking at it
        self.settleTimer = singleShotTimer(self, SETTLE_MS, self.scanNodes)
        self.devWatcher = QFileSystemWatcher(["/dev"], self)
        self.devWatcher.directoryChanged.connect(self.onDevChanged)

    def start(self):
        self.scanNodes()
        self.readTimer.start()

    def stop(self):
        self.stopped = True
        for timer in (self.readTimer, self.deniedTimer, self.settleTimer):
            timer.stop()
        for path in list(self.nodes):
            self.closeNode(path)

    def onDevChanged(self, path):
        if (self.stopped):
            return
        # a replug is the usual way a denied node gets its access, so the retry starts over at its shortest wait
        self.deniedRetryMs = DENIED_RETRY_MS
        if (self.deniedTimer.isActive()):
            self.deniedTimer.start(self.deniedRetryMs)
        self.settleTimer.start()

    def scanNodes(self):
        paths = hidpp.findHidppNodes()
        removed = [path for path in self.nodes if path not in paths]
        for path in removed:
            self.closeNode(path)
        self.denied &= set(paths)
        for path in paths:
            if (path not in self.nodes):
                self.openNode(path)
        if (removed):
            self.searchAll()
        self.updateDeniedTimer()
        self.changed.emit()

    def openNode(self, path):
        try:
            node = hidpp.HidppNode.open(path)
        except OSError as error:
            self.reportError(path, "Couldn't open", error)
            self.denied.add(path)
            return
        self.forgetErrors(path)
        self.denied.discard(path)
        worker = NodeWorker(path, node, self.message.emit)
        retryTimer = singleShotTimer(self, WAKE_RETRY_MS, lambda: self.requestSearch(path))
        self.nodes[path] = NodeState(path, worker, retryTimer)
        worker.start()
        self.requestSearch(path)

    def reportError(self, path, what, error):
        # an errno name tells a missing ACL from a failing device or from running out of fds, and printing it once per path keeps a retry loop quiet
        detail = errno.errorcode.get(error.errno) if (isinstance(error, OSError) and error.errno) else None
        detail = detail or str(error) or type(error).__name__
        if ((path, detail) in self.reportedErrors):
            return
        self.reportedErrors.add((path, detail))
        print(what + " " + path + ": " + detail, file=sys.stderr)

    def forgetErrors(self, path):
        self.reportedErrors = {entry for entry in self.reportedErrors if entry[0] != path}

    def closeNode(self, path):
        state = self.nodes.pop(path, None)
        if (state is None):
            return
        state.retryTimer.stop()
        state.retryTimer.deleteLater()
        state.worker.stop()
        for mouse in list(self.mice):
            if (mouse.nodePath == path):
                self.detachHidpp(mouse)

    def retryDenied(self):
        before = (self.status(), set(self.nodes))
        # a path discovery no longer lists may be a different device that took the same hidraw number
        self.denied &= set(hidpp.findHidppNodes())
        for path in list(self.denied):
            self.openNode(path)
        self.deniedRetryMs = min(self.deniedRetryMs * 2, DENIED_RETRY_MAX_MS)
        self.deniedTimer.stop()
        self.updateDeniedTimer()
        if ((self.status(), set(self.nodes)) != before):
            self.changed.emit()

    def updateDeniedTimer(self):
        if (not self.denied):
            self.deniedTimer.stop()
            self.deniedRetryMs = DENIED_RETRY_MS
        elif (not self.deniedTimer.isActive()):
            self.deniedTimer.start(self.deniedRetryMs)

    def searchAll(self):
        for path in list(self.nodes):
            self.requestSearch(path)

    def requestSearch(self, path):
        state = self.nodes.get(path)
        if (state is None):
            return
        if (state.searching):
            state.searchAgain = True
            return
        state.searching = True
        # a notice that comes in while this search runs has to keep its slot out of what the search skips
        state.noticedSlots.clear()
        state.worker.queueWork(("search", frozenset(state.skipped), time.monotonic()))

    def finishSearch(self, state):
        state.searching = False
        state.searched = True
        if (state.searchAgain):
            state.searchAgain = False
            self.requestSearch(state.path)

    def onMessage(self, message):
        kind = message[0]
        worker = message[1]
        state = self.nodes.get(worker.path)
        # a message from a worker that has since been replaced belongs to a node that's gone
        if (state is None or state.worker is not worker):
            return
        if (kind == "report"):
            self.onReport(state, message[2])
        elif (kind == "searched"):
            self.onSearched(state, message[2], message[3])
        elif (kind == "read"):
            self.onRead(state, message[2], message[3], message[4])
        elif (kind == "identified"):
            self.onIdentified(state, message[2], message[3], message[4])
        elif (kind == "failed"):
            self.onFailed(state, message[2])
        elif (kind == "lost"):
            self.onLost(state, message[2])

    def onSearched(self, state, result, queuedAt):
        try:
            followUp = state.followUp
            state.followUp = False
            state.skipped |= set(result.skipped) - state.noticedSlots
            for found in result.mice:
                self.registerFound(state, found, queuedAt=queuedAt)
            retry = False
            if (state.retrySlots):
                foundSlots = {found.slot for found in result.mice}
                if (state.retrySlots - foundSlots - state.skipped):
                    retry = True
                state.retrySlots.clear()
            # a slot that answered the ping but not the rest may not have been ready, so it gets one more search, and only one
            if (result.unknown and not followUp):
                retry = True
                state.followUp = True
            if (retry):
                state.retryTimer.start()
        finally:
            self.finishSearch(state)
        self.changed.emit()

    def onFailed(self, state, item):
        kind = item[0]
        if (kind == "search"):
            self.finishSearch(state)
        elif (kind == "read"):
            self.settleRead(state, item[1])
        elif (kind == "identify"):
            self.settleIdentify(state, item[1])

    def onLost(self, state, error):
        path = state.path
        self.reportError(path, "Lost", error)
        self.closeNode(path)
        if (path in hidpp.findHidppNodes()):
            self.denied.add(path)
            self.updateDeniedTimer()
        self.searchAll()
        self.changed.emit()

    def registerFound(self, state, found, linkUp=False, queuedAt=0.0):
        path = state.path
        key = found.unitId if found.unitId else path + "#" + str(found.slot)
        for other in list(self.mice):
            if (other.key != key and other.nodePath == path and other.slot == found.slot):
                self.detachHidpp(other)
        # a slot that turns out to hold a mouse can't stay skipped
        state.skipped.discard(found.slot)
        mouse = self.mouseFor(key, pathKey=not found.unitId)
        moved = (mouse.nodePath != path or mouse.slot != found.slot)
        mouse.name = found.name or mouse.name
        mouse.nodePath = path
        mouse.slot = found.slot
        mouse.feature = found.feature
        self.applyOutcome(mouse, found.outcome, found.reading, queuedAt, woke=(moved or linkUp))

    def mouseFor(self, key, pathKey=False):
        for mouse in self.mice:
            if (mouse.key == key):
                return mouse
        mouse = Mouse(key, pathKey)
        self.mice.append(mouse)
        return mouse

    def findByNode(self, path, slot):
        for mouse in self.mice:
            if (mouse.nodePath == path and mouse.slot == slot):
                return mouse
        return None

    def detachHidpp(self, mouse):
        mouse.nodePath = None
        mouse.slot = None
        mouse.feature = None
        if (mouse.wakeTimer is not None):
            mouse.wakeTimer.stop()
            mouse.wakeTimer.deleteLater()
            mouse.wakeTimer = None
        if (mouse.upowerPath is None and mouse in self.mice):
            self.removeMouse(mouse)

    def removeMouse(self, mouse):
        self.mice.remove(mouse)
        # a unit id or serial stays with its mouse and clears itself above REARM_PERCENT, while a path key can pass to a different mouse
        if (mouse.pathKey):
            self.warner.forget(mouse.key)

    def markWarningSent(self, key, percent):
        self.warner.markSent(key, percent)

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
        percent = self.warner.update(mouse.key, mouse.reading, mouse.asleep)
        if (percent is not None):
            self.lowBattery.emit(mouse.key, self.displayNames()[mouse.key], percent)

    def applyOutcome(self, mouse, outcome, reading, queuedAt, woke=False):
        if (outcome == hidpp.ANSWER):
            self.applyReading(mouse, reading, woke=woke)
        elif (outcome == hidpp.ERROR):
            # the device answered with an error, so it's awake and its last reading stands, while a mouse with no reading yet gets read again soon
            if (mouse.reading is None):
                self.retryLater(mouse)
        elif (mouse.batteryEventAt is not None and mouse.batteryEventAt >= queuedAt):
            # a battery event that arrived while this request waited shows the mouse is awake, so it wins over the timeout
            return
        else:
            self.applyReading(mouse, None)

    def readMouse(self, mouse):
        state = self.nodes.get(mouse.nodePath)
        if (state is None):
            return
        if (mouse.slot in state.pendingReads):
            # one more read after the pending one is enough for any burst of requests
            state.readAgain.add(mouse.slot)
            return
        state.pendingReads.add(mouse.slot)
        state.worker.queueWork(("read", mouse.slot, mouse.feature, time.monotonic()))

    def settleRead(self, state, slot):
        state.pendingReads.discard(slot)
        if (slot in state.readAgain):
            state.readAgain.discard(slot)
            mouse = self.findByNode(state.path, slot)
            if (mouse is not None):
                self.readMouse(mouse)

    def readAll(self):
        for mouse in self.mice:
            if (mouse.nodePath is not None):
                self.readMouse(mouse)

    def onRead(self, state, slot, result, queuedAt):
        self.settleRead(state, slot)
        mouse = self.findByNode(state.path, slot)
        if (mouse is None):
            return
        outcome, reading = result
        self.applyOutcome(mouse, outcome, reading, queuedAt)
        self.changed.emit()

    def requestIdentify(self, state, slot):
        if (slot in state.pendingIdentifies):
            # a newer link up notice gets its own identify once the pending one is done
            state.identifyAgain.add(slot)
            return
        state.pendingIdentifies.add(slot)
        state.worker.queueWork(("identify", slot, time.monotonic()))

    def settleIdentify(self, state, slot):
        state.pendingIdentifies.discard(slot)
        if (slot in state.identifyAgain):
            state.identifyAgain.discard(slot)
            self.requestIdentify(state, slot)

    def onIdentified(self, state, slot, outcome, queuedAt):
        again = slot in state.identifyAgain
        self.settleIdentify(state, slot)
        result, found = outcome
        if (result == hidpp.FOUND):
            self.registerFound(state, found, linkUp=True, queuedAt=queuedAt)
        elif (result == hidpp.SKIP and not again):
            # a notice that came in during this identify already asked for another one, which decides the slot instead
            mouse = self.findByNode(state.path, slot)
            if (mouse is not None):
                self.detachHidpp(mouse)
            state.skipped.add(slot)
        self.changed.emit()

    def retryLater(self, mouse):
        if (mouse.wakeTimer is None):
            mouse.wakeTimer = singleShotTimer(self, WAKE_RETRY_MS, lambda: self.readMouse(mouse))
        if (not mouse.wakeTimer.isActive()):
            mouse.wakeTimer.start()

    def onWake(self, mouse):
        # a burst of wake signs costs one read now and the retry, never more
        if (mouse.wakeTimer is not None and mouse.wakeTimer.isActive()):
            return
        self.retryLater(mouse)
        self.readMouse(mouse)

    def onReport(self, state, report):
        if (len(report) < 5 or hidpp.isReply(report)):
            return
        slot = report[1]
        if (report[2] == hidpp.CONNECTION_NOTIFICATION and state.searching):
            state.noticedSlots.add(slot)
        mouse = self.findByNode(state.path, slot)
        if (mouse is not None):
            self.onMouseReport(state, mouse, report)
            return
        if (slot in state.skipped):
            if (report[2] == hidpp.CONNECTION_NOTIFICATION):
                state.skipped.discard(slot)
                self.requestSearch(state.path)
            return
        state.retrySlots.add(slot)
        self.requestSearch(state.path)

    def onMouseReport(self, state, mouse, report):
        kind, reading = hidpp.parseEvent(report, mouse.feature)
        before = (mouse.reading, mouse.asleep)
        if (kind == hidpp.EVENT_BATTERY):
            mouse.batteryEventAt = time.monotonic()
            self.applyReading(mouse, reading)
        elif (kind == hidpp.EVENT_LINK_DOWN):
            mouse.asleep = True
        elif (kind == hidpp.EVENT_LINK_UP):
            self.requestIdentify(state, mouse.slot)
        elif (kind == hidpp.EVENT_OTHER and mouse.asleep):
            self.onWake(mouse)
        # most reports from an awake mouse change nothing on screen, so only a real change redraws
        if ((mouse.reading, mouse.asleep) != before):
            self.changed.emit()

    def applyUPower(self, upowerMouse):
        mouse = self.mouseFor(upowerMouse.key, pathKey=upowerMouse.pathKey)
        mouse.upowerPath = upowerMouse.path
        if (mouse.name is None):
            mouse.name = upowerMouse.name
        # UPower sends no sleep signal, so a changed reading is the only sign the mouse is in use
        self.applyReading(mouse, upowerMouse.reading, woke=(mouse.reading != upowerMouse.reading))
        self.changed.emit()

    def removeUPower(self, path):
        for mouse in list(self.mice):
            if (mouse.upowerPath == path):
                mouse.upowerPath = None
                if (mouse.nodePath is None):
                    self.removeMouse(mouse)
        self.changed.emit()

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
        if (any(not state.searched for state in self.nodes.values())):
            return STATUS_SEARCHING
        if (self.nodes):
            return STATUS_WAITING
        if (self.denied):
            return STATUS_DENIED
        return STATUS_NONE
