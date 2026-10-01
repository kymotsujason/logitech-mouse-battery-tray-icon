#!/usr/bin/env python3
import fcntl
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, QUrl, pyqtSlot
from PyQt6.QtDBus import QDBus, QDBusConnection, QDBusMessage, QDBusServiceWatcher
from PyQt6.QtGui import QAction, QDesktopServices, QIcon, QPalette
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

import autostart
import hidpp
import mice
import panelColor
from icon import IconState, makeIcon
from notify import NO_REPLY, Notifier
from upower import UPowerWatcher
from version import VERSION

APP_ID = "logitech-mouse-battery"
APP_NAME = "Mouse Battery"
LOCK_NAME = "logitech-mouse-battery.lock"
WATCHER_SERVICE = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
NO_TRAY_NOTICE_MS = 10 * 1000
HOST_RETRY_MS = 2 * 1000
HOST_RETRY_MAX_MS = 60 * 1000
INSTALL_SETTLE_MS = 2 * 1000
CALL_TIMEOUT_MS = 2000
TARGET_NICENESS = 10
EXTENSION_UUIDS = ["appindicatorsupport@rgcjonas.gmail.com", "ubuntu-appindicators@ubuntu.com"]
EXTENSION_URL = "https://extensions.gnome.org/extension/615/appindicator-support/"
PROJECT_URL = "https://github.com/kymotsujason/logitech-mouse-battery-tray-icon"
APP_FOLDER = os.path.dirname(os.path.abspath(__file__))
STATUS_TEXT = {
    mice.STATUS_WAITING: "Move your mouse to wake it.",
    mice.STATUS_DENIED: "Can't read the receiver. Unplug it and plug it back in.",
    mice.STATUS_NONE: "No Logitech mouse found",
}


def iconStateFor(mouse):
    if (mouse is None or mouse.reading is None):
        return IconState(empty=True)
    reading = mouse.reading
    return IconState(percent=reading.percent, charging=(reading.charging in hidpp.EXTERNAL_POWER), asleep=mouse.asleep)


def mouseLine(name, mouse):
    if (mouse.reading is None):
        return name + ", no reading yet"
    level = hidpp.levelText(mouse.reading)
    if (mouse.asleep):
        return name + ", asleep, " + level + " at " + time.strftime("%H:%M", time.localtime(mouse.lastRead))
    if (mouse.reading.charging in (1, 2)):
        return name + ", charging, " + level
    if (mouse.reading.charging == 3):
        return name + ", full, " + level
    return name + ", " + level


def statusLines(mouseList):
    names = mouseList.displayNames()
    lines = [mouseLine(names[mouse.key], mouse) for mouse in mouseList.mice]
    if (lines):
        return lines
    return [STATUS_TEXT[mouseList.status()]]


def printException(kind, value, tb):
    traceback.print_exception(kind, value, tb)


def printThreadException(args):
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)


def raiseNiceness(target=TARGET_NICENESS):
    # os.nice adds to the current value and niceness carries across exec, thus it's only topped up
    current = os.nice(0)
    if (current < target):
        os.nice(target - current)


def takeLock(runtimeDir):
    if (not runtimeDir):
        return (True, None)
    try:
        handle = open(os.path.join(runtimeDir, LOCK_NAME), "w")
    except OSError:
        return (True, None)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return (False, None)
    return (True, handle)


def readVersion(folder):
    try:
        with open(os.path.join(folder, "version.py")) as f:
            text = f.read()
    except OSError:
        return None
    match = re.search(r'VERSION = "([^"]*)"', text)
    return match.group(1) if match else None


def hostRegistered(bus):
    message = QDBusMessage.createMethodCall(WATCHER_SERVICE, WATCHER_PATH, "org.freedesktop.DBus.Properties", "Get")
    message.setArguments([WATCHER_SERVICE, "IsStatusNotifierHostRegistered"])
    reply = bus.call(message, QDBus.CallMode.Block, CALL_TIMEOUT_MS)
    if (reply.type() != QDBusMessage.MessageType.ReplyMessage or not reply.arguments()):
        return False
    return bool(panelColor.plain(reply.arguments()[0]))


def nameHasOwner(bus, name):
    message = QDBusMessage.createMethodCall("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner")
    message.setArguments([name])
    reply = bus.call(message, QDBus.CallMode.Block, CALL_TIMEOUT_MS)
    return (reply.type() == QDBusMessage.MessageType.ReplyMessage and bool(reply.arguments() and reply.arguments()[0]))


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


def configFolder(env):
    # the XDG base directory spec says a relative path is invalid and should be ignored
    value = env.get("XDG_CONFIG_HOME", "")
    if (os.path.isabs(value)):
        return value
    return os.path.join(os.path.expanduser("~"), ".config")


class MouseBatteryApp(QObject):
    def __init__(self, app, sessionBus=None, systemBus=None):
        super().__init__()
        self.app = app
        self.sessionBus = sessionBus if sessionBus is not None else QDBusConnection.sessionBus()
        self.systemBus = systemBus if systemBus is not None else QDBusConnection.systemBus()
        self.configHome = configFolder(os.environ)
        self.tray = None
        self.shownState = None
        self.shownColor = None
        self.shownTooltip = None
        self.statusActions = []
        self.extensionUuid = None
        self.portalScheme = 0
        self.hostWatcher = None
        self.colorErrors = set()
        self.mice = mice.MouseList()
        self.mice.changed.connect(self.updateTray)
        self.mice.lowBattery.connect(self.onLowBattery)
        self.upower = UPowerWatcher(self.systemBus)
        self.upower.mouseChanged.connect(self.mice.applyUPower)
        self.upower.mouseRemoved.connect(self.mice.removeUPower)
        self.notifier = Notifier(self.sessionBus)
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.onMenuShown)
        self.separator = self.menu.addSeparator()
        self.loginAction = self.menu.addAction("Open at Login")
        self.loginAction.setCheckable(True)
        self.loginAction.setChecked(autostart.isEnabled(self.configHome))
        self.loginAction.triggered.connect(self.onLoginToggled)
        self.menu.addAction("About Mouse Battery").triggered.connect(lambda: self.showAbout())
        self.menu.addAction("Quit").triggered.connect(lambda: self.quitApp())
        self.noticeTimer = mice.singleShotTimer(self, NO_TRAY_NOTICE_MS, self.onNoTray)
        self.hostRetryMs = HOST_RETRY_MS
        self.hostTimer = mice.singleShotTimer(self, HOST_RETRY_MS, self.checkHost)
        self.installTimer = mice.singleShotTimer(self, INSTALL_SETTLE_MS, self.onInstallChanged)
        self.installWatcher = QFileSystemWatcher(self)
        self.plasmarcWatcher = QFileSystemWatcher(self)
        self.updateTray()

    def start(self):
        self.mice.start()
        self.upower.watchOwner()
        self.upower.start()
        self.hostWatcher = QDBusServiceWatcher(WATCHER_SERVICE, self.sessionBus, QDBusServiceWatcher.WatchModeFlag.WatchForRegistration, self)
        self.hostWatcher.serviceRegistered.connect(self.onWatcherRegistered)
        self.sessionBus.connect("", WATCHER_PATH, WATCHER_SERVICE, "StatusNotifierHostRegistered", self.onHostRegistered)
        self.watchInstallFolder()
        self.watchColors()
        self.noticeTimer.start()
        self.checkHost()

    def stop(self):
        self.noticeTimer.stop()
        self.hostTimer.stop()
        self.mice.stop()

    def quitApp(self):
        self.app.quit()

    def onWatcherRegistered(self, name):
        self.hostRetryMs = HOST_RETRY_MS
        self.checkHost()

    @pyqtSlot(QDBusMessage)
    def onHostRegistered(self, message):
        self.hostRetryMs = HOST_RETRY_MS
        self.checkHost()

    def checkHost(self):
        if (self.tray is not None):
            return
        # Qt 6.4 caches its first tray check for the whole process, thus the tray icon waits for a registered host
        if (not hostRegistered(self.sessionBus)):
            # the watcher signals only report a change, and a failed query looks the same as a "no host" answer here, so both are asked again
            self.hostTimer.start(self.hostRetryMs)
            self.hostRetryMs = min(self.hostRetryMs * 2, HOST_RETRY_MAX_MS)
            return
        self.hostTimer.stop()
        self.noticeTimer.stop()
        self.tray = QSystemTrayIcon(self)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self.onActivated)
        self.shownState = None
        self.shownTooltip = None
        try:
            self.updateTray()
        except Exception:
            # a failed first draw still leaves the icon shown and the app running, as a failed slot does
            sys.excepthook(*sys.exc_info())
        self.tray.show()

    def onNoTray(self):
        self.checkHost()
        if (self.tray is not None):
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
            self.onLoginToggled(False)
            self.quitApp()

    def onMenuShown(self):
        self.mice.readAll()
        self.loginAction.setChecked(autostart.isEnabled(self.configHome))

    def onActivated(self, reason):
        if (reason == QSystemTrayIcon.ActivationReason.Trigger):
            self.mice.readAll()

    def onLoginToggled(self, checked):
        try:
            autostart.setEnabled(self.configHome, checked)
        except OSError as error:
            print("Couldn't change Open at Login: " + str(error), file=sys.stderr)
        self.loginAction.setChecked(autostart.isEnabled(self.configHome))

    def onLowBattery(self, key, name, percent):
        notificationId, error = self.notifier.send("Mouse battery low", name + " has " + str(percent) + "% left.")
        # NoReply means the bus already handed the message to the server (or is starting the server for it), and asking again after a timeout would block every reading
        if (notificationId is None and error != NO_REPLY):
            return
        self.mice.warner.markSent(key, percent)

    def showAbout(self):
        QMessageBox.about(None, "About Mouse Battery", "Mouse Battery " + VERSION + "\n\nShows the battery of Logitech mice in the system tray.\n\n" + PROJECT_URL + "\n\nLicensed under the GNU GPL, version 3 or later.\nNot affiliated with Logitech.")

    def updateStatusActions(self, lines):
        while (len(self.statusActions) < len(lines)):
            action = QAction(self.menu)
            action.setEnabled(False)
            self.menu.insertAction(self.separator, action)
            self.statusActions.append(action)
        while (len(self.statusActions) > len(lines)):
            self.menu.removeAction(self.statusActions.pop())
        for action, line in zip(self.statusActions, lines):
            action.setText(line)

    def baseColor(self):
        palette = self.app.palette().color(QPalette.ColorRole.WindowText)
        try:
            return panelColor.baseColor(os.environ, self.configHome, panelColor.dataDirs(os.environ), self.portalScheme, palette)
        except Exception as error:
            # every redraw asks again, thus each error is printed only the first time
            message = "Couldn't read the panel color: " + str(error)
            if (message not in self.colorErrors):
                self.colorErrors.add(message)
                print(message, file=sys.stderr)
            return palette

    def updateTray(self, *args):
        lines = statusLines(self.mice)
        self.updateStatusActions(lines)
        if (self.tray is None):
            return
        state = iconStateFor(self.mice.shownMouse())
        color = self.baseColor()
        if (state != self.shownState or color != self.shownColor):
            self.tray.setIcon(makeIcon(state, color))
            self.shownState = state
            self.shownColor = color
        tooltip = "\n".join(lines)
        if (tooltip != self.shownTooltip):
            self.tray.setToolTip(tooltip)
            self.shownTooltip = tooltip

    def watchColors(self):
        self.app.paletteChanged.connect(self.onColorsChanged)
        self.plasmarcWatcher.fileChanged.connect(self.onColorsChanged)
        self.watchPlasmarc()
        if ("GNOME" in panelColor.desktopNames(os.environ)):
            self.portalScheme = panelColor.readPortalColorScheme(self.sessionBus)
            self.sessionBus.connect("", panelColor.PORTAL_PATH, panelColor.SETTINGS_INTERFACE, "SettingChanged", self.onPortalSetting)

    def watchPlasmarc(self):
        # KDE replaces plasmarc on every save, thus the watch is added again after each change
        path = os.path.join(self.configHome, "plasmarc")
        if (os.path.exists(path) and path not in self.plasmarcWatcher.files()):
            self.plasmarcWatcher.addPath(path)

    def onColorsChanged(self, *args):
        self.watchPlasmarc()
        self.updateTray()

    @pyqtSlot(QDBusMessage)
    def onPortalSetting(self, message):
        arguments = message.arguments()
        if (len(arguments) >= 3 and arguments[0] == panelColor.APPEARANCE and arguments[1] == "color-scheme"):
            value = panelColor.plain(arguments[2])
            self.portalScheme = value if isinstance(value, int) else 0
            self.updateTray()

    def watchInstallFolder(self):
        self.installWatcher.directoryChanged.connect(lambda path: self.installTimer.start())
        self.installWatcher.fileChanged.connect(lambda path: self.installTimer.start())
        self.installWatcher.addPath(APP_FOLDER)
        self.installWatcher.addPath(os.path.join(APP_FOLDER, "version.py"))

    def onInstallChanged(self):
        versionPath = os.path.join(APP_FOLDER, "version.py")
        if (os.path.exists(versionPath) and versionPath not in self.installWatcher.files()):
            self.installWatcher.addPath(versionPath)
        if (not os.path.exists(os.path.join(APP_FOLDER, "tray.py"))):
            self.quitApp()
            return
        newVersion = readVersion(APP_FOLDER)
        if (newVersion is None or newVersion == VERSION):
            return
        try:
            check = subprocess.run([sys.executable, "-B", "-c", "import tray"], cwd=APP_FOLDER, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            print("Version " + newVersion + " couldn't be checked (" + str(error) + "), thus " + VERSION + " keeps running", file=sys.stderr)
            return
        if (check.returncode != 0):
            print("Version " + newVersion + " didn't import, thus " + VERSION + " keeps running", file=sys.stderr)
            return
        self.mice.stop()
        os.execv(sys.executable, [sys.executable, "-B", os.path.join(APP_FOLDER, "tray.py")])


def main():
    sys.excepthook = printException
    threading.excepthook = printThreadException
    raiseNiceness()
    # the xdg portal registers the app by this name once the tray icon starts, and it needs a desktop file to match
    QApplication.setDesktopFileName(APP_ID)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(QIcon.fromTheme(APP_ID))
    app.setQuitOnLastWindowClosed(False)
    canRun, lockHandle = takeLock(os.environ.get("XDG_RUNTIME_DIR"))
    if (not canRun):
        Notifier().send(APP_NAME, "Mouse Battery is already running.")
        return 0
    mouseApp = MouseBatteryApp(app)
    mouseApp.start()
    code = app.exec()
    mouseApp.stop()
    return code


if (__name__ == "__main__"):
    sys.exit(main())
