import os

import autostart
from exceptionHooks import printLine

SLEEP_CHECK_KEY = "sleepCheckSeconds"
SLEEP_CHECK_CHOICES = (0, 30, 60, 300)
LEVEL_RANGE_KEY = "levelRange"
LEVEL_RANGE_CHOICES = ("day", "week", "month", "halfYear", "year", "all")
LEVEL_RANGE_DEFAULT = "week"
GROUP = "General"


def settingsPath(configHome):
    return os.path.join(configHome, "logitech-mouse-battery", "settings.ini")


def readValues(configHome):
    # QSettings opens the file by its path and follows a link another account planted in it, so the file is found the way the autostart override is, and a run as root with another account's config reads nothing of theirs
    path = settingsPath(configHome)
    try:
        folder, name, shown = autostart.openFolder(path, False)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        printLine("Couldn't read the settings in " + path + ": " + str(error))
        return {}
    try:
        autostart.checkFolder(folder, shown)
        text, info = autostart.readFile(folder, name, shown)
        if (info is not None):
            autostart.checkFile(info, os.path.join(shown, name))
    except OSError as error:
        printLine("Couldn't read the settings in " + path + ": " + str(error))
        return {}
    finally:
        os.close(folder)
    return parseValues(text or "")


def parseValues(text):
    # keys above the first group belong to General, as they do for QSettings, which wrote the files this reads
    values = {}
    inGroup = True
    for line in text.splitlines():
        stripped = line.strip()
        if (stripped.startswith("[")):
            inGroup = (stripped == "[" + GROUP + "]")
            continue
        key, separator, value = stripped.partition("=")
        if (inGroup and separator and not stripped.startswith((";", "#"))):
            values[key.strip()] = value.strip()
    return values


def setValue(text, key, value):
    result = []
    inGroup = True
    written = False
    for line in text.splitlines():
        stripped = line.strip()
        if (stripped.startswith("[")):
            if (inGroup and not written and result):
                result.append(key + "=" + value)
                written = True
            inGroup = (stripped == "[" + GROUP + "]")
        elif (inGroup and stripped.partition("=")[0].strip() == key):
            if (not written):
                result.append(key + "=" + value)
                written = True
            continue
        result.append(line)
    if (not written):
        if (not result or not inGroup):
            result.append("[" + GROUP + "]")
        result.append(key + "=" + value)
    return "\n".join(result) + "\n"


def saveValue(configHome, key, value):
    try:
        folder, name, shown = autostart.openFolder(settingsPath(configHome), True)
    except (OSError, ValueError):
        return False
    try:
        text, info = autostart.readFile(folder, name, shown)
        autostart.writeText(folder, name, shown, info, setValue(text or "", key, value))
    except OSError:
        return False
    finally:
        os.close(folder)
    return True


def loadSleepCheck(configHome):
    # INI values read back as text, so the choice is turned back into a number, and anything else is Off
    try:
        seconds = int(readValues(configHome).get(SLEEP_CHECK_KEY, 0))
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
