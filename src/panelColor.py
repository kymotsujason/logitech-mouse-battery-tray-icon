import os

from PyQt6.QtDBus import QDBusMessage
from PyQt6.QtGui import QColor

import autostart
import dbusCalls

WHITE = QColor(255, 255, 255)
PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SETTINGS_INTERFACE = "org.freedesktop.portal.Settings"
UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
NOT_FOUND = "org.freedesktop.portal.Error.NotFound"
APPEARANCE = "org.freedesktop.appearance"
PREFER_DARK = 1
PREFER_LIGHT = 2


def desktopNames(env):
    return [name.upper() for name in env.get("XDG_CURRENT_DESKTOP", "").split(":") if name]


def dataDirs(env):
    # the XDG base directory spec says relative paths are invalid and should be ignored
    home = env.get("XDG_DATA_HOME", "")
    if (not os.path.isabs(home)):
        home = os.path.join(os.path.expanduser("~"), ".local", "share")
    system = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [home] + [path for path in system.split(":") if os.path.isabs(path)]


def readIni(path):
    # found the way the autostart override is, so a run as root with another account's config never follows a link that account planted, and a FIFO or a device at the name can't hang the tray
    try:
        folder, name, shown = autostart.openFolder(path, False)
    except (OSError, ValueError):
        return None
    try:
        text = autostart.readFile(folder, name, shown)[0]
    except OSError:
        return None
    finally:
        os.close(folder)
    if (text is None):
        return None
    groups = {}
    group = groups.setdefault("", {})
    # KConfig ends a line only at a newline, so a Unicode line separator inside a value stays part of it
    for line in text.split("\n"):
        stripped = line.strip()
        if (stripped.startswith("[") and stripped.endswith("]")):
            group = groups.setdefault(stripped[1:-1], {})
            continue
        key, separator, value = stripped.partition("=")
        if (separator and not stripped.startswith("#")):
            group[key.strip()] = value.strip()
    return groups


def parseColor(text):
    parts = text.split(",")
    if (len(parts) < 3):
        return None
    try:
        return QColor(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def plasmaThemeColor(configHome, dirs):
    # a Plasma theme with its own colors file sets the panel's text, and one without follows the color scheme
    plasmarc = readIni(os.path.join(configHome, "plasmarc"))
    themeName = "default"
    if (plasmarc is not None):
        themeName = plasmarc.get("Theme", {}).get("name") or "default"
    for base in dirs:
        colors = readIni(os.path.join(base, "plasma", "desktoptheme", themeName, "colors"))
        if (colors is not None):
            return parseColor(colors.get("Colors:Window", {}).get("ForegroundNormal", ""))
    return None


def baseColor(env, configHome, dirs, portalScheme, paletteColor):
    names = desktopNames(env)
    if ("KDE" in names):
        color = plasmaThemeColor(configHome, dirs)
        return color if color is not None else QColor(paletteColor)
    # GNOME Classic forces a light top bar, and GNOME Flashback's panel isn't GNOME Shell's
    if ("GNOME" in names and "GNOME-CLASSIC" not in names and "GNOME-FLASHBACK" not in names):
        # GNOME Shell only turns its top bar light for a prefer light setting
        return QColor(paletteColor) if portalScheme == PREFER_LIGHT else QColor(WHITE)
    return QColor(paletteColor)


def portalRead(method, key="color-scheme"):
    message = QDBusMessage.createMethodCall(PORTAL_SERVICE, PORTAL_PATH, SETTINGS_INTERFACE, method)
    message.setArguments([APPEARANCE, key])
    return message


def askPortalSetting(bus, owner, key, onAnswer, method="ReadOne"):
    # onAnswer gets the number and the error name, or None, once the portal answers, and a key an older portal doesn't have comes back as an error
    def onReply(reply):
        error = dbusCalls.failure(reply)
        # only a portal older than ReadOne gets asked with Read
        if (error == UNKNOWN_METHOD and method == "ReadOne"):
            askPortalSetting(bus, owner, key, onAnswer, "Read")
            return
        if (error is not None or not reply.arguments()):
            onAnswer(0, error)
            return
        value = dbusCalls.plain(reply.arguments()[0])
        onAnswer(value if isinstance(value, int) else 0, None)

    dbusCalls.callLater(bus, portalRead(method, key), onReply, owner)


def askPortalColorScheme(bus, owner, onAnswer, method="ReadOne"):
    askPortalSetting(bus, owner, "color-scheme", onAnswer, method)
