from PyQt6.QtCore import QTimer
from PyQt6.QtDBus import QDBus, QDBusMessage, QDBusPendingCallWatcher, QDBusPendingReply, QDBusVariant

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


def callLater(bus, message, onReply, owner, timeoutMs=DEFAULT_TIMEOUT_MS):
    # a slow or hung peer would hold the tray still for the whole timeout, so the reply, or the error standing in for it, reaches onReply from the event loop, and never once owner is gone
    pending = bus.asyncCall(message, timeoutMs)
    reply = QDBusPendingReply(pending)
    if (reply.isFinished()):
        # a call on a connection that's down finishes at once, and Qt never signals a watcher for it
        timer = QTimer(owner)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: handOver(timer, reply, onReply))
        timer.start(0)
        return
    watcher = QDBusPendingCallWatcher(pending, owner)
    watcher.finished.connect(lambda finished: handOver(finished, QDBusPendingReply(finished), onReply))


def handOver(carrier, reply, onReply):
    carrier.deleteLater()
    onReply(reply.reply())


def failure(reply):
    # the error name of a call that failed, or None when it got a reply
    if (reply.type() == QDBusMessage.MessageType.ReplyMessage):
        return None
    return reply.errorName()
