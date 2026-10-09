import os

from PyQt6.QtCore import QSettings

SLEEP_CHECK_KEY = "sleepCheckSeconds"
SLEEP_CHECK_CHOICES = (0, 30, 60, 300)


def settingsPath(configHome):
    return os.path.join(configHome, "logitech-mouse-battery", "settings.ini")


def loadSleepCheck(configHome):
    # INI values read back as text, so the choice is turned back into a number, and anything else is Off
    value = QSettings(settingsPath(configHome), QSettings.Format.IniFormat).value(SLEEP_CHECK_KEY, 0)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 0
    return seconds if seconds in SLEEP_CHECK_CHOICES else 0


def saveSleepCheck(configHome, seconds):
    settings = QSettings(settingsPath(configHome), QSettings.Format.IniFormat)
    settings.setValue(SLEEP_CHECK_KEY, seconds)
    settings.sync()
    return settings.status() == QSettings.Status.NoError
