#!/usr/bin/env python3
import fcntl
import os
import signal
import sys
import time

from PyQt6 import sip
from PyQt6.QtCore import QFileSystemWatcher, QObject, Qt, pyqtSlot
from PyQt6.QtDBus import QDBusConnection, QDBusMessage
from PyQt6.QtGui import QAction, QIcon, QPalette, QSessionManager
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

import autostart
import dbusCalls
import hidpp
import mice
import panelColor
from exceptionHooks import installExceptionHooks
from icon import IconState, makeIcon
from installWatch import InstallWatcher
from notify import NO_REPLY, Notifier
from serviceClient import ServiceWatcher
from trayHost import TrayHost
from upower import UPowerWatcher
from version import VERSION

APP_ID = "logitech-mouse-battery"
APP_NAME = "Mouse Battery"
LOCK_NAME = "logitech-mouse-battery.lock"
TARGET_NICENESS = 10
PROJECT_URL = "https://github.com/kymotsujason/logitech-mouse-battery-tray-icon"
PORTAL_RETRY_MS = 10 * 1000


def iconStateFor(mouse):
    if (mouse is None or mouse.reading is None):
        return IconState(empty=True)
    reading = mouse.reading
    return IconState(percent=reading.percent, charging=(reading.charging in hidpp.EXTERNAL_POWER), asleep=mouse.asleep)


def mouseLine(name, mouse, now=None):
    if (mouse.reading is None):
        return name + ", no reading yet"
    level = hidpp.levelText(mouse.reading)
    if (mouse.stale):
        return name + ", " + level + " " + mice.readingTime(mouse.lastRead, time.time() if now is None else now)
    if (mouse.asleep):
        return name + ", asleep, " + level + " " + mice.readingTime(mouse.lastRead, time.time() if now is None else now)
    charging = mouse.reading.charging
    if (charging in (hidpp.CHARGING_CHARGING, hidpp.CHARGING_SLOW)):
        return name + ", charging, " + level
    if (charging == hidpp.CHARGING_FULL):
        return name + ", full, " + level
    if (charging == hidpp.CHARGING_ERROR):
        return name + ", charging error, " + level
    if (charging == hidpp.CHARGING_PENDING):
        return name + ", pending charge, " + level
    if (charging == hidpp.CHARGING_UNKNOWN):
        return name + ", unknown state, " + level
    return name + ", " + level


def statusLines(mouseList):
    names = mouseList.displayNames()
    lines = [mouseLine(names[mouse.key], mouse) for mouse in mouseList.mice]
    if (not lines):
        lines = [mice.STATUS_TEXT[mouseList.status()]]
    elif (mouseList.serviceLost()):
        # the mice listed don't say the service is gone, and without it no HID++ reading or warning comes
        lines.append(mice.STATUS_TEXT[mice.STATUS_NO_SERVICE])
    # a receiver or cable the service can't open needs a replug whatever else the menu shows
    denied = mice.STATUS_TEXT[mice.STATUS_DENIED]
    if (mouseList.anyDenied() and denied not in lines):
        lines.append(denied)
    return lines


def raiseNiceness(target=TARGET_NICENESS):
    # os.nice adds to the current value and niceness carries across exec, so it's only topped up
    current = os.nice(0)
    if (current < target):
        os.nice(target - current)


def takeLock(runtimeDir):
    if (not runtimeDir):
        return (True, None)
    try:
        # a link at the lock's name could point a run with someone else's environment at a file to truncate, so it isn't followed and nothing is truncated
        fd = os.open(os.path.join(runtimeDir, LOCK_NAME), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        handle = os.fdopen(fd, "r+")
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
        self.cachedColor = None
        self.aboutBox = None
        self.noticeErrors = set()
        self.mouseList = mice.MouseList()
        self.mouseList.changed.connect(self.updateTray)
        self.mouseList.lowBattery.connect(self.onLowBattery)
        # the 300 s read also looks at the panel color again, which catches a plasmarc made after the app started
        self.mouseList.readTimer.timeout.connect(self.onColorsChanged)
        # the tray reads HID++ mice only through the service, which alone may open the receivers
        self.serviceWatcher = ServiceWatcher(self.systemBus)
        self.serviceWatcher.stateChanged.connect(self.mouseList.applyService)
        self.mouseList.readRequested.connect(self.serviceWatcher.readAll)
        self.upower = UPowerWatcher(self.systemBus)
        self.upower.mouseChanged.connect(self.mouseList.applyUPower)
        self.upower.mouseRemoved.connect(self.mouseList.removeUPower)
        self.notifier = Notifier(self.sessionBus)
        self.trayHost = TrayHost(self.sessionBus, self.notifier)
        self.trayHost.ready.connect(self.showTray)
        self.trayHost.dontOpenAtLogin.connect(self.onDontOpenAtLogin)
        self.installWatcher = InstallWatcher()
        self.installWatcher.removed.connect(self.quitApp)
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
        self.portalTimer = mice.singleShotTimer(self, PORTAL_RETRY_MS, self.onPortalRetry)
        self.updateTray()

    def start(self):
        self.mouseList.start()
        self.serviceWatcher.start()
        self.upower.watchOwner()
        self.upower.start()
        self.installWatcher.start()
        self.watchColors()
        self.trayHost.start()

    def stop(self):
        self.trayHost.stop()
        self.upower.stop()
        self.portalTimer.stop()
        self.serviceWatcher.stop()
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
        enabled, error = self.setOpenAtLogin(False)
        if (enabled):
            # quitting now would bring the app back at the next login with no word why
            self.notifier.send(APP_NAME, "Couldn't turn off Open at Login." + ("" if error is None else " " + error))
            return
        self.quitApp()

    def onMenuShown(self):
        self.mouseList.readAll()
        self.loginAction.setChecked(autostart.isEnabled(self.configHome))

    def onActivated(self, reason):
        if (reason == QSystemTrayIcon.ActivationReason.Trigger):
            self.mouseList.readAll()

    def onLoginToggled(self, checked):
        enabled, error = self.setOpenAtLogin(checked)
        # the menu puts its check mark back on its own, which says nothing about why
        if (error is not None):
            self.notifier.send(APP_NAME, "Couldn't change Open at Login. " + error)

    def setOpenAtLogin(self, checked):
        error = None
        try:
            autostart.setEnabled(self.configHome, checked)
        except OSError as failure:
            error = str(failure)
            print("Couldn't change Open at Login: " + error, file=sys.stderr)
        enabled = autostart.isEnabled(self.configHome)
        self.loginAction.setChecked(enabled)
        return (enabled, error)

    def onLowBattery(self, key, name, percent):
        # the warner counts the warning as sent from here on, so a reading that comes while it's on its way doesn't send it again
        self.notifier.send("Mouse battery low", name + " has " + str(percent) + "% left.", onSent=lambda notificationId, error: self.onLowBatterySent(key, name, percent, notificationId, error))

    def onLowBatterySent(self, key, name, percent, notificationId, error):
        # NoReply means the bus already handed the message to the server (or is starting the server for it), and asking again after a timeout would send it twice
        if (notificationId is None and error != NO_REPLY):
            # the warning is given back, so it goes out again with the next reading, and each failure is printed once
            if ((key, error) not in self.noticeErrors):
                self.noticeErrors.add((key, error))
                print("Couldn't send the low battery notice for " + name + ": " + str(error), file=sys.stderr)
            self.mouseList.markWarningFailed(key, percent)
            return
        self.mouseList.markWarningSent(key, percent)

    def showAbout(self):
        # a second click brings the open box forward instead of stacking another one
        if (self.aboutBox is None or sip.isdeleted(self.aboutBox) or not self.aboutBox.isVisible()):
            # KDE's platform theme shows the box through its own message dialog, which adds the buttons again each time the same box is shown, so every opening gets a new one
            self.aboutBox = QMessageBox(QMessageBox.Icon.NoIcon, "About Mouse Battery", "Mouse Battery " + VERSION + "\n\nShows the battery of Logitech mice in the system tray.\n\n" + PROJECT_URL + "\n\nLicensed under the GNU GPL, version 3 or later.\nNot affiliated with Logitech.", QMessageBox.StandardButton.Ok)
            self.aboutBox.setIconPixmap(self.app.windowIcon().pixmap(64, 64))
            self.aboutBox.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.aboutBox.show()
        self.aboutBox.raise_()
        self.aboutBox.activateWindow()

    def updateStatusActions(self, lines):
        while (len(self.statusActions) < len(lines)):
            action = QAction(self.menu)
            action.setEnabled(False)
            self.menu.insertAction(self.separator, action)
            self.statusActions.append(action)
        while (len(self.statusActions) > len(lines)):
            action = self.statusActions.pop()
            self.menu.removeAction(action)
            action.deleteLater()
        for action, line in zip(self.statusActions, lines):
            # a single & in menu text marks a shortcut key, while the tooltip shows text as written
            action.setText(line.replace("&", "&&"))

    def clearColor(self):
        self.cachedColor = None

    def baseColor(self):
        if (self.cachedColor is not None):
            return self.cachedColor
        palette = self.app.palette().color(QPalette.ColorRole.WindowText)
        try:
            self.cachedColor = panelColor.baseColor(os.environ, self.configHome, panelColor.dataDirs(os.environ), self.portalScheme, palette)
        except Exception as error:
            # every redraw asks again, so each error is printed only the first time
            message = "Couldn't read the panel color: " + str(error)
            if (message not in self.colorErrors):
                self.colorErrors.add(message)
                print(message, file=sys.stderr)
            return palette
        return self.cachedColor

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
        # Plasma draws the tooltip as plain text, where an escaped name would show its entities, while Qt's own tray tooltip turns rich once it holds a tag, so a device name can't bring one in
        tooltip = "\n".join(lines).replace("<", "").replace(">", "")
        if (tooltip != self.shownTooltip):
            self.tray.setToolTip(tooltip)
            self.shownTooltip = tooltip

    def watchColors(self):
        self.app.paletteChanged.connect(self.onColorsChanged)
        self.plasmarcWatcher.fileChanged.connect(self.onColorsChanged)
        self.watchPlasmarc()
        if ("GNOME" in panelColor.desktopNames(os.environ)):
            self.sessionBus.connect(panelColor.PORTAL_SERVICE, panelColor.PORTAL_PATH, panelColor.SETTINGS_INTERFACE, "SettingChanged", self.onPortalSetting)
            panelColor.askPortalColorScheme(self.sessionBus, self, lambda scheme, error: self.onPortalScheme(scheme, error, True))

    def onPortalRetry(self):
        panelColor.askPortalColorScheme(self.sessionBus, self, lambda scheme, error: self.onPortalScheme(scheme, error, False))

    def onPortalScheme(self, scheme, error, firstRead):
        # a read that failed says nothing about the scheme, which a SettingChanged may have set while it waited
        if (error is None):
            self.portalScheme = scheme
        # the portal can still be starting at login, so a first read that got no answer gets one more try
        if (firstRead and error == NO_REPLY):
            self.portalTimer.start()
        self.onColorsChanged()

    def watchPlasmarc(self):
        # KDE replaces plasmarc on every save, so the watch is added again after each change
        path = os.path.join(self.configHome, "plasmarc")
        if (os.path.exists(path) and path not in self.plasmarcWatcher.files()):
            self.plasmarcWatcher.addPath(path)

    def onColorsChanged(self, *args):
        self.clearColor()
        self.watchPlasmarc()
        self.updateTray()

    @pyqtSlot(QDBusMessage)
    def onPortalSetting(self, message):
        arguments = message.arguments()
        if (len(arguments) >= 3 and arguments[0] == panelColor.APPEARANCE and arguments[1] == "color-scheme"):
            value = dbusCalls.plain(arguments[2])
            self.portalScheme = value if isinstance(value, int) else 0
            self.clearColor()
            self.updateTray()


def neverRestart(manager):
    # autostart opens the app at login, so a restored session would start a second copy
    manager.setRestartHint(QSessionManager.RestartHint.RestartNever)


def main():
    installExceptionHooks()
    # Python's handler only raises KeyboardInterrupt in the next slot, where the exception hook prints it, so Ctrl+C never ended the app
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    raiseNiceness()
    # the xdg portal registers the app by this name once the tray icon starts, and it needs a desktop file to match
    QApplication.setDesktopFileName(APP_ID)
    app = QApplication(sys.argv)
    app.saveStateRequest.connect(neverRestart)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(QIcon.fromTheme(APP_ID))
    app.setQuitOnLastWindowClosed(False)
    canRun, lockHandle = takeLock(os.environ.get("XDG_RUNTIME_DIR"))
    if (not canRun):
        Notifier().sendNow(APP_NAME, "Mouse Battery is already running.")
        return 0
    mouseApp = MouseBatteryApp(app)
    mouseApp.start()
    code = app.exec()
    mouseApp.stop()
    return code


if (__name__ == "__main__"):
    sys.exit(main())
