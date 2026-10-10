import os

import autostart
from exceptionHooks import printLine

SLEEP_CHECK_KEY = "sleepCheckSeconds"
SLEEP_CHECK_CHOICES = (0, 30, 60, 300)
LEVEL_RANGE_KEY = "levelRange"
LEVEL_RANGE_CHOICES = ("day", "week", "month", "halfYear", "year", "all")
LEVEL_RANGE_DEFAULT = "week"
GROUP = "General"
SPACES = " \t\n\r\v\f"
HEX_DIGITS = "0123456789abcdefABCDEF"
OCTAL_DIGITS = "01234567"
ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\"": "\"", "?": "?", "'": "'", "\\": "\\"}
BOM = "\ufeff"
PACKED = ("@ByteArray(", "@Variant(", "@DateTime(", "@Rect(", "@Size(", "@Point(")


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


def iniLines(text):
    # each logical line as QSettings' readIniLine finds it, as (start, end, equals), where a quote, a backslash before a line break, or a ";" outside quotes decides where it ends
    lines = []
    # QSettings skips a byte order mark at the start of the file
    position = 1 if text.startswith(BOM) else 0
    while (position < len(text)):
        start = position
        while (start < len(text) and text[start] in SPACES):
            start += 1
        index = start
        inQuotes = False
        equals = -1
        while (index < len(text)):
            ch = text[index]
            index += 1
            if (ch == "="):
                if (not inQuotes and equals == -1):
                    equals = index - 1
            elif (ch in "\n\r"):
                if (index == start + 1):
                    start += 1
                elif (not inQuotes):
                    index -= 1
                    break
            elif (ch == "\\"):
                if (index < len(text)):
                    escaped = text[index]
                    index += 1
                    if (index < len(text) and (escaped + text[index]) in ("\n\r", "\r\n")):
                        index += 1
            elif (ch == "\""):
                inQuotes = not inQuotes
            elif (ch == ";"):
                if (index == start + 1):
                    while (index < len(text) and text[index] not in "\n\r"):
                        index += 1
                    while (index < len(text) and text[index] in SPACES):
                        index += 1
                    start = index
                elif (not inQuotes):
                    index -= 1
                    break
        position = index
        if (index > start):
            lines.append((start, index, equals))
    return lines


def unescapedKey(text):
    # Qt decodes the key in one go, which drops a byte order mark at its start
    result = []
    index = 1 if text.startswith(BOM) else 0
    while (index < len(text)):
        ch = text[index]
        if (ch == "\\"):
            result.append("/")
            index += 1
            continue
        if (ch != "%" or index == len(text) - 1):
            result.append(ch)
            index += 1
            continue
        first = index + 1
        digits = 2
        if (text[first] == "U"):
            first += 1
            digits = 4
        code = text[first:first + digits]
        if (first + digits > len(text) or not all(digit in HEX_DIGITS for digit in code)):
            result.append("%")
            index += 1
            continue
        result.append(chr(int(code, 16)))
        index = first + digits
    return "".join(result)


def skipBlanks(text, index):
    while (index < len(text) and text[index] in " \t"):
        index += 1
    return index


def unescapedValue(text):
    # QSettings' iniUnescapedStringList, where a character code past 16 bits keeps only its low 16 bits as Qt's does, and a comma outside quotes makes a list, which reads as None since no choice is one
    result = ""
    isList = False
    inQuotes = False
    quoted = False
    decoded = False
    index = skipBlanks(text, 0)
    chopLimit = 0
    while (index < len(text)):
        ch = text[index]
        if (ch == "\\"):
            index += 1
            if (index >= len(text)):
                # Qt ends the value here without taking off its trailing spaces
                return None if isList else result
            ch = text[index]
            index += 1
            if (ch in ESCAPES):
                result += ESCAPES[ch]
            elif (ch == "x" and index < len(text) and text[index] in HEX_DIGITS):
                code = 0
                while (index < len(text) and text[index] in HEX_DIGITS):
                    code = code * 16 + int(text[index], 16)
                    index += 1
                result += chr(code & 0xFFFF)
            elif (ch in OCTAL_DIGITS):
                code = int(ch)
                while (index < len(text) and text[index] in OCTAL_DIGITS):
                    code = code * 8 + int(text[index])
                    index += 1
                result += chr(code & 0xFFFF)
            elif (ch in "\n\r"):
                if (index < len(text) and text[index] in "\n\r" and text[index] != ch):
                    index += 1
            chopLimit = len(result)
        elif (ch == "\""):
            index += 1
            quoted = True
            inQuotes = not inQuotes
            if (not inQuotes):
                index = skipBlanks(text, index)
                chopLimit = len(result)
        elif (ch == "," and not inQuotes):
            isList = True
            result = ""
            quoted = False
            index = skipBlanks(text, index + 1)
            chopLimit = 0
        else:
            stop = index + 1
            while (stop < len(text) and text[stop] not in "\\\","):
                stop += 1
            # Qt's decoder drops a byte order mark only at the start of the first run of plain text it decodes
            run = text[index:stop]
            if (not decoded and run.startswith(BOM)):
                run = run[1:]
            decoded = True
            result += run
            index = stop
    if (not quoted):
        result = result[:chopLimit] + result[chopLimit:].rstrip(" \t")
    return None if isList else result


def parseValues(text):
    # read the way QSettings reads them, so a file it wrote or one edited by hand gives the same values, while a list or a packed type such as @Variant(...) reads as None, since no choice is one
    values = {}
    for start, end, key, raw in generalEntries(text):
        values[key] = textValue(unescapedValue(raw))
    return values


def textValue(value):
    if (value is None or not value.startswith("@")):
        return value
    if (value.endswith(")")):
        if (value.startswith("@String(")):
            return value[8:-1]
        if (value == "@Invalid()" or value.startswith(PACKED)):
            return None
    if (value.startswith("@@")):
        return value[1:]
    return value


def isGeneralHeader(line):
    # the General group holds the keys above any group header and those under one named general in any case, with no closing bracket needed
    close = line.find("]")
    return (line[1:] if close == -1 else line[1:close]).strip(SPACES).lower() == "general"


def generalEntries(text):
    # (start, end, key, raw value) for each key in the General group
    entries = []
    inGeneral = True
    for start, end, equals in iniLines(text):
        if (text.startswith("[", start)):
            inGeneral = isGeneralHeader(text[start:end])
            continue
        if (not inGeneral or equals == -1):
            continue
        entries.append((start, end, unescapedKey(text[start:equals].rstrip(" \t")), text[equals + 1:end]))
    return entries


def setValue(text, key, value):
    # every line this doesn't change is copied through as it was, so values only QSettings can read, line separators, and line endings all survive
    entries = [entry for entry in generalEntries(text) if entry[2] == key]
    line = key + "=" + value
    if (entries):
        for start, end, found, raw in reversed(entries[1:]):
            text = text[:start] + text[end:]
        start, end = entries[0][:2]
        return text[:start] + line + text[end:]
    newline = "\r\n" if "\r\n" in text else "\n"
    general = None
    inGeneral = True
    for start, end, equals in iniLines(text):
        if (text.startswith("[", start)):
            inGeneral = isGeneralHeader(text[start:end])
        if (inGeneral):
            general = end
    if (general is None):
        if (text and not text.endswith(("\n", "\r"))):
            text += newline
        return text + "[" + GROUP + "]" + newline + line + newline
    # after the end of that physical line, so a comment trailing the last General line stays with it
    breakAt = general
    while (breakAt < len(text) and text[breakAt] not in "\n\r"):
        breakAt += 1
    if (breakAt >= len(text)):
        return text + newline + line + newline
    if (text.startswith("\r\n", breakAt)):
        breakAt += 1
    return text[:breakAt + 1] + line + newline + text[breakAt + 1:]


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
