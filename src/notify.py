import html

from PyQt6.QtCore import QMetaType, QObject, QTimer, QVariant, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage

import dbusCalls

SERVICE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
APP_NAME = "Mouse Battery"
APP_ICON = "logitech-mouse-battery"
CALL_TIMEOUT_MS = 5000
NO_REPLY = "org.freedesktop.DBus.Error.NoReply"
# the notification spec sets no order between NotificationClosed and the ActionInvoked of the same click, so a closed notice's handler stays a moment longer
HANDLER_GRACE_MS = 5000


def uintArgument(value):
    variant = QVariant(value)
    variant.convert(QMetaType(QMetaType.Type.UInt.value))
    return variant


def stringListVariant(values):
    # a QDBusArgument holding a string list fails to marshal when it's the first marshaling in a process on PyQt6 6.4 and 6.6, while a QVariant converted to QStringList goes out as "as" on 6.4, 6.6, and 6.11
    variant = QVariant(list(values))
    variant.convert(QMetaType(QMetaType.Type.QStringList.value))
    return variant


def notifyMessage(summary, body, actions):
    flat = []
    for key, label in actions:
        flat.extend([key, label])
    message = QDBusMessage.createMethodCall(SERVICE, PATH, SERVICE, "Notify")
    # a plain list goes out as av and a plain dict fails on PyQt6 6.4, so both are typed by hand
    message.setArguments([APP_NAME, uintArgument(0), APP_ICON, summary, body, stringListVariant(flat), QVariant({"desktop-entry": APP_ICON}), -1])
    return message


class Notifier(QObject):
    def __init__(self, bus=None):
        super().__init__()
        self.bus = bus if bus is not None else QDBusConnection.sessionBus()
        self.handlers = {}
        self.bodyMarkup = None
        # only the notification server's own signals count, so another program on the bus can't press a button
        self.bus.connect(SERVICE, PATH, SERVICE, "ActionInvoked", self.onActionInvoked)
        self.bus.connect(SERVICE, PATH, SERVICE, "NotificationClosed", self.onNotificationClosed)

    def supportsBodyMarkup(self):
        if (self.bodyMarkup is not None):
            return self.bodyMarkup
        message = QDBusMessage.createMethodCall(SERVICE, PATH, SERVICE, "GetCapabilities")
        reply = dbusCalls.call(self.bus, message, CALL_TIMEOUT_MS)
        if (dbusCalls.failure(reply) is not None or not reply.arguments()):
            # a server that can't answer yet is asked again with the next notice, and None says it didn't answer
            return None
        self.bodyMarkup = "body-markup" in [str(capability) for capability in dbusCalls.plain(reply.arguments()[0])]
        return self.bodyMarkup

    def bodyText(self, body):
        escaped = html.escape(body, quote=False)
        # a body with nothing to escape reads the same either way, so the server isn't asked
        if (escaped == body):
            return body
        # only a server that answered without body-markup is known to show tags as written, so the body goes out escaped for every other server, including one that didn't answer
        if (self.supportsBodyMarkup() is False):
            return body
        return escaped

    def send(self, summary, body, actions=(), onAction=None):
        message = notifyMessage(summary, self.bodyText(body), actions)
        reply = dbusCalls.call(self.bus, message, CALL_TIMEOUT_MS)
        if (dbusCalls.failure(reply) is not None or not reply.arguments()):
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

    @pyqtSlot(QDBusMessage)
    def onNotificationClosed(self, message):
        arguments = message.arguments()
        if (not arguments or arguments[0] not in self.handlers):
            return
        notificationId = arguments[0]
        handler = self.handlers[notificationId]
        QTimer.singleShot(HANDLER_GRACE_MS, lambda: self.dropHandler(notificationId, handler))

    def dropHandler(self, notificationId, handler):
        # a server can give a closed notice's id to a new one, whose handler stays
        if (self.handlers.get(notificationId) is handler):
            del self.handlers[notificationId]
