from PyQt6.QtCore import QMetaType, QObject, QVariant, pyqtSlot
from PyQt6.QtDBus import QDBusArgument, QDBusConnection, QDBusMessage

import dbusCalls

SERVICE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
APP_NAME = "Mouse Battery"
APP_ICON = "logitech-mouse-battery"
CALL_TIMEOUT_MS = 5000
NO_REPLY = "org.freedesktop.DBus.Error.NoReply"


def uintArgument(value):
    variant = QVariant(value)
    variant.convert(QMetaType(QMetaType.Type.UInt.value))
    return variant


def stringListArgument(values):
    argument = QDBusArgument()
    argument.add(values, QMetaType.Type.QStringList.value)
    return argument


class Notifier(QObject):
    def __init__(self, bus=None):
        super().__init__()
        self.bus = bus if bus is not None else QDBusConnection.sessionBus()
        self.handlers = {}
        self.bus.connect("", PATH, SERVICE, "ActionInvoked", self.onActionInvoked)

    def send(self, summary, body, actions=(), onAction=None):
        flat = []
        for key, label in actions:
            flat.extend([key, label])
        message = QDBusMessage.createMethodCall(SERVICE, PATH, SERVICE, "Notify")
        # a plain list goes out as av and a plain dict fails on PyQt6 6.4, thus both are typed by hand
        message.setArguments([APP_NAME, uintArgument(0), APP_ICON, summary, body, stringListArgument(flat), QVariant({"desktop-entry": APP_ICON}), -1])
        reply = dbusCalls.call(self.bus, message, CALL_TIMEOUT_MS)
        if (reply.type() != QDBusMessage.MessageType.ReplyMessage or not reply.arguments()):
            return (None, reply.errorName())
        notificationId = reply.arguments()[0]
        if (onAction is not None):
            self.handlers[notificationId] = onAction
        return (notificationId, None)

    @pyqtSlot(QDBusMessage)
    def onActionInvoked(self, message):
        arguments = message.arguments()
        if (len(arguments) < 2):
            return
        handler = self.handlers.get(arguments[0])
        if (handler is not None):
            handler(str(arguments[1]))
