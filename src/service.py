#!/usr/bin/env python3
import json
import os
import signal
import socket
import sys
import time
from collections import OrderedDict

from PyQt6.QtCore import QCoreApplication, QObject, QSocketNotifier, QTimer, pyqtClassInfo, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusMessage

import history
import serviceState
from exceptionHooks import installExceptionHooks, printLine
from receivers import ReceiverList, printTraceback
from upower import UPowerWatcher
from version import VERSION

# a device can send a report every millisecond, and each one would otherwise be its own encode and system bus broadcast
BROADCAST_MS = 200
# any local account may ask for a read, so each account gets a few at once and then one per refill, against the tray's own read every 300 s
READ_ALL_BURST = 5
READ_ALL_REFILL_SECONDS = serviceState.READ_ALL_REFILL_SECONDS
READ_ALL_LIMITED = serviceState.READ_ALL_LIMITED
CALLER_UNKNOWN = serviceState.CALLER_UNKNOWN


@pyqtClassInfo("D-Bus Interface", serviceState.INTERFACE)
class BatteryService(QDBusAbstractAdaptor):
    StateChanged = pyqtSignal(str)

    def __init__(self, parent, receiverList, bus):
        super().__init__(parent)
        self.receiverList = receiverList
        self.bus = bus
        self.sentState = None
        self.callerBudgets = OrderedDict()
        # GetHistory has a budget of its own, so an open window never costs anyone a read
        self.historyBudgets = OrderedDict()
        self.levelBudgets = OrderedDict()
        self.setAutoRelaySignals(True)
        self.broadcastTimer = QTimer(self)
        self.broadcastTimer.setSingleShot(True)
        self.broadcastTimer.setInterval(BROADCAST_MS)
        self.broadcastTimer.timeout.connect(self.broadcast)
        receiverList.changed.connect(self.onChanged)

    @pyqtSlot(result=str)
    def GetState(self):
        state = serviceState.encodeState(self.receiverList)
        # a pending read or search marks the state busy without a change signal, and the state can go back to the last one sent before any broadcast, which would leave this caller holding one nothing corrects
        if (state != self.sentState):
            self.sentState = None
            self.onChanged()
        return state

    # QtDBus hands a slot the call itself when its last argument is a QDBusMessage, and leaves it out of the method's D-Bus signature
    @pyqtSlot(QDBusMessage)
    def ReadAll(self, message):
        caller = self.callerOf(message)
        if (caller is None):
            self.refuse(message, CALLER_UNKNOWN, "Couldn't tell which account asked for the read.")
            return
        # a call that joins the batch already queued adds no device work, so it gets the same empty reply without spending a read
        if (self.receiverList.isReadAllQueued()):
            return
        if (not self.takeRead(caller)):
            self.refuse(message, READ_ALL_LIMITED, "Each account gets " + str(READ_ALL_BURST) + " reads at once and then one every " + str(READ_ALL_REFILL_SECONDS) + " seconds.")
            return
        self.receiverList.requestReadAll()

    @pyqtSlot(QDBusMessage)
    def ReadSilent(self, message):
        caller = self.callerOf(message)
        if (caller is None):
            self.refuse(message, CALLER_UNKNOWN, "Couldn't tell which account asked for the read.")
            return
        # a batch already queued covers this one, so it costs no read
        if (self.receiverList.isReadSilentQueued() or self.receiverList.isReadAllQueued()):
            return
        if (not self.takeRead(caller)):
            self.refuse(message, READ_ALL_LIMITED, "Each account gets " + str(READ_ALL_BURST) + " reads at once and then one every " + str(READ_ALL_REFILL_SECONDS) + " seconds.")
            return
        self.receiverList.requestReadSilent()

    @pyqtSlot(str, QDBusMessage, result=str)
    def GetHistory(self, key, message):
        caller = self.callerOf(message)
        if (caller is None):
            self.refuse(message, CALLER_UNKNOWN, "Couldn't tell which account asked for the history.")
            return ""
        if (len(key) > serviceState.MAX_KEY_LENGTH):
            self.refuse(message, serviceState.INVALID_ARGS, "A key is at most " + str(serviceState.MAX_KEY_LENGTH) + " characters.")
            return ""
        if (not self.takeFrom(self.historyBudgets, caller)):
            self.refuse(message, READ_ALL_LIMITED, "Each account gets " + str(READ_ALL_BURST) + " history reads at once and then one every " + str(READ_ALL_REFILL_SECONDS) + " seconds.")
            return ""
        try:
            summary = self.receiverList.recorder.summary(key)
        except history.HistoryUnavailable:
            self.refuse(message, serviceState.HISTORY_UNAVAILABLE, "The history isn't available right now.")
            return ""
        except Exception:
            # a slot that raises answers an empty string, which a caller would read as a history
            printTraceback("Error reading the history:")
            self.refuse(message, serviceState.HISTORY_UNAVAILABLE, "The history isn't available right now.")
            return ""
        return json.dumps(summary, sort_keys=True)

    @pyqtSlot(str, str, QDBusMessage, result=str)
    def GetLevels(self, key, rangeName, message):
        caller = self.callerOf(message)
        if (caller is None):
            self.refuse(message, CALLER_UNKNOWN, "Couldn't tell which account asked for the levels.")
            return ""
        if (len(key) > serviceState.MAX_KEY_LENGTH):
            self.refuse(message, serviceState.INVALID_ARGS, "A key is at most " + str(serviceState.MAX_KEY_LENGTH) + " characters.")
            return ""
        if (rangeName not in history.LEVEL_RANGES):
            self.refuse(message, serviceState.INVALID_ARGS, "The range is one of " + ", ".join(history.LEVEL_RANGES) + ".")
            return ""
        if (not self.takeFrom(self.levelBudgets, caller, serviceState.LEVELS_BURST, serviceState.LEVELS_REFILL_SECONDS)):
            self.refuse(message, READ_ALL_LIMITED, "Each account gets " + str(serviceState.LEVELS_BURST) + " level reads at once and then one every " + str(serviceState.LEVELS_REFILL_SECONDS) + " seconds.")
            return ""
        try:
            levels = self.receiverList.recorder.levels(key, rangeName)
        except history.HistoryUnavailable:
            self.refuse(message, serviceState.HISTORY_UNAVAILABLE, "The history isn't available right now.")
            return ""
        except Exception:
            printTraceback("Error reading the levels:")
            self.refuse(message, serviceState.HISTORY_UNAVAILABLE, "The history isn't available right now.")
            return ""
        return json.dumps(levels, sort_keys=True)

    def refuse(self, message, name, text):
        # an empty reply would look like a read that ran, so a caller couldn't tell its readings may be old
        message.setDelayedReply(True)
        self.bus.send(message.createErrorReply(name, text))

    def callerOf(self, message):
        # a unique name is new on every connection, so the budget follows the account behind it
        reply = self.bus.interface().serviceUid(message.service())
        return reply.value() if reply.isValid() else None

    def takeRead(self, caller):
        return self.takeFrom(self.callerBudgets, caller)

    def takeFrom(self, budgets, caller, burst=None, refillSeconds=None):
        burst = READ_ALL_BURST if burst is None else burst
        refillSeconds = READ_ALL_REFILL_SECONDS if refillSeconds is None else refillSeconds
        now = time.monotonic()
        # a full budget is what an unseen account starts with, so dropping it changes nothing while the map stays as small as the accounts calling lately
        # every update moves its budget to the end, so the front is the oldest and the prune stops at the first one still refilling instead of walking every account
        while (budgets):
            other, (reads, since) = next(iter(budgets.items()))
            if (reads + (now - since) / refillSeconds < burst):
                break
            del budgets[other]
        reads, since = budgets.pop(caller, (burst, now))
        reads = min(burst, reads + (now - since) / refillSeconds)
        if (reads < 1):
            budgets[caller] = (reads, now)
            return False
        budgets[caller] = (reads - 1, now)
        return True

    def onChanged(self):
        # changes during the wait ride along instead of restarting it, so a steady stream still goes out once per wait
        if (not self.broadcastTimer.isActive()):
            self.broadcastTimer.start()

    def broadcast(self):
        state = serviceState.encodeState(self.receiverList)
        if (state != self.sentState):
            self.sentState = state
            self.StateChanged.emit(state)


class ServiceObject(QObject):
    pass


class UPowerHistory(QObject):
    # the service keeps its own UPower watcher whose readings go only to the history, never into the state the tray reads
    def __init__(self, recorder, bus):
        super().__init__()
        self.recorder = recorder
        self.watcher = UPowerWatcher(bus)
        self.watcher.mouseChanged.connect(self.onMouse)
        self.watcher.mouseRemoved.connect(self.onRemoved)
        self.paths = {}

    def start(self):
        self.watcher.watchOwner()
        self.watcher.start()

    def stop(self):
        self.watcher.stop()

    def onMouse(self, mouse):
        # a serial can show up after a device's first reading, which ends the history kept under its path
        old = self.paths.get(mouse.path)
        if (old is not None and old[0] != mouse.key):
            self.recorder.addMarker(old[0], history.ORIGIN_GAP, source=history.SOURCE_UPOWER, pathKey=old[1])
            self.recorder.forget(old[0], source=history.SOURCE_UPOWER)
            old = None
        wasAsleep = old is not None and old[2]
        if (mouse.asleep and not wasAsleep and self.awakeElsewhere(mouse)):
            # a receiver's path reads asleep while a cable path charges the same mouse, so the receiver's sighting isn't a sleep
            self.paths[mouse.path] = (mouse.key, mouse.pathKey, False)
            return
        self.paths[mouse.path] = (mouse.key, mouse.pathKey, mouse.asleep)
        if (mouse.asleep):
            # UPower repeats a sleeping device every 30 s, and only the first of those is a sleep
            if (not wasAsleep):
                self.recorder.addMarker(mouse.key, history.ORIGIN_ASLEEP, source=history.SOURCE_UPOWER, pathKey=mouse.pathKey)
            return
        if (wasAsleep):
            # one sleep seen ending shows this mouse's link drops when it sleeps, so its drops can be measured from here on
            self.recorder.addMarker(mouse.key, history.ORIGIN_WAKE, source=history.SOURCE_UPOWER, pathKey=mouse.pathKey)
            self.recorder.setWakeSupport(mouse.key, True, pathKey=mouse.pathKey, source=history.SOURCE_UPOWER)
        self.recorder.addReading(mouse.key, mouse.reading, history.ORIGIN_UPOWER, name=mouse.name, source=history.SOURCE_UPOWER, pathKey=mouse.pathKey)

    def awakeElsewhere(self, mouse):
        return any(path != mouse.path and saved[0] == mouse.key and not saved[2] for path, saved in self.paths.items())

    def onRemoved(self, path):
        found = self.paths.pop(path, None)
        if (found is not None):
            self.recorder.addMarker(found[0], history.ORIGIN_GAP, source=history.SOURCE_UPOWER, pathKey=found[1])
            self.recorder.forget(found[0], source=history.SOURCE_UPOWER)


def stateFolder(env):
    # systemd hands the unit's StateDirectory over as a colon separated list, and only the first is the history's
    folder = env.get("STATE_DIRECTORY", "").split(":")[0]
    return folder if os.path.isabs(folder) else None


def serve(bus, receiverList):
    # the bus exports the adaptor through its parent, so the caller keeps both alive for as long as it serves
    holder = ServiceObject()
    adaptor = BatteryService(holder, receiverList, bus)
    if (not bus.registerObject(serviceState.OBJECT_PATH, holder)):
        printLine("Couldn't register " + serviceState.OBJECT_PATH + ": " + bus.lastError().message())
        return None
    if (not bus.registerService(serviceState.SERVICE_NAME)):
        printLine("Couldn't take " + serviceState.SERVICE_NAME + ": " + bus.lastError().message())
        return None
    return (holder, adaptor)


def quitOnSigterm(app):
    # systemd stops the service with SIGTERM, and Python runs a handler only once Qt hands it control, which the wakeup byte's notifier does
    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    signal.set_wakeup_fd(writer.fileno())
    # a quit asked for before the event loop runs would be lost, so it's queued
    signal.signal(signal.SIGTERM, lambda number, frame: QTimer.singleShot(0, app.quit))
    notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read, app)
    notifier.activated.connect(lambda: reader.recv(64))
    return (reader, writer, notifier)


def main(bus=None):
    installExceptionHooks()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    if (bus is None):
        bus = QDBusConnection.systemBus()
    folder = stateFolder(os.environ)
    recorder = history.Recorder(folder)
    receiverList = ReceiverList(recorder)
    served = serve(bus, receiverList)
    # exiting 1 lets systemd restart a service that started before the bus loaded its policy
    if (served is None):
        return 1
    # an upgrade restarts the service, so the journal shows which version took over
    printLine("Mouse Battery service " + VERSION + " started")
    if (folder is None):
        printLine("No state folder was given, so no history is kept")
    else:
        # opening now puts any trouble with the file in the journal at start instead of at the first reading
        recorder.available()
    # the wakeup fd is one of these sockets, so they're held until the loop ends instead of being freed and closed under the signal handler
    sigterm = quitOnSigterm(app)
    upowerHistory = UPowerHistory(recorder, bus)
    upowerHistory.start()
    receiverList.start()
    code = app.exec()
    upowerHistory.stop()
    receiverList.stop()
    # closing the nodes above writes their gap rows, so the recorder stops last
    recorder.stop()
    return code


if (__name__ == "__main__"):
    sys.exit(main())
