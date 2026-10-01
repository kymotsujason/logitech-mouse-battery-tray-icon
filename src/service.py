#!/usr/bin/env python3
import signal
import sys

from PyQt6.QtCore import QCoreApplication, QObject, pyqtClassInfo, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusAbstractAdaptor, QDBusConnection

import serviceState
from exceptionHooks import installExceptionHooks
from receivers import ReceiverList


@pyqtClassInfo("D-Bus Interface", serviceState.INTERFACE)
class BatteryService(QDBusAbstractAdaptor):
    StateChanged = pyqtSignal(str)

    def __init__(self, parent, receiverList):
        super().__init__(parent)
        self.receiverList = receiverList
        self.sentState = None
        self.setAutoRelaySignals(True)
        receiverList.changed.connect(self.onChanged)

    @pyqtSlot(result=str)
    def GetState(self):
        return serviceState.encodeState(self.receiverList)

    @pyqtSlot()
    def ReadAll(self):
        self.receiverList.requestReadAll()

    def onChanged(self):
        state = serviceState.encodeState(self.receiverList)
        if (state != self.sentState):
            self.sentState = state
            self.StateChanged.emit(state)


class ServiceObject(QObject):
    pass


def serve(bus, receiverList):
    # the bus exports the adaptor through its parent, so the caller keeps both alive for as long as it serves
    holder = ServiceObject()
    adaptor = BatteryService(holder, receiverList)
    if (not bus.registerObject(serviceState.OBJECT_PATH, holder)):
        print("Couldn't register " + serviceState.OBJECT_PATH + ": " + bus.lastError().message(), file=sys.stderr)
        return None
    if (not bus.registerService(serviceState.SERVICE_NAME)):
        print("Couldn't take " + serviceState.SERVICE_NAME + ": " + bus.lastError().message(), file=sys.stderr)
        return None
    return (holder, adaptor)


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
    receiverList.start()
    code = app.exec()
    receiverList.stop()
    return code


if (__name__ == "__main__"):
    sys.exit(main())
