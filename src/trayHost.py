import os
import shutil
import sys

from PyQt6.QtCore import QObject, QProcess, QUrl, pyqtSignal, pyqtSlot
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
EXTENSION_CHECK_MS = 5 * 1000
EXTENSION_ENABLE_MS = 10 * 1000
EXTENSION_UUIDS = ["appindicatorsupport@rgcjonas.gmail.com", "ubuntu-appindicators@ubuntu.com"]
EXTENSION_URL = "https://extensions.gnome.org/extension/615/appindicator-support/"


def askHostRegistered(bus, owner, onAnswer):
    message = QDBusMessage.createMethodCall(WATCHER_SERVICE, WATCHER_PATH, "org.freedesktop.DBus.Properties", "Get")
    message.setArguments([WATCHER_SERVICE, "IsStatusNotifierHostRegistered"])

    def onReply(reply):
        if (dbusCalls.failure(reply) is not None or not reply.arguments()):
            onAnswer(False)
            return
        onAnswer(bool(dbusCalls.plain(reply.arguments()[0])))

    dbusCalls.callLater(bus, message, onReply, owner, CALL_TIMEOUT_MS)


def askNameHasOwner(bus, owner, name, onAnswer):
    message = QDBusMessage.createMethodCall("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner")
    message.setArguments([name])
    dbusCalls.callLater(bus, message, lambda reply: onAnswer(dbusCalls.failure(reply) is None and bool(reply.arguments() and reply.arguments()[0])), owner, CALL_TIMEOUT_MS)


def runCommand(owner, arguments, timeoutMs, onDone):
    # onDone gets the exit code and None, or None and why the command didn't run to its end, while the tray keeps running
    process = QProcess(owner)
    process.setStandardOutputFile(QProcess.nullDevice())
    process.setStandardErrorFile(QProcess.nullDevice())
    timedOut = []
    timer = mice.singleShotTimer(process, timeoutMs, lambda: timedOut.append(1) or process.kill())

    def onFinished(exitCode, exitStatus):
        timer.stop()
        process.deleteLater()
        if (timedOut):
            onDone(None, "it didn't finish in time")
        elif (exitStatus != QProcess.ExitStatus.NormalExit):
            onDone(None, "it crashed")
        else:
            onDone(exitCode, None)

    def onError(error):
        # a process that never started sends no finished signal, while every other error is followed by one
        if (error == QProcess.ProcessError.FailedToStart):
            timer.stop()
            process.deleteLater()
            onDone(None, process.errorString())

    process.finished.connect(onFinished)
    process.errorOccurred.connect(onError)
    timer.start()
    process.start(arguments[0], arguments[1:])


def findInstalledExtension(owner, onFound, uuids=None):
    # gnome-extensions is asked about each uuid in turn, and onFound gets the first one installed, or None
    if (uuids is None):
        if (shutil.which("gnome-extensions") is None):
            onFound(None)
            return
        uuids = list(EXTENSION_UUIDS)
    if (not uuids):
        onFound(None)
        return

    def onDone(code, why):
        if (code == 0):
            onFound(uuids[0])
            return
        # a slow or failed answer for one uuid says nothing about the next one
        findInstalledExtension(owner, onFound, uuids[1:])

    runCommand(owner, ["gnome-extensions", "info", uuids[0]], EXTENSION_CHECK_MS, onDone)


class TrayHost(QObject):
    ready = pyqtSignal()
    dontOpenAtLogin = pyqtSignal()

    def __init__(self, sessionBus, notifier):
        super().__init__()
        self.sessionBus = sessionBus
        self.notifier = notifier
        self.isReady = False
        self.checking = False
        self.checkAgain = False
        self.noticeDue = False
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
        if (self.checking):
            # a host that registers while the watcher answers may not be in that answer, so it's asked once more
            self.checkAgain = True
            return
        self.checking = True
        # Qt 6.4 caches its first tray check for the whole process, so the tray icon waits for a registered host
        askHostRegistered(self.sessionBus, self, self.onHostAnswer)

    def onHostAnswer(self, registered):
        self.checking = False
        if (self.isReady):
            return
        if (registered):
            self.isReady = True
            self.noticeDue = False
            self.hostTimer.stop()
            self.noticeTimer.stop()
            self.ready.emit()
            return
        if (self.checkAgain):
            self.checkAgain = False
            self.checkHost()
            return
        # the watcher signals only report a change, and a failed query looks the same as a "no host" answer here, so both are asked again
        self.hostTimer.start(self.retryMs)
        self.retryMs = min(self.retryMs * 2, HOST_RETRY_MAX_MS)
        if (self.noticeDue):
            self.noticeDue = False
            askNameHasOwner(self.sessionBus, self, "org.gnome.Shell", self.onShellAnswer)

    def onNoTray(self):
        # the notice goes out only once a last check has found no host
        self.noticeDue = True
        self.checkHost()

    def onShellAnswer(self, hasShell):
        if (self.isReady):
            return
        if (hasShell and "GNOME-FLASHBACK" not in panelColor.desktopNames(os.environ)):
            findInstalledExtension(self, self.onExtensionFound)
            return
        self.sendNotice("Mouse Battery needs a system tray with StatusNotifierItem support to show its icon.", [])

    def onExtensionFound(self, uuid):
        # a host that showed up while gnome-extensions answered makes the notice moot
        if (self.isReady):
            return
        self.extensionUuid = uuid
        action = ("turnOn", "Turn On") if uuid is not None else ("getExtension", "Get Extension")
        self.sendNotice("Mouse Battery needs the AppIndicator extension to show in the top bar.", [action])

    def sendNotice(self, body, actions):
        self.notifier.send(APP_NAME, body, actions + [("dontOpen", "Don't Open at Login")], self.onNoticeAction)

    def onNoticeAction(self, key):
        if (key == "turnOn" and self.extensionUuid is not None):
            uuid = self.extensionUuid
            runCommand(self, ["gnome-extensions", "enable", uuid], EXTENSION_ENABLE_MS, lambda code, why: self.onEnabled(uuid, code, why))
        elif (key == "getExtension"):
            QDesktopServices.openUrl(QUrl(EXTENSION_URL))
        elif (key == "dontOpen"):
            self.dontOpenAtLogin.emit()

    def onEnabled(self, uuid, code, why):
        if (why is not None):
            print("Couldn't turn on " + uuid + ": " + why, file=sys.stderr)
        elif (code != 0):
            print("Couldn't turn on " + uuid + ": gnome-extensions enable exited with " + str(code), file=sys.stderr)
