from PyQt6.QtCore import QMetaType, QObject, QVariant, pyqtSlot
from PyQt6.QtDBus import QDBus, QDBusArgument, QDBusConnection, QDBusMessage

import hidpp

SERVICE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
APP_NAME = "Mouse Battery"
APP_ICON = "logitech-mouse-battery"
LOW_PERCENT = 10
CRITICAL_PERCENT = 5
REARM_PERCENT = 20
CALL_TIMEOUT_MS = 5000


class LowBatteryWarner:
    def __init__(self):
        self.sent = {}

    def update(self, key, reading, asleep):
        if (asleep or reading is None):
            return None
        if (reading.charging in hidpp.EXTERNAL_POWER):
            self.sent.pop(key, None)
            return None
        if (reading.charging != 0 or reading.percent is None):
            return None
        # a charge the app never saw (suspend, mouse off, wall charger, new batteries) only shows as a high reading, and the margin over LOW_PERCENT keeps a wobbling level from warning twice
        if (reading.percent > REARM_PERCENT):
            self.sent.pop(key, None)
            return None
        sent = self.sent.get(key, set())
        if (reading.percent <= CRITICAL_PERCENT and CRITICAL_PERCENT not in sent):
            return reading.percent
        if (reading.percent <= LOW_PERCENT and LOW_PERCENT not in sent):
            return reading.percent
        return None

    def markSent(self, key, percent):
        sent = self.sent.setdefault(key, set())
        sent.add(LOW_PERCENT)
        if (percent <= CRITICAL_PERCENT):
            sent.add(CRITICAL_PERCENT)


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
        reply = self.bus.call(message, QDBus.CallMode.Block, CALL_TIMEOUT_MS)
        if (reply.type() != QDBusMessage.MessageType.ReplyMessage or not reply.arguments()):
            return None
        notificationId = reply.arguments()[0]
        if (onAction is not None):
            self.handlers[notificationId] = onAction
        return notificationId

    @pyqtSlot(QDBusMessage)
    def onActionInvoked(self, message):
        arguments = message.arguments()
        if (len(arguments) < 2):
            return
        handler = self.handlers.get(arguments[0])
        if (handler is not None):
            handler(str(arguments[1]))
