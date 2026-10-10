import os
import tempfile

from PyQt6.QtCore import QSettings

import autostart
from exceptionHooks import printLine

SLEEP_CHECK_KEY = "sleepCheckSeconds"
SLEEP_CHECK_CHOICES = (0, 30, 60, 300)
LEVEL_RANGE_KEY = "levelRange"
LEVEL_RANGE_CHOICES = ("day", "week", "month", "halfYear", "year", "all")
LEVEL_RANGE_DEFAULT = "week"
COPY_PREFIX = "logitech-mouse-battery-"


def settingsPath(configHome):
    return os.path.join(configHome, "logitech-mouse-battery", "settings.ini")


def privateCopy(work, name, text):
    # QSettings opens the file by its path and follows a link another account planted in it, so it only ever gets a copy in a fresh folder only this account can open, and each copy has a name of its own since QSettings keeps the files it has read in a cache by their path
    path = os.path.join(work, name)
    if (text is not None):
        with open(path, "xb") as f:
            f.write(text.encode("utf-8", "surrogateescape"))
    return QSettings(path, QSettings.Format.IniFormat)


def choiceValues(qtSettings):
    # a list, a byte array, or any other type QSettings unpacks is no choice, so it reads as None
    values = {}
    for key in (SLEEP_CHECK_KEY, LEVEL_RANGE_KEY):
        if (qtSettings.contains(key)):
            value = qtSettings.value(key)
            values[key] = value if isinstance(value, str) else None
    return values


def readText(configHome):
    # the file is found the way the autostart override is, so a run as root with another account's config reads nothing of theirs
    folder, name, shown = autostart.openFolder(settingsPath(configHome), False)
    try:
        autostart.checkFolder(folder, shown)
        text, info = autostart.readFile(folder, name, shown)
        if (info is not None):
            autostart.checkFile(info, os.path.join(shown, name))
    finally:
        os.close(folder)
    return text


def readValues(configHome):
    path = settingsPath(configHome)
    try:
        text = readText(configHome)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        printLine("Couldn't read the settings in " + path + ": " + str(error))
        return {}
    if (text is None):
        return {}
    try:
        with tempfile.TemporaryDirectory(prefix=COPY_PREFIX, ignore_cleanup_errors=True) as work:
            return choiceValues(privateCopy(work, "read.ini", text))
    except OSError as error:
        printLine("Couldn't read the settings in " + path + ": " + str(error))
        return {}


def editedText(text, key, value):
    # QSettings writes the whole copy again, dropping comments as every release through 1.1.1 did, and its text is only used once a fresh copy of it reads back the new value
    with tempfile.TemporaryDirectory(prefix=COPY_PREFIX, ignore_cleanup_errors=True) as work:
        editor = privateCopy(work, "edit.ini", text)
        editor.setValue(key, value)
        editor.sync()
        del editor
        with open(os.path.join(work, "edit.ini"), "rb") as f:
            edited = f.read().decode("utf-8", "surrogateescape")
        if (choiceValues(privateCopy(work, "check.ini", edited)).get(key) != value):
            return None
        return edited


def saveValue(configHome, key, value):
    try:
        folder, name, shown = autostart.openFolder(settingsPath(configHome), True)
    except (OSError, ValueError):
        return False
    try:
        text, info = autostart.readFile(folder, name, shown)
        edited = editedText(text, key, value)
        if (edited is None):
            return False
        autostart.writeText(folder, name, shown, info, edited)
    except OSError:
        return False
    finally:
        os.close(folder)
    return True


def loadSleepCheck(configHome):
    # INI values read back as text, so the choice is turned back into a number, and anything else is Off
    try:
        seconds = int(readValues(configHome).get(SLEEP_CHECK_KEY) or 0)
    except ValueError:
        return 0
    return seconds if seconds in SLEEP_CHECK_CHOICES else 0


def saveSleepCheck(configHome, seconds):
    return saveValue(configHome, SLEEP_CHECK_KEY, str(seconds))


def loadLevelRange(configHome):
    value = readValues(configHome).get(LEVEL_RANGE_KEY, LEVEL_RANGE_DEFAULT)
    return value if value in LEVEL_RANGE_CHOICES else LEVEL_RANGE_DEFAULT


def saveLevelRange(configHome, rangeName):
    return saveValue(configHome, LEVEL_RANGE_KEY, rangeName)
