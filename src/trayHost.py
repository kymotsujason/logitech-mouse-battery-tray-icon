import os
import shutil
import subprocess
import sys

from PyQt6.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtDBus import QDBusMessage, QDBusServiceWatcher
from PyQt6.QtGui import QDesktopServices

import dbusCalls
import mice
import panelColor

APP_NAME = "Mouse Battery"
WATCHER_SERVICE = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
NO_TRAY_NOTICE_MS = 10 * 1000
HOST_RETRY_MS = 2 * 1000
HOST_RETRY_MAX_MS = 60 * 1000
CALL_TIMEOUT_MS = 2000
EXTENSION_UUIDS = ["appindicatorsupport@rgcjonas.gmail.com", "ubuntu-appindicators@ubuntu.com"]
EXTENSION_URL = "https://extensions.gnome.org/extension/615/appindicator-support/"


def hostRegistered(bus):
    message = QDBusMessage.createMethodCall(WATCHER_SERVICE, WATCHER_PATH, "org.freedesktop.DBus.Properties", "Get")
    message.setArguments([WATCHER_SERVICE, "IsStatusNotifierHostRegistered"])
    reply = dbusCalls.call(bus, message, CALL_TIMEOUT_MS)
    if (dbusCalls.failure(reply) is not None or not reply.arguments()):
        return False
    return bool(dbusCalls.plain(reply.arguments()[0]))


def nameHasOwner(bus, name):
    message = QDBusMessage.createMethodCall("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner")
    message.setArguments([name])
    reply = dbusCalls.call(bus, message, CALL_TIMEOUT_MS)
    return (dbusCalls.failure(reply) is None and bool(reply.arguments() and reply.arguments()[0]))


def installedExtension():
    if (shutil.which("gnome-extensions") is None):
        return None
    for uuid in EXTENSION_UUIDS:
        try:
            result = subprocess.run(["gnome-extensions", "info", uuid], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if (result.returncode == 0):
            return uuid
    return None


class TrayHost(QObject):
    ready = pyqtSignal()
    dontOpenAtLogin = pyqtSignal()

    def __init__(self, sessionBus, notifier):
        super().__init__()
        self.sessionBus = sessionBus
        self.notifier = notifier
        self.isReady = False
        self.extensionUuid = None
        self.hostWatcher = None
        self.retryMs = HOST_RETRY_MS
        self.noticeTimer = mice.singleShotTimer(self, NO_TRAY_NOTICE_MS, self.onNoTray)
        self.hostTimer = mice.singleShotTimer(self, HOST_RETRY_MS, self.checkHost)

    def start(self):
        self.hostWatcher = QDBusServiceWatcher(WATCHER_SERVICE, self.sessionBus, QDBusServiceWatcher.WatchModeFlag.WatchForRegistration, self)
        self.hostWatcher.serviceRegistered.connect(self.onWatcherRegistered)
        self.sessionBus.connect(WATCHER_SERVICE, WATCHER_PATH, WATCHER_SERVICE, "StatusNotifierHostRegistered", self.onHostRegistered)
        self.noticeTimer.start()
        self.checkHost()

    def stop(self):
        self.noticeTimer.stop()
        self.hostTimer.stop()

    def onWatcherRegistered(self, name):
        self.retryMs = HOST_RETRY_MS
        self.checkHost()

    @pyqtSlot(QDBusMessage)
    def onHostRegistered(self, message):
        self.retryMs = HOST_RETRY_MS
        self.checkHost()

    def checkHost(self):
        if (self.isReady):
            return
        # Qt 6.4 caches its first tray check for the whole process, so the tray icon waits for a registered host
        if (not hostRegistered(self.sessionBus)):
            # the watcher signals only report a change, and a failed query looks the same as a "no host" answer here, so both are asked again
            self.hostTimer.start(self.retryMs)
            self.retryMs = min(self.retryMs * 2, HOST_RETRY_MAX_MS)
            return
        self.isReady = True
        self.hostTimer.stop()
        self.noticeTimer.stop()
        self.ready.emit()

    def onNoTray(self):
        self.checkHost()
        if (self.isReady):
            return
        actions = []
        if (nameHasOwner(self.sessionBus, "org.gnome.Shell") and "GNOME-FLASHBACK" not in panelColor.desktopNames(os.environ)):
            body = "Mouse Battery needs the AppIndicator extension to show in the top bar."
            self.extensionUuid = installedExtension()
            if (self.extensionUuid is not None):
                actions.append(("turnOn", "Turn On"))
            else:
                actions.append(("getExtension", "Get Extension"))
        else:
            body = "Mouse Battery needs a system tray with StatusNotifierItem support to show its icon."
        actions.append(("dontOpen", "Don't Open at Login"))
        self.notifier.send(APP_NAME, body, actions, self.onNoticeAction)

    def onNoticeAction(self, key):
        if (key == "turnOn" and self.extensionUuid is not None):
            try:
                subprocess.run(["gnome-extensions", "enable", self.extensionUuid], timeout=10)
            except (OSError, subprocess.TimeoutExpired) as error:
                print("Couldn't turn on " + self.extensionUuid + ": " + str(error), file=sys.stderr)
        elif (key == "getExtension"):
            QDesktopServices.openUrl(QUrl(EXTENSION_URL))
        elif (key == "dontOpen"):
            self.dontOpenAtLogin.emit()
