import contextlib
import errno
import os
import stat

FILE_NAME = "logitech-mouse-battery.desktop"
OWN_KEYS = {"Type": "Application", "Name": "Mouse Battery", "Hidden": "true"}
# gnome-session reads this key too, so an override can turn the app off with it in place of Hidden
GNOME_KEY = "X-GNOME-Autostart-enabled"
# the kernel follows at most 40 links in one path, and a loop of links would otherwise never end
MAX_LINKS = 40


def overridePath(configHome):
    return os.path.join(configHome, "autostart", FILE_NAME)


def parseEntry(text):
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


def openFolder(path, create):
    # each name is opened from the folder before it without following links, and links are followed here by hand, so the checks and the change that follows act on the same folders even when another account swaps a name along the path in between
    pending = [part for part in reversed(os.path.abspath(path).split(os.sep)) if part]
    folder = os.open(os.sep, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    shown = os.sep
    links = 0
    try:
        while (True):
            name = pending.pop()
            if (name in (".", "..") and not pending):
                raise IsADirectoryError(errno.EISDIR, "Is a directory", shown)
            if (name == "."):
                continue
            try:
                fd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder)
            except FileNotFoundError:
                if (not pending):
                    return (folder, name, shown)
                if (not create):
                    raise
                checkFolder(folder, shown)
                with contextlib.suppress(FileExistsError):
                    os.mkdir(name, 0o777, dir_fd=folder)
                pending.append(name)
                continue
            target = None
            try:
                info = os.fstat(fd)
                if (stat.S_ISLNK(info.st_mode)):
                    # a link another account made anywhere along the path, such as ~/.config leading to /etc/xdg for a run as root, would lead the change into files that aren't this account's
                    if (info.st_uid not in (os.getuid(), 0)):
                        raise PermissionError(errno.EACCES, "Permission denied, a link uid " + str(info.st_uid) + " made", os.path.join(shown, name))
                    target = os.readlink("", dir_fd=fd)
                elif (pending and not stat.S_ISDIR(info.st_mode)):
                    raise NotADirectoryError(errno.ENOTDIR, "Not a directory", os.path.join(shown, name))
            except BaseException:
                os.close(fd)
                raise
            if (target is not None):
                os.close(fd)
                links += 1
                if (links > MAX_LINKS):
                    raise OSError(errno.ELOOP, "Too many levels of symbolic links", os.path.join(shown, name))
                if (target.startswith(os.sep)):
                    top = os.open(os.sep, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
                    os.close(folder)
                    folder = top
                    shown = os.sep
                pending.extend(part for part in reversed(target.split(os.sep)) if part)
                # a last name leading to "/" leaves nothing to open, and it names a folder as "." does
                if (not pending):
                    raise IsADirectoryError(errno.EISDIR, "Is a directory", shown)
                continue
            if (not pending):
                os.close(fd)
                return (folder, name, shown)
            os.close(folder)
            folder = fd
            shown = (os.path.dirname(shown) if name == ".." else os.path.join(shown, name))
    except BaseException:
        os.close(folder)
        raise


def checkFolder(folder, shown):
    # a link can lead into another account's files, such as /etc for a run as root with the user's config, so the folder the override sits in, or the one its folder is made in, has to belong to this account
    owner = os.fstat(folder).st_uid
    if (owner != os.getuid()):
        raise PermissionError(errno.EACCES, "Permission denied, the folder belongs to uid " + str(owner) + ", not this account", shown)


def checkFile(info, path):
    if (info.st_uid != os.getuid()):
        raise PermissionError(errno.EACCES, "Permission denied, the file belongs to uid " + str(info.st_uid) + ", not this account", path)


def readFile(folder, name, shown):
    # a FIFO at the override's name would hang the menu and /dev/zero would read without end, so the open doesn't wait and only a regular file is read
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC, dir_fd=folder)
    except FileNotFoundError:
        return (None, None)
    with os.fdopen(fd, encoding="utf-8", errors="surrogateescape") as f:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)):
            raise OSError(errno.EINVAL, "Not a regular file", os.path.join(shown, name))
        return (f.read(), info)


def writeText(folder, name, shown, info, text):
    # the new text replaces the old file only once it's all on disk, so a failed write leaves the old override whole
    checkFolder(folder, shown)
    if (info is not None):
        checkFile(info, os.path.join(shown, name))
        # root passes os.access for a 0400 file, so the owner's write bit is checked directly
        if ((info.st_mode & stat.S_IWUSR) == 0):
            raise PermissionError(errno.EACCES, "Permission denied, the file is read only", os.path.join(shown, name))
    temp = name + ".new"
    # a leftover or planted file at the temporary name goes first, and the new one is made fresh without following a link, so the text can't land in another file
    with contextlib.suppress(FileNotFoundError):
        os.unlink(temp, dir_fd=folder)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666, dir_fd=folder)
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
            f.flush()
            if (info is not None):
                os.fchmod(fd, stat.S_IMODE(info.st_mode))
            os.fsync(fd)
        os.replace(temp, name, src_dir_fd=folder, dst_dir_fd=folder)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temp, dir_fd=folder)
        raise


def isEnabled(configHome):
    try:
        folder, name, shown = openFolder(overridePath(configHome), False)
    except OSError:
        return True
    try:
        text = readFile(folder, name, shown)[0]
    except OSError:
        return True
    finally:
        os.close(folder)
    if (text is None):
        return True
    keys = parseEntry(text)
    return (keys.get("Hidden", "").lower() != "true" and keys.get(GNOME_KEY, "").lower() != "false")


def setHidden(text, value, gnomeEnabled):
    result = []
    inEntry = False
    written = False
    for line in text.splitlines():
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
    return "\n".join(result) + "\n"


def changeOverride(folder, name, shown, enabled):
    text, info = readFile(folder, name, shown)
    if (enabled):
        if (text is None):
            return
        if ("Exec" not in parseEntry(text)):
            checkFolder(folder, shown)
            checkFile(info, os.path.join(shown, name))
            os.unlink(name, dir_fd=folder)
            return
        writeText(folder, name, shown, info, setHidden(text, "false", "true"))
        return
    if (text is None):
        writeText(folder, name, shown, None, "[Desktop Entry]\n" + "".join(key + "=" + value + "\n" for key, value in OWN_KEYS.items()))
        return
    writeText(folder, name, shown, info, setHidden(text, "true", "false"))


def setEnabled(configHome, enabled):
    try:
        folder, name, shown = openFolder(overridePath(configHome), not enabled)
    except FileNotFoundError:
        # with no autostart folder there's no override to remove
        if (not enabled):
            raise
        return
    try:
        changeOverride(folder, name, shown, enabled)
    finally:
        os.close(folder)
