import os
import queue
import select
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
DEFAULT_NAME = "Logitech mouse"
STATUS_MICE = "mice"
STATUS_WAITING = "waiting"
STATUS_DENIED = "denied"
STATUS_NONE = "none"
STATUS_TEXT = {
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
        self.wakeRead, self.wakeWrite = os.pipe()
        os.set_blocking(self.wakeWrite, False)
        node.onEvent = self.onEvent

    def onEvent(self, report):
        self.post(("report", self.path, report))

    def queueWork(self, item):
        self.work.put(item)
        self.wake()

    def wake(self):
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
        if (not self.is_alive()):
            self.closePipe()

    def closePipe(self):
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
        except OSError:
            if (not self.stopping.is_set()):
                self.post(("lost", self.path))
        finally:
            self.node.close()

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
                # sys.excepthook never sees another thread's exceptions, thus print it here and keep serving the node
                traceback.print_exc()
                self.post(("failed", self.path, item))

    def runItem(self, item):
        kind = item[0]
        if (kind == "search"):
            return ("searched", self.path, hidpp.searchNode(self.node, item[1]))
        if (kind == "read"):
            return ("read", self.path, item[1], hidpp.readBattery(self.node, item[2]))
        if (kind == "identify"):
            return ("identified", self.path, item[1], hidpp.identifySlot(self.node, item[1]))
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
        self.wakeTimer = None


class NodeState:
    def __init__(self, path, worker, retryTimer):
        self.path = path
        self.worker = worker
        self.retryTimer = retryTimer
        self.skipped = set()
        self.searching = False
        self.searchAgain = False
        self.retrySlots = set()


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
        self.warner = LowBatteryWarner()
        # workers emit this from their own threads, thus Qt queues it onto this object's thread
        self.message.connect(self.onMessage)
        self.readTimer = QTimer(self)
        self.readTimer.setInterval(READ_INTERVAL_MS)
        self.readTimer.timeout.connect(self.readAll)
        self.deniedTimer = QTimer(self)
        self.deniedTimer.setInterval(DENIED_RETRY_MS)
        self.deniedTimer.timeout.connect(self.retryDenied)
        # udev sets the uaccess ACL a moment after a node shows up, thus wait before looking at it
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
        except OSError:
            self.denied.add(path)
            return
        self.denied.discard(path)
        worker = NodeWorker(path, node, self.message.emit)
        retryTimer = singleShotTimer(self, WAKE_RETRY_MS, lambda: self.requestSearch(path))
        self.nodes[path] = NodeState(path, worker, retryTimer)
        worker.start()
        self.requestSearch(path)

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
        for path in list(self.denied):
            self.openNode(path)
        self.updateDeniedTimer()
        self.changed.emit()

    def updateDeniedTimer(self):
        if (not self.denied):
            self.deniedTimer.stop()
        elif (not self.deniedTimer.isActive()):
            self.deniedTimer.start()

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
        state.worker.queueWork(("search", frozenset(state.skipped)))

    def finishSearch(self, state):
        state.searching = False
        if (state.searchAgain):
            state.searchAgain = False
            self.requestSearch(state.path)

    def onMessage(self, message):
        kind = message[0]
        path = message[1]
        if (kind == "report"):
            self.onReport(path, message[2])
        elif (kind == "searched"):
            self.onSearched(path, message[2])
        elif (kind == "read"):
            self.onRead(path, message[2], message[3])
        elif (kind == "identified"):
            self.onIdentified(path, message[2], message[3])
        elif (kind == "failed"):
            self.onFailed(path, message[2])
        elif (kind == "lost"):
            self.onLost(path)

    def onSearched(self, path, result):
        state = self.nodes.get(path)
        if (state is None):
            return
        state.skipped |= set(result.skipped)
        for found in result.mice:
            self.registerFound(path, found)
        if (state.retrySlots):
            foundSlots = {found.slot for found in result.mice}
            if (state.retrySlots - foundSlots - state.skipped):
                state.retryTimer.start()
            state.retrySlots.clear()
        self.finishSearch(state)
        self.changed.emit()

    def onFailed(self, path, item):
        state = self.nodes.get(path)
        if (state is not None and item[0] == "search"):
            self.finishSearch(state)

    def onLost(self, path):
        if (path not in self.nodes):
            return
        self.closeNode(path)
        if (path in hidpp.findHidppNodes()):
            self.denied.add(path)
            self.updateDeniedTimer()
        self.searchAll()
        self.changed.emit()

    def registerFound(self, path, found, linkUp=False):
        key = found.unitId if found.unitId else path + "#" + str(found.slot)
        for other in list(self.mice):
            if (other.key != key and other.nodePath == path and other.slot == found.slot):
                self.detachHidpp(other)
        mouse = self.mouseFor(key, pathKey=not found.unitId)
        moved = (mouse.nodePath != path or mouse.slot != found.slot)
        mouse.name = found.name or mouse.name
        mouse.nodePath = path
        mouse.slot = found.slot
        mouse.feature = found.feature
        self.applyReading(mouse, found.reading, woke=(moved or linkUp))

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

    def readMouse(self, mouse):
        state = self.nodes.get(mouse.nodePath)
        if (state is not None):
            state.worker.queueWork(("read", mouse.slot, mouse.feature))

    def readAll(self):
        for mouse in self.mice:
            if (mouse.nodePath is not None):
                self.readMouse(mouse)

    def onRead(self, path, slot, result):
        mouse = self.findByNode(path, slot)
        if (mouse is None):
            return
        outcome, reading = result
        self.applyReading(mouse, reading if outcome == hidpp.ANSWER else None)
        self.changed.emit()

    def onWake(self, mouse):
        # a burst of wake signs costs one read now and the retry, never more
        if (mouse.wakeTimer is None):
            mouse.wakeTimer = singleShotTimer(self, WAKE_RETRY_MS, lambda: self.readMouse(mouse))
        if (mouse.wakeTimer.isActive()):
            return
        mouse.wakeTimer.start()
        self.readMouse(mouse)

    def onReport(self, path, report):
        state = self.nodes.get(path)
        if (state is None or len(report) < 5 or hidpp.isReply(report)):
            return
        slot = report[1]
        mouse = self.findByNode(path, slot)
        if (mouse is not None):
            kind, reading = hidpp.parseEvent(report, mouse.feature)
            if (kind == hidpp.EVENT_BATTERY):
                self.applyReading(mouse, reading)
            elif (kind == hidpp.EVENT_LINK_DOWN):
                mouse.asleep = True
            elif (kind == hidpp.EVENT_LINK_UP):
                state.worker.queueWork(("identify", slot))
            elif (kind == hidpp.EVENT_OTHER and mouse.asleep):
                self.onWake(mouse)
            self.changed.emit()
            return
        if (slot in state.skipped):
            if (report[2] == hidpp.CONNECTION_NOTIFICATION):
                state.skipped.discard(slot)
                self.requestSearch(path)
            return
        state.retrySlots.add(slot)
        self.requestSearch(path)

    def onIdentified(self, path, slot, outcome):
        state = self.nodes.get(path)
        if (state is None):
            return
        result, found = outcome
        if (result == hidpp.FOUND):
            self.registerFound(path, found, linkUp=True)
        elif (result == hidpp.SKIP):
            mouse = self.findByNode(path, slot)
            if (mouse is not None):
                self.detachHidpp(mouse)
            state.skipped.add(slot)
        self.changed.emit()

    def applyUPower(self, upowerMouse):
        mouse = self.mouseFor(upowerMouse.key, pathKey=upowerMouse.pathKey)
        mouse.upowerPath = upowerMouse.path
        if (mouse.name is None):
            mouse.name = upowerMouse.name
        # UPower sends no sleep signal, thus a changed reading is the only sign the mouse is in use
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
        if (self.nodes):
            return STATUS_WAITING
        if (self.denied):
            return STATUS_DENIED
        return STATUS_NONE
