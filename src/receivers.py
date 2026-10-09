import errno
import os
import queue
import select
import threading
import time
import traceback

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, pyqtSignal

import hidpp
import history
from exceptionHooks import printLine

WAKE_RETRY_MS = 6 * 1000
ERROR_RETRY_LIMIT = 3
SETTLE_MS = 1500
DENIED_RETRY_MS = 5 * 1000
DENIED_RETRY_MAX_MS = 300 * 1000
READ_ALL_GAP_MS = 1000
# any local account can ask for a read, so the search a read brings with it can't enumerate every slot at the read's own rate
READ_ALL_SEARCH_GAP_MS = 60 * 1000
# a node that served this long before it died counts as a one-off instead of a failing device, so its backoff and its printed error start over
STABLE_MS = 60 * 1000
# /dev churn restarts the settle wait, and this caps how far the scan it delays can be pushed out
SETTLE_CEILING_MS = 5 * 1000
# one identify costs about a dozen requests, so this holds a stream of link up notices to the rate a read already runs at
IDENTIFY_GAP_MS = 1000
# a write a stalled hub or a busy receiver turns away fails only its request, while these, or an end of file with no errno, mean the device is gone
GONE_ERRNOS = (errno.ENODEV, errno.EIO, errno.ESHUTDOWN, errno.EBADF, errno.ENXIO, errno.ENOENT)
TRANSIENT_ERROR_LIMIT = 3
STOP_WAIT_SECONDS = 1.0


def printTraceback(heading):
    printLine(heading)
    try:
        traceback.print_exc()
    except (OSError, ValueError):
        pass


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
        self.transientErrors = 0
        node.onEvent = self.onEvent
        node.stopEvent = self.stopping
        node.wakeFd = self.wakeRead

    def onEvent(self, report):
        # each post is a queued hop onto the Qt thread, and another program polling the receiver gets a reply to every one of its requests, which the main thread drops anyway
        if (len(report) < 5 or hidpp.isReply(report)):
            return
        # stamped as it's read, since a search's reading crosses to the Qt thread only after events this thread read later
        self.post(("report", self, report, time.monotonic()))

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

    def signalStop(self):
        self.stopping.set()
        self.wake()

    def stop(self, timeout=STOP_WAIT_SECONDS):
        self.signalStop()
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
                printTraceback("Error reading " + self.path + ":")
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
                self.transientErrors = 0
            except OSError as error:
                if (error.errno is None or error.errno in GONE_ERRNOS or self.transientErrors + 1 >= TRANSIENT_ERROR_LIMIT):
                    raise
                self.transientErrors += 1
                self.post(("failed", self, item, error))
            except Exception:
                # sys.excepthook never sees another thread's exceptions, so print it here and keep serving the node
                printTraceback("Error running " + str(item[0]) + " on " + self.path + ":")
                self.post(("failed", self, item))

    def runItem(self, item):
        kind = item[0]
        if (kind == "search"):
            return ("searched", self, hidpp.searchNode(self.node, item[1]), item[2])
        if (kind == "read"):
            return ("read", self, item[1], hidpp.readBattery(self.node, item[2]), item[3])
        if (kind == "identify"):
            return ("identified", self, item[1], hidpp.identifySlot(self.node, item[1]), item[2])
        if (kind == "wake"):
            return ("wakeFound", self, item[1], hidpp.findWakeIndex(self.node, item[1]))
        raise ValueError("unknown work item " + repr(kind))


def singleShotTimer(parent, interval, slot):
    timer = QTimer(parent)
    timer.setSingleShot(True)
    timer.setInterval(interval)
    timer.timeout.connect(slot)
    return timer


def nodeSignature(path):
    # a replug makes a new inode and udev granting access changes the mode or group, while the times any account can set aren't part of it
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_ino, info.st_rdev, info.st_mode, info.st_uid, info.st_gid)


class ReceiverMouse:
    def __init__(self, key, pathKey=False):
        self.key = key
        # a key built from a path can pass to a different mouse later, while a unit id or serial stays with its mouse
        self.pathKey = pathKey
        self.name = None
        self.nodePath = None
        self.slot = None
        self.feature = None
        self.reading = None
        self.asleep = False
        self.lastRead = None
        self.wokeAt = None
        self.batteryEventAt = None
        self.wakeTimer = None
        self.errorRetries = 0
        # the wireless status feature's index from the latest identify, which makes its events wakes
        self.wakeIndex = None
        # a unit id's mouse whose lookup didn't answer, so its next read or battery event asks again
        self.wakeLookupDue = False
        self.reportsItself = False
        self.historyRevision = 0


class NodeState:
    def __init__(self, path, worker, retryTimer, identifyTimer=None):
        self.path = path
        self.worker = worker
        self.retryTimer = retryTimer
        self.identifyTimer = identifyTimer
        self.stableTimer = None
        self.openedAt = time.monotonic()
        self.identifiedAt = {}
        self.skipped = set()
        self.searching = False
        self.searchAgain = False
        self.searched = False
        self.followUp = False
        self.reportSearchAt = None
        self.searchStartedAt = None
        self.retrySlots = set()
        self.noticedSlots = set()
        self.pendingReads = set()
        self.readAgain = set()
        self.pendingIdentifies = set()
        self.identifyAgain = set()
        self.pendingWakeLookups = set()


class ReceiverList(QObject):
    changed = pyqtSignal()
    message = pyqtSignal(object)

    def __init__(self, recorder=None):
        super().__init__()
        # with no history folder the recorder still keeps its rules in memory, which is where reports on its own comes from
        self.recorder = recorder if recorder is not None else history.Recorder(None)
        self.recorder.changed.connect(self.onHistoryChanged)
        self.nodes = {}
        self.denied = set()
        self.deniedSignatures = {}
        # each path's device node the last time it was denied, which outlives an open so a new device or new access is told from the same one failing again
        self.lastDeniedSignatures = {}
        self.mice = []
        self.stopped = False
        self.reportedErrors = set()
        # what was last printed for each path, so a node that keeps opening and dying prints its open once instead of each cycle
        self.lastPrinted = {}
        self.listingFailed = False
        self.listedOnce = False
        # workers emit this from their own threads, so Qt queues it onto this object's thread
        self.message.connect(self.onMessage)
        self.deniedRetryMs = DENIED_RETRY_MS
        self.deniedTimer = singleShotTimer(self, DENIED_RETRY_MS, self.retryDenied)
        # udev sets the node's group and mode a moment after it shows up, so wait before looking at it
        self.settleTimer = singleShotTimer(self, SETTLE_MS, self.scanNodes)
        self.settleSince = None
        self.readAllTimer = singleShotTimer(self, READ_ALL_GAP_MS, self.runReadAll)
        self.lastReadAll = None
        # a silent read has its own queue, so a full read asked for while one waits still runs in full
        self.readSilentTimer = singleShotTimer(self, READ_ALL_GAP_MS, self.runReadSilent)
        self.lastReadSilent = None
        self.lastReadAllSearch = None
        self.devWatcher = QFileSystemWatcher(["/dev"], self)
        self.devWatcher.directoryChanged.connect(self.onDevChanged)

    def start(self):
        self.scanNodes()

    def stop(self):
        self.stopped = True
        for timer in (self.deniedTimer, self.settleTimer, self.readAllTimer, self.readSilentTimer):
            timer.stop()
        self.closeNodes(list(self.nodes))

    def isBusy(self):
        # a queued batch isn't work in flight, and any local caller can keep one queued, which would hold every check at its full wait
        return any(state.searching or state.pendingReads or state.pendingIdentifies for state in self.nodes.values())

    def isSearching(self):
        # with no listing yet the service can't say there's no receiver, so it's still looking instead of finding none
        if (self.listingFailed and not self.listedOnce):
            return True
        return any(not state.searched for state in self.nodes.values())

    def isReadAllQueued(self):
        return self.readAllTimer.isActive()

    def requestReadAll(self):
        # any local user may ask, so a batch that waits for its turn takes in every call until it runs
        if (self.isReadAllQueued()):
            return
        gap = READ_ALL_GAP_MS / 1000
        now = time.monotonic()
        if (self.lastReadAll is not None and now - self.lastReadAll < gap):
            self.readAllTimer.start(max(1, int((self.lastReadAll + gap - now) * 1000)))
            return
        self.runReadAll()

    def runReadAll(self):
        self.lastReadAll = time.monotonic()
        self.readAll()
        self.searchNodesWithoutMice()

    def isReadSilentQueued(self):
        return self.readSilentTimer.isActive()

    def requestReadSilent(self):
        if (self.isReadSilentQueued()):
            return
        gap = READ_ALL_GAP_MS / 1000
        now = time.monotonic()
        if (self.lastReadSilent is not None and now - self.lastReadSilent < gap):
            self.readSilentTimer.start(max(1, int((self.lastReadSilent + gap - now) * 1000)))
            return
        self.runReadSilent()

    def runReadSilent(self):
        self.lastReadSilent = time.monotonic()
        # a mouse that reports each step on its own needs no timed read
        for mouse in self.mice:
            if (mouse.nodePath is not None and not self.recorder.reportsItself(mouse.key)):
                self.readMouse(mouse)

    def searchNodesWithoutMice(self):
        now = time.monotonic()
        if (self.lastReadAllSearch is not None and (now - self.lastReadAllSearch) * 1000 < READ_ALL_SEARCH_GAP_MS):
            return
        # 1.0.1 searched every node whenever the tray or check started, so a mouse that slept through the first search is looked for again
        withMice = {mouse.nodePath for mouse in self.mice}
        for path, state in list(self.nodes.items()):
            # a search that's already running stands in for this one, instead of a whole second search queued after it
            if (path not in withMice and not state.searching):
                self.lastReadAllSearch = now
                self.requestSearch(path)

    def onDevChanged(self, path):
        if (self.stopped):
            return
        now = time.monotonic()
        if (self.settleSince is None or not self.settleTimer.isActive()):
            self.settleSince = now
        # steady churn would otherwise push the scan out for as long as it lasts, so the wait a burst can add up to is capped
        waited = (now - self.settleSince) * 1000
        self.settleTimer.start(max(1, int(min(SETTLE_MS, SETTLE_CEILING_MS - waited))))

    def listNodes(self):
        try:
            paths = hidpp.findHidppNodes()
        except OSError as error:
            # a retry loop over a listing that keeps failing, such as one out of fds, prints once until a listing works
            if (not self.listingFailed):
                self.listingFailed = True
                printLine("Couldn't list HID++ nodes: " + (errno.errorcode.get(error.errno) or str(error)))
            return None
        self.listingFailed = False
        self.listedOnce = True
        return paths

    def scanNodes(self):
        self.settleSince = None
        before = (set(self.nodes), set(self.denied), self.isSearching())
        paths = self.listNodes()
        if (paths is None):
            # a listing that failed can't say what was plugged in or taken out, so nothing changes, and the single shot timer has to come back since only a /dev change arms it otherwise
            self.settleTimer.start(SETTLE_MS)
            if (self.isSearching() != before[2]):
                self.changed.emit()
            return
        removed = [path for path in self.nodes if path not in paths]
        for path in removed:
            self.printRemoved(path)
        self.closeNodes(removed)
        self.keepDenied(paths)
        # a node that shows up at a path discovery dropped is a new device, so its errors get printed again
        self.reportedErrors = {entry for entry in self.reportedErrors if entry[0] in paths}
        for path in paths:
            if (path in self.nodes):
                continue
            # any account can set a /dev entry's times, so a denied node waits for its backoff unless its device node really changed
            if (path in self.denied and self.deniedSignatures.get(path) == nodeSignature(path)):
                continue
            self.openNode(path)
        if (removed):
            self.searchAll()
        self.updateDeniedTimer()
        # a scan that found nothing new has no state for the tray to redraw, and every /dev change runs one
        if ((set(self.nodes), set(self.denied), self.isSearching()) != before):
            self.changed.emit()

    def openNode(self, path):
        # taken before the attempt, so a group or mode udev sets right after a failed open still counts as a change at the next scan
        signature = nodeSignature(path)
        try:
            node = hidpp.HidppNode.open(path)
        except OSError as error:
            self.reportError(path, "Couldn't open", error)
            self.deny(path, signature)
            return
        # a node whose pipe or thread can't be made is closed and left for the retry instead of leaking its fd, and the worker's messages queue on this thread until the state below exists
        worker = None
        try:
            worker = NodeWorker(path, node, self.message.emit)
            worker.start()
        except (OSError, RuntimeError) as error:
            if (worker is not None):
                worker.closePipe()
            node.close()
            self.reportError(path, "Couldn't start", error)
            self.deny(path, signature)
            return
        retryTimer = singleShotTimer(self, WAKE_RETRY_MS, lambda: self.retrySearch(path))
        identifyTimer = singleShotTimer(self, IDENTIFY_GAP_MS, lambda: self.runDeferredIdentifies(path))
        self.nodes[path] = NodeState(path, worker, retryTimer, identifyTimer)
        self.denied.discard(path)
        self.deniedSignatures.pop(path, None)
        if (self.lastPrinted.get(path) not in ("Opened", "Failing")):
            self.lastPrinted[path] = "Opened"
            printLine("Opened " + path)
        elif (self.lastPrinted.get(path) == "Failing"):
            # the journal's last word on this node is that it keeps failing, so a node that serves as long as a stable one says it works again, while one that dies after each search stays quiet
            self.nodes[path].stableTimer = singleShotTimer(self, STABLE_MS, lambda: self.printWorkingAgain(path))
            self.nodes[path].stableTimer.start()
        self.requestSearch(path)

    def printWorkingAgain(self, path):
        if (path not in self.nodes or self.lastPrinted.get(path) != "Failing"):
            return
        # Qt can fire a timer a little early, so the errors are forgotten here instead of left to the loss's own check, and the next loss is printed as news
        self.forgetErrors(path)
        self.lastPrinted[path] = "Opened"
        printLine(path + " working again")

    def deny(self, path, signature):
        # a path denied with a different device node than last time is a new device or new access, so the retry starts over at its shortest wait
        if (path not in self.lastDeniedSignatures or self.lastDeniedSignatures[path] != signature):
            self.deniedRetryMs = DENIED_RETRY_MS
        self.lastDeniedSignatures[path] = signature
        self.denied.add(path)
        self.deniedSignatures[path] = signature

    def keepDenied(self, paths):
        # a path discovery no longer lists may be a different device that took the same hidraw number
        self.denied &= set(paths)
        self.deniedSignatures = {path: signature for path, signature in self.deniedSignatures.items() if path in self.denied}

    def reportError(self, path, what, error):
        # an errno name tells a missing ACL from a failing device or from running out of fds, and printing each message once per path keeps a retry loop quiet
        detail = errno.errorcode.get(error.errno) if (isinstance(error, OSError) and error.errno) else None
        detail = detail or str(error) or type(error).__name__
        if ((path, what, detail) in self.reportedErrors):
            # the open after the first loss was printed, so without a line here the journal would end on that open while every cycle fails
            if (what == "Lost" and self.lastPrinted.get(path) == "Opened"):
                self.lastPrinted[path] = "Failing"
                printLine(path + " keeps failing (" + detail + "), retrying quietly")
            return
        self.reportedErrors.add((path, what, detail))
        self.lastPrinted[path] = what
        printLine(what + " " + path + ": " + detail)

    def printRemoved(self, path):
        self.lastPrinted[path] = "Removed"
        printLine("Removed " + path)

    def forgetErrors(self, path):
        self.reportedErrors = {entry for entry in self.reportedErrors if entry[0] != path}

    def closeNodes(self, paths):
        # every worker hears the stop first, so busy nodes share one wait instead of taking turns on the Qt thread
        for path in paths:
            self.nodes[path].worker.signalStop()
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        for path in paths:
            self.closeNode(path, timeout=max(0.0, deadline - time.monotonic()))

    def closeNode(self, path, timeout=STOP_WAIT_SECONDS):
        state = self.nodes.pop(path, None)
        if (state is None):
            return
        for timer in (state.retryTimer, state.identifyTimer, state.stableTimer):
            if (timer is not None):
                timer.stop()
                timer.deleteLater()
        state.worker.stop(timeout)
        for mouse in list(self.mice):
            if (mouse.nodePath == path):
                self.removeMouse(mouse)

    def retryDenied(self):
        before = (set(self.nodes), set(self.denied))
        # a denied path discovery can't vouch for may be a different device that took its number, so nothing opens when the listing fails, while the single shot timer still has to come back
        paths = self.listNodes()
        # doubled before the opens, so a new device node one of them meets starts its wait over at the shortest
        self.deniedRetryMs = min(self.deniedRetryMs * 2, DENIED_RETRY_MAX_MS)
        if (paths is not None):
            self.keepDenied(paths)
            for path in list(self.denied):
                self.openNode(path)
        self.deniedTimer.stop()
        self.updateDeniedTimer()
        if ((set(self.nodes), set(self.denied)) != before):
            self.changed.emit()

    def updateDeniedTimer(self):
        # the wait it reached stands, since a node that opens and dies again would otherwise get the shortest wait every cycle
        if (not self.denied):
            self.deniedTimer.stop()
        elif (not self.deniedTimer.isActive()):
            self.deniedTimer.start(self.deniedRetryMs)
        elif (self.deniedTimer.remainingTime() > self.deniedRetryMs):
            # the timer is shared, so a wait that just started over brings the next retry closer, while nothing pushes another path's further away
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
        state.searchStartedAt = time.monotonic()
        # a notice that comes in while this search runs has to keep its slot out of what the search skips
        state.noticedSlots.clear()
        state.worker.queueWork(("search", frozenset(state.skipped), time.monotonic()))

    def retrySearch(self, path):
        state = self.nodes.get(path)
        if (state is None):
            return
        # a search already running looks at every waiting slot and arms the retry again when one stays unresolved
        if (state.searching):
            return
        # the retry's search counts toward the gap, so a report during it rides on the retry it arms instead of queueing another
        state.reportSearchAt = time.monotonic()
        self.requestSearch(path)

    def finishSearch(self, state):
        state.searching = False
        state.searched = True
        if (state.searchAgain):
            state.searchAgain = False
            # the search a report queued counts toward the gap, so reports during it ride on the retry it arms
            state.reportSearchAt = time.monotonic()
            self.requestSearch(state.path)

    def onMessage(self, message):
        kind = message[0]
        worker = message[1]
        state = self.nodes.get(worker.path)
        # a message from a worker that has since been replaced belongs to a node that's gone
        if (state is None or state.worker is not worker):
            return
        if (kind == "report"):
            self.onReport(state, message[2], message[3])
        elif (kind == "searched"):
            self.onSearched(state, message[2], message[3])
        elif (kind == "read"):
            self.onRead(state, message[2], message[3], message[4])
        elif (kind == "identified"):
            self.onIdentified(state, message[2], message[3], message[4])
        elif (kind == "wakeFound"):
            self.onWakeFound(state, message[2], message[3])
        elif (kind == "failed"):
            self.onFailed(state, message[2], message[3] if len(message) > 3 else None)
        elif (kind == "lost"):
            self.onLost(state, message[2])

    def onSearched(self, state, result, queuedAt):
        try:
            followUp = state.followUp
            state.followUp = False
            skipped = set(result.skipped) - state.noticedSlots
            # a keyboard paired into a known mouse's slot would otherwise be read with the mouse's battery feature, as an identify that skips the slot already handles
            for slot in skipped:
                mouse = self.findByNode(state.path, slot)
                if (mouse is not None):
                    self.removeMouse(mouse)
            state.skipped |= skipped
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
            else:
                # a retry a report armed while it waited out the gap is answered by this search, which looked at every slot that's waiting
                state.retryTimer.stop()
        finally:
            self.finishSearch(state)
            # a first search that fails partway still has to tell the tray it's done looking
            self.changed.emit()

    def onFailed(self, state, item, error=None):
        if (error is not None):
            self.reportError(state.path, "Request failed on", error)
        kind = item[0]
        if (kind == "search"):
            self.finishSearch(state)
            # a request the device turned away says nothing about what's paired, and a slot still waiting needs another look, so the search runs again after the retry wait
            if (error is not None or state.retrySlots):
                state.retryTimer.start()
            # a first search that raises would otherwise leave the menu saying it's looking until something else redraws it
            self.changed.emit()
            return
        if (kind == "wake"):
            # the lookup stays due, so the mouse's next read or battery event asks again
            state.pendingWakeLookups.discard(item[1])
            return
        if (kind == "read"):
            self.settleRead(state, item[1])
        elif (kind == "identify"):
            self.settleIdentify(state, item[1])
        mouse = self.findByNode(state.path, item[1])
        if (error is not None and mouse is not None):
            self.retryLater(mouse)

    def onLost(self, state, error):
        path = state.path
        if ((time.monotonic() - state.openedAt) * 1000 >= STABLE_MS):
            self.forgetErrors(path)
            self.deniedRetryMs = DENIED_RETRY_MS
        # a node discovery no longer lists was unplugged, which reads as an I/O error just like a failing one
        paths = self.listNodes()
        # a listing that fails, such as one out of fds, can't say the node was unplugged, so it's closed and retried like a failing one
        listed = paths is None or path in paths
        if (listed):
            self.reportError(path, "Lost", error)
        else:
            self.printRemoved(path)
        self.closeNode(path)
        if (listed):
            self.deny(path, nodeSignature(path))
            self.updateDeniedTimer()
        self.searchAll()
        self.changed.emit()

    def registerFound(self, state, found, linkUp=False, queuedAt=0.0):
        path = state.path
        key = found.unitId if found.unitId else path + "#" + str(found.slot)
        for other in list(self.mice):
            if (other.key != key and other.nodePath == path and other.slot == found.slot):
                self.removeMouse(other)
        # a slot that turns out to hold a mouse can't stay skipped
        state.skipped.discard(found.slot)
        isNew = all(other.key != key for other in self.mice)
        mouse = self.mouseFor(key, pathKey=not found.unitId)
        moved = (mouse.nodePath != path or mouse.slot != found.slot)
        if (moved):
            # a mouse that used up its quick retries on its old node gets them again on the new one
            mouse.errorRetries = 0
        mouse.name = found.name or mouse.name
        mouse.nodePath = path
        mouse.slot = found.slot
        mouse.feature = found.feature
        # a unit id's feature index doesn't change, so a lookup that didn't answer keeps the one it had, while a slot key may now hold another device
        if (found.wakeAnswered or not found.unitId):
            mouse.wakeIndex = found.wakeIndex
            mouse.wakeLookupDue = False
            supported = mouse.wakeIndex is not None
        elif (mouse.wakeIndex is not None):
            supported = True
        else:
            # nothing says the mouse lacks the feature, so its saved flag stands until a lookup answers
            mouse.wakeLookupDue = True
            supported = None
        self.recorder.setWakeSupport(key, supported, pathKey=mouse.pathKey)
        # the service may have missed something before a mouse it hadn't seen, one that moved, or one whose link came back
        if (isNew or moved or linkUp):
            self.recorder.addMarker(key, history.ORIGIN_GAP, pathKey=mouse.pathKey)
        reading = found.reading
        if (found.outcome == hidpp.ANSWER and found.readAt is not None and mouse.batteryEventAt is not None and mouse.batteryEventAt > found.readAt and mouse.reading is not None):
            # a battery event the worker read after this reading has already reached the mouse, so the older reading doesn't replace it
            reading = mouse.reading
        self.applyOutcome(mouse, found.outcome, reading, queuedAt, woke=(moved or linkUp))
        if (mouse.wakeLookupDue and found.outcome == hidpp.ANSWER):
            # the mouse answered its battery read, so the read this brings asks for the index again instead of leaving it until the next link up
            self.retryLater(mouse)

    def mouseFor(self, key, pathKey=False):
        for mouse in self.mice:
            if (mouse.key == key):
                return mouse
        mouse = ReceiverMouse(key, pathKey)
        self.mice.append(mouse)
        return mouse

    def findByNode(self, path, slot):
        for mouse in self.mice:
            if (mouse.nodePath == path and mouse.slot == slot):
                return mouse
        return None

    def removeMouse(self, mouse):
        if (mouse in self.mice):
            self.recorder.addMarker(mouse.key, history.ORIGIN_GAP, pathKey=mouse.pathKey)
        if (mouse.wakeTimer is not None):
            mouse.wakeTimer.stop()
            mouse.wakeTimer.deleteLater()
            mouse.wakeTimer = None
        if (mouse in self.mice):
            self.mice.remove(mouse)

    def applyReading(self, mouse, reading, woke=False, origin=history.ORIGIN_READ):
        now = time.time()
        mouse.errorRetries = 0
        if (reading is None):
            self.markAsleep(mouse)
            return
        if (woke or mouse.asleep or mouse.reading is None):
            mouse.wokeAt = now
        mouse.reading = reading
        mouse.asleep = False
        mouse.lastRead = now
        self.recorder.addReading(mouse.key, reading, origin, name=mouse.name, pathKey=mouse.pathKey)
        mouse.reportsItself = self.recorder.reportsItself(mouse.key)

    def markAsleep(self, mouse):
        # a mouse already marked asleep adds no more rows, so a switched off mouse under timed reads adds nothing
        if (not mouse.asleep):
            self.recorder.addMarker(mouse.key, history.ORIGIN_ASLEEP, pathKey=mouse.pathKey)
        mouse.asleep = True

    def markWake(self, mouse):
        self.recorder.addMarker(mouse.key, history.ORIGIN_WAKE, pathKey=mouse.pathKey)
        mouse.wokeAt = time.time()

    def onHistoryChanged(self, key):
        for mouse in self.mice:
            if (mouse.key == key):
                before = (mouse.historyRevision, mouse.reportsItself)
                mouse.historyRevision = self.recorder.revision(key)
                mouse.reportsItself = self.recorder.reportsItself(key)
                if ((mouse.historyRevision, mouse.reportsItself) != before):
                    self.changed.emit()

    def applyOutcome(self, mouse, outcome, reading, queuedAt, woke=False):
        if (outcome == hidpp.ANSWER):
            self.applyReading(mouse, reading, woke=woke)
        elif (outcome == hidpp.ERROR):
            # the device answered with an error, such as a busy receiver, so it's there and its last reading stands, and a few quick reads follow before it waits for its next event or the timed read
            if (mouse.errorRetries < ERROR_RETRY_LIMIT):
                mouse.errorRetries += 1
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
        self.lookUpWake(state, mouse)
        if (mouse.slot in state.pendingReads):
            # one more read after the pending one is enough for any burst of requests
            state.readAgain.add(mouse.slot)
            return
        state.pendingReads.add(mouse.slot)
        state.worker.queueWork(("read", mouse.slot, mouse.feature, time.monotonic()))

    def lookUpWake(self, state, mouse):
        if (not mouse.wakeLookupDue or mouse.slot in state.pendingWakeLookups):
            return
        state.pendingWakeLookups.add(mouse.slot)
        state.worker.queueWork(("wake", mouse.slot))

    def onWakeFound(self, state, slot, result):
        state.pendingWakeLookups.discard(slot)
        mouse = self.findByNode(state.path, slot)
        answered, wakeIndex = result
        # an identify that answered since this was queued already set the index
        if (mouse is None or not mouse.wakeLookupDue or not answered):
            return
        mouse.wakeIndex = wakeIndex
        mouse.wakeLookupDue = False
        self.recorder.setWakeSupport(mouse.key, wakeIndex is not None, pathKey=mouse.pathKey)

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
        now = time.monotonic()
        last = state.identifiedAt.get(slot)
        waited = None if last is None else (now - last) * 1000
        # the notice isn't dropped, it waits out the gap, so a slot that keeps announcing itself costs one identify per gap instead of one each
        if (waited is not None and waited < IDENTIFY_GAP_MS):
            state.identifyAgain.add(slot)
            if (not state.identifyTimer.isActive()):
                state.identifyTimer.start(max(1, int(IDENTIFY_GAP_MS - waited)))
            return
        state.identifiedAt[slot] = now
        state.identifyAgain.discard(slot)
        state.pendingIdentifies.add(slot)
        state.worker.queueWork(("identify", slot, now))

    def runDeferredIdentifies(self, path):
        state = self.nodes.get(path)
        if (state is None):
            return
        for slot in sorted(state.identifyAgain):
            self.requestIdentify(state, slot)

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
                self.removeMouse(mouse)
            state.skipped.add(slot)
        elif (result == hidpp.UNKNOWN):
            mouse = self.findByNode(state.path, slot)
            # the slot announced itself but didn't finish answering, so its known mouse is read once more after a wait instead of staying asleep until the timed read
            if (mouse is not None):
                self.retryLater(mouse)
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

    def onReport(self, state, report, receivedAt):
        if (len(report) < 5 or hidpp.isReply(report)):
            return
        slot = report[1]
        if (report[2] == hidpp.CONNECTION_NOTIFICATION and state.searching):
            state.noticedSlots.add(slot)
        mouse = self.findByNode(state.path, slot)
        if (mouse is not None):
            self.onMouseReport(state, mouse, report, receivedAt)
            return
        if (slot in state.skipped):
            if (report[2] == hidpp.CONNECTION_NOTIFICATION):
                state.skipped.discard(slot)
                self.requestReportSearch(state, slot)
            return
        # a search only ever pings the slots HID++ defines, so a report from any other one can never resolve and would ask for a search per report
        if (slot not in hidpp.DEVICE_SLOTS):
            return
        self.requestReportSearch(state, slot)

    def requestReportSearch(self, state, slot):
        state.retrySlots.add(slot)
        # a slot that keeps reporting but never resolves would otherwise hold its node in back to back searches, so the retry wait spaces them
        now = time.monotonic()
        gapPassed = state.reportSearchAt is None or (now - state.reportSearchAt) * 1000 >= WAKE_RETRY_MS
        if (state.searching):
            # a new pairing's notice during an unrelated search, such as a node's first, gets the search right after it, while a search a report or a retry started, however long it runs, answers the reports during it through the retry it arms
            if (state.reportSearchAt is None or (state.searchStartedAt - state.reportSearchAt) * 1000 >= WAKE_RETRY_MS):
                state.reportSearchAt = now
                state.searchAgain = True
            return
        if (state.retryTimer.isActive() or not gapPassed):
            if (not state.retryTimer.isActive()):
                state.retryTimer.start()
            return
        state.reportSearchAt = now
        self.requestSearch(state.path)

    def onMouseReport(self, state, mouse, report, receivedAt):
        mouse.errorRetries = 0
        kind, reading = hidpp.parseEvent(report, mouse.feature, mouse.wakeIndex)
        before = (mouse.reading, mouse.asleep, mouse.lastRead, mouse.wokeAt)
        if (kind == hidpp.EVENT_BATTERY):
            mouse.batteryEventAt = receivedAt
            self.applyReading(mouse, reading, origin=history.ORIGIN_EVENT)
            # a mouse the tray's silent reads skip still sends these, and it's awake to answer
            self.lookUpWake(state, mouse)
        elif (kind == hidpp.EVENT_LINK_DOWN):
            self.markAsleep(mouse)
        elif (kind == hidpp.EVENT_LINK_UP):
            self.markWake(mouse)
            self.requestIdentify(state, mouse.slot)
        elif (kind == hidpp.EVENT_WAKE):
            # the wireless status event is a wake whatever the service thought, and the battery event in the same wake set carries the level, so only a mouse marked asleep is read
            self.markWake(mouse)
            if (mouse.asleep):
                self.onWake(mouse)
        elif (kind == hidpp.EVENT_OTHER and mouse.asleep):
            # a mouse without the wireless status feature keeps the old rule, where any report while asleep is a wake
            if (mouse.wakeIndex is None):
                self.markWake(mouse)
            self.onWake(mouse)
        # most reports from an awake mouse change nothing, while a battery report that repeats its reading still moves lastRead, which the tray needs to tell which path read last
        if ((mouse.reading, mouse.asleep, mouse.lastRead, mouse.wokeAt) != before):
            self.changed.emit()
