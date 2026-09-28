import contextlib
import errno
import os
import stat

FILE_NAME = "logitech-mouse-battery.desktop"
OWN_KEYS = {"Type": "Application", "Name": "Mouse Battery", "Hidden": "true"}


def overridePath(configHome):
    return os.path.join(configHome, "autostart", FILE_NAME)


def readEntry(path):
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
    except FileNotFoundError:
        return None
    keys = {}
    inEntry = False
    for line in text.splitlines():
        stripped = line.strip()
        if (stripped.startswith("[")):
            inEntry = (stripped == "[Desktop Entry]")
            continue
        key, separator, value = stripped.partition("=")
        if (inEntry and separator and not stripped.startswith("#")):
            keys[key.strip()] = value.strip()
    return keys


def writeText(path, text):
    # the new text replaces the old file only once it's all on disk, thus a failed write leaves the old override whole
    path = os.path.realpath(path)
    oldMode = None
    if (os.path.exists(path)):
        oldMode = os.stat(path).st_mode
        if ((oldMode & stat.S_IWUSR) == 0):
            raise PermissionError(errno.EACCES, "Permission denied", path)
    temp = path + ".new"
    try:
        with open(temp, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if (oldMode is not None):
            os.chmod(temp, stat.S_IMODE(oldMode))
        os.replace(temp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(temp)
        raise


def isEnabled(configHome):
    try:
        keys = readEntry(overridePath(configHome))
    except OSError:
        return True
    return (keys is None or keys.get("Hidden", "").lower() != "true")


def setHidden(path, value):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.read().splitlines()
    result = []
    inEntry = False
    written = False
    for line in lines:
        stripped = line.strip()
        if (stripped.startswith("[")):
            if (inEntry and not written):
                result.append("Hidden=" + value)
                written = True
            inEntry = (stripped == "[Desktop Entry]")
        elif (inEntry and stripped.partition("=")[0].strip() == "Hidden"):
            if (not written):
                result.append("Hidden=" + value)
                written = True
            continue
        result.append(line)
    if (not written):
        if (not inEntry):
            result.append("[Desktop Entry]")
        result.append("Hidden=" + value)
    writeText(path, "\n".join(result) + "\n")


def setEnabled(configHome, enabled):
    path = overridePath(configHome)
    keys = readEntry(path)
    if (enabled):
        if (keys is None):
            return
        if ("Exec" not in keys):
            os.remove(path)
            return
        setHidden(path, "false")
        return
    if (keys is None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        writeText(path, "[Desktop Entry]\n" + "".join(key + "=" + value + "\n" for key, value in OWN_KEYS.items()))
        return
    setHidden(path, "true")
