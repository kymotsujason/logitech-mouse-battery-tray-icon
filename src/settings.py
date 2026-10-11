import errno
import fcntl
import os
import stat
import tempfile
import time

from PyQt6.QtCore import QSettings

import autostart
from exceptionHooks import printLine

SLEEP_CHECK_KEY = "sleepCheckSeconds"
SLEEP_CHECK_CHOICES = (0, 30, 60, 300)
LEVEL_RANGE_KEY = "levelRange"
LEVEL_RANGE_CHOICES = ("day", "week", "month", "halfYear", "year", "all")
LEVEL_RANGE_DEFAULT = "week"
COPY_PREFIX = "logitech-mouse-battery-"
# anyone who can open the settings folder can hold its lock, so a save waits this long and then gives up
LOCK_WAIT_SECONDS = 2.0


def settingsPath(configHome):
    return os.path.join(configHome, "logitech-mouse-battery", "settings.ini")


def sleepChoice(value):
    # INI values read back as text, so the choice is turned back into a number, and anything else is no choice
    if (type(value) is not int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
    return value if value in SLEEP_CHECK_CHOICES else None


def choiceValues(qtSettings):
    # a list, a byte array, or any other type QSettings unpacks is no choice, so it reads as None, while a packed integer is a number of seconds as 1.1.1 read it
    values = {}
    for key in (SLEEP_CHECK_KEY, LEVEL_RANGE_KEY):
        if (qtSettings.contains(key)):
            value = qtSettings.value(key)
            if (key == SLEEP_CHECK_KEY and type(value) is int):
                values[key] = value
            else:
                values[key] = value if isinstance(value, str) else None
    return values


def parsedValues(text):
    # PyQt unpickles a Python value in the file while QSettings reads it, so only text from a folder and file that passed the checks gets here, and QSettings only ever opens a copy in a fresh folder only this account can open, since it follows a link at the path it's given
    with tempfile.TemporaryDirectory(prefix=COPY_PREFIX, ignore_cleanup_errors=True) as work:
        path = os.path.join(work, "read.ini")
        with open(path, "xb") as f:
            f.write(text.encode("utf-8", "surrogateescape"))
        return choiceValues(QSettings(path, QSettings.Format.IniFormat))


def checkPrivate(info, what, shown):
    # a file this account owns can still be one any account can change, and QSettings would unpickle what they put in it, while group write is left alone since many systems give each account a private group and a umask of 002
    if (info.st_mode & stat.S_IWOTH):
        raise PermissionError(errno.EACCES, "Permission denied, any account can write the " + what, shown)


def checkedText(folder, name, shown):
    autostart.checkFolder(folder, shown)
    checkPrivate(os.fstat(folder), "folder", shown)
    text, info = autostart.readFile(folder, name, shown)
    if (info is not None):
        autostart.checkFile(info, os.path.join(shown, name))
        checkPrivate(info, "file", os.path.join(shown, name))
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
    # a value QSettings can't decode, such as a broken pickle, raises SystemError or whatever else PyQt meets, and the tray still has to start
    try:
        return parsedValues(text)
    except Exception as error:
        printLine("Couldn't read the settings in " + path + ": " + str(error))
        return {}


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
        try:
            values = {} if text is None else parsedValues(text)
        except Exception:
            return False
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
