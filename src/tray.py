#!/usr/bin/env python3
import fcntl
import os
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QFileSystemWatcher, QObject, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage
from PyQt6.QtGui import QAction, QIcon, QPalette
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

import autostart
import dbusCalls
import hidpp
import mice
import panelColor
from icon import IconState, makeIcon
from installWatch import InstallWatcher
from notify import NO_REPLY, Notifier
from trayHost import TrayHost
from upower import UPowerWatcher
from version import VERSION

APP_ID = "logitech-mouse-battery"
APP_NAME = "Mouse Battery"
LOCK_NAME = "logitech-mouse-battery.lock"
TARGET_NICENESS = 10
PROJECT_URL = "https://github.com/kymotsujason/logitech-mouse-battery-tray-icon"


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
    if (mouse.reading.charging in (hidpp.CHARGING_CHARGING, hidpp.CHARGING_SLOW)):
        return name + ", charging, " + level
    if (mouse.reading.charging == hidpp.CHARGING_FULL):
        return name + ", full, " + level
    return name + ", " + level


def statusLines(mouseList):
    names = mouseList.displayNames()
    lines = [mouseLine(names[mouse.key], mouse) for mouse in mouseList.mice]
    if (lines):
        return lines
    return [mice.STATUS_TEXT[mouseList.status()]]


def printException(kind, value, tb):
    traceback.print_exception(kind, value, tb)


def printThreadException(args):
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)


def installExceptionHooks():
    # PyQt6 aborts the whole app on an exception in a slot unless sys.excepthook is replaced, and nothing restarts an app started from autostart
    sys.excepthook = printException
    threading.excepthook = printThreadException


def raiseNiceness(target=TARGET_NICENESS):
    # os.nice adds to the current value and niceness carries across exec, so it's only topped up
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
        self.portalScheme = 0
        self.colorErrors = set()
        self.noticeErrors = set()
        self.mouseList = mice.MouseList()
        self.mouseList.changed.connect(self.updateTray)
        self.mouseList.lowBattery.connect(self.onLowBattery)
        self.upower = UPowerWatcher(self.systemBus)
        self.upower.mouseChanged.connect(self.mouseList.applyUPower)
        self.upower.mouseRemoved.connect(self.mouseList.removeUPower)
        self.notifier = Notifier(self.sessionBus)
        self.trayHost = TrayHost(self.sessionBus, self.notifier)
        self.trayHost.ready.connect(self.showTray)
        self.trayHost.dontOpenAtLogin.connect(self.onDontOpenAtLogin)
        self.installWatcher = InstallWatcher()
        self.installWatcher.removed.connect(self.quitApp)
        self.installWatcher.restarting.connect(self.mouseList.stop)
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.onMenuShown)
        self.separator = self.menu.addSeparator()
        self.loginAction = self.menu.addAction("Open at Login")
        self.loginAction.setCheckable(True)
        self.loginAction.setChecked(autostart.isEnabled(self.configHome))
        self.loginAction.triggered.connect(self.onLoginToggled)
        self.menu.addAction("About Mouse Battery").triggered.connect(lambda: self.showAbout())
        self.menu.addAction("Quit").triggered.connect(lambda: self.quitApp())
        self.plasmarcWatcher = QFileSystemWatcher(self)
        self.updateTray()

    def start(self):
        self.mouseList.start()
        self.upower.watchOwner()
        self.upower.start()
        self.installWatcher.start()
        self.watchColors()
        self.trayHost.start()

    def stop(self):
        self.trayHost.stop()
        self.mouseList.stop()

    def quitApp(self):
        self.app.quit()

    def showTray(self):
        if (self.tray is not None):
            return
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

    def onDontOpenAtLogin(self):
        self.onLoginToggled(False)
        self.quitApp()

    def onMenuShown(self):
        self.mouseList.readAll()
        self.loginAction.setChecked(autostart.isEnabled(self.configHome))

    def onActivated(self, reason):
        if (reason == QSystemTrayIcon.ActivationReason.Trigger):
            self.mouseList.readAll()

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
            # the warning stays due and goes out again with the next reading, so each failure is printed once
            if ((key, error) not in self.noticeErrors):
                self.noticeErrors.add((key, error))
                print("Couldn't send the low battery notice for " + name + ": " + str(error), file=sys.stderr)
            return
        self.mouseList.markWarningSent(key, percent)

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
            # every redraw asks again, so each error is printed only the first time
            message = "Couldn't read the panel color: " + str(error)
            if (message not in self.colorErrors):
                self.colorErrors.add(message)
                print(message, file=sys.stderr)
            return palette

    def updateTray(self, *args):
        lines = statusLines(self.mouseList)
        self.updateStatusActions(lines)
        if (self.tray is None):
            return
        state = iconStateFor(self.mouseList.shownMouse())
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
            self.sessionBus.connect(panelColor.PORTAL_SERVICE, panelColor.PORTAL_PATH, panelColor.SETTINGS_INTERFACE, "SettingChanged", self.onPortalSetting)

    def watchPlasmarc(self):
        # KDE replaces plasmarc on every save, so the watch is added again after each change
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
            value = dbusCalls.plain(arguments[2])
            self.portalScheme = value if isinstance(value, int) else 0
            self.updateTray()


def main():
    installExceptionHooks()
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
