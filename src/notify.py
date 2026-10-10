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


def isNotificationId(value):
    # the spec's ids are uint32, while a server that sends another type can't match a handler, and a list can't key one at all
    return isinstance(value, int) and not isinstance(value, bool)


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
        self.askingCapabilities = False
        self.waitingSends = []
        # only the notification server's own signals count, so another program on the bus can't press a button
        self.bus.connect(SERVICE, PATH, SERVICE, "ActionInvoked", self.onActionInvoked)
        self.bus.connect(SERVICE, PATH, SERVICE, "NotificationClosed", self.onNotificationClosed)

    def bodyText(self, body):
        escaped = html.escape(body, quote=False)
        # a body with nothing to escape reads the same either way, so the server isn't asked
        if (escaped == body):
            return body
        # only a server that answered without body-markup is known to show tags as written, so the body goes out escaped for every other server, including one that didn't answer
        if (self.bodyMarkup is False):
            return body
        return escaped

    def send(self, summary, body, actions=(), onAction=None, onSent=None):
        # onSent gets the notice's id and None, or None and the error name, once the server answers
        if (self.bodyMarkup is None and html.escape(body, quote=False) != body):
            # a body escaping would change waits for the server to say whether it reads markup
            self.waitingSends.append((summary, body, actions, onAction, onSent))
            self.askCapabilities()
            return
        self.sendNotice(summary, self.bodyText(body), actions, onAction, onSent)

    def askCapabilities(self):
        if (self.askingCapabilities):
            return
        self.askingCapabilities = True
        message = QDBusMessage.createMethodCall(SERVICE, PATH, SERVICE, "GetCapabilities")
        dbusCalls.callLater(self.bus, message, self.onCapabilities, self, CALL_TIMEOUT_MS)

    def onCapabilities(self, reply):
        self.askingCapabilities = False
        # a server that can't answer yet is asked again with the next notice, and the bodies waiting now go out escaped
        if (dbusCalls.failure(reply) is None and reply.arguments()):
            capabilities = dbusCalls.plain(reply.arguments()[0])
            # an answer of another type counts as no answer
            if (isinstance(capabilities, list)):
                self.bodyMarkup = "body-markup" in [str(capability) for capability in capabilities]
        waiting = self.waitingSends
        self.waitingSends = []
        for summary, body, actions, onAction, onSent in waiting:
            self.sendNotice(summary, self.bodyText(body), actions, onAction, onSent)

    def sendNotice(self, summary, body, actions, onAction, onSent):
        message = notifyMessage(summary, body, actions)
        dbusCalls.callLater(self.bus, message, lambda reply: self.onNotified(reply, onAction, onSent), self, CALL_TIMEOUT_MS)

    def onNotified(self, reply, onAction, onSent):
        if (dbusCalls.failure(reply) is not None or not reply.arguments()):
            if (onSent is not None):
                onSent(None, reply.errorName())
            return
        notificationId = reply.arguments()[0]
        # the server took the notice whatever type its id came in, so it still counts as sent
        if (onAction is not None and isNotificationId(notificationId)):
            self.handlers[notificationId] = onAction
        if (onSent is not None):
            onSent(notificationId, None)

    def sendNow(self, summary, body):
        # a process about to exit runs no event loop to take an answer, so this one waits for the server
        reply = dbusCalls.call(self.bus, notifyMessage(summary, self.bodyText(body), ()), CALL_TIMEOUT_MS)
        if (dbusCalls.failure(reply) is not None or not reply.arguments()):
            return (None, reply.errorName())
        return (reply.arguments()[0], None)

    @pyqtSlot(QDBusMessage)
    def onActionInvoked(self, message):
        arguments = message.arguments()
        if (len(arguments) < 2 or not isNotificationId(arguments[0])):
            return
        handler = self.handlers.get(arguments[0])
        if (handler is not None):
            handler(str(arguments[1]))

    @pyqtSlot(QDBusMessage)
    def onNotificationClosed(self, message):
        arguments = message.arguments()
        if (not arguments or not isNotificationId(arguments[0]) or arguments[0] not in self.handlers):
            return
        notificationId = arguments[0]
        handler = self.handlers[notificationId]
        QTimer.singleShot(HANDLER_GRACE_MS, lambda: self.dropHandler(notificationId, handler))

    def dropHandler(self, notificationId, handler):
        # a server can give a closed notice's id to a new one, whose handler stays
        if (self.handlers.get(notificationId) is handler):
            del self.handlers[notificationId]
