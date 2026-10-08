import sys

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusError, QDBusMessage, QDBusServiceWatcher

import serviceState

# the first call can start the service through D-Bus activation, which takes longer than a blocking call may wait at login
CALL_TIMEOUT_MS = 25 * 1000
RETRY_MS = 5 * 1000
RETRY_MAX_MS = 60 * 1000


def stateFrom(message):
    arguments = message.arguments()
    if (not arguments or not isinstance(arguments[0], str)):
        return None
    return serviceState.decodeState(arguments[0])


class ServiceWatcher(QObject):
    stateChanged = pyqtSignal(object)

    def __init__(self, bus=None):
        super().__init__()
        self.bus = bus if bus is not None else QDBusConnection.systemBus()
        self.retryMs = RETRY_MS
        self.retryTimer = QTimer(self)
        self.retryTimer.setSingleShot(True)
        self.retryTimer.timeout.connect(self.getState)
        self.readRetryTimer = QTimer(self)
        self.readRetryTimer.setSingleShot(True)
        self.readRetryTimer.timeout.connect(self.readAll)
        self.ownerWatcher = None
        self.readSent = False
        # each kind of trouble is printed once until the service answers again, so a retry loop stays quiet in the journal
        self.problems = set()
        # a ReadAll can fail while GetState answers, so its trouble is printed once until a ReadAll goes through instead of once per GetState
        self.readProblems = set()

    def start(self):
        # subscribing by the service's name before the first call keeps a forged signal out and a change in between in
        self.bus.connect(serviceState.SERVICE_NAME, serviceState.OBJECT_PATH, serviceState.INTERFACE, "StateChanged", self.onStateChanged)
        self.ownerWatcher = QDBusServiceWatcher(serviceState.SERVICE_NAME, self.bus, QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange, self)
        self.ownerWatcher.serviceOwnerChanged.connect(self.onOwnerChanged)
        self.getState()

    def stop(self):
        self.retryTimer.stop()
        self.readRetryTimer.stop()

    def callService(self, member, replySlot, errorSlot):
        message = QDBusMessage.createMethodCall(serviceState.SERVICE_NAME, serviceState.OBJECT_PATH, serviceState.INTERFACE, member)
        return self.bus.callWithCallback(message, replySlot, errorSlot, CALL_TIMEOUT_MS)

    def getState(self):
        # Qt returns False without calling either slot when the connection is down
        if (not self.callService("GetState", self.onReply, self.onError)):
            self.reportProblem("noConnection", "Couldn't reach the battery service: the system bus connection is down")
            self.fail()

    def reportProblem(self, kind, line):
        if (kind in self.problems):
            return
        self.problems.add(kind)
        print(line, file=sys.stderr)

    def readAll(self):
        if (not self.callService("ReadAll", self.onReadAllDone, self.onReadAllFailed)):
            self.getState()

    def fail(self):
        self.stateChanged.emit(None)
        if (not self.retryTimer.isActive()):
            self.retryTimer.start(self.retryMs)
            self.retryMs = min(self.retryMs * 2, RETRY_MAX_MS)

    @pyqtSlot(QDBusMessage)
    def onReply(self, message):
        state = stateFrom(message)
        if (state is None):
            self.reportProblem("undecodable", "The battery service sent a state this version can't read")
            self.fail()
            return
        if (self.problems):
            self.problems.clear()
            print("Reached the battery service again", file=sys.stderr)
        self.retryMs = RETRY_MS
        self.stateChanged.emit(state)
        if (not self.readSent):
            # a service that udev started at boot can hold readings from then, as the tray read every mouse when it started in 1.0.1
            self.readSent = True
            self.readAll()

    @pyqtSlot(QDBusError, QDBusMessage)
    def onError(self, error, message):
        self.reportProblem(error.name(), "Couldn't reach the battery service: " + error.name() + ": " + error.message())
        self.fail()

    @pyqtSlot(QDBusMessage)
    def onReadAllDone(self, message):
        self.readRetryTimer.stop()
        self.readProblems.clear()

    @pyqtSlot(QDBusError, QDBusMessage)
    def onReadAllFailed(self, error, message):
        if (error.name() == serviceState.READ_ALL_LIMITED):
            # the account has a read again by the refill time, and every refusal before then folds into the one read asked for then
            if (not self.readRetryTimer.isActive()):
                self.readRetryTimer.start(int(serviceState.READ_ALL_REFILL_SECONDS * 1000))
            return
        if (error.name() not in self.readProblems):
            self.readProblems.add(error.name())
            print("Couldn't ask the battery service for a fresh reading: " + error.name() + ": " + error.message(), file=sys.stderr)
        # a service that holds its name but doesn't answer shows only here, so GetState finds out and fails the same way when it doesn't answer either
        self.getState()

    @pyqtSlot(QDBusMessage)
    def onStateChanged(self, message):
        state = stateFrom(message)
        if (state is None):
            self.reportProblem("undecodable", "The battery service sent a state this version can't read")
            self.fail()
            return
        self.stateChanged.emit(state)

    def onOwnerChanged(self, name, oldOwner, newOwner):
        if (not newOwner):
            self.reportProblem("gone", "The battery service went away")
            self.stateChanged.emit(None)
            return
        self.retryTimer.stop()
        self.retryMs = RETRY_MS
        self.getState()
