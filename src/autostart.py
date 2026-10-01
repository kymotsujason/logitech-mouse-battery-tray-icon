import contextlib
import errno
import os
import stat

FILE_NAME = "logitech-mouse-battery.desktop"
OWN_KEYS = {"Type": "Application", "Name": "Mouse Battery", "Hidden": "true"}
# gnome-session reads this key too, so an override can turn the app off with it in place of Hidden
GNOME_KEY = "X-GNOME-Autostart-enabled"


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
    # the new text replaces the old file only once it's all on disk, so a failed write leaves the old override whole
    path = os.path.realpath(path)
    oldMode = None
    if (os.path.exists(path)):
        info = os.stat(path)
        oldMode = info.st_mode
        # root passes os.access for a 0400 file, so the owner's write bit and the owner are checked directly
        if ((oldMode & stat.S_IWUSR) == 0 or info.st_uid != os.getuid()):
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
    if (keys is None):
        return True
    return (keys.get("Hidden", "").lower() != "true" and keys.get(GNOME_KEY, "").lower() != "false")


def setHidden(path, value, gnomeEnabled):
    with open(path, encoding="utf-8", errors="surrogateescape") as f:
        lines = f.read().splitlines()
    result = []
    inEntry = False
    written = False
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip()
        if (stripped.startswith("[")):
            if (inEntry and not written):
                result.append("Hidden=" + value)
                written = True
            inEntry = (stripped == "[Desktop Entry]")
        elif (inEntry and key == "Hidden"):
            if (not written):
                result.append("Hidden=" + value)
                written = True
            continue
        elif (inEntry and key == GNOME_KEY):
            result.append(GNOME_KEY + "=" + gnomeEnabled)
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
        setHidden(path, "false", "true")
        return
    if (keys is None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        writeText(path, "[Desktop Entry]\n" + "".join(key + "=" + value + "\n" for key, value in OWN_KEYS.items()))
        return
    setHidden(path, "true", "false")
