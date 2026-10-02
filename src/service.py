#!/usr/bin/env python3
import signal
import socket
import sys
import time

from PyQt6.QtCore import QCoreApplication, QObject, QSocketNotifier, QTimer, pyqtClassInfo, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusMessage

import serviceState
from exceptionHooks import installExceptionHooks
from receivers import ReceiverList

# a device can send a report every millisecond, and each one would otherwise be its own encode and system bus broadcast
BROADCAST_MS = 200
# any local account may ask for a read, so each account gets a few at once and then one per refill, against the tray's own read every 300 s
READ_ALL_BURST = 5
READ_ALL_REFILL_SECONDS = 30


@pyqtClassInfo("D-Bus Interface", serviceState.INTERFACE)
class BatteryService(QDBusAbstractAdaptor):
    StateChanged = pyqtSignal(str)

    def __init__(self, parent, receiverList, bus):
        super().__init__(parent)
        self.receiverList = receiverList
        self.bus = bus
        self.sentState = None
        self.callerBudgets = {}
        self.setAutoRelaySignals(True)
        self.broadcastTimer = QTimer(self)
        self.broadcastTimer.setSingleShot(True)
        self.broadcastTimer.setInterval(BROADCAST_MS)
        self.broadcastTimer.timeout.connect(self.broadcast)
        receiverList.changed.connect(self.onChanged)

    @pyqtSlot(result=str)
    def GetState(self):
        return serviceState.encodeState(self.receiverList)

    # QtDBus hands a slot the call itself when its last argument is a QDBusMessage, and leaves it out of the method's D-Bus signature
    @pyqtSlot(QDBusMessage)
    def ReadAll(self, message):
        caller = self.callerOf(message)
        if (caller is None or not self.takeRead(caller)):
            return
        self.receiverList.requestReadAll()

    def callerOf(self, message):
        # a unique name is new on every connection, so the budget follows the account behind it
        reply = self.bus.interface().serviceUid(message.service())
        return reply.value() if reply.isValid() else None

    def takeRead(self, caller):
        now = time.monotonic()
        reads, since = self.callerBudgets.get(caller, (READ_ALL_BURST, now))
        reads = min(READ_ALL_BURST, reads + (now - since) / READ_ALL_REFILL_SECONDS)
        if (reads < 1):
            self.callerBudgets[caller] = (reads, now)
            return False
        self.callerBudgets[caller] = (reads - 1, now)
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


def serve(bus, receiverList):
    # the bus exports the adaptor through its parent, so the caller keeps both alive for as long as it serves
    holder = ServiceObject()
    adaptor = BatteryService(holder, receiverList, bus)
    if (not bus.registerObject(serviceState.OBJECT_PATH, holder)):
        print("Couldn't register " + serviceState.OBJECT_PATH + ": " + bus.lastError().message(), file=sys.stderr)
        return None
    if (not bus.registerService(serviceState.SERVICE_NAME)):
        print("Couldn't take " + serviceState.SERVICE_NAME + ": " + bus.lastError().message(), file=sys.stderr)
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
    receiverList = ReceiverList()
    served = serve(bus, receiverList)
    # exiting 1 lets systemd restart a service that started before the bus loaded its policy
    if (served is None):
        return 1
    # the wakeup fd is one of these sockets, so they're held until the loop ends instead of being freed and closed under the signal handler
    sigterm = quitOnSigterm(app)
    receiverList.start()
    code = app.exec()
    receiverList.stop()
    return code


if (__name__ == "__main__"):
    sys.exit(main())
