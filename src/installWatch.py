import os
import re
import subprocess
import sys

from PyQt6.QtCore import QFileSystemWatcher, QObject, pyqtSignal

import mice
from version import VERSION

INSTALL_SETTLE_MS = 2 * 1000
APP_FOLDER = os.path.dirname(os.path.abspath(__file__))


def readVersion(folder):
    try:
        with open(os.path.join(folder, "version.py")) as f:
            text = f.read()
    except OSError:
        return None
    match = re.search(r'VERSION = "([^"]*)"', text)
    return match.group(1) if match else None


class InstallWatcher(QObject):
    # every later version takes over from the one installed before it through this watch, so the app folder keeps tray.py and version.py's VERSION line at its top
    removed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.watcher = QFileSystemWatcher(self)
        self.timer = mice.singleShotTimer(self, INSTALL_SETTLE_MS, self.onChanged)

    def start(self):
        self.watcher.directoryChanged.connect(lambda path: self.timer.start())
        self.watcher.fileChanged.connect(lambda path: self.timer.start())
        self.watcher.addPath(APP_FOLDER)
        self.watcher.addPath(os.path.join(APP_FOLDER, "version.py"))

    def onChanged(self):
        versionPath = os.path.join(APP_FOLDER, "version.py")
        if (os.path.exists(versionPath) and versionPath not in self.watcher.files()):
            self.watcher.addPath(versionPath)
        if (not os.path.exists(os.path.join(APP_FOLDER, "tray.py"))):
            self.removed.emit()
            return
        newVersion = readVersion(APP_FOLDER)
        if (newVersion is None or newVersion == VERSION):
            return
        try:
            check = subprocess.run([sys.executable, "-B", "-c", "import tray"], cwd=APP_FOLDER, capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            print("Version " + newVersion + " couldn't be checked (" + str(error) + "), so " + VERSION + " keeps running", file=sys.stderr)
            return
        if (check.returncode != 0):
            print("Version " + newVersion + " didn't import, so " + VERSION + " keeps running", file=sys.stderr)
            return
        # the hidraw fds, the wake pipes, and the lock file are opened without inheritance (PEP 446), so the exec closes them and the new version takes the lock
        try:
            os.execv(sys.executable, [sys.executable, "-B", os.path.join(APP_FOLDER, "tray.py")])
        except OSError as error:
            print("Version " + newVersion + " couldn't start (" + str(error) + "), so " + VERSION + " keeps running", file=sys.stderr)
