from PyQt6.QtDBus import QDBus, QDBusMessage, QDBusVariant

DEFAULT_TIMEOUT_MS = 2000


def plain(value):
    while (isinstance(value, QDBusVariant)):
        value = value.variant()
    return value


def pathText(value):
    # an object path can arrive as a QDBusObjectPath or as a plain string
    value = plain(value)
    return value.path() if hasattr(value, "path") else str(value)


def call(bus, message, timeoutMs=DEFAULT_TIMEOUT_MS):
    return bus.call(message, QDBus.CallMode.Block, timeoutMs)


def failure(reply):
    # the error name of a call that failed, or None when it got a reply
    if (reply.type() == QDBusMessage.MessageType.ReplyMessage):
        return None
    return reply.errorName()
