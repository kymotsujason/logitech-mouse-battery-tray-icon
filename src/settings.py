import errno
import fcntl
import os
import re
import time

import autostart
from exceptionHooks import printLine

SLEEP_CHECK_KEY = "sleepCheckSeconds"
SLEEP_CHECK_CHOICES = (0, 30, 60, 300)
LEVEL_RANGE_KEY = "levelRange"
LEVEL_RANGE_CHOICES = ("day", "week", "month", "halfYear", "year", "all")
LEVEL_RANGE_DEFAULT = "week"
# anyone who can open the settings folder can hold its lock, so a save waits this long and then gives up
LOCK_WAIT_SECONDS = 2.0


def settingsPath(configHome):
    return os.path.join(configHome, "logitech-mouse-battery", "settings.ini")


def sleepChoice(value):
    # a choice handed in as text is turned into a number, and anything else is no choice
    if (type(value) is not int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
    return value if value in SLEEP_CHECK_CHOICES else None


def choiceValues(text):
    # PyQt unpickles a Python value it finds while QSettings reads a file, so the file is read here by hand, and only a plain or quoted choice in the General group counts, while any other form, such as @Variant(...), is no choice
    values = {}
    group = None
    if (text.startswith("\ufeff")):
        text = text[1:]
    for line in re.split("\r\n|\r|\n", text):
        line = line.strip(" \t")
        if (line == "" or line[0] in ";#"):
            continue
        if (line[0] == "["):
            group = line[1:-1] if line.endswith("]") else None
            continue
        key, equals, value = line.partition("=")
        if (group != "General" or equals == ""):
            continue
        key = key.strip(" \t")
        value = value.strip(" \t")
        if (len(value) >= 2 and value[0] == "\"" and value[-1] == "\""):
            value = value[1:-1]
        if (key == LEVEL_RANGE_KEY):
            values[key] = value if value in LEVEL_RANGE_CHOICES else None
        elif (key == SLEEP_CHECK_KEY):
            values[key] = int(value) if value in [str(seconds) for seconds in SLEEP_CHECK_CHOICES] else None
    return values


def checkedText(folder, name, shown):
    autostart.checkFolder(folder, shown)
    text, info = autostart.readFile(folder, name, shown)
    if (info is not None):
        autostart.checkFile(info, os.path.join(shown, name))
    return (text, info)


def readText(configHome):
    # the file is found the way the autostart override is, so a run as root with another account's config reads nothing of theirs
    folder, name, shown = autostart.openFolder(settingsPath(configHome), False)
    try:
        return checkedText(folder, name, shown)[0]
    finally:
        os.close(folder)


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
    return choiceValues(text)


def fileText(values):
    # the app owns this file and only ever writes these two keys, whose choices need no quoting, so a save writes them fresh instead of editing whatever else the file holds
    lines = ["[General]"]
    level = values.get(LEVEL_RANGE_KEY)
    if (level in LEVEL_RANGE_CHOICES):
        lines.append(LEVEL_RANGE_KEY + "=" + level)
    seconds = sleepChoice(values.get(SLEEP_CHECK_KEY))
    if (seconds is not None):
        lines.append(SLEEP_CHECK_KEY + "=" + str(seconds))
    return "\n".join(lines) + "\n"


def lockFolder(folder, shown):
    # a save writes the whole file from what it read, so two saves at once would each drop the other's change, and the folder's O_PATH descriptor can't take a lock
    lock = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=folder)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    try:
        while (True):
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except BlockingIOError:
                if (time.monotonic() >= deadline):
                    raise TimeoutError(errno.ETIMEDOUT, "Timed out waiting for another save", shown)
                time.sleep(0.01)
    except BaseException:
        os.close(lock)
        raise


def saveValue(configHome, key, value):
    try:
        folder, name, shown = autostart.openFolder(settingsPath(configHome), True)
    except (OSError, ValueError):
        return False
    lock = None
    try:
        autostart.checkFolder(folder, shown)
        lock = lockFolder(folder, shown)
        text, info = checkedText(folder, name, shown)
        values = {} if text is None else choiceValues(text)
        values[key] = value
        autostart.writeText(folder, name, shown, info, fileText(values))
    except (OSError, ValueError):
        return False
    finally:
        if (lock is not None):
            os.close(lock)
        os.close(folder)
    return True


def loadSleepCheck(configHome):
    seconds = sleepChoice(readValues(configHome).get(SLEEP_CHECK_KEY))
    return 0 if seconds is None else seconds


def saveSleepCheck(configHome, seconds):
    seconds = sleepChoice(seconds)
    if (seconds is None):
        return False
    return saveValue(configHome, SLEEP_CHECK_KEY, seconds)


def loadLevelRange(configHome):
    value = readValues(configHome).get(LEVEL_RANGE_KEY, LEVEL_RANGE_DEFAULT)
    return value if value in LEVEL_RANGE_CHOICES else LEVEL_RANGE_DEFAULT


def saveLevelRange(configHome, rangeName):
    if (rangeName not in LEVEL_RANGE_CHOICES):
        return False
    return saveValue(configHome, LEVEL_RANGE_KEY, rangeName)
